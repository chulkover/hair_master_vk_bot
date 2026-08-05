"""VK-бот мастера по волосам.

Что уже умеет:
  расчёт стоимости: процедура -> длина -> густота -> цена;
  запись: день -> время -> подтверждение, «мои записи» и отмена;
  подписку на день, в котором свободного времени нет,
  и уведомление подписчиков, когда чужая отмена освободила окошко;
  напоминание о подтверждении за сутки и автоотмену за 12 часов —
  этим занимается фоновый поток, он же единственный, кто пишет первым
  по часам, а не в ответ на сообщение.

Этот файл отвечает только за диалог с клиентом: состояния, клавиатуры,
тексты. Расписание — в schedule.py, устройство базы — в db.py,
настройки — в config.py, а всё, что про сам мессенджер (отправка,
клавиатуры, приём сообщений через ВК), — в messenger.py.
"""

import os
import threading
import time
from datetime import date, datetime, timedelta

import config
import db        # состояние диалога: чтобы шаг клиента не терялся при перезапуске
import messenger  # прослойка над мессенджером: весь код ВК спрятан за ней
import schedule  # расписание: свободные окошки, записи, подписки


# =========================================================================
# 1. Часовой пояс и подключение к мессенджеру
# =========================================================================
# Пояс выставляем первым делом, до любого обращения ко времени: расписание,
# напоминания и «сейчас» считаются в локальном времени, а на хостинге
# контейнер по умолчанию живёт по UTC.

def set_timezone():
    """Перевести процесс в часовой пояс мастера (config.TIMEZONE).

    tzset() есть только на Unix, поэтому под Windows просто оставляем
    системный пояс — на компьютере мастера он и так правильный.
    """
    os.environ["TZ"] = config.TIMEZONE
    if hasattr(time, "tzset"):
        time.tzset()


set_timezone()

# Мессенджеры этого запуска. Какие именно (сейчас — только ВКонтакте) выбирает
# messenger.create() по настройке; дальше main.py зовёт методы bot и про ВК
# ничего не знает. Всё, что ниже — send(), клавиатуры, приём сообщений —
# тонкие обёртки над bot, чтобы остальной код и тесты обращались к привычным
# именам.
bot = messenger.create()

# ВАЖНО: почти везде в этом файле параметр `user_id` — это не голый номер,
# а messenger.Client(platform, id): кто и в каком мессенджере. База у ВК
# и Telegram общая, а номера у них свои и могут совпасть, поэтому человека
# опознаём парой. Числовой номер достаём как user_id.id, платформу —
# user_id.platform. На границе с расписанием и базой пара распадается на два
# явных аргумента (platform, user_id); внутри диалога она ходит одним Client.
Client = messenger.Client


def owner():
    """Владелец как Client — по текущему config.OWNER_ID.

    Считаем на месте, а не держим готовой константой: номер мастера читается
    из настроек, и вычислять его каждый раз надёжнее, чем закешировать один
    раз при импорте. Владелец всегда со стороны ВК: его номер — это VK ID.
    """
    return Client("vk", config.OWNER_ID)


def send(user_id, text, keyboard=None):
    """Отправить сообщение пользователю (при желании — с клавиатурой)."""
    bot.send(user_id, text, keyboard)


def notify_owner(text):
    """Сообщить владельцу сообщества — о новой записи, отмене, автоотмене.

    Клавиатуру не шлём намеренно: она заменила бы владельцу ту, что у него
    сейчас на экране, а он в этот момент может быть посреди своего разговора
    с ботом.

    Ошибку ловим и живём дальше. Владелец мог не написать сообществу первым
    (тогда ВК не даст боту написать ему) или закрыть сообщения — но клиент,
    который прямо сейчас записывается, не должен из-за этого получить
    «что-то пошло не так». Уведомление здесь — приятное дополнение к записи,
    а не её часть.
    """
    if not config.OWNER_ID:
        return  # уведомления выключены в настройках

    try:
        send(owner(), text)
    except messenger.MessengerError as error:
        print(f"не смогла написать владельцу: {error}")


def client_name(user_id):
    """Имя клиента: «Мария Петрова» — или пустая строка, если не узнали.

    Ссылка вида vk.com/id123456 мастеру не говорит ничего: чтобы понять, кто
    записался, ему пришлось бы открывать её в браузере. Имя он видит сразу
    в сообщении. Как именно его узнаём — дело мессенджера, см. user_name().
    """
    return bot.user_name(user_id)


def client_card(user_id):
    """Кто клиент — строкой для сообщения мастеру: имя и ссылка на страницу."""
    link = bot.user_link(user_id)
    name = client_name(user_id)
    return f"Клиент: {name}\n{link}" if name else f"Клиент: {link}"


def client_of(row):
    """Клиент записи или подписки как Client(platform, id).

    Строки из базы несут platform и user_id отдельными столбцами; там, где
    надо кому-то написать (напоминание, уведомление об отмене), собираем из
    них обратно пару — по ней bot.send сам выберет нужный мессенджер.
    """
    return Client(row["platform"], row["user_id"])


# =========================================================================
# 2. Состояния диалога
# =========================================================================
# Бот должен помнить, на каком шаге находится каждый клиент.
# Иначе он не поймёт, что «Длинные» — это ответ про длину волос,
# а не какая-то команда.

MAIN_MENU = "MAIN_MENU"
SELECTING_SERVICE = "SELECTING_SERVICE"
SELECTING_LENGTH = "SELECTING_LENGTH"
SELECTING_DENSITY = "SELECTING_DENSITY"
PRICE_CALCULATED = "PRICE_CALCULATED"
SELECTING_DATE = "SELECTING_DATE"
SELECTING_TIME = "SELECTING_TIME"
CONFIRMING = "CONFIRMING"
MY_BOOKINGS = "MY_BOOKINGS"
CANCEL_CONFIRM = "CANCEL_CONFIRM"
MY_SUBS = "MY_SUBS"                        # список подписок и отписка
SELECTING_SUB_DATE = "SELECTING_SUB_DATE"  # выбор дня для подписки
SUB_CONFIRM = "SUB_CONFIRM"                # «подписать вас на 31.07?»

# --- шаги владельца -------------------------------------------------------
# Их много, но устроены они одинаково: почти ничего не помнят между
# сообщениями. Где выбор виден на самой кнопке (день, час, номер записи),
# состояние вообще не нужно; где нужен — лежит в памяти и в базу не едет,
# см. комментарий к OWNER_DRAFT ниже.
OWNER_CABINET = "OWNER_CABINET"              # меню кабинета
OWNER_SCHEDULE = "OWNER_SCHEDULE"            # дни и записи внутри дня
OWNER_CANCEL_REASON = "OWNER_CANCEL_REASON"  # почему отменяем запись
OWNER_CLOSE_KIND = "OWNER_CLOSE_KIND"        # что закрываем: день, часы, срок
OWNER_CLOSE_SINCE = "OWNER_CLOSE_SINCE"      # с какого дня
OWNER_CLOSE_UNTIL = "OWNER_CLOSE_UNTIL"      # по какой день
OWNER_CLOSE_FROM = "OWNER_CLOSE_FROM"        # с какого часа
OWNER_CLOSE_TO = "OWNER_CLOSE_TO"            # по какой час
OWNER_CLOSE_REASON = "OWNER_CLOSE_REASON"    # что сказать клиентам
OWNER_CLOSE_CONFIRM = "OWNER_CLOSE_CONFIRM"  # «отменить N записей и закрыть?»
OWNER_CLOSURES = "OWNER_CLOSURES"            # что закрыто, снятие
OWNER_WORK = "OWNER_WORK"                    # график: дни и часы
OWNER_WORKDAYS = "OWNER_WORKDAYS"            # переключение дней недели
OWNER_WORK_START = "OWNER_WORK_START"        # начало рабочего дня
OWNER_WORK_END = "OWNER_WORK_END"            # конец рабочего дня

# Ключи, в которых собирается будущее закрытие, пока мастер отвечает на
# вопросы: день, часы, причина. Живут ТОЛЬКО в памяти — в DIALOG_FIELDS их
# нет, и в базу они не попадают.
#
# Так сделано намеренно. Хранить их в базе значило бы пять новых колонок
# в dialogs ради черновика, который живёт полминуты. Плата — перезапуск бота
# посреди заполнения его теряет; поэтому каждый экран кабинета проверяет, что
# нужное на месте, и если нет, возвращает мастера в кабинет вместо того,
# чтобы падать.
OWNER_DRAFT = ("schedule_day", "close_kind", "close_since", "close_until",
               "close_from", "close_to", "close_reason", "close_affected",
               "cancel_booking_id")

# Все шаги кабинета одним списком: по нему handle_message() решает, отдавать
# ли сообщение handle_owner(). Перечисление, а не префикс «OWNER_» в имени
# состояния, — чтобы случайно совпавшая строка из базы не открыла кабинет.
OWNER_STATES = (
    OWNER_CABINET, OWNER_SCHEDULE, OWNER_CANCEL_REASON,
    OWNER_CLOSE_KIND, OWNER_CLOSE_SINCE, OWNER_CLOSE_UNTIL,
    OWNER_CLOSE_FROM, OWNER_CLOSE_TO, OWNER_CLOSE_REASON,
    OWNER_CLOSE_CONFIRM, OWNER_CLOSURES,
    OWNER_WORK, OWNER_WORKDAYS, OWNER_WORK_START, OWNER_WORK_END,
)

# Здесь хранится состояние всех пользователей:
#   {123456: {"state": "SELECTING_LENGTH", "service": "keratin"}, ...}
#
# Это рабочая копия в памяти, а не единственное место хранения: то же состояние
# лежит в базе, поэтому перезапуск бота не сбивает клиента с его шага. Копия
# нужна именно потому, что get_user() отдаёт словарь, который вызывающий код
# меняет по месту («user["service"] = key»), и таких мест десятки. Если каждый
# вызов поднимал бы из базы свежий словарь, правки терялись бы между вызовами
# внутри одного сообщения.
#
# Из этого следует, что база сама о правках не узнает: сохранение вызывается
# явно — см. save_user() и два места, откуда он вызывается.
users = {}

# Словарь общий на два потока: диалог отвечает на сообщения, планировщик пишет
# первым по часам. Обычно они заняты разными клиентами, но не всегда: клиенту
# может прийти уведомление об освободившемся окошке ровно в ту секунду, когда
# он сам что-то пишет боту. Тогда оба потока правят один и тот же словарь и
# сохраняют его в базу — а записалось бы состояние того, кто закончил вторым.
#
# Замок разводит их по очереди: пока один разговаривает с клиентом, второй
# ждёт. Разговор занимает доли секунды, так что ожидание незаметно.
#
# RLock, а не Lock: рассылка подписчикам идёт и из ответа на сообщение (клиент
# отменил запись), и из планировщика (запись сняли за неподтверждение).
# В первом случае замок уже взят этим же потоком, и обычный Lock повесил бы
# бота на самом себе.
DIALOG_LOCK = threading.RLock()


def get_user(user_id):
    """Вернуть данные пользователя, при первой встрече подняв их из базы.

    user_id — это Client(platform, id); в памяти (users) он и служит ключом,
    а базе передаётся распавшимся на два столбца.
    """
    if user_id not in users:
        saved = db.load_dialog(user_id.platform, user_id.id)
        users[user_id] = revive(saved) if saved else {"state": MAIN_MENU}
    return users[user_id]


def revive(saved):
    """Проверить поднятое из базы состояние: годится ли оно ещё в дело.

    Клиент мог уйти на середине выбора времени и вернуться через неделю —
    выбранный тогда день уже прошёл, и продолжать с него некуда. В таком
    случае честнее начать с меню, чем предлагать вчерашние окошки.
    """
    today = date.today().isoformat()

    for field in ("day", "sub_day"):
        chosen = saved.get(field)
        if chosen is not None and chosen < today:
            return {"state": MAIN_MENU}

    return saved


def save_user(user_id):
    """Запомнить состояние клиента в базе.

    В главном меню строку не храним, а удаляем: там состояние ничем не
    отличается от того, что get_user() выдаёт при первой встрече, а лишние
    поля от прошлого расчёта всё равно перезаписываются раньше, чем
    понадобятся. Так в таблице лежат только те, кто сейчас посреди диалога.
    """
    user = users.get(user_id)
    if user is None:
        return  # до get_user() дело не дошло — сохранять нечего

    if user["state"] == MAIN_MENU:
        db.forget_dialog(user_id.platform, user_id.id)
    else:
        db.save_dialog(user_id.platform, user_id.id, user)


# =========================================================================
# 3. Клавиатуры
# =========================================================================

BACK_TO_MENU = "В меню"


# В одном ряду не больше 5 кнопок — это уже ограничение библиотеки.
# Про лимит на число рядов знает сам мессенджер (см. messenger.py): сколько
# их поместится на экране — его забота, а не диалога.
MAX_BUTTONS_IN_ROW = 5

# А на телефоне комфортно влезают только 3 кнопки с текстом вроде «10:00»:
# экран узкий, подписи на пятерке кнопок обрезаются. Для коротких подписей
# (номера записей) можно и больше, для всего остального — вот этот предел.
COMFORT_BUTTONS_IN_ROW = 3


def build_keyboard(rows):
    """Клавиатура как список рядов кнопок: [["Кератин"], ["Ботокс"], ["В меню"]].

    Ряды так и остаются рядами — в клавиатуру своего формата (у ВК VkKeyboard,
    у Telegram будет своё) их превратит мессенджер в момент отправки, внутри
    send(). Поэтому одна и та же клавиатура годится для любого канала, а собрать
    её можно хоть на импорте, до появления самого мессенджера.

    Обёртка тонкая, но полезная: даёт клавиатуре имя (видно, что это не просто
    список) и одно место, где потом можно проверить или дополнить ряды.
    """
    return rows


def chunk(items, size):
    """Разложить список по рядам: chunk(["a","b","c"], 2) -> [["a","b"],["c"]].

    Нужно для клавиатуры: 20 кнопок времени в один ряд не влезут.
    Последний ряд получается короче, если элементы не поделились ровно.
    """
    return [items[i:i + size] for i in range(0, len(items), size)]


def titles_keyboard(catalog, per_row=1):
    """Клавиатура из названий словаря услуг/длин/густоты + кнопка «В меню».

    Плюс подхода: добавили новую услугу в config.py — кнопка появилась сама.
    per_row — сколько кнопок в ряд: у длины и густоты названия короткие,
    их можно ставить по две и экономить ряды.
    """
    titles = [item["title"] for item in catalog.values()]
    rows = chunk(titles, per_row)
    rows.append([BACK_TO_MENU])
    return build_keyboard(rows)


def hints_text(catalog):
    """Расшифровка вариантов под вопросом: «Короткие — до плеч».

    Живёт рядом с titles_keyboard(), потому что делает то же самое: собирает
    из справочника в config.py то, что увидит клиент. Добавили вариант
    в настройки — появились и кнопка, и строка с пояснением.

    Нужна там, где название кнопки само по себе ничего не говорит: длину
    и особенно густоту своих волос человек про себя не знает, а ответ
    определяет и цену, и сколько времени занять в расписании.

    Вариант без подсказки просто пропускаем, а не падаем: кнопка у него всё
    равно есть, и добавленная в настройки длина без hint должна стоить
    строчки пояснения, а не всего сообщения.
    """
    return "\n".join(f"{item['title']} — {item['hint']}"
                     for item in catalog.values() if item.get("hint"))


# Подпись кнопки записей меняется у каждого клиента: «Мои записи (3/5)».
# Поэтому это не готовая клавиатура, а функция: собираем её заново
# при каждой отправке, иначе все увидят чужой счётчик.
MY_BOOKINGS_BUTTON = "Мои записи"


MY_SUBS_BUTTON = "Мои подписки"

# Вход в кабинет. Кнопку видит только владелец и только в главном меню —
# см. menu_keyboard() и разбор MAIN_MENU в handle_message().
#
# Кабинет появился не ради красоты: в главном меню владельца уже четыре ряда
# из пяти возможных, и четыре его кнопки туда не влезут — пропадёт выход
# «В меню». Поэтому одна дверь, а за ней всё остальное.
CABINET_BUTTON = "Кабинет"

SCHEDULE_BUTTON = "Расписание"
CLOSE_BUTTON = "Закрыть время"
CLOSURES_BUTTON = "Что закрыто"
WORK_BUTTON = "График работы"

# Возврат в кабинет. Не «Назад»: так подписана листалка страниц на экране
# времени, и в одной переписке две разные «Назад» путали бы.
TO_CABINET = "В кабинет"

# Возврат к списку дней с экрана одного дня.
OTHER_SCHEDULE_DAY = "Другой день"

# Что именно закрываем.
CLOSE_WHOLE_DAY = "Весь день"
CLOSE_HOURS = "Часть дня"
CLOSE_PERIOD = "Несколько дней"
CLOSE_PAUSE = "Пауза до отмены"

# Подтверждение закрытия и снятие.
CLOSE_YES = "Да, закрыть"
CLOSE_NO = "Нет, оставить"

# График работы.
WORKDAYS_BUTTON = "Рабочие дни"
WORK_START_BUTTON = "Начало дня"
WORK_END_BUTTON = "Конец дня"
WORK_DONE = "Готово"

# На сколько дней вперёд «пауза до отмены» закрывает запись. Год — это просто
# «надолго»: снимается она кнопкой, а не по сроку, но какой-то конец у строки
# в базе быть обязан, иначе её пришлось бы отличать от остальных отдельным
# признаком.
PAUSE_DAYS = 365

# Сколько дней показывать мастеру кнопками, когда он выбирает, что закрыть.
CLOSE_DAYS_TO_SHOW = 9


def my_bookings_button(user_id):
    """«Мои записи» или «Мои записи (3/5)», если записи есть."""
    count = schedule.active_count(user_id.platform, user_id.id)
    if count == 0:
        return MY_BOOKINGS_BUTTON
    return f"{MY_BOOKINGS_BUTTON} ({count}/{config.MAX_ACTIVE_BOOKINGS})"


def my_subs_button(user_id):
    """«Мои подписки (2/3)» — счётчик такой же, как у записей."""
    count = len(schedule.user_subscriptions(user_id.platform, user_id.id))
    if count == 0:
        return MY_SUBS_BUTTON
    return f"{MY_SUBS_BUTTON} ({count}/{config.MAX_SUBSCRIPTIONS})"


def menu_keyboard(user_id):
    """Клавиатура главного меню — со счётчиком записей этого клиента.

    Кнопку раздела не показываем, если раздел пуст: нажать «Мои записи»
    и услышать «записей нет» — тупик, за который клиент зря платил
    нажатием. Поэтому третий ряд то есть, то нет, то из одной кнопки:
    записи и подписки делят его между собой.
    """
    rows = [
        ["Узнать стоимость"],
        ["Записаться"],
    ]

    sections = []
    if schedule.active_count(user_id.platform, user_id.id):
        sections.append(my_bookings_button(user_id))
    if schedule.user_subscriptions(user_id.platform, user_id.id):
        sections.append(my_subs_button(user_id))
    if sections:
        rows.append(sections)

    # Владельцу — вход в кабинет. Кнопка есть только здесь, в главном меню,
    # и только у него: на других экранах она не появляется даже у владельца.
    # Так дела мастера не могут встретиться с его же записью как клиента —
    # из шага записи в кабинет просто нет двери.
    if is_owner(user_id):
        rows.append([CABINET_BUTTON])

    return build_keyboard(rows)


SERVICE_KEYBOARD = titles_keyboard(config.SERVICES)  # названия длинные — по одной
LENGTH_KEYBOARD = titles_keyboard(config.LENGTHS, per_row=2)
DENSITY_KEYBOARD = titles_keyboard(config.DENSITIES, per_row=2)

RESULT_KEYBOARD = build_keyboard([
    ["Записаться"],
    ["Посчитать ещё раз"],
    [BACK_TO_MENU],
])

OTHER_DAY = "Выбрать другой день"
OTHER_TIME = "Выбрать другое время"

# Вход в подписку. Кнопок две, потому что «нужного времени нет» клиент
# понимает в двух разных местах: на списке дней и внутри дня.
NO_DAY = "Нужного дня нет"
NO_TIME = "Нет нужного времени"
SUBSCRIBE = "Подписаться"

# Листание страниц с временем.
PAGE_PREV = "← Назад"
PAGE_NEXT = "Далее →"

# Окошек на одной странице: 2 ряда по 3 кнопки. Было 3 ряда, но на экране
# времени появился свой ряд под «Нет нужного времени», а лимит VK — 5 рядов:
# окошки + листание + подписка + «другой день / в меню».
SLOTS_PER_PAGE = COMFORT_BUTTONS_IN_ROW * 2

CONFIRM_KEYBOARD = build_keyboard([
    ["Подтвердить запись"],
    [OTHER_TIME],
    [BACK_TO_MENU],
])

CANCEL_YES = "Да, отменить запись"
CANCEL_NO = "Нет, оставить"

# Подпись не «Перенести»: рядом стоит «Выбрать другое время» с экрана времени,
# и две похожие кнопки в одной переписке путали бы.
MOVE_BOOKING = "Перенести на другое время"

CANCEL_KEYBOARD = build_keyboard([
    [MOVE_BOOKING],
    [CANCEL_YES],
    [CANCEL_NO],
    [BACK_TO_MENU],  # выход должен быть на любом экране
])

SUBSCRIBE_KEYBOARD = build_keyboard([
    [SUBSCRIBE],
    [OTHER_DAY],  # здесь ведёт к списку занятых дней, а не свободных
    [BACK_TO_MENU],
])

# Клиент уже подписан на этот день — подписывать второй раз не на что,
# остаются только два выхода.
ALREADY_SUBSCRIBED_KEYBOARD = build_keyboard([
    [OTHER_DAY],
    [BACK_TO_MENU],
])


def limit_keyboard(user_id):
    """Клавиатура для случая «лимит записей исчерпан»."""
    return build_keyboard([
        [my_bookings_button(user_id)],
        [BACK_TO_MENU],
    ])


CONFIRM_COMING = "Подтверждаю"
CANCEL_BOOKING = "Отменить запись"

# Клавиатура напоминания: ответить на «придёте?» можно только двумя способами,
# и оба должны быть под рукой — иначе клиент, который не сможет прийти, просто
# промолчит, и время освободится лишь автоотменой за 12 часов.
#
# Все три кнопки работают из любого состояния: напоминание приходит когда
# ему пора, в том числе посреди выбора времени для другой записи, и подменять
# клиенту клавиатуру тупиком нельзя.
REMINDER_KEYBOARD = build_keyboard([
    [CONFIRM_COMING],
    [CANCEL_BOOKING],
    [BACK_TO_MENU],
])

# Напоминание в самый день процедуры. «Подтверждаю» здесь нет: подтверждение
# получено ещё вчера, и кнопка, которая отвечает «подтверждать нечего», только
# сбивает с толку. А отмена нужна: именно в этот день и срываются планы.
DAY_REMINDER_KEYBOARD = build_keyboard([
    [CANCEL_BOOKING],
    [BACK_TO_MENU],
])


# =========================================================================
# 4. Расчёт цены и длительности
# =========================================================================

def round_to(value, step):
    """Округлить число до ближайшего кратного step (6182 -> 6200)."""
    return int(round(value / step) * step)


def plural(number, one, few, many):
    """Правильное окончание: 1 час, 2 часа, 5 часов."""
    if 11 <= number % 100 <= 14:
        return many
    last = number % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def format_duration(minutes):
    """240 -> «4 часа», 150 -> «2 часа 30 минут»."""
    hours = minutes // 60
    rest = minutes % 60

    parts = []
    if hours:
        parts.append(f"{hours} {plural(hours, 'час', 'часа', 'часов')}")
    if rest:
        parts.append(f"{rest} {plural(rest, 'минута', 'минуты', 'минут')}")
    return " ".join(parts) if parts else "меньше часа"


def calculate(service_key, length_key, density_key):
    """Посчитать вилку цены и длительность.

    Возвращает кортеж: (цена_от, цена_до, минуты).
    """
    service = config.SERVICES[service_key]
    length = config.LENGTHS[length_key]
    density = config.DENSITIES[density_key]

    price = service["base_price"] * length["price_k"] * density["price_k"]
    price_from = round_to(price * (1 - config.PRICE_SPREAD), 100)
    price_to = round_to(price * (1 + config.PRICE_SPREAD), 100)

    minutes = service["base_minutes"] * length["time_k"] * density["time_k"]
    minutes = round_to(minutes, 30)

    return price_from, price_to, minutes


# =========================================================================
# 5. Разбор ответа пользователя
# =========================================================================

def find_key(catalog, text):
    """Найти ключ услуги/длины/густоты по тексту сообщения.

    Клиент может нажать кнопку, а может написать руками «кератин» —
    сравниваем в нижнем регистре, поэтому оба варианта сработают.
    Если не нашли — вернём None.
    """
    for key, item in catalog.items():
        if item["title"].lower() == text:
            return key
    return None


# =========================================================================
# 6. Шаги сценария
# =========================================================================

def contacts():
    """Адрес и телефон одним куском.

    Одна функция на четыре сообщения — приветствие, подтверждение записи,
    «готово, записала» и «подписала». Это те моменты, когда клиент как раз
    и спрашивает «а где вы находитесь?»: ответ должен быть в сообщении,
    а не в отдельной переписке с мастером.
    """
    return f"Адрес: {config.ADDRESS}\nТелефон: {config.PHONE}"


def booking_card(booking):
    """Запись так, как её видит мастер: что, когда, какие волосы, почём.

    Длина и густота мастеру нужнее, чем клиенту: по ним он заранее понимает,
    сколько уйдёт материала и не придётся ли задержаться. Клиенту эти строки
    в его сообщениях не показываем — он сам их только что и выбирал.
    """
    return (
        f"{config.SERVICES[booking['service']]['title']}\n"
        f"{schedule.day_label(booking['date'])}, {booking['start']}–"
        f"{schedule.end_time(booking)}\n"
        f"Длина: {config.LENGTHS[booking['length']]['title']}, "
        f"густота: {config.DENSITIES[booking['density']]['title']}\n"
        f"Стоимость: {booking['price_from']}–{booking['price_to']} ₽"
    )


def late_note(booking):
    """Приписка про позднюю отмену — или пустая строка, если отменили заранее.

    Отмена за неделю и отмена за час — для мастера разные новости: во втором
    случае время уже не продать, и узнать об этом он должен сразу, а не вечером
    из расписания.

    Отрицательный остаток означает, что запись успела начаться: приписывать
    к ней «оставалось столько-то» бессмысленно.
    """
    left = schedule.minutes_left(booking)

    if left <= 0 or left > config.LATE_CANCEL_HOURS * 60:
        return ""

    return f"\n\nДо записи оставалось {format_duration(left)}."


def forget_move(user_id):
    """Забыть, что клиент собирался что-то переносить.

    Вызывается на выходе в меню и в начале нового расчёта — то есть везде,
    откуда начинается что-то другое. Без этого брошенный посреди дела перенос
    остался бы висеть в состоянии, и следующая обычная запись сняла бы старую:
    признаком переноса служит как раз move_id.
    """
    get_user(user_id).pop("move_id", None)


def show_menu(user_id, greeting=False):
    user = get_user(user_id)
    forget_move(user_id)
    user["state"] = MAIN_MENU

    if greeting:
        text = (
            "Привет! Я помогу подобрать процедуру, рассчитать примерную "
            "стоимость и записаться к мастеру.\n\n"
            f"{contacts()}\n\n"
            "Выбери, что нужно:"
        )
    else:
        text = "Главное меню. Что дальше?"

    send(user_id, text, menu_keyboard(user_id))


def ask_service(user_id):
    user = get_user(user_id)
    forget_move(user_id)  # начали заново — значит это уже не перенос
    user["state"] = SELECTING_SERVICE
    send(user_id, "Выберите процедуру:", SERVICE_KEYBOARD)


def ask_length(user_id):
    user = get_user(user_id)
    user["state"] = SELECTING_LENGTH
    send(user_id,
         "Выберите длину волос:\n\n" + hints_text(config.LENGTHS),
         LENGTH_KEYBOARD)


def ask_density(user_id):
    user = get_user(user_id)
    user["state"] = SELECTING_DENSITY
    send(user_id,
         "Выберите густоту волос:\n\n" + hints_text(config.DENSITIES),
         DENSITY_KEYBOARD)


def show_price(user_id):
    user = get_user(user_id)
    user["state"] = PRICE_CALCULATED

    price_from, price_to, minutes = calculate(
        user["service"], user["length"], user["density"]
    )

    # Запоминаем результат: он понадобится дальше, чтобы подобрать окошки
    # нужной длины и сохранить цену в самой записи.
    user["minutes"] = minutes
    user["price_from"] = price_from
    user["price_to"] = price_to

    text = (
        f"Процедура: {config.SERVICES[user['service']]['title']}\n"
        f"Длина волос: {config.LENGTHS[user['length']]['title']}\n"
        f"Густота волос: {config.DENSITIES[user['density']]['title']}\n\n"
        f"Ориентировочная стоимость: {price_from}–{price_to} ₽\n"
        f"Примерная продолжительность: {format_duration(minutes)}\n\n"
        "Точную цену мастер назовёт после осмотра волос."
    )
    send(user_id, text, RESULT_KEYBOARD)


# =========================================================================
# 7. Запись: день -> время -> подтверждение
# =========================================================================

def show_limit_message(user_id):
    """Объяснить, почему записаться нельзя, и что с этим делать."""
    user = get_user(user_id)
    user["state"] = MAIN_MENU  # чтобы кнопка «Мои записи» ниже сразу работала

    send(
        user_id,
        f"У вас уже {config.MAX_ACTIVE_BOOKINGS} активных записей — "
        "это максимум.\n\n"
        "Чтобы записаться на новое время, сначала отмените одну из текущих:\n"
        "«Мои записи» → «Отменить …»\n\n"
        "Записи, которые уже прошли, место в лимите не занимают.",
        limit_keyboard(user_id),
    )


def start_booking(user_id):
    """Начать запись с выбора процедуры — сначала проверив лимит.

    Проверка стоит в самом начале: заставлять клиента выбрать процедуру,
    длину и густоту, чтобы в конце сказать «нельзя», — издевательство.

    Спрашиваем то же самое, что и «Узнать стоимость»: без услуги, длины
    и густоты мы не знаем, сколько времени занять в расписании. Разница
    одна — лимит: посчитать цену можно всегда, записаться нет.
    """
    if schedule.limit_reached(user_id.platform, user_id.id):
        show_limit_message(user_id)
        return

    ask_service(user_id)


def book_button(user_id):
    """Нажали «Записаться» — на каком бы шаге клиент ни находился.

    Кнопка есть в каждом старом сообщении с главным меню, а сообщения
    в переписке живут вечно: нажать её могут и со списка записей, и посреди
    выбора густоты. Поэтому разбор один на всех и лежит здесь.

    Если цена только что посчитана, параметры процедуры уже известны —
    идём сразу к выбору дня. Во всех остальных случаях начинаем сначала:
    хранить в памяти прошлый расчёт и молча записывать по нему нельзя,
    клиент мог прийти уже за другой процедурой.
    """
    if get_user(user_id)["state"] == PRICE_CALCULATED:
        ask_date(user_id)
    else:
        start_booking(user_id)


def ask_date(user_id):
    """Показать дни, в которых есть окно под процедуру этого клиента."""
    user = get_user(user_id)

    # Лимит проверяем и здесь: сюда можно прийти не только из start_booking(),
    # но и из калькулятора — «Узнать стоимость» лимитом не ограничен.
    #
    # При переносе не проверяем: записей у клиента не прибавится, старая уйдёт
    # вместе с новой. Иначе клиент, набравший максимум, не смог бы передвинуть
    # ни одну из своих записей — а это ровно то, что ему в такой момент нужно.
    if not user.get("move_id") and schedule.limit_reached(user_id.platform, user_id.id):
        show_limit_message(user_id)
        return

    days = schedule.work_days(user["minutes"], user.get("move_id"))

    if not days:
        send(
            user_id,
            "Свободных окошек на ближайшие дни нет 😔\n"
            "Напишите мастеру напрямую — возможно, что-то освободится.",
            menu_keyboard(user_id),
        )
        user["state"] = MAIN_MENU
        return

    user["state"] = SELECTING_DATE

    # По 3 дня в ряд: 8 дней укладываются в 3 ряда, плюс ряд подписки
    # и ряд «В меню» — ровно 5. Поэтому DAYS_TO_SHOW нельзя поднимать выше 9.
    rows = chunk([schedule.day_label(day) for day in days],
                 COMFORT_BUTTONS_IN_ROW)
    rows.append([NO_DAY])
    rows.append([BACK_TO_MENU])

    send(
        user_id,
        f"Процедура займёт {format_duration(user['minutes'])}.\n"
        "Выберите день:",
        build_keyboard(rows),
    )


def ask_time(user_id, day, page=0):
    """Показать свободное время выбранного дня — страницами по SLOTS_PER_PAGE.

    page — номер страницы, считая с нуля. Все окошки дня в клавиатуру
    не влезают: на телефоне это 3 кнопки в ряд, то есть SLOTS_PER_PAGE
    за страницу.
    """
    user = get_user(user_id)
    slots = schedule.free_slots(day, user["minutes"],
                                exclude_id=user.get("move_id"))

    if not slots:
        # Пока клиент выбирал, день успели занять — возвращаем к списку дней.
        send(user_id, "Этот день только что заняли. Выберите другой:")
        ask_date(user_id)
        return

    # Округление ВВЕРХ: при 16 окошках и 6 на странице нужно 3 страницы,
    # иначе последние четыре окошка остались бы без страницы.
    pages_total = (len(slots) + SLOTS_PER_PAGE - 1) // SLOTS_PER_PAGE

    # Страховка от кнопки из старого сообщения: там мог быть номер страницы,
    # которой больше нет (окошки заняли, страниц стало меньше).
    page = max(0, min(page, pages_total - 1))

    # Срез нужной страницы: при page=1 и 6 окошках на странице это slots[6:12].
    page_slots = slots[page * SLOTS_PER_PAGE:(page + 1) * SLOTS_PER_PAGE]

    user["day"] = day
    user["page"] = page
    user["state"] = SELECTING_TIME

    rows = chunk(page_slots, COMFORT_BUTTONS_IN_ROW)

    # Кнопку листания не рисуем, если листать некуда. «Заблокированных»
    # кнопок у ботов нет, зато клавиатура собирается заново перед каждой
    # отправкой — значит лишнюю кнопку можно просто не добавлять.
    nav = []
    if page > 0:
        nav.append(PAGE_PREV)
    if page < pages_total - 1:
        nav.append(PAGE_NEXT)
    if nav:
        rows.append(nav)

    rows.append([NO_TIME])
    rows.append([OTHER_DAY, BACK_TO_MENU])

    text = f"{schedule.day_label(day)} — свободное время:"
    if pages_total > 1:
        text += f"\nСтраница {page + 1} из {pages_total}"

    send(user_id, text, build_keyboard(rows))


def show_confirmation(user_id, start):
    """Показать итог записи перед сохранением."""
    user = get_user(user_id)
    user["time"] = start
    user["state"] = CONFIRMING

    # При переносе показываем, откуда переносим: иначе на этом экране два
    # времени — старое и новое — и клиент не видит, какое из них какое.
    previous = ""
    if user.get("move_id"):
        old = schedule.get_booking(user["move_id"])
        if old is not None:
            previous = (f"Вместо {schedule.pretty_date(old['date'])} "
                        f"в {old['start']}\n\n")

    text = (
        "Проверьте запись:\n\n"
        f"{previous}"
        f"Процедура: {config.SERVICES[user['service']]['title']}\n"
        f"Дата: {schedule.pretty_date(user['day'])}\n"
        f"Время начала: {start}\n"
        f"Продолжительность: {format_duration(user['minutes'])}\n"
        f"Ориентировочная стоимость: {user['price_from']}–{user['price_to']} ₽\n\n"
        f"{contacts()}"
    )
    send(user_id, text, CONFIRM_KEYBOARD)


def save_booking(user_id):
    """Сохранить запись или перенести старую — если время ещё свободно.

    Оба случая заканчиваются одинаково (запись есть, подписка на этот день
    больше не нужна, владельцу ушло сообщение), поэтому и живут в одной
    функции: развилок в ней две, а не два сценария целиком.
    """
    user = get_user(user_id)
    moving = user.get("move_id")

    # Ещё одна проверка лимита, прямо перед сохранением. Та, в ask_date(),
    # была для удобства клиента, а эта — настоящая: она спрашивает базу
    # заново, поэтому видит записи, сделанные пока клиент выбирал время.
    # При переносе её нет по той же причине, что и в ask_date().
    if not moving and schedule.limit_reached(user_id.platform, user_id.id):
        show_limit_message(user_id)
        return

    previous = schedule.get_booking(moving) if moving else None

    if moving and (previous is None
                   or previous["status"] not in schedule.ACTIVE_STATUSES):
        # Пока клиент выбирал новое время, старую запись отменили или она
        # успела пройти. Переносить нечего, а записать «заодно» новую было бы
        # уже не тем, о чём он просил.
        forget_move(user_id)
        send(user_id, "Запись, которую вы переносили, уже не активна.")
        show_my_bookings(user_id)
        return

    parameters = dict(
        platform=user_id.platform,
        user_id=user_id.id,
        day=user["day"],
        start=user["time"],
        minutes=user["minutes"],
        service=user["service"],
        length=user["length"],
        density=user["density"],
        price_from=user["price_from"],
        price_to=user["price_to"],
    )

    if moving:
        booking = schedule.move_booking(moving, **parameters)
    else:
        booking = schedule.create_booking(**parameters)

    if booking is None:
        # Между показом кнопки и нажатием окошко заняли — обычное дело,
        # когда клиентов много. Само уведомление время не бронирует.
        # Перенос при этом не срывается: старая запись цела, и move_id
        # остаётся — клиент просто выбирает другое время.
        send(user_id, "К сожалению, это окошко уже заняли 😔")
        ask_time(user_id, user["day"])
        return

    # Клиент дождался своего дня и записался — ждать в нём больше нечего.
    schedule.remove_subscription(user_id.platform, user_id.id, booking["date"])

    forget_move(user_id)
    user["state"] = MAIN_MENU

    if previous is not None:
        headline = (f"Перенесла запись с "
                    f"{schedule.pretty_date(previous['date'])} "
                    f"{previous['start']} на "
                    f"{schedule.pretty_date(booking['date'])} "
                    f"в {booking['start']}.")
    else:
        headline = (f"Готово! Записала вас на "
                    f"{schedule.pretty_date(booking['date'])} "
                    f"в {booking['start']}.")

    send(
        user_id,
        f"{headline}\n\n"
        f"Процедура: {config.SERVICES[booking['service']]['title']}\n"
        f"Продолжительность: {format_duration(booking['minutes'])}\n"
        f"Стоимость: {booking['price_from']}–{booking['price_to']} ₽\n\n"
        f"{contacts()}",
        menu_keyboard(user_id),  # счётчик записей уже увеличился
    )

    # Владельцу — последним. Клиент ждёт ответа прямо сейчас, а уведомление
    # мастеру стоит ещё двух обращений к VK: узнать имя и отправить.
    if previous is not None:
        notify_owner("Клиент перенёс запись\n\n"
                     f"Было: {schedule.day_label(previous['date'])}, "
                     f"{previous['start']}\n\n"
                     f"{booking_card(booking)}\n\n"
                     f"{client_card(user_id)}")
    else:
        notify_owner("Новая запись\n\n"
                     f"{booking_card(booking)}\n\n"
                     f"{client_card(user_id)}")

    # Старое время освободилось — ровно как при отмене, и ждать его кто-то мог.
    if previous is not None:
        notify_subscribers(previous)


# =========================================================================
# 8. Подписка на свободное окошко и отписка
# =========================================================================
# Клиенту не подошло ни одно окошко — вместо «до свидания» предлагаем
# подождать чужую отмену. Экранов три: список занятых дней, подтверждение
# и «Мои подписки» (он же отписка). Прийти на подтверждение можно и минуя
# список — с экрана времени, там день уже выбран.


def ask_sub_date(user_id):
    """Показать дни, в которых времени нет, — на них можно подписаться."""
    user = get_user(user_id)
    days = schedule.busy_days(user["minutes"], user.get("move_id"))

    if not days:
        # Занятых дней нет вообще: клиент нажал «нужного дня нет», хотя
        # свободно всё. Отправляем обратно к выбору дня.
        send(user_id, "Похоже, все ближайшие дни свободны 🙂")
        ask_date(user_id)
        return

    user["state"] = SELECTING_SUB_DATE

    rows = chunk([schedule.day_label(day) for day in days],
                 COMFORT_BUTTONS_IN_ROW)
    rows.append([BACK_TO_MENU])

    send(
        user_id,
        "Здесь дни, где время под вашу процедуру уже занято.\n"
        "Можно подписаться: если кто-то отменится, я сразу напишу.\n\n"
        "Выберите день:",
        build_keyboard(rows),
    )


def ask_sub_confirm(user_id, day):
    """Спросить, подписывать ли клиента на этот день.

    Сюда ведут оба входа: и список занятых дней, и кнопка на экране времени.
    """
    user = get_user(user_id)

    # То же, что «Нужного дня нет» при полностью свободном расписании:
    # подписываться не на что, поэтому возвращаем клиента к выбору времени.
    # Сюда попадают только с экрана времени — в списке занятых дней
    # свободных дней по определению нет.
    if not schedule.has_bookings(day):
        send(
            user_id,
            f"{schedule.day_label(day)} — совсем свободный день, "
            "в нём открыто всё рабочее время 🙂",
        )
        ask_time(user_id, day)
        return

    user["sub_day"] = day
    user["state"] = SUB_CONFIRM

    if schedule.is_subscribed(user_id.platform, user_id.id, day):
        send(
            user_id,
            f"Вы уже подписаны на {schedule.pretty_date(day)} — "
            "напишу, как только там появится окошко.",
            ALREADY_SUBSCRIBED_KEYBOARD,
        )
        return

    if schedule.subscriptions_limit_reached(user_id.platform, user_id.id):
        # Состояние сбрасываем в меню, чтобы кнопка «Мои подписки» ниже
        # сразу работала — так же, как сделано с лимитом записей.
        user["state"] = MAIN_MENU
        send(
            user_id,
            f"У вас уже {config.MAX_SUBSCRIPTIONS} "
            f"{plural(config.MAX_SUBSCRIPTIONS, 'подписка', 'подписки', 'подписок')}"
            " — это максимум.\n\n"
            "Чтобы подписаться на этот день, откажитесь от одной из текущих:\n"
            "«Мои подписки» → номер подписки.\n\n"
            "Подписка снимается и сама: когда день проходит "
            "или когда вы записались на этот день.",
            build_keyboard([[my_subs_button(user_id)], [BACK_TO_MENU]]),
        )
        return

    send(
        user_id,
        f"Подписать вас на {schedule.pretty_date(day)}?\n\n"
        f"Процедура: {config.SERVICES[user['service']]['title']}\n"
        f"Нужно времени: {format_duration(user['minutes'])}\n"
        f"Стоимость: {user['price_from']}–{user['price_to']} ₽\n\n"
        "Я напишу, если в этот день освободится подходящее окошко.",
        SUBSCRIBE_KEYBOARD,
    )


def do_subscribe(user_id):
    """Оформить подписку на выбранный день."""
    user = get_user(user_id)
    day = user["sub_day"]

    subscription = schedule.add_subscription(
        platform=user_id.platform,
        user_id=user_id.id,
        day=day,
        minutes=user["minutes"],
        service=user["service"],
        length=user["length"],
        density=user["density"],
        price_from=user["price_from"],
        price_to=user["price_to"],
    )

    if subscription is None:
        # Подписка уже есть или лимит исчерпан — ask_sub_confirm() объяснит,
        # что именно случилось, ей для этого не нужно ничего пересчитывать.
        ask_sub_confirm(user_id, day)
        return

    user["state"] = MAIN_MENU
    # В меню теперь есть «Мои подписки» — эта подписка первая или очередная.
    send(
        user_id,
        f"Подписала вас на {schedule.pretty_date(day)}.\n\n"
        "Если кто-то отменит запись и освободится окошко под вашу процедуру, "
        "я сразу напишу — записаться можно будет прямо из сообщения.\n"
        "Место при этом не бронируется: кто первый записался, того и время.\n\n"
        f"{contacts()}",
        menu_keyboard(user_id),
    )


def show_my_subs(user_id):
    """Показать подписки клиента и кнопки отписки.

    Устроено как «Мои записи»: кнопки — номера из списка, они короткие
    и влезают в один ряд. Разница одна — подтверждения нет: подписка
    ничего не занимает, случайно снять её не страшно, а вернуть легко.
    """
    user = get_user(user_id)
    subscriptions = schedule.user_subscriptions(user_id.platform, user_id.id)

    if not subscriptions:
        user["state"] = MAIN_MENU
        send(
            user_id,
            "У вас нет подписок.\n"
            "Подписаться можно при записи — на экране выбора дня или времени.",
            menu_keyboard(user_id),
        )
        return

    user["state"] = MY_SUBS

    lines = [f"Вы ждёте окошко "
             f"({len(subscriptions)}/{config.MAX_SUBSCRIPTIONS}):"]
    for number, subscription in enumerate(subscriptions, start=1):
        lines.append(
            f"\n{number}. {config.SERVICES[subscription['service']]['title']}\n"
            f"{schedule.pretty_date(subscription['date'])}, "
            f"нужно {format_duration(subscription['minutes'])}\n"
            f"Стоимость: {subscription['price_from']}–"
            f"{subscription['price_to']} ₽"
        )
    lines.append("\nЧтобы отказаться от подписки, нажмите её номер.")

    numbers = [str(number) for number in range(1, len(subscriptions) + 1)]
    rows = chunk(numbers, MAX_BUTTONS_IN_ROW)
    rows.append([BACK_TO_MENU])

    send(user_id, "\n".join(lines), build_keyboard(rows))


def do_unsubscribe(user_id, subscription):
    """Снять подписку и показать, что осталось."""
    day = subscription["date"]

    if schedule.remove_subscription(user_id.platform, user_id.id, day):
        send(user_id, f"Больше не жду для вас окошко "
                      f"{schedule.pretty_date(day)}.")
    else:
        send(user_id, "Не нашла эту подписку — возможно, её уже сняли.")

    show_my_subs(user_id)


def offer_free_slot(subscriber_id, subscription):
    """Написать одному подписчику и сразу открыть ему выбор времени.

    Параметры процедуры берём из подписки, а не из памяти бота: клиент мог
    за это время посчитать стоимость чего-то другого, а ждёт он именно то,
    на что подписывался. Заодно уведомление продолжает работать после
    перезапуска бота, когда в памяти о клиенте ничего нет.
    """
    # Замок на весь разговор: если клиент прямо сейчас пишет боту сам, его
    # сообщение подождёт — иначе два потока перебили бы состояние друг другу.
    with DIALOG_LOCK:
        user = get_user(subscriber_id)

        for field in ("service", "length", "density", "minutes",
                      "price_from", "price_to"):
            user[field] = subscription[field]

        send(
            subscriber_id,
            f"Освободилось окошко "
            f"{schedule.pretty_date(subscription['date'])} 🎉\n\n"
            f"Процедура: {config.SERVICES[subscription['service']]['title']}\n"
            f"Нужно времени: {format_duration(subscription['minutes'])}\n"
            f"Стоимость: {subscription['price_from']}–"
            f"{subscription['price_to']} ₽\n\n"
            "Место не бронируется, поэтому записывайтесь, пока время свободно.",
        )

        # Дальше клиент идёт обычным путём записи: время -> подтверждение.
        # Отдельного сценария для «записи из уведомления» не нужно, и подписка
        # снимется сама — save_booking() это уже делает.
        ask_time(subscriber_id, subscription["date"])

        # Единственное место, где состояние диалога меняет фоновый поток,
        # а не ответ на сообщение. Без этой строки уведомление переживало бы
        # перезапуск, а шаг, на который оно клиента поставило, — нет.
        save_user(subscriber_id)


def notify_subscribers(cancelled):
    """Рассказать подписчикам дня, что запись отменили и время открылось.

    Вызывается из отмены — единственного места, где время может
    освободиться. Поэтому фоновый поток с обходом расписания по часам
    подпискам не нужен: всё происходит внутри handle_message().

    Пишем всем подходящим сразу, без очереди: кто первый нажмёт время,
    того и окошко.
    """
    day = cancelled["date"]

    cancelled_by = Client(cancelled["platform"], cancelled["user_id"])

    for subscription in schedule.day_subscribers(day):
        subscriber = Client(subscription["platform"], subscription["user_id"])

        if subscriber == cancelled_by:
            continue  # сам отменил — сам и освободил, сообщать нечего

        # Молчим, если записаться всё равно нельзя: клиент набрал максимум
        # записей, или освободившееся время короче его процедуры (free_slots
        # сама учтёт и уборку, и конец рабочего дня). Уведомление, из которого
        # нельзя записаться, — просто спам.
        if schedule.limit_reached(subscriber.platform, subscriber.id):
            continue
        if not schedule.free_slots(day, subscription["minutes"]):
            continue

        try:
            offer_free_slot(subscriber, subscription)
        except messenger.MessengerError as error:
            # Клиент закрыл сообщения от сообщества или удалил диалог.
            # Остальные подписчики тут не при чём — рассылку продолжаем.
            print(f"не смогла написать {subscriber.id}: {error}")


# =========================================================================
# 9. Мои записи и отмена
# =========================================================================

# Что клиент видит вместо внутреннего названия статуса.
STATUS_LABELS = {
    "NEW": "подтверждение спрошу за сутки",
    "REMINDED": "⚠️ ждёт вашего подтверждения",
    "CONFIRMED": "✅ подтверждена",
}


def show_my_bookings(user_id):
    """Показать активные записи клиента и кнопки отмены."""
    user = get_user(user_id)
    bookings = schedule.user_bookings(user_id.platform, user_id.id)

    if not bookings:
        user["state"] = MAIN_MENU
        send(
            user_id,
            "У вас нет активных записей.\n"
            "Нажмите «Записаться», чтобы выбрать время.",
            menu_keyboard(user_id),
        )
        return

    user["state"] = MY_BOOKINGS

    # Нумерация с единицы: «запись 0» клиенту показывать странно.
    lines = [f"Ваши записи ({len(bookings)}/{config.MAX_ACTIVE_BOOKINGS}):"]
    for number, booking in enumerate(bookings, start=1):
        lines.append(
            f"\n{number}. {config.SERVICES[booking['service']]['title']}\n"
            f"{schedule.pretty_date(booking['date'])}, "
            f"{booking['start']}–{schedule.end_time(booking)}\n"
            f"Стоимость: {booking['price_from']}–{booking['price_to']} ₽\n"
            # .get, а не [...]: неизвестный статус лучше показать как есть,
            # чем упасть на подписи к нему. База такого не пропустит (CHECK
            # у колонки status), но сообщение клиенту — не то место, где
            # стоит падать.
            f"Статус: {STATUS_LABELS.get(booking['status'], booking['status'])}"
        )
    lines.append("\nЧтобы отменить запись, нажмите её номер.")

    # Кнопки — просто номера: пять коротких кнопок влезают в один ряд даже
    # на телефоне (подпись в один символ), а ряд «В меню» остаётся вторым.
    # Раньше каждая запись занимала целый ряд, и при пяти записях
    # «В меню» уезжало за границу видимого.
    numbers = [str(number) for number in range(1, len(bookings) + 1)]
    rows = chunk(numbers, MAX_BUTTONS_IN_ROW)
    rows.append([BACK_TO_MENU])

    send(user_id, "\n".join(lines), build_keyboard(rows))


def ask_cancel_confirm(user_id, booking):
    """Спросить, что делать с записью: перенести, отменить или оставить.

    Экран остался с подтверждением отмены — отменить случайным нажатием
    по-прежнему нельзя. Перенос добавлен сюда же, а не отдельным пунктом
    в «Мои записи»: выбирать запись дважды, сперва для отмены, потом для
    переноса, клиенту незачем — он уже выбрал, о какой идёт речь.
    """
    user = get_user(user_id)
    user["cancel_id"] = booking["id"]  # запоминаем, о КАКОЙ записи речь
    user["state"] = CANCEL_CONFIRM

    send(
        user_id,
        "Что сделать с записью?\n\n"
        f"{config.SERVICES[booking['service']]['title']}\n"
        f"{schedule.pretty_date(booking['date'])}, {booking['start']}",
        CANCEL_KEYBOARD,
    )


def start_move(user_id):
    """Нажали «Перенести» — начинаем выбор нового дня и времени.

    Параметры процедуры берём из самой записи, а не из памяти: клиент мог
    успеть посчитать стоимость чего-то другого, а переносит он именно эту
    процедуру и по той цене, по которой записывался.

    move_id — признак того, что идёт перенос, а не новая запись. По нему
    save_booking() понимает, что старую запись надо снять, а лимит проверять
    не нужно: записей у клиента не прибавится.
    """
    user = get_user(user_id)
    booking = schedule.get_booking(user["cancel_id"])

    if booking is None or booking["status"] not in schedule.ACTIVE_STATUSES:
        send(user_id, "Эта запись уже не активна — переносить нечего.")
        show_my_bookings(user_id)
        return

    for field in ("service", "length", "density", "minutes",
                  "price_from", "price_to"):
        user[field] = booking[field]

    user["move_id"] = booking["id"]
    ask_date(user_id)


def do_cancel(user_id):
    """Отменить запись и показать, что осталось."""
    user = get_user(user_id)
    booking = schedule.cancel_booking(user["cancel_id"], user_id.platform,
                                      user_id.id)

    if booking is None:
        send(user_id, "Не нашла эту запись — возможно, её уже отменили.")
        show_my_bookings(user_id)
        return

    send(
        user_id,
        f"Запись на {schedule.pretty_date(booking['date'])} "
        f"в {booking['start']} отменена. Время снова свободно.",
    )
    show_my_bookings(user_id)

    # Рассылка последней: клиент, который отменяет, ждёт ответа прямо сейчас,
    # а уведомления — это ещё несколько запросов к VK.
    notify_owner("Клиент отменил запись\n\n"
                 f"{booking_card(booking)}\n\n"
                 f"{client_card(user_id)}"
                 f"{late_note(booking)}")
    notify_subscribers(booking)


def do_confirm(user_id):
    """Клиент нажал «Подтверждаю»."""
    confirmed = schedule.confirm_bookings(user_id.platform, user_id.id)

    if not confirmed:
        # Нажали кнопку из старого сообщения: всё уже подтверждено, ту запись
        # успели отменить — или бот пока ни о чём не спрашивал.
        send(user_id,
             "Подтверждать нечего — все записи, о которых я спрашивала, "
             "уже в силе.\nПро остальные напомню за сутки до процедуры.",
             menu_keyboard(user_id))
        return

    lines = ["Спасибо! Жду вас:"]
    for booking in confirmed:
        lines.append(
            f"\n{config.SERVICES[booking['service']]['title']}\n"
            f"{schedule.pretty_date(booking['date'])}, {booking['start']}"
        )
    lines.append("\nЕсли планы изменятся — отмените запись, "
                 "и я предложу это время другим.")

    # Состояние не трогаем: клиент мог подтверждать запись прямо посреди
    # выбора времени для новой, и сбрасывать его в меню было бы обидно.
    send(user_id, "\n".join(lines), menu_keyboard(user_id))


def start_cancel(user_id):
    """Клиент нажал «Отменить запись» — надо понять, какую именно.

    Кнопка приходит вместе с напоминанием, но нажать её могут и позже, когда
    напоминание пришло уже про другую запись. Поэтому отменяем ту, о которой
    спрашивали (REMINDED), а если такая не одна — просим выбрать номер:
    отмена необратима, угадывать тут нельзя.
    """
    bookings = schedule.user_bookings(user_id.platform, user_id.id)

    if not bookings:
        send(user_id, "Активных записей нет — отменять нечего.",
             menu_keyboard(user_id))
        return

    waiting = [booking for booking in bookings
               if booking["status"] == "REMINDED"]

    if len(waiting) == 1:
        ask_cancel_confirm(user_id, waiting[0])
        return

    if len(bookings) == 1:
        # Запись всего одна — какую отменять, вопросов не вызывает.
        ask_cancel_confirm(user_id, bookings[0])
        return

    send(user_id, "Выберите, какую запись отменить:")
    show_my_bookings(user_id)


# =========================================================================
# 9а. Кабинет владельца
# =========================================================================
# Всё, что бот показывает не клиенту, а мастеру: расписание, закрытие времени,
# отмена записи с причиной и рабочий график. Ролей ради этого не заводим:
# мастер здесь один и это владелец сообщества, поэтому всё различие —
# сравнение с config.OWNER_ID в is_owner().
#
# Вход только из главного меню (см. menu_keyboard): из шага записи в кабинет
# двери нет, поэтому дела мастера не могут перебить владельцу его собственную
# запись — состояние диалога у него одно на оба занятия.
#
# Экраны стараются ничего не помнить: где выбор виден на самой кнопке (день,
# час, номер записи), состояние не нужно вовсе. Где помнить приходится —
# черновик закрытия — он живёт в памяти и в базу не едет, а каждый экран
# проверяет, на месте ли нужное, и молча возвращает мастера в кабинет, если
# бота перезапустили на середине.

# Что видит мастер вместо внутреннего названия статуса. От клиентских подписей
# отличаются намеренно: клиенту важно, что от него хотят, мастеру — можно ли
# на эту запись рассчитывать.
OWNER_STATUS_LABELS = {
    "NEW": "напоминание ещё не отправляла",
    "REMINDED": "⚠️ не подтверждена",
    "CONFIRMED": "✅ подтверждена",
}

# Сколько символов причины оставляем. Ограничение не от жадности: причина
# уходит клиенту в сообщении и хранится в записи, а мастер вполне может
# случайно отправить туда пересланный текст на три экрана.
MAX_REASON = 300


def is_owner(user_id):
    """Это владелец сообщества?

    Проверка живёт в функции, а не в сравнениях по месту: их несколько —
    кнопка в меню и разбор каждого шага кабинета, — и разъехаться они
    не должны. Нулевой OWNER_ID владельцем не делает никого.
    """
    return bool(config.OWNER_ID) and user_id == owner()


def forget_draft(user_id):
    """Забыть недособранное закрытие и выбранный день расписания."""
    user = get_user(user_id)
    for field in OWNER_DRAFT:
        user.pop(field, None)


def clean_reason(text):
    """Причина от мастера: без лишних пробелов и не длиннее разумного."""
    return " ".join(text.split())[:MAX_REASON]


def closed_note():
    """Строка про паузу для кабинета: закрыто ли что-нибудь прямо сейчас."""
    today = date.today().isoformat()
    closures = schedule.all_closures()

    if schedule.closed_all_day(today, closures):
        return f"Сегодня запись закрыта: {schedule.closure_reason(today, closures)}"
    if closures:
        return f"Закрытых промежутков впереди: {len(closures)}"
    return "Ничего не закрыто, запись открыта."


def show_cabinet(user_id):
    """Кабинет мастера — всё, что он может сделать."""
    user = get_user(user_id)
    forget_draft(user_id)
    user["state"] = OWNER_CABINET

    send(
        user_id,
        "Кабинет мастера.\n\n"
        f"{closed_note()}\n"
        f"Работаете: {work_schedule_text()}",
        build_keyboard([
            [SCHEDULE_BUTTON],
            [CLOSE_BUTTON],
            [CLOSURES_BUTTON],
            [WORK_BUTTON],
            [BACK_TO_MENU],
        ]),
    )


# --- расписание -----------------------------------------------------------

def show_schedule_days(user_id):
    """Расписание: ближайшие дни, в которых кто-то записан."""
    user = get_user(user_id)
    days = schedule.days_with_bookings()[:config.DAYS_TO_SHOW]

    if not days:
        send(user_id, "Записей на ближайшие дни нет — время свободно.")
        show_cabinet(user_id)
        return

    user["state"] = OWNER_SCHEDULE
    user.pop("schedule_day", None)  # вернулись к списку — день больше не выбран

    lines = ["Ближайшие дни с записями:\n"]
    for day, count in days:
        lines.append(f"{schedule.day_label(day)} — {count} "
                     f"{plural(count, 'запись', 'записи', 'записей')}")
    lines.append("\nДней, которых нет в списке, никто не занял.")

    rows = chunk([schedule.day_label(day) for day, _ in days],
                 COMFORT_BUTTONS_IN_ROW)
    rows.append([TO_CABINET, BACK_TO_MENU])

    send(user_id, "\n".join(lines), build_keyboard(rows))


def show_schedule_day(user_id, day):
    """Расписание одного дня: кто, когда и на что записан.

    Записи пронумерованы, и номер — это кнопка отмены. Так же, как у клиента
    в «Моих записях»: номер короткий, пять штук влезают в один ряд, а искать
    для отмены отдельный экран мастеру не приходится.
    """
    user = get_user(user_id)
    bookings = schedule.day_bookings(day)

    if not bookings:
        # День успели освободить, пока мастер смотрел на список.
        send(user_id, f"{schedule.day_label(day)} — записей уже нет.")
        show_schedule_days(user_id)
        return

    user["state"] = OWNER_SCHEDULE
    user["schedule_day"] = day

    lines = [f"{schedule.pretty_date(day)}, {len(bookings)} "
             f"{plural(len(bookings), 'запись', 'записи', 'записей')}:"]

    for number, booking in enumerate(bookings, start=1):
        lines.append(
            f"\n{number}. {booking['start']}–{schedule.end_time(booking)}  "
            f"{config.SERVICES[booking['service']]['title']}\n"
            f"{client_card(client_of(booking))}\n"
            f"{config.LENGTHS[booking['length']]['title'].lower()}, "
            f"{config.DENSITIES[booking['density']]['title'].lower()}, "
            f"{booking['price_from']}–{booking['price_to']} ₽\n"
            f"{OWNER_STATUS_LABELS.get(booking['status'], booking['status'])}"
        )

    lines.append("\nЧтобы отменить запись, нажмите её номер.")

    numbers = [str(number) for number in range(1, len(bookings) + 1)]
    rows = chunk(numbers, MAX_BUTTONS_IN_ROW)
    rows.append([OTHER_SCHEDULE_DAY])
    rows.append([TO_CABINET, BACK_TO_MENU])

    send(user_id, "\n".join(lines), build_keyboard(rows))


# --- отмена записи мастером ----------------------------------------------

def ask_cancel_reason(user_id, booking):
    """Спросить, почему мастер отменяет запись.

    Причину спрашиваем до отмены, а не после: клиент, у которого сняли запись
    без объяснения, придёт выяснять к мастеру — то есть ровно та переписка,
    которой бот и должен был избавить.
    """
    user = get_user(user_id)
    user["cancel_booking_id"] = booking["id"]
    user["state"] = OWNER_CANCEL_REASON

    send(
        user_id,
        "Отменяем запись:\n\n"
        f"{booking_card(booking)}\n"
        f"{client_card(client_of(booking))}\n\n"
        "Напишите причину — я передам её клиенту.",
        build_keyboard([[TO_CABINET, BACK_TO_MENU]]),
    )


def do_master_cancel(user_id, reason):
    """Отменить запись от лица мастера и написать клиенту."""
    user = get_user(user_id)
    booking_id = user.get("cancel_booking_id")

    if booking_id is None:
        # Бота перезапустили посреди ввода причины.
        show_cabinet(user_id)
        return

    booking = schedule.cancel_by_master(booking_id, reason)
    user.pop("cancel_booking_id", None)

    if booking is None:
        send(user_id, "Эту запись уже отменили — ничего не делаю.")
        show_schedule_days(user_id)
        return

    try:
        send(
            client_of(booking),
            "Мастер отменил вашу запись 😔\n\n"
            f"{config.SERVICES[booking['service']]['title']}\n"
            f"{schedule.pretty_date(booking['date'])}, {booking['start']}\n\n"
            f"Причина: {reason}\n\n"
            "Извините за неудобство. Записаться на другое время можно кнопкой "
            "ниже — или напишите нам, подберём вместе.\n\n"
            f"{contacts()}",
            menu_keyboard(client_of(booking)),
        )
    except messenger.MessengerError as error:
        print(f"не смогла написать {booking['user_id']}: {error}")
        send(user_id, "Клиенту написать не получилось — сообщите ему сами.")

    send(user_id, "Запись отменена, клиенту написала.")

    # Время освободилось по-настоящему — как при обычной отмене.
    notify_subscribers(booking)

    day = user.get("schedule_day")
    if day:
        show_schedule_day(user_id, day)
    else:
        show_schedule_days(user_id)


# --- закрытие времени -----------------------------------------------------

def ask_close_kind(user_id):
    """С чего начинается закрытие: что именно закрываем."""
    user = get_user(user_id)
    forget_draft(user_id)
    user["state"] = OWNER_CLOSE_KIND

    send(
        user_id,
        "Что закрыть?\n\n"
        f"«{CLOSE_WHOLE_DAY}» — не работаете в этот день.\n"
        f"«{CLOSE_HOURS}» — отлучитесь на несколько часов.\n"
        f"«{CLOSE_PERIOD}» — отпуск или несколько дней подряд.\n"
        f"«{CLOSE_PAUSE}» — запись закрыта, пока не откроете сами.",
        build_keyboard([
            [CLOSE_WHOLE_DAY],
            [CLOSE_HOURS],
            [CLOSE_PERIOD],
            [CLOSE_PAUSE],
            [TO_CABINET],
        ]),
    )


def ask_close_since(user_id):
    """Выбрать день (или первый день периода)."""
    user = get_user(user_id)
    user["state"] = OWNER_CLOSE_SINCE

    days = schedule.upcoming_work_days(CLOSE_DAYS_TO_SHOW)
    rows = chunk([schedule.day_label(day) for day in days],
                 COMFORT_BUTTONS_IN_ROW)
    rows.append([TO_CABINET])

    question = ("С какого дня?" if user["close_kind"] == CLOSE_PERIOD
                else "Какой день?")
    send(user_id, question, build_keyboard(rows))


def ask_close_until(user_id):
    """Выбрать последний день периода — из тех, что после первого."""
    user = get_user(user_id)
    user["state"] = OWNER_CLOSE_UNTIL

    days = [day for day in schedule.upcoming_work_days(CLOSE_DAYS_TO_SHOW * 2)
            if day > user["close_since"]][:CLOSE_DAYS_TO_SHOW]

    if not days:
        send(user_id, "Дальше рабочих дней не нашлось — закрываю один день.")
        user["close_until"] = user["close_since"]
        ask_close_reason(user_id)
        return

    rows = chunk([schedule.day_label(day) for day in days],
                 COMFORT_BUTTONS_IN_ROW)
    rows.append([TO_CABINET])

    send(user_id,
         f"По какой день включительно?\n"
         f"Первый закрытый — {schedule.pretty_date(user['close_since'])}.",
         build_keyboard(rows))


def ask_close_from(user_id):
    """С какого часа мастера не будет."""
    user = get_user(user_id)
    user["state"] = OWNER_CLOSE_FROM

    rows = chunk(schedule.work_hours(), COMFORT_BUTTONS_IN_ROW)
    rows.append([TO_CABINET])

    send(user_id,
         f"{schedule.pretty_date(user['close_since'])} — с какого часа?",
         build_keyboard(rows))


def ask_close_to(user_id):
    """По какой час. Показываем только то, что позже начала."""
    user = get_user(user_id)
    user["state"] = OWNER_CLOSE_TO

    hours = schedule.work_hours(first=user["close_from"])
    if not hours:
        # Начало пришлось на самый конец дня — закрываем до конца.
        user["close_to"] = schedule.work_end()
        ask_close_reason(user_id)
        return

    rows = chunk(hours, COMFORT_BUTTONS_IN_ROW)
    rows.append([TO_CABINET])

    send(user_id, f"С {user['close_from']} — по какой час?",
         build_keyboard(rows))


def ask_close_reason(user_id):
    """Что сказать клиентам, чьи записи придётся отменить."""
    user = get_user(user_id)
    user["state"] = OWNER_CLOSE_REASON

    send(
        user_id,
        f"Закрываю: {closure_text(user)}.\n\n"
        "Напишите причину — её увидят клиенты, чьи записи попадут "
        "в это время.",
        build_keyboard([[TO_CABINET, BACK_TO_MENU]]),
    )


def closure_text(draft):
    """Человеческое описание того, что закрываем: для вопросов и подтверждений."""
    since = schedule.pretty_date(draft["close_since"])

    if draft["close_kind"] == CLOSE_PAUSE:
        return "запись целиком, пока не откроете"
    if draft["close_kind"] == CLOSE_PERIOD:
        return f"с {since} по {schedule.pretty_date(draft['close_until'])}"
    if draft["close_kind"] == CLOSE_HOURS:
        return f"{since}, с {draft['close_from']} до {draft['close_to']}"
    return since


def ask_close_confirm(user_id):
    """Показать, что будет закрыто и сколько записей это заденет."""
    user = get_user(user_id)
    user["state"] = OWNER_CLOSE_CONFIRM

    affected = schedule.bookings_in_closure(
        user["close_since"], user["close_until"],
        user.get("close_from"), user.get("close_to"),
    )
    user["close_affected"] = len(affected)

    lines = [f"Закрываю {closure_text(user)}.",
             f"Причина: {user['close_reason']}"]

    if affected:
        lines.append(f"\nВ это время {len(affected)} "
                     f"{plural(len(affected), 'запись', 'записи', 'записей')} — "
                     "я их отменю и напишу каждому клиенту причину:")
        for booking in affected:
            lines.append(f"  {schedule.day_label(booking['date'])} "
                         f"{booking['start']} — "
                         f"{config.SERVICES[booking['service']]['title']}")
    else:
        lines.append("\nЗаписей в это время нет — никого не потревожу.")

    send(user_id, "\n".join(lines),
         build_keyboard([[CLOSE_YES], [CLOSE_NO], [TO_CABINET]]))


def do_close(user_id):
    """Закрыть время и отменить попавшие в него записи."""
    user = get_user(user_id)

    for field in ("close_kind", "close_since", "close_until", "close_reason"):
        if user.get(field) is None:
            show_cabinet(user_id)  # перезапуск посреди заполнения
            return

    reason = user["close_reason"]
    affected = schedule.bookings_in_closure(
        user["close_since"], user["close_until"],
        user.get("close_from"), user.get("close_to"),
    )

    schedule.add_closure(user["close_since"], user["close_until"],
                         user.get("close_from"), user.get("close_to"), reason)

    cancelled = schedule.cancel_many_by_master(affected, reason)

    # Подписчикам НЕ пишем: время не освободилось, оно закрыто. Уведомление
    # «освободилось окошко» привело бы их к экрану, где записаться некуда.
    failed = 0
    for booking in cancelled:
        try:
            send(
                client_of(booking),
                "Мастер отменил вашу запись 😔\n\n"
                f"{config.SERVICES[booking['service']]['title']}\n"
                f"{schedule.pretty_date(booking['date'])}, "
                f"{booking['start']}\n\n"
                f"Причина: {reason}\n\n"
                "Извините за неудобство. Как только смогу — запишу вас "
                "на другое время, выберите его кнопкой ниже.\n\n"
                f"{contacts()}",
                menu_keyboard(client_of(booking)),
            )
        except messenger.MessengerError as error:
            failed += 1
            print(f"не смогла написать {booking['user_id']}: {error}")

    report = [f"Закрыла {closure_text(user)}."]
    if cancelled:
        report.append(f"Отменила записей: {len(cancelled)}, клиентам написала.")
    if failed:
        report.append(f"Не дошло до {failed} — этим напишите сами.")

    send(user_id, "\n".join(report))
    show_cabinet(user_id)


def show_closures(user_id):
    """Что закрыто и как это снять."""
    user = get_user(user_id)
    closures = schedule.all_closures()

    if not closures:
        send(user_id, "Ничего не закрыто — запись открыта на все рабочие дни.")
        show_cabinet(user_id)
        return

    user["state"] = OWNER_CLOSURES

    lines = ["Закрытое время:"]
    for number, closure in enumerate(closures, start=1):
        when = schedule.pretty_date(closure["since"])
        if closure["until"] != closure["since"]:
            when += f" — {schedule.pretty_date(closure['until'])}"
        if closure["start"]:
            when += f", с {closure['start']} до {closure['finish']}"
        lines.append(f"\n{number}. {when}\n{closure['reason']}")

    lines.append("\nЧтобы снова открыть запись, нажмите номер.")
    lines.append("Отменённые записи при этом не вернутся — "
                 "клиентам придётся записаться заново.")

    numbers = [str(number) for number in range(1, len(closures) + 1)]
    rows = chunk(numbers, MAX_BUTTONS_IN_ROW)
    rows.append([TO_CABINET, BACK_TO_MENU])

    send(user_id, "\n".join(lines), build_keyboard(rows))


def do_open(user_id, closure):
    """Снять закрытие."""
    if schedule.remove_closure(closure["id"]):
        send(user_id, f"Открыла {schedule.pretty_date(closure['since'])}.")
    else:
        send(user_id, "Это закрытие уже снято.")
    show_closures(user_id)


# --- рабочий график -------------------------------------------------------

def work_schedule_text():
    """График одной строкой: «Пн–Сб, 10:00–20:00» — но честно, с пропусками."""
    days = schedule.work_weekdays()
    if not days:
        return "выходных нет только у роботов — рабочие дни не заданы"

    names = ", ".join(schedule.WEEKDAYS[day] for day in sorted(days))
    return f"{names}, {schedule.work_start()}–{schedule.work_end()}"


def show_work(user_id):
    """График работы: что менять."""
    user = get_user(user_id)
    user["state"] = OWNER_WORK

    send(
        user_id,
        f"График работы: {work_schedule_text()}.\n\n"
        "Он влияет только на новые записи. Уже записанных клиентов "
        "изменение графика не трогает — если время перестало подходить, "
        "запись нужно отменить или перенести.",
        build_keyboard([
            [WORKDAYS_BUTTON],
            [WORK_START_BUTTON],
            [WORK_END_BUTTON],
            [TO_CABINET, BACK_TO_MENU],
        ]),
    )


def show_workdays(user_id):
    """Переключатель дней недели: нажал — включил, нажал ещё раз — выключил.

    Ничего не запоминаем между сообщениями: текущий набор лежит в базе,
    и каждое нажатие пишет туда сразу. Поэтому экран одинаково работает
    и после перезапуска, и из старого сообщения.
    """
    user = get_user(user_id)
    user["state"] = OWNER_WORKDAYS

    days = schedule.work_weekdays()
    labels = [f"{'✅' if number in days else '❌'} {name}"
              for number, name in enumerate(schedule.WEEKDAYS)]

    rows = chunk(labels, COMFORT_BUTTONS_IN_ROW)
    rows.append([WORK_DONE])

    send(user_id,
         "Рабочие дни — нажмите, чтобы включить или выключить:\n\n"
         f"Сейчас: {work_schedule_text()}",
         build_keyboard(rows))


def toggle_workday(user_id, number):
    """Включить или выключить день недели."""
    days = set(schedule.work_weekdays())

    if number in days:
        if len(days) == 1:
            send(user_id, "Это последний рабочий день — "
                          "совсем без рабочих дней записаться будет некуда.")
            show_workdays(user_id)
            return
        days.remove(number)
    else:
        days.add(number)

    schedule.set_work_schedule(days=days)
    show_workdays(user_id)


# Из каких часов мастер выбирает границы рабочего дня. Не все подряд:
# при трёх кнопках в ряду сутки заняли бы восемь рядов, а VK покажет пять.
# Утро и вечер разведены — начало дня в 22:00 никому не нужно, а вот конец
# в 22:00 вполне бывает.
WORK_START_HOURS = range(7, 16)   # 07:00–15:00
WORK_END_HOURS = range(14, 24)    # 14:00–23:00


def ask_work_hour(user_id, which):
    """Выбрать начало или конец рабочего дня."""
    user = get_user(user_id)
    user["state"] = OWNER_WORK_START if which == "start" else OWNER_WORK_END

    hours = WORK_START_HOURS if which == "start" else WORK_END_HOURS
    rows = chunk([schedule.to_time(hour * 60) for hour in hours],
                 COMFORT_BUTTONS_IN_ROW)
    rows.append([TO_CABINET])

    what = "начинается" if which == "start" else "заканчивается"
    send(user_id, f"Во сколько {what} рабочий день?", build_keyboard(rows))


def set_work_hour(user_id, which, moment):
    """Сохранить границу рабочего дня, если она не переворачивает день."""
    start = moment if which == "start" else schedule.work_start()
    finish = moment if which == "end" else schedule.work_end()

    if schedule.to_minutes(start) >= schedule.to_minutes(finish):
        send(user_id, f"Не получится: рабочий день с {start} до {finish} "
                      "выходит пустым или наоборот.")
        show_work(user_id)
        return

    if which == "start":
        schedule.set_work_schedule(start=moment)
    else:
        schedule.set_work_schedule(finish=moment)

    send(user_id, f"Готово: {work_schedule_text()}.")
    show_work(user_id)


# --- разбор нажатий в кабинете -------------------------------------------

def find_day(msg, days):
    """День, чья подпись совпала с нажатой кнопкой, или None.

    Списки дней всюду пересобираются на лету, поэтому кнопка из старого
    сообщения просто не найдётся — и мастер увидит свежий список вместо
    закрытия неизвестно чего.
    """
    for day in days:
        if schedule.day_label(day).lower() == msg:
            return day
    return None


def handle_owner(user_id, msg, text):
    """Все шаги кабинета. Что это владелец, вызывающий уже проверил.

    msg — приведённое к нижнему регистру сообщение, им сравниваются кнопки.
    text — то, что мастер написал на самом деле: причина отмены уходит
    клиенту как есть, и превращать её в «заболела, простите» нельзя.
    """
    user = get_user(user_id)
    state = user["state"]

    # Выход в кабинет работает на любом его экране — включая те, где ждут
    # свободный текст: иначе из ввода причины было бы не выбраться.
    if msg == TO_CABINET.lower():
        show_cabinet(user_id)
        return

    if state == OWNER_CABINET:
        if msg == SCHEDULE_BUTTON.lower():
            show_schedule_days(user_id)
        elif msg == CLOSE_BUTTON.lower():
            ask_close_kind(user_id)
        elif msg == CLOSURES_BUTTON.lower():
            show_closures(user_id)
        elif msg == WORK_BUTTON.lower():
            show_work(user_id)
        else:
            send(user_id, "Не поняла. Выберите кнопку ниже:")
            show_cabinet(user_id)
        return

    # --- расписание ---
    if state == OWNER_SCHEDULE:
        if msg == OTHER_SCHEDULE_DAY.lower():
            show_schedule_days(user_id)
            return

        day = user.get("schedule_day")
        if day and msg.isdigit():
            # Список читаем заново: пока мастер смотрел, запись могли отменить.
            bookings = schedule.day_bookings(day)
            if 1 <= int(msg) <= len(bookings):
                ask_cancel_reason(user_id, bookings[int(msg) - 1])
                return

        chosen = find_day(msg, [day for day, _ in schedule.days_with_bookings()])
        if chosen:
            show_schedule_day(user_id, chosen)
            return

        send(user_id, "Не поняла. Выберите день или номер записи:")
        show_schedule_days(user_id)
        return

    if state == OWNER_CANCEL_REASON:
        reason = clean_reason(text)
        if not reason:
            send(user_id, "Напишите причину текстом — её увидит клиент.")
            return
        do_master_cancel(user_id, reason)
        return

    # --- закрытие времени ---
    if state == OWNER_CLOSE_KIND:
        if msg == CLOSE_PAUSE.lower():
            user["close_kind"] = CLOSE_PAUSE
            user["close_since"] = date.today().isoformat()
            user["close_until"] = (date.today()
                                   + timedelta(days=PAUSE_DAYS)).isoformat()
            ask_close_reason(user_id)
        elif msg in (CLOSE_WHOLE_DAY.lower(), CLOSE_HOURS.lower(),
                     CLOSE_PERIOD.lower()):
            user["close_kind"] = {
                CLOSE_WHOLE_DAY.lower(): CLOSE_WHOLE_DAY,
                CLOSE_HOURS.lower(): CLOSE_HOURS,
                CLOSE_PERIOD.lower(): CLOSE_PERIOD,
            }[msg]
            ask_close_since(user_id)
        else:
            ask_close_kind(user_id)
        return

    # Дальше идут шаги, которым нужен собранный черновик. Его нет — значит
    # бота перезапустили на середине: возвращаем мастера в кабинет.
    if state in (OWNER_CLOSE_SINCE, OWNER_CLOSE_UNTIL, OWNER_CLOSE_FROM,
                 OWNER_CLOSE_TO, OWNER_CLOSE_REASON, OWNER_CLOSE_CONFIRM):
        if user.get("close_kind") is None:
            show_cabinet(user_id)
            return

    if state == OWNER_CLOSE_SINCE:
        day = find_day(msg, schedule.upcoming_work_days(CLOSE_DAYS_TO_SHOW))
        if day is None:
            ask_close_since(user_id)
            return

        user["close_since"] = day
        if user["close_kind"] == CLOSE_PERIOD:
            ask_close_until(user_id)
        elif user["close_kind"] == CLOSE_HOURS:
            user["close_until"] = day  # отлучка всегда внутри одного дня
            ask_close_from(user_id)
        else:
            user["close_until"] = day
            ask_close_reason(user_id)
        return

    if state == OWNER_CLOSE_UNTIL:
        later = [day for day in schedule.upcoming_work_days(CLOSE_DAYS_TO_SHOW * 2)
                 if day > user["close_since"]][:CLOSE_DAYS_TO_SHOW]
        day = find_day(msg, later)
        if day is None:
            ask_close_until(user_id)
            return
        user["close_until"] = day
        ask_close_reason(user_id)
        return

    if state == OWNER_CLOSE_FROM:
        if msg not in schedule.work_hours():
            ask_close_from(user_id)
            return
        user["close_from"] = msg
        ask_close_to(user_id)
        return

    if state == OWNER_CLOSE_TO:
        if msg not in schedule.work_hours(first=user["close_from"]):
            ask_close_to(user_id)
            return
        user["close_to"] = msg
        ask_close_reason(user_id)
        return

    if state == OWNER_CLOSE_REASON:
        reason = clean_reason(text)
        if not reason:
            send(user_id, "Напишите причину текстом — её увидят клиенты.")
            return
        user["close_reason"] = reason
        ask_close_confirm(user_id)
        return

    if state == OWNER_CLOSE_CONFIRM:
        if msg == CLOSE_YES.lower():
            do_close(user_id)
        elif msg == CLOSE_NO.lower():
            send(user_id, "Ничего не закрыла.")
            show_cabinet(user_id)
        else:
            ask_close_confirm(user_id)
        return

    if state == OWNER_CLOSURES:
        closures = schedule.all_closures()
        if msg.isdigit() and 1 <= int(msg) <= len(closures):
            do_open(user_id, closures[int(msg) - 1])
            return
        send(user_id, "Не поняла. Нажмите номер закрытия:")
        show_closures(user_id)
        return

    # --- рабочий график ---
    if state == OWNER_WORK:
        if msg == WORKDAYS_BUTTON.lower():
            show_workdays(user_id)
        elif msg == WORK_START_BUTTON.lower():
            ask_work_hour(user_id, "start")
        elif msg == WORK_END_BUTTON.lower():
            ask_work_hour(user_id, "end")
        else:
            show_work(user_id)
        return

    if state == OWNER_WORKDAYS:
        if msg == WORK_DONE.lower():
            show_work(user_id)
            return
        # Подпись кнопки — «✅ Пн», поэтому ищем по названию дня, а не целиком:
        # значок в ней меняется от нажатия к нажатию.
        for number, name in enumerate(schedule.WEEKDAYS):
            if msg.endswith(name.lower()):
                toggle_workday(user_id, number)
                return
        show_workdays(user_id)
        return

    if state in (OWNER_WORK_START, OWNER_WORK_END):
        which = "start" if state == OWNER_WORK_START else "end"
        if msg.count(":") == 1 and msg.replace(":", "").isdigit():
            set_work_hour(user_id, which, msg)
            return
        ask_work_hour(user_id, which)
        return

    # Шаг кабинета, которого мы не знаем, — то же, что неизвестный шаг
    # у клиента: честно возвращаем в начало, а не молчим.
    print(f"неизвестный шаг кабинета {state!r} — возвращаю в кабинет")
    show_cabinet(user_id)


# =========================================================================
# 10. Фоновый планировщик: напоминание и автоотмена
# =========================================================================
# Всё остальное бот делает в ответ на сообщение. Напоминание за сутки и
# автоотмена за 12 часов — единственное, что происходит по часам, а не по
# нажатию кнопки: значит, нужен кто-то, кто просто смотрит на время. Это
# отдельный поток, который раз в SCHEDULER_TICK_SECONDS перебирает записи.
#
# Что «напоминание уже отправлено», помнит не поток, а статус записи в базе:
# поток можно перезапускать сколько угодно, второй раз клиента не потревожат.
#
# Этот же поток раз в сутки убирает из базы старое — больше некому: всё
# остальное происходит в ответ на сообщение клиента.

# Когда мы напомнили — в этом запуске бота. Нужно только для того, чтобы не
# отменить запись сразу же после напоминания: клиенту надо дать время ответить.
reminded_at = {}

# Момент старта. От него отсчитываем то же время ожидания для записей,
# которым напомнили ДО перезапуска: времени напоминания в базе нет, а
# отменять их в первую же минуту после подъёма бота нечестно.
STARTED_AT = datetime.now()

# День последней уборки. None — в этом запуске ещё не убирались.
last_cleanup = None


def send_reminder(booking):
    """Напомнить о записи и попросить подтверждение."""
    # Статус меняем ПЕРЕД отправкой. Если VK откажется принять сообщение
    # (клиент закрыл личку), напоминание пропадёт — зато мы не будем дёргать
    # его каждую минуту до самой процедуры.
    if schedule.mark_reminded(booking["id"]) is None:
        return  # клиент успел отменить или подтвердить запись сам

    reminded_at[str(booking["id"])] = datetime.now()

    text = (
        f"Напоминаю о записи!\n\n"
        f"Процедура: {config.SERVICES[booking['service']]['title']}\n"
        f"{schedule.pretty_date(booking['date'])}, {booking['start']}–"
        f"{schedule.end_time(booking)}\n"
        f"Стоимость: {booking['price_from']}–{booking['price_to']} ₽\n\n"
        "Подтвердите, пожалуйста, что придёте, "
        "а если не получится — отмените запись."
    )

    if config.AUTOCANCEL_BEFORE_HOURS is not None:
        text += (
            f"\nБез подтверждения запись снимется за "
            f"{config.AUTOCANCEL_BEFORE_HOURS} "
            f"{plural(config.AUTOCANCEL_BEFORE_HOURS, 'час', 'часа', 'часов')} "
            "до начала — время нужно отдать другим."
        )

    try:
        send(client_of(booking), text, REMINDER_KEYBOARD)
    except messenger.MessengerError as error:
        print(f"не смогла напомнить {booking['user_id']}: {error}")


def send_day_reminder(booking):
    """Напомнить в день процедуры: «сегодня, ждём вас».

    Подтверждение к этому моменту уже получено вчера, поэтому и разговор
    другой: не «придёте?», а адрес и время. Отсюда и клавиатура без
    «Подтверждаю» — подтверждать нечего, а вот сорваться в последний момент
    человек может, и кнопка отмены должна быть под рукой.

    Отметку ставим ДО отправки, как и в напоминании за сутки: если VK
    откажется принять сообщение, напоминание пропадёт — зато мы не будем
    пробовать снова каждую минуту до самой процедуры.
    """
    if not schedule.mark_day_reminded(booking["id"]):
        return  # кто-то успел раньше

    text = (
        f"Сегодня ждём вас в {booking['start']}!\n\n"
        f"Процедура: {config.SERVICES[booking['service']]['title']}\n"
        f"Продолжительность: {format_duration(booking['minutes'])}\n"
        f"Стоимость: {booking['price_from']}–{booking['price_to']} ₽\n\n"
        f"{contacts()}\n\n"
        "Если планы изменились — отмените запись, "
        "я сразу предложу это время другим."
    )

    try:
        send(client_of(booking), text, DAY_REMINDER_KEYBOARD)
    except messenger.MessengerError as error:
        print(f"не смогла напомнить {booking['user_id']}: {error}")


def ready_to_expire(booking):
    """Клиент успел увидеть напоминание и ответить?

    Время напоминания живёт в памяти процесса, поэтому после перезапуска мы
    его не знаем — тогда отсчитываем от старта бота. Клиент получит лишний
    час, и это правильнее, чем отменить запись человеку, чьё напоминание
    могло не дойти.
    """
    since = reminded_at.get(str(booking["id"]), STARTED_AT)
    return datetime.now() - since >= timedelta(
        minutes=config.CONFIRM_REACT_MINUTES
    )


def do_expire(booking):
    """Снять неподтверждённую запись и предложить время подписчикам."""
    expired = schedule.expire_booking(booking["id"])
    if expired is None:
        return  # подтвердили или отменили, пока мы собирались

    reminded_at.pop(str(booking["id"]), None)

    try:
        send(
            client_of(expired),
            f"Запись на {schedule.pretty_date(expired['date'])} "
            f"в {expired['start']} снята: подтверждения не было.\n\n"
            "Если планы не изменились, запишитесь заново — "
            "время пока свободно.",
            menu_keyboard(client_of(expired)),
        )
    except messenger.MessengerError as error:
        print(f"не смогла написать {expired['user_id']}: {error}")

    # Дальше как при обычной отмене: время освободилось, и его кто-то ждёт.
    #
    # Про позднюю отмену тут не пишем: автоотмена всегда поздняя (за 12 часов
    # до процедуры), и приписка была бы в каждом таком сообщении.
    notify_owner("Запись снята: клиент не подтвердил\n\n"
                 f"{booking_card(expired)}\n\n"
                 f"{client_card(client_of(expired))}")
    notify_subscribers(expired)


def cleanup_once_a_day():
    """Убрать из базы то, что уже никому не нужно, — не чаще раза в сутки.

    Планировщик тикает каждую минуту, а уборка — дело редкое, поэтому сверяем
    дату, а не считаем часы. После перезапуска бот уберётся в этот день ещё
    раз, и это ничего не стоит: удалять будет нечего.

    Что именно удаляется и почему — в db.cleanup().
    """
    global last_cleanup

    today = date.today()
    if last_cleanup == today:
        return

    removed = db.cleanup()
    last_cleanup = today

    if removed:
        print(f"Уборка базы: удалено строк — {removed}")


def scheduler_tick():
    """Один проход по расписанию: кому напомнить, у кого снять запись."""
    cleanup_once_a_day()

    for booking in schedule.due_reminders():
        send_reminder(booking)

    for booking in schedule.due_expired():
        if ready_to_expire(booking):
            do_expire(booking)

    # Напоминание в день записи — последним. Порядок важен: запись, которую
    # только что сняли за неподтверждение, до сюда уже не доедет (её статус
    # больше не активен), и клиент не получит «сегодня ждём вас» через
    # секунду после «запись снята».
    for booking in schedule.due_day_reminders():
        send_day_reminder(booking)


def run_scheduler():
    """Вечный цикл фонового потока.

    try/except внутри цикла обязателен: без него любая единичная ошибка
    убила бы поток совсем — бот продолжал бы отвечать на сообщения, и то,
    что напоминания больше не приходят, заметили бы через неделю.
    """
    while True:
        # Сначала пауза, потом работа: на старте бот занят подключением
        # к longpoll, и лезть в базу в ту же секунду незачем.
        time.sleep(config.SCHEDULER_TICK_SECONDS)

        try:
            scheduler_tick()
        except Exception as error:
            print(f"Ошибка планировщика: {error}")


# =========================================================================
# 11. Главный обработчик сообщений
# =========================================================================

def handle_message(user_id, text):
    user = get_user(user_id)
    msg = text.strip().lower()

    # --- команды, которые работают на любом шаге ---
    # Кнопки в переписке живут вечно: клиент может пролистать чат вверх и
    # нажать кнопку из вчерашнего сообщения, а бот с тех пор ушёл в другое
    # состояние. Плюс фоновые напоминания приходят когда им пора, посреди
    # любого шага. Поэтому выходы в разделы работают откуда угодно.
    if msg in ("привет", "начать", "старт", "меню", BACK_TO_MENU.lower()):
        show_menu(user_id, greeting=msg in ("привет", "начать", "старт"))
        return

    if msg == CONFIRM_COMING.lower():
        do_confirm(user_id)
        return

    # Ровно == , а не startswith: иначе «Да, отменить запись» на экране
    # подтверждения отмены попало бы сюда и увело клиента по кругу.
    if msg == CANCEL_BOOKING.lower():
        start_cancel(user_id)
        return

    # «Записаться» — по той же причине, что и выходы в разделы: кнопка есть
    # в каждом старом сообщении с меню, и нажать её могут откуда угодно.
    # Что делать дальше, решает book_button() — это зависит от шага клиента.
    if msg == "записаться":
        book_button(user_id)
        return

    # startswith, а не ==: в подписи кнопки едет счётчик «(3/5)»,
    # и сравнение целиком перестало бы совпадать.
    if msg.startswith(MY_BOOKINGS_BUTTON.lower()):
        show_my_bookings(user_id)
        return

    if msg.startswith(MY_SUBS_BUTTON.lower()):
        show_my_subs(user_id)
        return

    state = user["state"]

    # --- главное меню ---
    if state == MAIN_MENU:
        # «Записаться» сюда не попадает: эта кнопка работает из любого
        # состояния и разобрана выше.
        if msg == "узнать стоимость":
            ask_service(user_id)
        elif msg == CABINET_BUTTON.lower() and is_owner(user_id):
            # Единственная дверь в кабинет. Здесь же и проверка владельца:
            # слово «кабинет», написанное клиентом, ничего не открывает.
            show_cabinet(user_id)
        else:
            # Любое непонятное сообщение здесь — это «здравствуйте».
            #
            # Живой человек начинает не с «начать», а с «Здравствуйте, можно
            # записаться на кератин?». Раньше первым, что он слышал от бота,
            # было «Не понял» — худшее из возможных первых впечатлений, да
            # ещё и неправдой: бот прекрасно знает, что делать дальше.
            #
            # Повтор приветствия тому, кто пишет мимо кнопок второй раз,
            # не мешает, а помогает: раз он не понял, что нажимать, объяснить
            # ещё раз полезнее, чем упрекнуть.
            show_menu(user_id, greeting=True)
        return

    # --- выбор процедуры ---
    if state == SELECTING_SERVICE:
        key = find_key(config.SERVICES, msg)
        if key is None:
            send(user_id, "Выберите процедуру кнопкой ниже:", SERVICE_KEYBOARD)
        else:
            user["service"] = key
            ask_length(user_id)
        return

    # --- выбор длины ---
    if state == SELECTING_LENGTH:
        key = find_key(config.LENGTHS, msg)
        if key is None:
            # Подсказки повторяем: клиент, который ответил мимо кнопок, как раз
            # и есть тот, кто не понял вопроса.
            send(user_id,
                 "Выберите длину волос кнопкой ниже:\n\n"
                 + hints_text(config.LENGTHS),
                 LENGTH_KEYBOARD)
        else:
            user["length"] = key
            ask_density(user_id)
        return

    # --- выбор густоты ---
    if state == SELECTING_DENSITY:
        key = find_key(config.DENSITIES, msg)
        if key is None:
            send(user_id,
                 "Выберите густоту волос кнопкой ниже:\n\n"
                 + hints_text(config.DENSITIES),
                 DENSITY_KEYBOARD)
        else:
            user["density"] = key
            show_price(user_id)  # все параметры собраны — считаем
        return

    # --- цена показана ---
    if state == PRICE_CALCULATED:
        # «Записаться» разобрана выше: отсюда она ведёт сразу к выбору дня,
        # потому что процедура, длина и густота уже известны.
        if msg == "посчитать ещё раз":
            ask_service(user_id)
        else:
            send(user_id, "Выбери кнопку ниже:", RESULT_KEYBOARD)
        return

    # --- выбор дня ---
    if state == SELECTING_DATE:
        if msg == NO_DAY.lower():
            ask_sub_date(user_id)
            return

        # Ищем день, чья подпись совпала с нажатой кнопкой.
        # Список берём тот же самый, так что «протухшие» кнопки из старого
        # сообщения просто не найдутся — и клиент увидит свежие дни.
        for day in schedule.work_days(user["minutes"],
                                      user.get("move_id")):
            if schedule.day_label(day).lower() == msg:
                ask_time(user_id, day)
                return
        send(user_id, "Не понял день. Выберите кнопкой:")
        ask_date(user_id)
        return

    # --- выбор времени ---
    if state == SELECTING_TIME:
        if msg == OTHER_DAY.lower():
            ask_date(user_id)
            return

        # День клиента устраивает, а время в нём — нет. Спрашивать день
        # заново не нужно, он уже выбран: сразу к подтверждению подписки.
        if msg == NO_TIME.lower():
            ask_sub_confirm(user_id, user["day"])
            return

        # Листание. Второй вариант в скобках — если клиент напишет руками
        # «далее» без стрелочки. Проверять выход за границы не нужно:
        # ask_time() сама поправит слишком большой или отрицательный номер.
        page = user.get("page", 0)
        if msg in (PAGE_NEXT.lower(), "далее"):
            ask_time(user_id, user["day"], page + 1)
            return
        if msg in (PAGE_PREV.lower(), "назад"):
            ask_time(user_id, user["day"], page - 1)
            return

        if msg in schedule.free_slots(user["day"], user["minutes"],
                                      exclude_id=user.get("move_id")):
            show_confirmation(user_id, msg)
            return
        send(user_id, "Это время недоступно. Выберите из свободных:")
        ask_time(user_id, user["day"], page)  # остаёмся на той же странице
        return

    # --- подтверждение записи ---
    if state == CONFIRMING:
        if msg == "подтвердить запись":
            save_booking(user_id)
        elif msg == OTHER_TIME.lower():
            ask_time(user_id, user["day"])
        else:
            send(user_id, "Выбери кнопку ниже:", CONFIRM_KEYBOARD)
        return

    # --- выбор дня для подписки ---
    if state == SELECTING_SUB_DATE:
        # Список пересобираем на лету: пока клиент думал, день мог
        # освободиться и уйти из «занятых». Тогда кнопка не найдётся,
        # и мы честно покажем свежий список.
        for day in schedule.busy_days(user["minutes"],
                                      user.get("move_id")):
            if schedule.day_label(day).lower() == msg:
                ask_sub_confirm(user_id, day)
                return
        send(user_id, "Не понял день. Выберите кнопкой:")
        ask_sub_date(user_id)
        return

    # --- подтверждение подписки ---
    if state == SUB_CONFIRM:
        if msg == SUBSCRIBE.lower():
            do_subscribe(user_id)
        elif msg == OTHER_DAY.lower():
            ask_sub_date(user_id)
        else:
            # Клавиатуру не пересобираем: ask_sub_confirm() сама решит,
            # какую показать — обычную или «вы уже подписаны».
            send(user_id, "Выбери кнопку ниже:")
            ask_sub_confirm(user_id, user["sub_day"])
        return

    # --- список записей ---
    if state == MY_BOOKINGS:
        # Список читаем заново: клиент мог отменить запись с другого
        # устройства, да и старое сообщение с кнопками никуда не девается.
        bookings = schedule.user_bookings(user_id.platform, user_id.id)

        # Цифры проверяем до преобразования, иначе «привет» уронит int().
        if msg.isdigit() and 1 <= int(msg) <= len(bookings):
            # Номера у клиента с 1, а в списке с 0 — отсюда «- 1».
            ask_cancel_confirm(user_id, bookings[int(msg) - 1])
            return

        send(user_id, "Не понял. Нажмите номер записи или «В меню»:")
        show_my_bookings(user_id)
        return

    # --- список подписок ---
    if state == MY_SUBS:
        # Читаем заново по той же причине, что и записи: подписка могла
        # сняться сама, если клиент за это время записался на тот день.
        subscriptions = schedule.user_subscriptions(user_id.platform, user_id.id)

        if msg.isdigit() and 1 <= int(msg) <= len(subscriptions):
            do_unsubscribe(user_id, subscriptions[int(msg) - 1])
            return

        send(user_id, "Не понял. Нажмите номер подписки или «В меню»:")
        show_my_subs(user_id)
        return

    # --- подтверждение отмены ---
    if state == CANCEL_CONFIRM:
        if msg == MOVE_BOOKING.lower():
            start_move(user_id)
        elif msg == CANCEL_YES.lower():
            do_cancel(user_id)
        elif msg == CANCEL_NO.lower():
            show_my_bookings(user_id)
        else:
            send(user_id, "Выбери кнопку ниже:", CANCEL_KEYBOARD)
        return

    # --- кабинет владельца ---
    if state in OWNER_STATES:
        # Проверяем владельца и здесь, а не только при показе кнопки: шаг
        # лежит в базе и переживает перезапуск, а OWNER_ID в настройках между
        # запусками мог поменяться. Чужого на этих шагах уводим в меню.
        if is_owner(user_id):
            handle_owner(user_id, msg, text)
        else:
            show_menu(user_id)
        return

    # --- шаг, которого мы не знаем ---
    # Сейчас разобраны все состояния, так что сюда попасть неоткуда. Но шаг
    # клиента теперь лежит в базе и переживает обновление кода: стоит
    # переименовать состояние — и у тех, кто был на нём, бот начнёт молча
    # проглатывать сообщения. Молчащий бот — худшее, что можно предложить,
    # поэтому непонятный шаг честно сбрасываем в меню.
    print(f"неизвестное состояние {state!r} у {user_id} — возвращаю в меню")
    show_menu(user_id)


# =========================================================================
# 12. Точка входа: бесконечное прослушивание сообщений
# =========================================================================

# База открывается здесь, в главном потоке, до всего остального. Не ради
# скорости: connect() сверяет версию схемы и на чужой базе останавливает бота
# понятным текстом. Сделай это первым фоновый поток — SystemExit убил бы
# только его, а бот продолжил бы работать без напоминаний.
db.connect()

# Запуск бота — только когда файл выполняют напрямую (`python main.py`).
# Тесты импортируют main, чтобы дёргать его функции; заводить им при этом
# фоновый поток и вечное прослушивание незачем — с одним каналом ВК заглушка
# отдавала пустой longpoll и цикл сам заканчивался, а с двумя каналами
# listen() ждёт очередь и висел бы. Поэтому запуск прячем за __main__, а
# db.connect() выше оставляем на импорте: на нём тесты создают схему в своей базе.
if __name__ == "__main__":
    # daemon=True — поток не мешает боту завершиться: по Ctrl+C процесс закроется,
    # не дожидаясь, пока планировщик доспит свою минуту.
    threading.Thread(target=run_scheduler, daemon=True).start()

    # Время печатаем сразу: если часовой пояс на хостинге не применился, это видно
    # в первой же строке логов, а не через сутки по жалобе клиента.
    print(f"Бот запущен {datetime.now():%d.%m.%Y %H:%M} "
          f"({config.TIMEZONE}), база {db.DB_FILE}. Ctrl+C — остановить.")

    for message in bot.listen():
        print(f"[{message.client.platform}:{message.client.id}] {message.text}")

        # Замок на обработку и сохранение вместе: пока идёт разговор, фоновый
        # поток не полезет в состояние этого же клиента со своим уведомлением.
        with DIALOG_LOCK:
            # try/except: если на одном сообщении что-то сломалось — пишем
            # в консоль и слушаем дальше. Бот не должен падать целиком
            # из-за одного клиента.
            try:
                handle_message(message.client, message.text)
            except messenger.MessengerError as error:
                # hint заполнен, когда у ошибки есть понятная человеку причина
                # (например, у сообщества выключены «Возможности ботов»);
                # иначе показываем общий текст.
                print(error.hint or f"Мессенджер вернул ошибку: {error}")
            except Exception as error:
                print(f"Ошибка при обработке сообщения: {error}")

            # Здесь, а не внутри handle_message(): у того около двадцати
            # выходов, и сохранение пришлось бы дописывать в каждый. И именно
            # после try/except — если обработка сломалась на середине,
            # состояние уже могло измениться, и записать его надо всё равно.
            try:
                save_user(message.client)
            except Exception as error:
                print(f"Не смогла сохранить состояние диалога: {error}")
