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
настройки — в config.py.
"""

import os
import threading
import time
from datetime import date, datetime, timedelta

import vk_api
from vk_api.exceptions import ApiError
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor

import config
import db       # состояние диалога: чтобы шаг клиента не терялся при перезапуске
import schedule  # расписание: свободные окошки, записи, подписки


# =========================================================================
# 1. Часовой пояс и подключение к VK
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

vk_session = vk_api.VkApi(token=config.VK_TOKEN)
api = vk_session.get_api()
longpoll = VkLongPoll(vk_session)


def send(user_id, text, keyboard=None):
    """Отправить сообщение пользователю (при желании — с клавиатурой)."""
    params = {
        "user_id": user_id,
        "message": text,
        "random_id": 0,  # 0 = VK сам не проверяет дубли, для учебного бота ок
    }
    if keyboard is not None:
        params["keyboard"] = keyboard
    api.messages.send(**params)


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


def get_user(user_id):
    """Вернуть данные пользователя, при первой встрече подняв их из базы."""
    if user_id not in users:
        saved = db.load_dialog(user_id)
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
        db.forget_dialog(user_id)
    else:
        db.save_dialog(user_id, user)


# =========================================================================
# 3. Клавиатуры
# =========================================================================

BACK_TO_MENU = "В меню"


# VK показывает у обычной клавиатуры не больше 5 рядов: сама библиотека
# разрешает 10, но лишние ряды клиент просто не рисует, и кнопки из них
# становятся недоступны. Поэтому 5 — наш настоящий лимит.
MAX_KEYBOARD_ROWS = 5

# В одном ряду не больше 5 кнопок — это уже ограничение библиотеки.
MAX_BUTTONS_IN_ROW = 5

# А на телефоне комфортно влезают только 3 кнопки с текстом вроде «10:00»:
# экран узкий, подписи на пятерке кнопок обрезаются. Для коротких подписей
# (номера записей) можно и больше, для всего остального — вот этот предел.
COMFORT_BUTTONS_IN_ROW = 3


def build_keyboard(rows):
    """Собрать клавиатуру из списка рядов кнопок.

    rows — список списков: [["Кератин"], ["Ботокс"], ["В меню"]]
    Каждый вложенный список — один ряд кнопок.
    """
    # Не падаем, но громко жалуемся в консоль: молча потерянная кнопка —
    # это часы поисков «почему у клиента нет выхода в меню».
    if len(rows) > MAX_KEYBOARD_ROWS:
        print(f"ВНИМАНИЕ: клавиатура из {len(rows)} рядов, "
              f"VK покажет только первые {MAX_KEYBOARD_ROWS}: {rows}")

    keyboard = VkKeyboard(one_time=False)
    for i, row in enumerate(rows):
        if i > 0:
            keyboard.add_line()  # перенос на новый ряд, но не перед первым
        for label in row:
            keyboard.add_button(label, VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()


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


# Подпись кнопки записей меняется у каждого клиента: «Мои записи (3/5)».
# Поэтому это не готовая клавиатура, а функция: собираем её заново
# при каждой отправке, иначе все увидят чужой счётчик.
MY_BOOKINGS_BUTTON = "Мои записи"


MY_SUBS_BUTTON = "Мои подписки"


def my_bookings_button(user_id):
    """«Мои записи» или «Мои записи (3/5)», если записи есть."""
    count = schedule.active_count(user_id)
    if count == 0:
        return MY_BOOKINGS_BUTTON
    return f"{MY_BOOKINGS_BUTTON} ({count}/{config.MAX_ACTIVE_BOOKINGS})"


def my_subs_button(user_id):
    """«Мои подписки (2/3)» — счётчик такой же, как у записей."""
    count = len(schedule.user_subscriptions(user_id))
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
    if schedule.active_count(user_id):
        sections.append(my_bookings_button(user_id))
    if schedule.user_subscriptions(user_id):
        sections.append(my_subs_button(user_id))
    if sections:
        rows.append(sections)

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

CANCEL_KEYBOARD = build_keyboard([
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

def show_menu(user_id, greeting=False):
    user = get_user(user_id)
    user["state"] = MAIN_MENU

    if greeting:
        text = (
            "Привет! Я помогу подобрать процедуру, рассчитать примерную "
            "стоимость и записаться к мастеру.\n\n"
            "Выбери, что нужно:"
        )
    else:
        text = "Главное меню. Что дальше?"

    send(user_id, text, menu_keyboard(user_id))


def ask_service(user_id):
    user = get_user(user_id)
    user["state"] = SELECTING_SERVICE
    send(user_id, "Выберите процедуру:", SERVICE_KEYBOARD)


def ask_length(user_id):
    user = get_user(user_id)
    user["state"] = SELECTING_LENGTH
    send(user_id, "Выберите длину волос:", LENGTH_KEYBOARD)


def ask_density(user_id):
    user = get_user(user_id)
    user["state"] = SELECTING_DENSITY
    send(user_id, "Выберите густоту волос:", DENSITY_KEYBOARD)


def show_price(user_id):
    user = get_user(user_id)
    user["state"] = PRICE_CALCULATED

    price_from, price_to, minutes = calculate(
        user["service"], user["length"], user["density"]
    )

    # Запоминаем результат: он понадобится дальше, чтобы подобрать окошки
    # нужной длины и записать цену в schedule.txt.
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
    """Нажали «Записаться» — сначала проверяем лимит, потом спрашиваем услугу.

    Проверка стоит в самом начале: заставлять клиента выбрать процедуру,
    длину и густоту, чтобы в конце сказать «нельзя», — издевательство.
    """
    if schedule.limit_reached(user_id):
        show_limit_message(user_id)
        return

    ask_service(user_id)


def ask_date(user_id):
    """Показать дни, в которых есть окно под процедуру этого клиента."""
    user = get_user(user_id)

    # Лимит проверяем и здесь: сюда можно прийти не только из start_booking(),
    # но и из калькулятора — «Узнать стоимость» лимитом не ограничен.
    if schedule.limit_reached(user_id):
        show_limit_message(user_id)
        return

    days = schedule.work_days(user["minutes"])

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
    slots = schedule.free_slots(day, user["minutes"])

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

    text = (
        "Проверьте запись:\n\n"
        f"Процедура: {config.SERVICES[user['service']]['title']}\n"
        f"Дата: {schedule.pretty_date(user['day'])}\n"
        f"Время начала: {start}\n"
        f"Продолжительность: {format_duration(user['minutes'])}\n"
        f"Ориентировочная стоимость: {user['price_from']}–{user['price_to']} ₽"
    )
    send(user_id, text, CONFIRM_KEYBOARD)


def save_booking(user_id):
    """Сохранить запись — если время свободно и лимит не исчерпан."""
    user = get_user(user_id)

    # Ещё одна проверка лимита, прямо перед сохранением. Та, в ask_date(),
    # была для удобства клиента, а эта — настоящая: она читает файл заново,
    # поэтому видит записи, сделанные пока клиент выбирал время.
    if schedule.limit_reached(user_id):
        show_limit_message(user_id)
        return

    booking = schedule.create_booking(
        user_id=user_id,
        day=user["day"],
        start=user["time"],
        minutes=user["minutes"],
        service=user["service"],
        length=user["length"],
        density=user["density"],
        price_from=user["price_from"],
        price_to=user["price_to"],
    )

    if booking is None:
        # Между показом кнопки и нажатием окошко заняли — обычное дело,
        # когда клиентов много. Само уведомление время не бронирует.
        send(user_id, "К сожалению, это окошко уже заняли 😔")
        ask_time(user_id, user["day"])
        return

    # Клиент дождался своего дня и записался — ждать в нём больше нечего.
    schedule.remove_subscription(user_id, booking["date"])

    user["state"] = MAIN_MENU
    send(
        user_id,
        f"Готово! Записала вас на {schedule.pretty_date(booking['date'])} "
        f"в {booking['start']}.\n\n"
        f"Процедура: {config.SERVICES[booking['service']]['title']}\n"
        f"Продолжительность: {format_duration(booking['minutes'])}\n"
        f"Стоимость: {booking['price_from']}–{booking['price_to']} ₽",
        menu_keyboard(user_id),  # счётчик записей уже увеличился
    )


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
    days = schedule.busy_days(user["minutes"])

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

    if schedule.is_subscribed(user_id, day):
        send(
            user_id,
            f"Вы уже подписаны на {schedule.pretty_date(day)} — "
            "напишу, как только там появится окошко.",
            ALREADY_SUBSCRIBED_KEYBOARD,
        )
        return

    if schedule.subscriptions_limit_reached(user_id):
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
        user_id=user_id,
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
        "Место при этом не бронируется: кто первый записался, того и время.",
        menu_keyboard(user_id),
    )


def show_my_subs(user_id):
    """Показать подписки клиента и кнопки отписки.

    Устроено как «Мои записи»: кнопки — номера из списка, они короткие
    и влезают в один ряд. Разница одна — подтверждения нет: подписка
    ничего не занимает, случайно снять её не страшно, а вернуть легко.
    """
    user = get_user(user_id)
    subscriptions = schedule.user_subscriptions(user_id)

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

    if schedule.remove_subscription(user_id, day):
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
    user = get_user(subscriber_id)

    for field in ("service", "length", "density", "minutes",
                  "price_from", "price_to"):
        user[field] = subscription[field]

    send(
        subscriber_id,
        f"Освободилось окошко {schedule.pretty_date(subscription['date'])} 🎉\n\n"
        f"Процедура: {config.SERVICES[subscription['service']]['title']}\n"
        f"Нужно времени: {format_duration(subscription['minutes'])}\n"
        f"Стоимость: {subscription['price_from']}–{subscription['price_to']} ₽\n\n"
        "Место не бронируется, поэтому записывайтесь, пока время свободно.",
    )

    # Дальше клиент идёт обычным путём записи: время -> подтверждение.
    # Отдельного сценария для «записи из уведомления» не нужно, и подписка
    # снимется сама — save_booking() это уже делает.
    ask_time(subscriber_id, subscription["date"])

    # Единственное место, где состояние диалога меняет фоновый поток, а не
    # ответ на сообщение. Без этой строки уведомление переживало бы перезапуск,
    # а шаг, на который оно клиента поставило, — нет.
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

    for subscription in schedule.day_subscribers(day):
        subscriber_id = subscription["user_id"]

        if subscriber_id == cancelled["user_id"]:
            continue  # сам отменил — сам и освободил, сообщать нечего

        # Молчим, если записаться всё равно нельзя: клиент набрал максимум
        # записей, или освободившееся время короче его процедуры (free_slots
        # сама учтёт и уборку, и конец рабочего дня). Уведомление, из которого
        # нельзя записаться, — просто спам.
        if schedule.limit_reached(subscriber_id):
            continue
        if not schedule.free_slots(day, subscription["minutes"]):
            continue

        try:
            offer_free_slot(subscriber_id, subscription)
        except ApiError as error:
            # Клиент закрыл сообщения от сообщества или удалил диалог.
            # Остальные подписчики тут не при чём — рассылку продолжаем.
            print(f"не смогла написать {subscriber_id}: {error}")


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
    bookings = schedule.user_bookings(user_id)

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
    """Спросить подтверждение отмены — чтобы не отменить случайным нажатием."""
    user = get_user(user_id)
    user["cancel_id"] = booking["id"]  # запоминаем, ЧТО именно отменяем
    user["state"] = CANCEL_CONFIRM

    send(
        user_id,
        "Отменить запись?\n\n"
        f"{config.SERVICES[booking['service']]['title']}\n"
        f"{schedule.pretty_date(booking['date'])}, {booking['start']}",
        CANCEL_KEYBOARD,
    )


def do_cancel(user_id):
    """Отменить запись и показать, что осталось."""
    user = get_user(user_id)
    booking = schedule.cancel_booking(user["cancel_id"], user_id)

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
    notify_subscribers(booking)


def do_confirm(user_id):
    """Клиент нажал «Подтверждаю»."""
    confirmed = schedule.confirm_bookings(user_id)

    if not confirmed:
        # Нажали кнопку из старого сообщения: всё уже подтверждено,
        # или ту запись успели отменить.
        send(user_id, "Подтверждать нечего — все ваши записи уже в силе.",
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
    bookings = schedule.user_bookings(user_id)

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
        send(booking["user_id"], text, REMINDER_KEYBOARD)
    except ApiError as error:
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
            expired["user_id"],
            f"Запись на {schedule.pretty_date(expired['date'])} "
            f"в {expired['start']} снята: подтверждения не было.\n\n"
            "Если планы не изменились, запишитесь заново — "
            "время пока свободно.",
            menu_keyboard(expired["user_id"]),
        )
    except ApiError as error:
        print(f"не смогла написать {expired['user_id']}: {error}")

    # Дальше как при обычной отмене: время освободилось, и его кто-то ждёт.
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
        # «Записаться» ведёт туда же, куда и «Узнать стоимость»: без услуги,
        # длины и густоты мы не знаем, сколько времени занять в расписании.
        # Разница одна — лимит: посчитать цену можно всегда, записаться нет.
        if msg == "узнать стоимость":
            ask_service(user_id)
        elif msg == "записаться":
            start_booking(user_id)
        else:
            send(user_id, "Не понял. Выбери кнопку ниже:",
                 menu_keyboard(user_id))
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
            send(user_id, "Выберите длину волос кнопкой ниже:", LENGTH_KEYBOARD)
        else:
            user["length"] = key
            ask_density(user_id)
        return

    # --- выбор густоты ---
    if state == SELECTING_DENSITY:
        key = find_key(config.DENSITIES, msg)
        if key is None:
            send(user_id, "Выберите густоту волос кнопкой ниже:", DENSITY_KEYBOARD)
        else:
            user["density"] = key
            show_price(user_id)  # все параметры собраны — считаем
        return

    # --- цена показана ---
    if state == PRICE_CALCULATED:
        if msg == "посчитать ещё раз":
            ask_service(user_id)
        elif msg == "записаться":
            ask_date(user_id)
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
        for day in schedule.work_days(user["minutes"]):
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

        if msg in schedule.free_slots(user["day"], user["minutes"]):
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
        for day in schedule.busy_days(user["minutes"]):
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
        bookings = schedule.user_bookings(user_id)

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
        subscriptions = schedule.user_subscriptions(user_id)

        if msg.isdigit() and 1 <= int(msg) <= len(subscriptions):
            do_unsubscribe(user_id, subscriptions[int(msg) - 1])
            return

        send(user_id, "Не понял. Нажмите номер подписки или «В меню»:")
        show_my_subs(user_id)
        return

    # --- подтверждение отмены ---
    if state == CANCEL_CONFIRM:
        if msg == CANCEL_YES.lower():
            do_cancel(user_id)
        elif msg == CANCEL_NO.lower():
            show_my_bookings(user_id)
        else:
            send(user_id, "Выбери кнопку ниже:", CANCEL_KEYBOARD)
        return


# =========================================================================
# 12. Точка входа: бесконечное прослушивание сообщений
# =========================================================================

# daemon=True — поток не мешает боту завершиться: по Ctrl+C процесс закроется,
# не дожидаясь, пока планировщик доспит свою минуту.
threading.Thread(target=run_scheduler, daemon=True).start()

# Время печатаем сразу: если часовой пояс на хостинге не применился, это видно
# в первой же строке логов, а не через сутки по жалобе клиента.
print(f"Бот запущен {datetime.now():%d.%m.%Y %H:%M} "
      f"({config.TIMEZONE}), база {db.DB_FILE}. Ctrl+C — остановить.")

for event in longpoll.listen():
    if event.type == VkEventType.MESSAGE_NEW and event.to_me:
        print(f"[{event.user_id}] {event.text}")

        # try/except: если на одном сообщении что-то сломалось — пишем в консоль
        # и слушаем дальше. Бот не должен падать целиком из-за одного клиента.
        try:
            handle_message(event.user_id, event.text)
        except ApiError as error:
            if error.code == 912:
                print(
                    "Ошибка 912: у сообщества выключены «Возможности ботов».\n"
                    "Управление сообществом -> Сообщения -> Настройки для бота "
                    "-> Возможности ботов: Включены."
                )
            else:
                print(f"VK вернул ошибку: {error}")
        except Exception as error:
            print(f"Ошибка при обработке сообщения: {error}")

        # Здесь, а не внутри handle_message(): у того около двадцати выходов,
        # и сохранение пришлось бы дописывать в каждый. И именно после
        # try/except — если обработка сломалась на середине, состояние уже
        # могло измениться, и записать его надо всё равно.
        try:
            save_user(event.user_id)
        except Exception as error:
            print(f"Не смогла сохранить состояние диалога: {error}")
