"""Расписание мастера: свободные окошки и хранение записей в schedule.txt.

Главная идея файла: время внутри модуля считаем в МИНУТАХ ОТ НАЧАЛА СУТОК.
    "10:00" -> 600
    "15:30" -> 930
С обычными числами всё просто: сложить длительность, сравнить, вычесть.
Наружу (в сообщения клиенту) отдаём привычные строки "HH:MM".

Дата хранится строкой "2026-08-03" — год-месяц-день. Такой формат удобен тем,
что строки в нём сортируются как даты, и его же понимает datetime.
"""

import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import config

# Файл трогают два потока: диалог с клиентами и фоновый планировщик.
# Любая правка — это «прочитать всё, изменить, записать всё», и если два
# потока сделают это одновременно, тот, кто записал последним, затрёт чужое
# изменение. Поэтому такие места целиком идут под этим замком.
#
# Замок берут только функции, которые МЕНЯЮТ файл. Чтению он не нужен:
# save_all() подменяет файл целиком и одним движением (см. ниже), так что
# читатель всегда видит либо старое состояние, либо новое, но не половину.
FILE_LOCK = threading.Lock()

# Файл лежит рядом с кодом, а не в рабочем каталоге запуска.
SCHEDULE_FILE = Path(__file__).parent / "schedule.txt"

# Порядок колонок в файле: одна запись — одна строка, поля разделены «|».
# Символ выбран такой, потому что его точно не будет внутри самих значений.
FIELDS = [
    "id",          # порядковый номер записи, пригодится для отмены
    "user_id",     # VK ID клиента
    "date",        # 2026-08-03
    "start",       # 14:00 — начало процедуры
    "minutes",     # длительность самой процедуры, без уборки
    "service",     # ключ услуги: keratin / botox / cold
    "length",      # ключ длины волос
    "density",     # ключ густоты
    "price_from",
    "price_to",
    "status",
]

# Первая строка файла — подсказка для человека, который откроет schedule.txt.
# При чтении мы её пропускаем, потому что она начинается с «#».
HEADER = "# " + "|".join(FIELDS)

# Жизнь записи по статусам:
#   NEW       — клиент записался, подтверждения ещё не спрашивали;
#   REMINDED  — бот напомнил и ждёт подтверждения;
#   CONFIRMED — клиент подтвердил, что придёт;
#   CANCELLED — отменил сам;
#   EXPIRED   — отменилась сама, подтверждения так и не было.
#
# Первые три занимают время в расписании, последние два — нет: окошко снова
# свободно. Отмену клиента и автоотмену различаем, чтобы мастер по файлу
# видел, что произошло: «передумал» и «не выходит на связь» — разные истории.
ACTIVE_STATUSES = ("NEW", "REMINDED", "CONFIRMED")

# Статусы, из которых запись ещё можно подтвердить.
UNCONFIRMED_STATUSES = ("NEW", "REMINDED")

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# =========================================================================
# 1. Время: строка <-> минуты
# =========================================================================

def to_minutes(text):
    """"14:30" -> 870 (минут от полуночи)."""
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def to_time(minutes):
    """870 -> "14:30", с ведущим нулём: 540 -> "09:00"."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def day_label(day):
    """"2026-08-03" -> "Пн 03.08" — короткая подпись для кнопки."""
    value = datetime.strptime(day, "%Y-%m-%d").date()
    return f"{WEEKDAYS[value.weekday()]} {value.strftime('%d.%m')}"


def pretty_date(day):
    """"2026-08-03" -> "03.08.2026" — привычный вид для сообщения."""
    return datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")


def booking_datetime(booking):
    """Дату и время записи склеиваем в один datetime.

    Так запись можно сравнить с datetime.now() («уже прошла?»)
    и по этому же значению отсортировать список.
    """
    return datetime.strptime(
        f"{booking['date']} {booking['start']}", "%Y-%m-%d %H:%M"
    )


def end_time(booking):
    """Время окончания процедуры (без уборки): "10:00" + 300 -> "15:00"."""
    return to_time(to_minutes(booking["start"]) + booking["minutes"])


# =========================================================================
# 2. Файл: чтение и запись
# =========================================================================

def read_all():
    """Прочитать все записи из schedule.txt в список словарей."""
    if not SCHEDULE_FILE.exists():
        return []  # файла ещё нет — значит, записей тоже нет

    bookings = []
    for line in SCHEDULE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue  # пустая строка или комментарий

        values = line.split("|")
        if len(values) != len(FIELDS):
            # Файл кто-то поправил руками и сломал. Не падаем из-за одной
            # строки: жалуемся в консоль и работаем с остальными.
            print(f"schedule.txt: пропускаю непонятную строку: {line}")
            continue

        # Порядок значений в строке совпадает с FIELDS — отсюда и словарь.
        booking = dict(zip(FIELDS, values))

        # Из файла всё приходит строками — числа переводим обратно в int.
        booking["user_id"] = int(booking["user_id"])
        booking["minutes"] = int(booking["minutes"])
        bookings.append(booking)

    return bookings


def write_lines(path, lines):
    """Записать файл целиком — так, чтобы читатель не увидел его недописанным.

    Обычная запись сначала обнуляет файл, а потом наполняет его заново, и в
    этот момент кто-то другой (у нас — фоновый планировщик) может прочитать
    пустоту или половину строк. Поэтому пишем в файл-времянку рядом и одним
    движением ставим её на место готового: replace() либо срабатывает целиком,
    либо не срабатывает вовсе. Бонусом файл не пострадает, если бот упадёт
    посреди записи.
    """
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(path)


def save_all(bookings):
    """Перезаписать файл целиком списком записей."""
    lines = [HEADER]
    for booking in bookings:
        lines.append("|".join(str(booking[field]) for field in FIELDS))
    write_lines(SCHEDULE_FILE, lines)


def next_id(bookings):
    """Следующий свободный номер записи."""
    if not bookings:
        return 1
    return max(int(booking["id"]) for booking in bookings) + 1


# =========================================================================
# 3. Занятость времени
# =========================================================================

def busy_intervals(bookings, day):
    """Занятые отрезки конкретного дня — в минутах, уже с уборкой на конце.

    Запись на 10:00 длительностью 5 часов при уборке 30 минут
    превращается в отрезок (600, 930), то есть 10:00–15:30.
    """
    intervals = []
    for booking in bookings:
        if booking["date"] != day:
            continue
        if booking["status"] not in ACTIVE_STATUSES:
            continue
        start = to_minutes(booking["start"])
        end = start + booking["minutes"] + config.CLEANUP_MINUTES
        intervals.append((start, end))
    return intervals


def is_free(bookings, day, start, minutes):
    """Можно ли начать процедуру в это время?

    start и minutes — числа (минуты). Проверяем три вещи:
    рабочие часы, пересечение с чужими записями, конец рабочего дня.
    """
    if start < to_minutes(config.WORK_START):
        return False

    # Сама процедура должна закончиться до конца рабочего дня.
    # Уборка после последнего клиента в рабочие часы уже не обязана влезть.
    if start + minutes > to_minutes(config.WORK_END):
        return False

    end = start + minutes + config.CLEANUP_MINUTES

    for busy_start, busy_end in busy_intervals(bookings, day):
        # Два отрезка НЕ пересекаются только если один целиком раньше другого.
        # Всё остальное — наложение, значит время занято.
        if end <= busy_start or start >= busy_end:
            continue
        return False

    return True


def earliest_start(day):
    """С какого времени вообще можно начинать в этот день.

    Для будущих дней — с начала рабочего дня. Для сегодня — не раньше,
    чем через MIN_LEAD_MINUTES от текущего момента: клиенту нужно доехать.
    """
    work_start = to_minutes(config.WORK_START)

    now = datetime.now()
    if day != now.strftime("%Y-%m-%d"):
        return work_start

    limit = now.hour * 60 + now.minute + config.MIN_LEAD_MINUTES

    # Округляем вверх до шага сетки: 13:10 при шаге 30 -> 13:30.
    remainder = limit % config.SLOT_STEP
    if remainder:
        limit += config.SLOT_STEP - remainder

    return max(work_start, limit)


def free_slots(day, minutes, bookings=None):
    """Список свободных начал процедуры в этот день: ["10:00", "10:30", ...].

    bookings можно передать снаружи, чтобы не перечитывать файл
    на каждый день, когда мы проверяем сразу две недели вперёд.
    """
    if bookings is None:
        bookings = read_all()

    slots = []
    start = earliest_start(day)
    last_start = to_minutes(config.WORK_END) - minutes

    while start <= last_start:
        if is_free(bookings, day, start, minutes):
            slots.append(to_time(start))
        start += config.SLOT_STEP

    return slots


def work_days(minutes):
    """Ближайшие рабочие дни, где есть хотя бы одно свободное окошко.

    Возвращает список строк-дат. Дни без окон в список не попадают —
    нет смысла показывать кнопку, за которой пусто.
    """
    bookings = read_all()  # читаем файл один раз на весь перебор
    days = []
    today = date.today()

    for shift in range(config.DAYS_AHEAD):
        day = today + timedelta(days=shift)

        if day.weekday() not in config.WORK_DAYS:
            continue  # выходной мастера

        key = day.strftime("%Y-%m-%d")
        if not free_slots(key, minutes, bookings):
            continue  # день целиком занят

        days.append(key)
        if len(days) == config.DAYS_TO_SHOW:
            break

    return days


def busy_days(minutes):
    """Ближайшие рабочие дни, где окошка под эту процедуру НЕТ.

    Обратная сторона work_days(): там дни, куда можно записаться, здесь —
    те, на которые остаётся только подписаться и ждать чужой отмены.

    Сегодня в список не попадает (range начинается с 1): даже если кто-то
    отменится через час, запас MIN_LEAD_MINUTES до конца дня уже не влезет.
    """
    bookings = read_all()
    days = []
    today = date.today()

    for shift in range(1, config.DAYS_AHEAD):
        day = today + timedelta(days=shift)

        if day.weekday() not in config.WORK_DAYS:
            continue  # выходной мастера

        key = day.strftime("%Y-%m-%d")
        if free_slots(key, minutes, bookings):
            continue  # окошки есть — это день для записи, а не для подписки

        days.append(key)
        if len(days) == config.DAYS_TO_SHOW:
            break

    return days


# =========================================================================
# 4. Создание записи
# =========================================================================

def create_booking(user_id, day, start, minutes, service, length, density,
                   price_from, price_to):
    """Проверить, что время свободно, и дописать запись в файл.

    Возвращает саму запись — или None, если окошко уже заняли.

    Проверка делается здесь, в момент создания, а не когда клиент только
    увидел кнопку: пока он думал, время мог занять кто-то другой.
    """
    with FILE_LOCK:
        bookings = read_all()  # свежее состояние файла, а не минутной давности

        if not is_free(bookings, day, to_minutes(start), minutes):
            return None

        booking = {
            "id": next_id(bookings),
            "user_id": user_id,
            "date": day,
            "start": start,
            "minutes": minutes,
            "service": service,
            "length": length,
            "density": density,
            "price_from": price_from,
            "price_to": price_to,
            "status": "NEW",
        }

        # Записался в последний момент — подтверждать нечего: клиент только
        # что сам выбрал это время. Спрашивать «придёте?» сразу после
        # «готово, записала» глупо, да и напоминать уже не за сутки.
        if minutes_left(booking) <= config.CONFIRM_BEFORE_HOURS * 60:
            booking["status"] = "CONFIRMED"

        bookings.append(booking)
        save_all(bookings)
        return booking


# =========================================================================
# 5. Записи клиента и отмена
# =========================================================================

def user_bookings(user_id, only_future=True):
    """Активные записи одного клиента, по возрастанию даты.

    only_future=True — показываем только то, что ещё не началось:
    прошлую процедуру отменять уже поздно.
    """
    bookings = []
    for booking in read_all():
        if booking["user_id"] != user_id:
            continue
        if booking["status"] not in ACTIVE_STATUSES:
            continue
        if only_future and booking_datetime(booking) <= datetime.now():
            continue
        bookings.append(booking)

    # Порядок в файле произвольный, клиенту нужен по времени начала.
    bookings.sort(key=booking_datetime)
    return bookings


def active_count(user_id):
    """Сколько активных записей у клиента сейчас — это и есть его счётчик.

    Считать отдельно ничего не надо: user_bookings() уже отбрасывает
    отменённые и прошедшие записи. Прошла процедура — счётчик сам уменьшился.
    """
    return len(user_bookings(user_id))


def limit_reached(user_id):
    """Клиент уже набрал максимум активных записей?"""
    return active_count(user_id) >= config.MAX_ACTIVE_BOOKINGS


def cancel_booking(booking_id, user_id):
    """Отменить запись клиента. Возвращает запись или None, если не нашли.

    Строку из файла НЕ удаляем, а меняем статус на CANCELLED. Так у мастера
    остаётся история отмен, а расписание про эту запись сразу забывает:
    busy_intervals() берёт только ACTIVE_STATUSES, значит время снова свободно.

    user_id тут не для красоты: он гарантирует, что клиент отменяет
    свою запись, а не чужую.
    """
    with FILE_LOCK:
        bookings = read_all()

        for booking in bookings:
            # id из файла приходит строкой, поэтому сравниваем как строки
            if str(booking["id"]) != str(booking_id):
                continue
            if booking["user_id"] != user_id:
                continue
            if booking["status"] not in ACTIVE_STATUSES:
                return None  # уже отменена — второй раз отменять нечего

            booking["status"] = "CANCELLED"
            save_all(bookings)
            return booking

        return None


# =========================================================================
# 6. Подписки на свободные окошки
# =========================================================================
# Клиент выбрал день, а времени в нём нет — он подписывается и ждёт, пока
# кто-нибудь отменится. Храним рядом с расписанием и в том же формате:
# одна подписка — одна строка, поля через «|».
#
# Параметры процедуры лежат в самой подписке, а не в памяти бота. Иначе
# после перезапуска мы бы знали, кого уведомить, но не знали бы, какое
# окошко ему подходит и по какой цене он считал.

SUBSCRIBERS_FILE = Path(__file__).parent / "subscribers.txt"

SUB_FIELDS = [
    "user_id",     # VK ID клиента
    "date",        # день, окошка в котором он ждёт
    "minutes",     # длительность процедуры: окно короче не подойдёт
    "service",
    "length",
    "density",
    "price_from",  # цена на момент подписки — по ней клиент и запишется
    "price_to",
]

SUB_HEADER = "# " + "|".join(SUB_FIELDS)


def read_subscriptions():
    """Все живые подписки. Вчерашние молча отбрасываем.

    Подписка живёт до конца своего дня, поэтому отдельного статуса ей не
    нужно: день прошёл — строка перестала что-либо значить. Из файла она
    исчезнет при ближайшей записи, save_subscriptions() пишет только живые.
    """
    if not SUBSCRIBERS_FILE.exists():
        return []

    today = date.today().strftime("%Y-%m-%d")
    subscriptions = []

    for line in SUBSCRIBERS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        values = line.split("|")
        if len(values) != len(SUB_FIELDS):
            print(f"subscribers.txt: пропускаю непонятную строку: {line}")
            continue

        subscription = dict(zip(SUB_FIELDS, values))
        subscription["user_id"] = int(subscription["user_id"])
        subscription["minutes"] = int(subscription["minutes"])

        # Даты в формате «2026-08-03» сравниваются как обычные строки.
        if subscription["date"] < today:
            continue

        subscriptions.append(subscription)

    return subscriptions


def save_subscriptions(subscriptions):
    """Перезаписать файл подписок целиком."""
    lines = [SUB_HEADER]
    for subscription in subscriptions:
        lines.append("|".join(str(subscription[field]) for field in SUB_FIELDS))
    write_lines(SUBSCRIBERS_FILE, lines)


def user_subscriptions(user_id):
    """Подписки одного клиента, по возрастанию даты."""
    subscriptions = [s for s in read_subscriptions() if s["user_id"] == user_id]
    subscriptions.sort(key=lambda subscription: subscription["date"])
    return subscriptions


def day_subscribers(day):
    """Все, кто ждёт окошко в этот день.

    Порядок оставляем как в файле — то есть по времени подписки. Никакой
    очереди это не создаёт (окошко не бронируется), но при рассылке
    уведомление первым уходит тому, кто ждёт дольше всех.
    """
    return [s for s in read_subscriptions() if s["date"] == day]


def has_bookings(day):
    """Есть ли в этот день хотя бы одна активная запись?

    Нужно для подписки: ждать освободившееся окошко имеет смысл только
    когда есть чему освобождаться. В дне, где не записан никто, время
    и так открыто целиком — подписываться там не на что.
    """
    return bool(busy_intervals(read_all(), day))


def is_subscribed(user_id, day):
    """Клиент уже ждёт окошко в этот день?"""
    return any(s["date"] == day for s in user_subscriptions(user_id))


def subscriptions_limit_reached(user_id):
    """Клиент уже набрал максимум подписок?"""
    return len(user_subscriptions(user_id)) >= config.MAX_SUBSCRIPTIONS


def add_subscription(user_id, day, minutes, service, length, density,
                     price_from, price_to):
    """Подписать клиента на день. Возвращает подписку или None.

    None — если он уже подписан на этот день или упёрся в лимит. Проверки
    здесь же, а не только в диалоге: между показом кнопки и нажатием клиент
    мог подписаться с другого устройства.
    """
    with FILE_LOCK:
        subscriptions = read_subscriptions()

        mine = [s for s in subscriptions if s["user_id"] == user_id]
        if any(s["date"] == day for s in mine):
            return None
        if len(mine) >= config.MAX_SUBSCRIPTIONS:
            return None

        subscription = {
            "user_id": user_id,
            "date": day,
            "minutes": minutes,
            "service": service,
            "length": length,
            "density": density,
            "price_from": price_from,
            "price_to": price_to,
        }

        subscriptions.append(subscription)
        save_subscriptions(subscriptions)
        return subscription


def remove_subscription(user_id, day):
    """Снять подписку клиента на день. True — если было что снимать.

    Вызывается и при отписке вручную, и когда клиент записался на этот
    день: ждать окошко, когда уже записан, незачем.
    """
    with FILE_LOCK:
        subscriptions = read_subscriptions()
        left = [s for s in subscriptions
                if not (s["user_id"] == user_id and s["date"] == day)]

        if len(left) == len(subscriptions):
            return False

        save_subscriptions(left)
        return True


# =========================================================================
# 7. Подтверждение записи и автоотмена
# =========================================================================
# За сутки до процедуры бот спрашивает клиента, придёт ли он, а если ответа
# нет — за 12 часов освобождает время. Здесь только «кому пора» и смена
# статуса; сами сообщения и фоновый поток — в main.py, потому что это уже
# разговор с клиентом, а не расписание.

def minutes_left(booking):
    """Сколько минут осталось до начала процедуры.

    Для прошедших записей число получается отрицательным — по нему их
    и отличаем от будущих.
    """
    left = booking_datetime(booking) - datetime.now()
    return int(left.total_seconds() // 60)


def due_reminders():
    """Записи, которым пора напомнить о подтверждении.

    Только статус NEW: REMINDED означает, что напоминание уже ушло, и второй
    раз тревожить клиента незачем. Именно поэтому «уже напомнили» живёт в
    файле, а не в памяти потока — перезапуск бота не должен приводить
    к повторной рассылке.
    """
    limit = config.CONFIRM_BEFORE_HOURS * 60
    return [booking for booking in read_all()
            if booking["status"] == "NEW" and 0 < minutes_left(booking) <= limit]


def due_expired():
    """Записи, которые пора отменить: напоминание ушло, подтверждения нет.

    Записи, до которых время уже дошло (minutes_left <= 0), не трогаем:
    отменять процедуру, которая вот-вот начнётся или уже идёт, поздно —
    пусть мастер решает сам.
    """
    if config.AUTOCANCEL_BEFORE_HOURS is None:
        return []  # автоотмена выключена в настройках

    limit = config.AUTOCANCEL_BEFORE_HOURS * 60
    return [booking for booking in read_all()
            if booking["status"] == "REMINDED"
            and 0 < minutes_left(booking) <= limit]


def set_status(booking_id, new_status, allowed_from):
    """Сменить статус записи. Возвращает запись или None.

    None — если записи нет или её статус уже не тот, которого мы ждали:
    например, планировщик собрался отменить запись, а клиент за эту секунду
    успел её подтвердить. Владельца здесь не проверяем, в отличие от
    cancel_booking(): статусы меняет бот, а не клиент.
    """
    with FILE_LOCK:
        bookings = read_all()

        for booking in bookings:
            if str(booking["id"]) != str(booking_id):
                continue
            if booking["status"] not in allowed_from:
                return None

            booking["status"] = new_status
            save_all(bookings)
            return booking

        return None


def mark_reminded(booking_id):
    """Отметить, что напоминание отправлено."""
    return set_status(booking_id, "REMINDED", ("NEW",))


def expire_booking(booking_id):
    """Отменить запись, которую клиент не подтвердил."""
    return set_status(booking_id, "EXPIRED", ("REMINDED",))


def confirm_bookings(user_id):
    """Подтвердить все будущие записи клиента. Возвращает подтверждённые.

    Подтверждаем сразу все, а не какую-то одну: клиент нажимает «Подтверждаю»
    в ответ на напоминание, и разбираться, к какой записи относилась кнопка
    (а сообщение могло быть и вчерашним), не нужно — обычно запись одна,
    а если их несколько, подтвердить их все и есть то, чего клиент хочет.
    """
    with FILE_LOCK:
        bookings = read_all()
        confirmed = []

        for booking in bookings:
            if booking["user_id"] != user_id:
                continue
            if booking["status"] not in UNCONFIRMED_STATUSES:
                continue
            if minutes_left(booking) <= 0:
                continue  # прошедшую запись подтверждать нечего

            booking["status"] = "CONFIRMED"
            confirmed.append(booking)

        if confirmed:
            save_all(bookings)

        confirmed.sort(key=booking_datetime)
        return confirmed
