"""Расписание мастера: свободные окошки и записи клиентов.

Главная идея файла: время внутри модуля считаем в МИНУТАХ ОТ НАЧАЛА СУТОК.
    "10:00" -> 600
    "15:30" -> 930
С обычными числами всё просто: сложить длительность, сравнить, вычесть.
Наружу (в сообщения клиенту) отдаём привычные строки "HH:MM".

Дата хранится строкой "2026-08-03" — год-месяц-день. Такой формат удобен тем,
что строки в нём сортируются как даты, и его же понимает datetime.

Записи и подписки лежат в базе — весь SQL про них собран здесь, ниже только
db.py. Наружу это не выходит: main.py получает словари с теми же ключами и не
знает, откуда они пришли.
"""

from datetime import date, datetime, timedelta

import config
import db

# Жизнь записи по статусам:
#   NEW       — клиент записался, подтверждения ещё не спрашивали;
#   REMINDED  — бот напомнил и ждёт подтверждения;
#   CONFIRMED — клиент подтвердил, что придёт;
#   CANCELLED — отменил сам;
#   EXPIRED   — отменилась сама, подтверждения так и не было.
#
# Первые три занимают время в расписании, последние два — нет: окошко снова
# свободно. Отмену клиента и автоотмену различаем, чтобы в истории было видно,
# что произошло: «передумал» и «не выходит на связь» — разные истории.
#
# Набор статусов повторён в схеме базы (CHECK у колонки status): база не даст
# записать статус, которого здесь нет.
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


def moment_key(moment):
    """datetime -> "2026-08-03 14:00" — момент времени в том виде, как в базе.

    Дата и время лежат в отдельных колонках, но склеенные через пробел они
    сравниваются как настоящее время: "2026-08-03 09:00" меньше, чем
    "2026-08-03 14:00", а оно меньше, чем любое время 4 августа. Поэтому
    «будущие записи» в запросе — это date || ' ' || start > сейчас, и
    вытаскивать прошедшие из базы, чтобы тут же их отбросить, не нужно.
    """
    return moment.strftime("%Y-%m-%d %H:%M")


# =========================================================================
# 2. База: откуда берутся записи
# =========================================================================
# Все запросы к таблице bookings живут в этом файле и ниже. Про SQL знает
# только schedule.py, main.py по-прежнему получает словари.
#
# Имена колонок совпадают с ключами словарей, поэтому SELECT * — это уже
# готовая запись: перекладывать значения по одному не нужно.

def placeholders(values):
    """"?, ?, ?" под IN (...) — по одному вопросу на значение.

    Сами значения уходят параметрами запроса, а не подставляются в текст:
    так их не нужно ни экранировать, ни бояться.
    """
    return ", ".join("?" * len(values))


def bookings_on(day):
    """Все записи одного дня — из них считается занятость времени.

    Отменённые тоже приходят: их отбрасывает busy_intervals(), которому всё
    равно, откуда пришёл список. Записей в одном дне единицы, отбирать их
    ещё и запросом смысла нет.
    """
    return db.query("SELECT * FROM bookings WHERE date = ?", (day,))


def bookings_ahead():
    """Записи на весь период, на который открыта запись.

    Перебор свободных дней (work_days, busy_days) спрашивает базу один раз,
    а не по разу на каждый день. История при этом не мешается: полгода
    прошедших записей остаются в базе, но в этот запрос не попадают.
    """
    today = date.today()
    last = today + timedelta(days=config.DAYS_AHEAD)
    return db.query("SELECT * FROM bookings WHERE date BETWEEN ? AND ?",
                    (today.isoformat(), last.isoformat()))


def get_booking(booking_id):
    """Запись по номеру или None, если такой нет."""
    return db.query_one("SELECT * FROM bookings WHERE id = ?", (booking_id,))


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

    bookings можно передать снаружи, чтобы не спрашивать базу на каждый день,
    когда мы проверяем сразу две недели вперёд.
    """
    if bookings is None:
        bookings = bookings_on(day)

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
    bookings = bookings_ahead()  # один запрос на весь перебор
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
    bookings = bookings_ahead()
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
    """Проверить, что время свободно, и добавить запись.

    Возвращает саму запись — или None, если окошко уже заняли.

    Проверка делается здесь, в момент создания, а не когда клиент только
    увидел кнопку: пока он думал, время мог занять кто-то другой. Проверка и
    вставка идут одной транзакцией: между «время свободно» и «записала» не
    должен влезть второй клиент с тем же окошком.
    """
    booking = {
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

    # Записался в последний момент — подтверждать нечего: клиент только что
    # сам выбрал это время. Спрашивать «придёте?» сразу после «готово,
    # записала» глупо, да и напоминать уже не за сутки.
    if minutes_left(booking) <= config.CONFIRM_BEFORE_HOURS * 60:
        booking["status"] = "CONFIRMED"

    columns = ["user_id", "date", "start", "minutes", "service", "length",
               "density", "price_from", "price_to", "status"]

    with db.transaction():
        if not is_free(bookings_on(day), day, to_minutes(start), minutes):
            return None

        # Номер выдаёт база, поэтому он появляется в записи только сейчас.
        booking["id"] = db.insert(
            f"INSERT INTO bookings ({', '.join(columns)}) "
            f"VALUES ({placeholders(columns)})",
            [booking[column] for column in columns],
        )

    return booking


# =========================================================================
# 5. Записи клиента и отмена
# =========================================================================

def active_condition(only_future=True):
    """Условие «активная запись клиента» и значения к нему.

    Отбор одинаков у списка записей и у их подсчёта, а держать его в двух
    запросах — это два места, где можно разойтись. Возвращаем кусок SQL и
    параметры к нему, чтобы условие было написано один раз.
    """
    sql = f"user_id = ? AND status IN ({placeholders(ACTIVE_STATUSES)})"
    params = ACTIVE_STATUSES

    if only_future:
        # Прошлую процедуру отменять уже поздно, и в лимит она не идёт.
        sql += " AND date || ' ' || start > ?"
        params += (moment_key(datetime.now()),)

    return sql, params


def user_bookings(user_id, only_future=True):
    """Активные записи одного клиента, по возрастанию даты."""
    condition, params = active_condition(only_future)
    return db.query(
        f"SELECT * FROM bookings WHERE {condition} ORDER BY date, start",
        (user_id,) + params,
    )


def active_count(user_id):
    """Сколько активных записей у клиента сейчас — это и есть его счётчик.

    Считает база: вытаскивать записи, чтобы тут же их посчитать, незачем.
    Прошла процедура — счётчик уменьшился сам, отдельно её нигде не закрываем.
    """
    condition, params = active_condition()
    row = db.query_one(f"SELECT count(*) AS n FROM bookings WHERE {condition}",
                       (user_id,) + params)
    return row["n"]


def limit_reached(user_id):
    """Клиент уже набрал максимум активных записей?"""
    return active_count(user_id) >= config.MAX_ACTIVE_BOOKINGS


def cancel_booking(booking_id, user_id):
    """Отменить запись клиента. Возвращает запись или None, если не нашли.

    Строку НЕ удаляем, а меняем статус на CANCELLED. Так у мастера остаётся
    история отмен, а расписание про эту запись сразу забывает: busy_intervals()
    берёт только ACTIVE_STATUSES, значит время снова свободно.

    user_id тут не для красоты: он гарантирует, что клиент отменяет свою
    запись, а не чужую. Все условия — в самом UPDATE, поэтому чужая запись,
    уже отменённая или несуществующая дают один и тот же ответ: ноль
    изменённых строк, то есть None. Читать базу дважды для этого не нужно.
    """
    changed = db.execute(
        f"UPDATE bookings SET status = 'CANCELLED' "
        f"WHERE id = ? AND user_id = ? "
        f"AND status IN ({placeholders(ACTIVE_STATUSES)})",
        (booking_id, user_id) + ACTIVE_STATUSES,
    )

    if not changed:
        return None

    return get_booking(booking_id)


# =========================================================================
# 6. Подписки на свободные окошки
# =========================================================================
# Клиент выбрал день, а времени в нём нет — он подписывается и ждёт, пока
# кто-нибудь отменится.
#
# Параметры процедуры лежат в самой подписке, а не в памяти бота. Иначе
# после перезапуска мы бы знали, кого уведомить, но не знали бы, какое
# окошко ему подходит и по какой цене он считал.
#
# Пара «клиент + день» — это первичный ключ таблицы. Дважды подписаться на один
# день нельзя не потому, что мы это проверили, а потому, что база не даст.

# Прошедшие подписки в выдачу не попадают («date >= сегодня»), но и не удаляются
# на месте: строка просто перестала что-либо значить, а убирает её db.cleanup().
# Статус подписке поэтому не нужен.

def user_subscriptions(user_id):
    """Подписки одного клиента, по возрастанию даты."""
    return db.query(
        "SELECT * FROM subscriptions WHERE user_id = ? AND date >= ? "
        "ORDER BY date",
        (user_id, date.today().isoformat()),
    )


def subscriptions_count(user_id):
    """Сколько дней клиент сейчас ждёт."""
    row = db.query_one(
        "SELECT count(*) AS n FROM subscriptions "
        "WHERE user_id = ? AND date >= ?",
        (user_id, date.today().isoformat()),
    )
    return row["n"]


def day_subscribers(day):
    """Все, кто ждёт окошко в этот день.

    Порядок не задаём: уведомление уходит всем подходящим сразу, окошко ни за
    кем не бронируется — кто первый запишется, того и место.
    """
    return db.query("SELECT * FROM subscriptions WHERE date = ?", (day,))


def has_bookings(day):
    """Есть ли в этот день хотя бы одна активная запись?

    Нужно для подписки: ждать освободившееся окошко имеет смысл только
    когда есть чему освобождаться. В дне, где не записан никто, время
    и так открыто целиком — подписываться там не на что.
    """
    row = db.query_one(
        f"SELECT count(*) AS n FROM bookings WHERE date = ? "
        f"AND status IN ({placeholders(ACTIVE_STATUSES)})",
        (day,) + ACTIVE_STATUSES,
    )
    return row["n"] > 0


def is_subscribed(user_id, day):
    """Клиент уже ждёт окошко в этот день?"""
    return db.query_one(
        "SELECT 1 FROM subscriptions WHERE user_id = ? AND date = ?",
        (user_id, day),
    ) is not None


def subscriptions_limit_reached(user_id):
    """Клиент уже набрал максимум подписок?"""
    return subscriptions_count(user_id) >= config.MAX_SUBSCRIPTIONS


def add_subscription(user_id, day, minutes, service, length, density,
                     price_from, price_to):
    """Подписать клиента на день. Возвращает подписку или None.

    None — если он уже подписан на этот день или упёрся в лимит. Проверки
    здесь же, а не только в диалоге: между показом кнопки и нажатием клиент
    мог подписаться с другого устройства.

    Про «уже подписан» отвечает сама вставка: пара «клиент + день» — первичный
    ключ, и OR IGNORE превращает попытку добавить дубль в ноль изменённых
    строк. Лимит же приходится считать отдельно, поэтому подсчёт и вставка
    идут одной транзакцией — иначе двумя одновременными нажатиями можно было
    бы получить на одну подписку больше разрешённого.
    """
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

    columns = list(subscription)

    with db.transaction():
        if subscriptions_count(user_id) >= config.MAX_SUBSCRIPTIONS:
            return None

        added = db.execute(
            f"INSERT OR IGNORE INTO subscriptions ({', '.join(columns)}) "
            f"VALUES ({placeholders(columns)})",
            [subscription[column] for column in columns],
        )

    return subscription if added else None


def remove_subscription(user_id, day):
    """Снять подписку клиента на день. True — если было что снимать.

    Вызывается и при отписке вручную, и когда клиент записался на этот
    день: ждать окошко, когда уже записан, незачем.

    Здесь, в отличие от записей, строка именно удаляется: история «кто чего
    ждал» никому не нужна, а сам факт подписки ничего не бронировал.
    """
    return db.execute(
        "DELETE FROM subscriptions WHERE user_id = ? AND date = ?",
        (user_id, day),
    ) > 0


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


def due_within(status, hours):
    """Записи в статусе status, до которых осталось не больше hours.

    Уже начавшиеся не берём: и напоминать, и отменять процедуру, которая
    вот-вот начнётся или уже идёт, поздно — пусть мастер решает сам.
    Отсюда «строго больше сейчас» в первом условии.
    """
    now = datetime.now()
    limit = now + timedelta(hours=hours)

    return db.query(
        "SELECT * FROM bookings WHERE status = ? "
        "AND date || ' ' || start > ? AND date || ' ' || start <= ? "
        "ORDER BY date, start",
        (status, moment_key(now), moment_key(limit)),
    )


def due_reminders():
    """Записи, которым пора напомнить о подтверждении.

    Только статус NEW: REMINDED означает, что напоминание уже ушло, и второй
    раз тревожить клиента незачем. Именно поэтому «уже напомнили» живёт в
    базе, а не в памяти потока — перезапуск бота не должен приводить
    к повторной рассылке.
    """
    return due_within("NEW", config.CONFIRM_BEFORE_HOURS)


def due_expired():
    """Записи, которые пора отменить: напоминание ушло, подтверждения нет."""
    if config.AUTOCANCEL_BEFORE_HOURS is None:
        return []  # автоотмена выключена в настройках

    return due_within("REMINDED", config.AUTOCANCEL_BEFORE_HOURS)


def set_status(booking_id, new_status, allowed_from):
    """Сменить статус записи. Возвращает запись или None.

    None — если записи нет или её статус уже не тот, которого мы ждали:
    например, планировщик собрался отменить запись, а клиент за эту секунду
    успел её подтвердить. Это и есть смысл условия по статусу внутри UPDATE:
    проверка и запись происходят одним неделимым действием, поэтому «успел» и
    «не успел» не могут случиться оба сразу.

    Владельца здесь не проверяем, в отличие от cancel_booking(): статусы
    меняет бот, а не клиент.
    """
    changed = db.execute(
        f"UPDATE bookings SET status = ? "
        f"WHERE id = ? AND status IN ({placeholders(allowed_from)})",
        (new_status, booking_id) + tuple(allowed_from),
    )

    if not changed:
        return None

    return get_booking(booking_id)


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

    Сначала выбираем, что подтверждать, потом подтверждаем — и то и другое
    внутри одной транзакции: вернуть клиенту список записей нужно, а между
    выбором и правкой планировщик не должен успеть отменить одну из них.
    """
    now = moment_key(datetime.now())

    with db.transaction():
        confirmed = db.query(
            f"SELECT * FROM bookings WHERE user_id = ? "
            f"AND status IN ({placeholders(UNCONFIRMED_STATUSES)}) "
            f"AND date || ' ' || start > ? "  # прошедшую подтверждать нечего
            f"ORDER BY date, start",
            (user_id,) + UNCONFIRMED_STATUSES + (now,),
        )

        if confirmed:
            ids = [booking["id"] for booking in confirmed]
            db.execute(
                f"UPDATE bookings SET status = 'CONFIRMED' "
                f"WHERE id IN ({placeholders(ids)})",
                ids,
            )

    # В базе статус уже новый — поправим и в словарях, которые отдаём наружу.
    for booking in confirmed:
        booking["status"] = "CONFIRMED"

    return confirmed
