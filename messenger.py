"""Прослойка между ботом и мессенджером.

main.py разговаривает с клиентом одними и теми же действиями: «отправь
сообщение», «собери клавиатуру», «узнай имя», «слушай входящие». Каким именно
мессенджером это сделано — ВКонтакте, а завтра, может, Telegram или MAX, —
диалогу всё равно. Поэтому всё, что знает про конкретную библиотеку, собрано
здесь, в одном файле, за общим интерфейсом.

Устроено это классической подменой (полиморфизмом):

  * Messenger   — общий «язык»: список действий, которые бот вправе требовать
                  от любого мессенджера. Сам он ничего не умеет;
  * VkMessenger — наследник, который эти действия закрывает через vk_api;
  * create()    — выбирает нужного наследника в момент запуска и отдаёт его
                  боту. Здесь и только здесь решается, «на чём» бот работает.

Смысл в том, что main.py зовёт bot.send(...), bot.listen() и не знает, ВК под
ними или нет. Появится второй мессенджер — рядом встанет ещё один наследник,
а диалоговый код не изменится ни строкой: в этом вся выгода.
"""

import collections
import queue
import threading
import time

import requests
import vk_api
from vk_api.exceptions import ApiError
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor

import config


# =========================================================================
# Общие типы: одинаковые у любого мессенджера
# =========================================================================

# Кто клиент: в каком мессенджере (platform) и под каким номером (id). Пара,
# а не один номер, потому что база у ВК и Telegram общая, а номера у них свои
# и вполне могут совпасть: ВК-12345 и TG-12345 — разные люди. Везде, где бот
# опознаёт человека — состояние диалога, записи, подписки, — опознаёт он именно
# этой парой. Числовой id нужен там, где надо обратиться в саму библиотеку
# (кому слать) или собрать ссылку на страницу.
Client = collections.namedtuple("Client", ["platform", "id"])

# Входящее сообщение в том виде, в каком его ждёт главный цикл: от кого (Client)
# и что (text). Больше боту знать не нужно — ни номера чата, ни типа события
# конкретной библиотеки. Каждый мессенджер сам превращает своё событие в это.
Incoming = collections.namedtuple("Incoming", ["client", "text"])


class MessengerError(Exception):
    """Мессенджер не смог доставить сообщение или выполнить запрос.

    Бот ловит именно это исключение, а не ошибку конкретной библиотеки
    (у ВК — ApiError): перевод «ошибка мессенджера» -> MessengerError делает
    адаптер. Так main.py не знает про vk_api и переживёт замену мессенджера.

    hint — понятная человеку подсказка, что чинить, или пустая строка. Нужна
    там, где по самой ошибке непонятно, куда идти: например, у сообщества
    выключены «Возможности ботов». Без подсказки в консоли остался бы голый
    код ошибки.
    """

    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint


# =========================================================================
# Messenger: общий интерфейс — что бот вправе требовать от мессенджера
# =========================================================================

class Messenger:
    """Общий «язык» бота с мессенджером. Список действий, не их выполнение.

    Каждый метод здесь — «дырка», которую конкретный мессенджер (наследник)
    закрывает по-своему. Сам Messenger ничего не делает: вызов любого метода —
    ошибка «этот мессенджер метод не реализовал». Так забытый в новом адаптере
    метод обнаружится сразу и громко, а не превратится в молчащего бота.
    """

    def send(self, user_id, text, rows=None):
        """Отправить пользователю сообщение, при желании — с клавиатурой.

        rows — ряды подписей кнопок ([["Кератин"], ["В меню"]]) или None.
        В формат конкретного мессенджера (у ВК — VkKeyboard, у Telegram будет
        своё) их превращает сам этот метод, уже в момент отправки. Диалог
        передаёт одни и те же ряды, а каждый канал сериализует их по-своему —
        поэтому одна и та же клавиатура годится и для ВК, и для Telegram.
        """
        raise NotImplementedError

    def user_name(self, user_id):
        """Имя пользователя строкой — или пустая строка, если не узнали."""
        raise NotImplementedError

    def user_link(self, user_id):
        """Ссылка на страницу пользователя — чтобы мастер открыл её одним кликом."""
        raise NotImplementedError

    def contact(self, user_id):
        """Имя и короткое имя аккаунта: («Мария Петрова», «chuul»).

        Короткое имя — то, чем человека называют вместо номера: @username
        в Telegram, домен страницы в ВК. Нужно для связки аккаунтов: человек
        присылает «vk.com/chuul» или «@d_chul», и найти по такому имени номер
        мы можем только у себя — мессенджеры по чужому имени номер не выдают.

        Что не узнали, отдаём пустой строкой, а не ошибкой: не знать имя
        неприятно, но не смертельно.
        """
        raise NotImplementedError

    def listen(self):
        """Бесконечно отдавать входящие сообщения как Incoming(user_id, text).

        Это генератор: главный цикл бота идёт по нему `for msg in bot.listen()`.
        Служебные события (не сообщения, не боту) мессенджер отсеивает сам —
        до бота доходит только то, на что нужно ответить.
        """
        raise NotImplementedError


# =========================================================================
# VkMessenger: тот же интерфейс, но через ВКонтакте
# =========================================================================

class VkMessenger(Messenger):
    """ВКонтакте: long polling, клавиатуры VkKeyboard, имена через users.get.

    Весь код, завязанный на vk_api, живёт внутри этого класса. Хочешь другой
    мессенджер — пишешь такой же класс рядом, main.py не трогаешь.
    """

    # ВК показывает у обычной клавиатуры не больше 5 рядов: сама библиотека
    # разрешает 10, но лишние ряды клиент просто не рисует, и кнопки из них
    # становятся недоступны. Поэтому 5 — наш настоящий лимит. Это ограничение
    # ВК, поэтому и живёт оно здесь, а не в диалоге.
    MAX_KEYBOARD_ROWS = 5

    def __init__(self, platform, token):
        # Своё имя платформы («vk») мессенджер знает сам: им он подписывает
        # входящие сообщения, чтобы бот понимал, в каком канале клиент.
        self.platform = platform
        self._session = vk_api.VkApi(token=token)
        self._api = self._session.get_api()
        self._longpoll = VkLongPoll(self._session)

    def send(self, user_id, text, rows=None):
        params = {
            "user_id": user_id,
            "message": text,
            "random_id": 0,  # 0 = ВК сам не проверяет дубли, для учебного бота ок
        }
        # Ряды кнопок превращаем в клавиатуру ВК здесь же, в момент отправки:
        # диалог держит их «сырыми», чтобы те же ряды ушли и в другой канал.
        if rows is not None:
            params["keyboard"] = self._keyboard(rows)
        try:
            self._api.messages.send(**params)
        except ApiError as error:
            # Прячем ошибку ВК за общим MessengerError, чтобы её ловил код,
            # ничего не знающий про vk_api. Код 912 — «Возможности ботов»
            # выключены; по нему одному не догадаться, где включить, поэтому
            # кладём в подсказку прямой путь в настройки сообщества.
            hint = ""
            if error.code == 912:
                hint = (
                    "Ошибка 912: у сообщества выключены «Возможности ботов».\n"
                    "Управление сообществом -> Сообщения -> Настройки для "
                    "бота -> Возможности ботов: Включены."
                )
            raise MessengerError(str(error), hint) from error

    def _keyboard(self, rows):
        """Ряды подписей -> клавиатура ВК (VkKeyboard JSON). Зовётся из send()."""
        # Не падаем, но громко жалуемся в консоль: молча потерянная кнопка —
        # это часы поисков «почему у клиента нет выхода в меню».
        if len(rows) > self.MAX_KEYBOARD_ROWS:
            print(f"ВНИМАНИЕ: клавиатура из {len(rows)} рядов, "
                  f"ВК покажет только первые {self.MAX_KEYBOARD_ROWS}: {rows}")

        keyboard = VkKeyboard(one_time=False)
        for i, row in enumerate(rows):
            if i > 0:
                keyboard.add_line()  # перенос на новый ряд, но не перед первым
            for label in row:
                keyboard.add_button(label, VkKeyboardColor.SECONDARY)
        return keyboard.get_keyboard()

    def contact(self, user_id):
        """Имя и домен страницы: («Мария Петрова», «chuul»). Спрашиваем ВК.

        Домен — то, что стоит в адресе страницы после vk.com. У страницы,
        которой короткое имя не задавали, он выглядит как «id12345» — и это
        тоже годится: по такой ссылке человек находится ничуть не хуже.

        Ловим любую ошибку, а не только ApiError: удалённая страница,
        оборванная сеть или заглушка vk_api в тестах не должны мешать мастеру
        узнать о записи. В худшем случае останется одна ссылка.
        """
        try:
            person = self._api.users.get(user_ids=user_id, fields="domain")[0]
            name = f"{person['first_name']} {person['last_name']}".strip()
            return name, person.get("domain", "")
        except Exception as error:
            print(f"не смогла узнать данные {user_id}: {error}")
            return "", ""

    def user_name(self, user_id):
        """Имя клиента: «Мария Петрова» — или пустая строка, если не узнали.

        Спрашиваем ВК при каждой отправке. Записей единицы в день, а держать
        имена про запас незачем: в contacts они лежат для поиска по короткому
        имени, а не чтобы показывать мастеру устаревшее.
        """
        return self.contact(user_id)[0]

    def user_link(self, user_id):
        # Ссылка вида vk.com/id123456: мастеру достаточно кликнуть, чтобы
        # увидеть, кто записался.
        return f"vk.com/id{user_id}"

    def listen(self):
        for event in self._longpoll.listen():
            if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                yield Incoming(Client(self.platform, event.user_id), event.text)


# =========================================================================
# TgMessenger: тот же интерфейс, но через Telegram
# =========================================================================

class TgMessenger(Messenger):
    """Telegram: тот же набор действий, что и у ВК, но через Bot API по HTTP.

    Никакой отдельной библиотеки: Telegram отвечает обычным JSON на запросы
    к https://api.telegram.org/bot<токен>/<метод>, и `requests` для этого
    достаточно. Весь HTTP спрятан в `_call()`, наружу торчат те же send/listen/
    user_name/user_link, что и у VkMessenger, — bot.py разницы не замечает.

    Две особенности Telegram, из-за которых код чуть отличается от ВК:

      * входящие приходят не потоком событий, а ответом на запрос getUpdates
        (long polling): спрашиваем «что нового с номера offset», в ответ пачка
        обновлений, offset двигаем за последнее — иначе те же придут снова;
      * узнать имя по чужому числовому id в Telegram нельзя (нет аналога
        users.get). Зато имя и @username лежат в каждом входящем сообщении —
        их и запоминаем в `_names`, чтобы потом показать мастеру.
    """

    API = "https://api.telegram.org/bot{token}/{method}"

    # getUpdates держит соединение открытым до 25 секунд, ожидая сообщение
    # (long polling): так бот узнаёт о новом почти мгновенно и не долбит сервер
    # пустыми запросами. Таймаут самого HTTP-запроса берём с запасом сверх
    # этого, чтобы рвал соединение Telegram, а не requests.
    POLL_SECONDS = 25
    POLL_TIMEOUT = POLL_SECONDS + 10

    def __init__(self, platform, token):
        self.platform = platform
        self._token = token
        self._offset = 0        # с какого обновления читать дальше
        self._names = {}        # id -> (имя, username) из входящих сообщений

    def _call(self, method, params, timeout=10):
        """Дёрнуть метод Bot API и вернуть его result. Ошибку -> MessengerError.

        Telegram на любой запрос отвечает JSON вида {"ok": ..., ...}. При ok=true
        полезное лежит в "result"; при ok=false там "error_code"/"description".
        Сетевую ошибку и отказ Telegram одинаково прячем за MessengerError —
        чтобы их ловил код, ничего не знающий про requests и про Bot API.
        """
        url = self.API.format(token=self._token, method=method)
        try:
            data = requests.post(url, json=params, timeout=timeout).json()
        except requests.RequestException as error:
            raise MessengerError(f"Telegram недоступен: {error}") from error
        if not data.get("ok"):
            raise MessengerError(
                f"Telegram отклонил {method}: "
                f"{data.get('error_code')} {data.get('description')}"
            )
        return data["result"]

    def send(self, user_id, text, rows=None):
        params = {
            "chat_id": user_id,
            "text": text,
            "reply_markup": self._keyboard(rows),
        }
        self._call("sendMessage", params)

    def _keyboard(self, rows):
        """Ряды подписей -> reply-клавиатура Telegram (reply_markup для send).

        rows=None означает «убрать клавиатуру»: у Telegram она держится на экране,
        пока её явно не снять (в ВК каждое сообщение несёт свою). Строки в рядах
        Telegram принимает как есть — кнопкой становится сам текст, отдельный
        объект на простую кнопку не нужен.
        """
        if rows is None:
            return {"remove_keyboard": True}
        return {"keyboard": rows, "resize_keyboard": True}

    def contact(self, user_id):
        """Имя и @username из последнего входящего: («Мария П», «masha»).

        Спросить Telegram по чужому номеру нельзя — такого метода у Bot API
        нет. Зато и имя, и @username лежат в каждом входящем сообщении, откуда
        мы их и запомнили. Не писал боту — вернём две пустые строки.
        """
        return self._names.get(user_id, ("", ""))

    def user_name(self, user_id):
        # Только из того, что человек сам прислал (см. contact). Не писал
        # боту — имени нет, вернём пустую строку: мастеру останется ссылка.
        return self.contact(user_id)[0]

    def user_link(self, user_id):
        # По числовому id ссылку на человека Telegram не даёт. Есть @username —
        # ведём на t.me/username; нет — показываем сам номер, чтобы мастер хотя
        # бы отличал клиентов и мог найти переписку у себя в чате с ботом.
        username = self.contact(user_id)[1]
        return f"t.me/{username}" if username else f"tg id {user_id}"

    def _remember(self, sender):
        """Запомнить имя и @username отправителя — из поля from входящего."""
        name = f"{sender.get('first_name', '')} {sender.get('last_name', '')}".strip()
        self._names[sender["id"]] = (name, sender.get("username", ""))

    def listen(self):
        # getUpdates и webhook у Telegram взаимоисключающие: если на боте когда-то
        # включали webhook, getUpdates отвечает 409, пока его не снять. Снимаем
        # один раз — как только до Telegram впервые достучались (флаг ниже), —
        # и дальше спокойно опрашиваем. deleteWebhook безвреден, даже если
        # webhook'а и не было.
        webhook_cleared = False
        while True:
            try:
                if not webhook_cleared:
                    self._call("deleteWebhook", {})
                    webhook_cleared = True
                updates = self._call(
                    "getUpdates",
                    {"offset": self._offset, "timeout": self.POLL_SECONDS},
                    timeout=self.POLL_TIMEOUT,
                )
            except MessengerError as error:
                # Обрыв связи или отказ не должны ронять бота: ждём и снова.
                print(f"Telegram: {error}; повтор через 5 c")
                time.sleep(5)
                continue
            for update in updates:
                # offset двигаем всегда, даже если сообщение нам не подходит,
                # иначе это же обновление вернётся в следующем запросе и цикл
                # застрянет на нём навсегда.
                self._offset = update["update_id"] + 1
                message = update.get("message")
                if not message or "text" not in message:
                    continue  # не сообщение или без текста (стикер, фото) — мимо
                sender = message["from"]
                self._remember(sender)
                yield Incoming(Client(self.platform, sender["id"]), message["text"])


# =========================================================================
# Bot: маршрутизатор поверх нескольких мессенджеров
# =========================================================================

class Bot:
    """Держит все включённые мессенджеры и шлёт через нужный.

    Главная его задача — по платформе клиента выбрать тот мессенджер, которым
    до этого клиента и правда достучаться. Клиента, записавшегося в Telegram,
    напоминание должно найти в Telegram, а не в ВК: за это отвечает `by()`.

    main.py обращается к боту клиентом (Client), а не голым номером, и потому
    не думает, в каком канале человек: `bot.send(client, ...)` сам уедет туда,
    куда надо.
    """

    def __init__(self, messengers):
        self._messengers = messengers  # {"vk": VkMessenger(...), ...}

    def by(self, platform):
        """Мессенджер этой платформы — или понятная ошибка, если его нет.

        «Нет мессенджера» означает запись из канала, который на этом запуске
        не включён (в config.cfg только vk, а запись — из tg). Молчать нельзя:
        клиент остался бы без напоминания, и никто бы не понял почему.
        """
        messenger = self._messengers.get(platform)
        if messenger is None:
            raise MessengerError(
                f"нет включённого мессенджера для платформы «{platform}» — "
                f"проверьте [bot] platform в config.cfg"
            )
        return messenger

    def send(self, client, text, rows=None):
        self.by(client.platform).send(client.id, text, rows)

    def user_name(self, client):
        return self.by(client.platform).user_name(client.id)

    def user_link(self, client):
        return self.by(client.platform).user_link(client.id)

    def contact(self, client):
        return self.by(client.platform).contact(client.id)

    def _pump(self, channel, box):
        """Слушать один канал и складывать входящие в общую очередь `box`.

        Крутится в своём потоке — по одному на канал. Если listen() оборвётся
        (сбой сети, ошибка библиотеки), не роняем весь бот и не гасим канал
        молча: жалуемся в консоль, ждём и слушаем заново. Иначе один упавший
        канал незаметно перестал бы принимать сообщения.
        """
        while True:
            try:
                for message in channel.listen():
                    box.put(message)
            except Exception as error:
                print(f"канал «{channel.platform}» оборвался: {error}; "
                      f"повтор через 5 c")
                time.sleep(5)

    def listen(self):
        """Входящие сообщения всех включённых мессенджеров, одной лентой.

        Один канал — просто отдаём его поток, без лишних потоков и очередей.

        Несколько — каждый слушаем в своём потоке (у каждого свой блокирующий
        приём: ВК ждёт longpoll, TG висит на getUpdates, и один не должен
        задерживать другой). Все они складывают входящие в общую очередь, а
        отсюда мы выдаём их по одному. Приём получается параллельным, а разбор
        — последовательным: за очередью один потребитель (главный цикл в
        main.py), и два сообщения не полезут в базу и в состояние диалога разом.
        """
        if len(self._messengers) == 1:
            (only,) = self._messengers.values()
            yield from only.listen()
            return

        box = queue.Queue()
        for platform, channel in self._messengers.items():
            threading.Thread(
                target=self._pump, args=(channel, box),
                name=f"listen-{platform}", daemon=True,
            ).start()
        while True:
            yield box.get()


# =========================================================================
# Выбор мессенджеров в момент запуска
# =========================================================================

def create():
    """Собрать бота с мессенджерами, выбранными для этого запуска.

    Какие включены — решается здесь, в одном месте, по настройке `platform`
    в разделе [bot] файла config.cfg. Значений может быть несколько через
    запятую: `platform = vk, tg` поднимет оба канала на одной базе. ВКонтакте
    стоит по умолчанию: раздела в настройках может не быть вовсе, старые
    установки от этого не сломаются.

    Ключ доступа каждый мессенджер берёт свой и только когда включён: у ВК это
    config.VK_TOKEN, у Telegram — config.read_tg_token(). Поэтому чисто ВК-
    установке TG-ключ не нужен — за него мы даже не заглядываем. Это
    единственное место, где решается, «на чём» работает бот; main.py обращается
    к клиентам по платформе и от набора каналов не зависит.
    """
    platforms = ["vk"]
    if config.SETTINGS.has_option("bot", "platform"):
        raw = config.SETTINGS.get("bot", "platform")
        platforms = [p.strip().lower() for p in raw.split(",") if p.strip()] or ["vk"]

    messengers = {}
    for platform in platforms:
        if platform in messengers:
            continue  # повтор в настройке (vk, vk) — не беда, просто пропускаем
        if platform == "vk":
            messengers["vk"] = VkMessenger("vk", config.VK_TOKEN)
        elif platform == "tg":
            messengers["tg"] = TgMessenger("tg", config.read_tg_token())
        else:
            raise SystemExit(
                f"В настройках [bot] platform указан неизвестный мессенджер "
                f"«{platform}». Поддерживаются: vk, tg."
            )
    return Bot(messengers)
