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
#   EXPIRED   — отменилась сама, подтверждения так и не было;
#   MOVED     — клиент перенёс её на другое время, вместо неё есть новая.
#
# Первые три занимают время в расписании, последние три — нет: окошко снова
# свободно. Отмену клиента, автоотмену и перенос различаем, чтобы в истории
# было видно, что произошло: «передумал», «не выходит на связь» и «перенёс» —
# разные истории, и мастеру они говорят разное.
#
# Набор статусов повторён в схеме базы (CHECK у колонки status): база не даст
# записать статус, которого здесь нет.
ACTIVE_STATUSES = ("NEW", "REMINDED", "CONFIRMED")

# Статус, из которого запись можно подтвердить: бот спросил «придёте?» и ждёт
# ответа. NEW сюда не входит намеренно — про такую запись клиента ещё
# не спрашивали, а подтверждённая запись напоминания больше не получает
# (due_reminders() берёт только NEW).
ASKED_STATUS = "REMINDED"

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
# 1а. Рабочий график мастера
# =========================================================================
# Дни недели и часы лежат в базе, а config.py задаёт значения по умолчанию:
# мастер меняет график из переписки, но свежая копия проекта работает и без
# единой строки в settings.
#
# Значения читаются из базы при каждом обращении, без запоминания в памяти.
# Обращений много — free_slots() спрашивает границы дня на каждом шаге сетки,
# то есть сотни раз за перебор двух недель, — но это сотни крошечных выборок
# из таблицы в три строки, вместе меньше миллисекунды. Зато нет ни устаревшей
# копии в памяти, ни вопроса, кто и когда её сбрасывает, ни разницы между
# потоком диалога и планировщиком.

def work_weekdays():
    """Рабочие дни недели: [0, 1, 2, 3, 4, 5], где 0 — понедельник."""
    saved = db.get_setting("work_days")
    if saved is None:
        return config.WORK_DAYS
    return [int(part) for part in saved.split(",") if part]


def work_start():
    """Начало рабочего дня, «10:00»."""
    return db.get_setting("work_start") or config.WORK_START


def work_end():
    """Конец рабочего дня: процедура должна успеть закончиться до него."""
    return db.get_setting("work_end") or config.WORK_END


def set_work_schedule(days=None, start=None, finish=None):
    """Сохранить график. Меняется только то, что передали."""
    if days is not None:
        db.set_setting("work_days", ",".join(str(day) for day in sorted(days)))
    if start is not None:
        db.set_setting("work_start", start)
    if finish is not None:
        db.set_setting("work_end", finish)


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


def days_with_bookings():
    """Ближайшие дни, где кто-то записан: [("2026-08-03", 2), ...].

    Для расписания мастера. Пустые дни в список не попадают — их и так видно
    по отсутствию: в дне, которого здесь нет, свободно всё рабочее время.

    Считаем в Python, а не запросом с GROUP BY: записи на две недели вперёд
    всё равно уже прочитаны одним bookings_ahead(), и второй поход в базу
    ради подсчёта строк, которых десятки, ничего не сэкономит.
    """
    days = {}
    for booking in bookings_ahead():
        if booking["status"] not in ACTIVE_STATUSES:
            continue
        days[booking["date"]] = days.get(booking["date"], 0) + 1

    return sorted(days.items())


def day_bookings(day):
    """Активные записи одного дня по возрастанию времени.

    Отличается от bookings_on(): та отдаёт всё подряд, включая отменённые,
    потому что её ответ идёт в расчёт занятости. Здесь же список читает
    человек, и отменённым записям в нём делать нечего.
    """
    return db.query(
        f"SELECT * FROM bookings WHERE date = ? "
        f"AND status IN ({placeholders(ACTIVE_STATUSES)}) "
        f"ORDER BY start",
        (day,) + ACTIVE_STATUSES,
    )


# =========================================================================
# 2а. Закрытое время: когда мастер не принимает
# =========================================================================
# Одна таблица на четыре случая — выходной, отлучка на пару часов, отпуск
# и пауза «пока не открою», — потому что для расписания это одно и то же:
# отрезок, в который записаться нельзя. Устройство строки — в db.py.
#
# Закрытие само записи не отменяет: их отменяет мастер и с причиной, которую
# получат клиенты. Здесь только «когда закрыто».

def closures_ahead():
    """Закрытия, задевающие ближайшие дни, на которые открыта запись.

    Как bookings_ahead(): один запрос на весь перебор двух недель. Прошлые
    закрытия не мешаются — они уже никого не касаются.
    """
    today = date.today()
    last = today + timedelta(days=config.DAYS_AHEAD)

    return db.query(
        "SELECT * FROM closures WHERE until >= ? AND since <= ? "
        "ORDER BY since, start",
        (today.isoformat(), last.isoformat()),
    )


def all_closures():
    """Все незакончившиеся закрытия — для экрана «что закрыто»."""
    return db.query(
        "SELECT * FROM closures WHERE until >= ? ORDER BY since, start",
        (date.today().isoformat(),),
    )


def get_closure(closure_id):
    """Закрытие по номеру или None."""
    return db.query_one("SELECT * FROM closures WHERE id = ?", (closure_id,))


def day_closures(day, closures=None):
    """Закрытия, накрывающие этот день."""
    if closures is None:
        closures = closures_ahead()
    return [closure for closure in closures
            if closure["since"] <= day <= closure["until"]]


def closed_intervals(day, closures=None):
    """Закрытые отрезки дня в минутах: [(720, 900)] — это 12:00–15:00.

    Закрытие без часов означает весь день: возвращаем сутки целиком, и дальше
    оно работает как любой другой занятый отрезок. Отдельной ветки «а если
    день закрыт» ни у кого не появляется.
    """
    intervals = []

    for closure in day_closures(day, closures):
        if closure["start"] is None or closure["finish"] is None:
            return [(0, 24 * 60)]
        intervals.append((to_minutes(closure["start"]),
                          to_minutes(closure["finish"])))

    return intervals


def closed_all_day(day, closures=None):
    """День закрыт целиком?"""
    return closed_intervals(day, closures) == [(0, 24 * 60)]


def closure_reason(day, closures=None):
    """Чем мастер объяснил закрытие этого дня. Пусто — если день открыт."""
    reasons = [closure["reason"] for closure in day_closures(day, closures)]
    return reasons[0] if reasons else ""


def add_closure(since, until, start, finish, reason):
    """Закрыть время. Возвращает добавленную строку.

    Пересечения с уже закрытым не ищем и не склеиваем: два закрытия на одно
    время дают тот же результат, а разбираться потом, какое из них чьё,
    мастеру было бы сложнее, чем снять лишнее.
    """
    closure = {
        "since": since,
        "until": until,
        "start": start,
        "finish": finish,
        "reason": reason,
    }
    columns = list(closure)

    closure["id"] = db.insert(
        f"INSERT INTO closures ({', '.join(columns)}) "
        f"VALUES ({placeholders(columns)})",
        [closure[column] for column in columns],
    )
    return closure


def remove_closure(closure_id):
    """Снять закрытие. True — если было что снимать.

    Здесь строка именно удаляется: истории «когда мастер был занят» никто
    не ведёт, а отменённые из-за закрытия записи остались в базе со своей
    причиной — по ним всё и восстанавливается.
    """
    return db.execute("DELETE FROM closures WHERE id = ?", (closure_id,)) > 0


def bookings_in_closure(since, until, start, finish):
    """Активные записи, попавшие в закрываемое время.

    Нужны дважды: показать мастеру, скольким людям придётся написать, и потом
    отменить их разом. Отбор по дате делает база, а по часам — Python:
    сравнение отрезков в SQL пришлось бы писать строками, и читалось бы оно
    совсем непонятно.
    """
    found = db.query(
        f"SELECT * FROM bookings WHERE date BETWEEN ? AND ? "
        f"AND status IN ({placeholders(ACTIVE_STATUSES)}) "
        f"ORDER BY date, start",
        (since, until) + ACTIVE_STATUSES,
    )

    if start is None or finish is None:
        return found  # закрыт весь день, попали все

    closed_start, closed_finish = to_minutes(start), to_minutes(finish)
    inside = []

    for booking in found:
        booking_start = to_minutes(booking["start"])
        booking_end = booking_start + booking["minutes"]
        # Та же проверка пересечения, что и в is_free(), только без уборки:
        # процедура, закончившаяся ровно к отлучке, мастеру не мешает.
        if booking_end <= closed_start or booking_start >= closed_finish:
            continue
        inside.append(booking)

    return inside


# =========================================================================
# 3. Занятость времени
# =========================================================================

def without(bookings, booking_id):
    """Тот же список записей, но без одной — той, которую сейчас переносят.

    При переносе запись не должна мешать сама себе. Клиент, который двигает
    процедуру с 12:00 на 12:30, целится в те самые часы, которые эта запись
    и занимает: не убрав её из расчёта, мы показали бы ему день без единого
    подходящего окошка и никуда не пустили.

    booking_id = None означает обычную запись — тогда список не меняется.
    """
    if booking_id is None:
        return bookings
    return [booking for booking in bookings if booking["id"] != booking_id]


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


def is_free(bookings, day, start, minutes, closed=()):
    """Можно ли начать процедуру в это время?

    start и minutes — числа (минуты). Проверяем четыре вещи: рабочие часы,
    конец рабочего дня, пересечение с чужими записями и с тем временем,
    которое мастер закрыл.

    closed — уже посчитанные отрезки закрытого времени этого дня
    (closed_intervals). Приходят снаружи по той же причине, что и bookings:
    перебирая две недели, мы читаем их из базы один раз, а не на каждый день.
    """
    if start < to_minutes(work_start()):
        return False

    # Сама процедура должна закончиться до конца рабочего дня.
    # Уборка после последнего клиента в рабочие часы уже не обязана влезть.
    if start + minutes > to_minutes(work_end()):
        return False

    end = start + minutes + config.CLEANUP_MINUTES

    # Закрытое мастером время ведёт себя ровно как чужая запись: разница
    # только в том, откуда взялся отрезок. Поэтому и проверка одна.
    for busy_start, busy_end in busy_intervals(bookings, day) + list(closed):
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
    work_start_minutes = to_minutes(work_start())

    now = datetime.now()
    if day != now.strftime("%Y-%m-%d"):
        return work_start_minutes

    limit = now.hour * 60 + now.minute + config.MIN_LEAD_MINUTES

    # Округляем вверх до шага сетки: 13:10 при шаге 30 -> 13:30.
    remainder = limit % config.SLOT_STEP
    if remainder:
        limit += config.SLOT_STEP - remainder

    return max(work_start_minutes, limit)


def free_slots(day, minutes, bookings=None, exclude_id=None, closures=None):
    """Список свободных начал процедуры в этот день: ["10:00", "10:30", ...].

    bookings и closures можно передать снаружи, чтобы не спрашивать базу
    на каждый день, когда мы проверяем сразу две недели вперёд.

    exclude_id — номер записи, которую клиент переносит: для него её время
    свободно, см. without().
    """
    if bookings is None:
        bookings = bookings_on(day)
    if closures is None:
        closures = closures_ahead()

    bookings = without(bookings, exclude_id)
    closed = closed_intervals(day, closures)

    slots = []
    start = earliest_start(day)
    last_start = to_minutes(work_end()) - minutes

    while start <= last_start:
        if is_free(bookings, day, start, minutes, closed):
            slots.append(to_time(start))
        start += config.SLOT_STEP

    return slots


def upcoming_work_days(count):
    """Ближайшие рабочие дни подряд, без оглядки на занятость.

    В отличие от work_days(), который отбирает дни для клиента, здесь нужен
    просто календарь: мастер закрывает день независимо от того, есть ли в нём
    свободные окошки и записан ли кто-нибудь.
    """
    days = []
    today = date.today()

    for shift in range(config.DAYS_AHEAD + 1):
        day = today + timedelta(days=shift)
        if day.weekday() in work_weekdays():
            days.append(day.isoformat())
        if len(days) == count:
            break

    return days


def work_hours(first=None):
    """Часы рабочего дня кнопками: ["10:00", "11:00", ...].

    Для экранов мастера, где он выбирает границы отлучки. Шаг — час, а не
    SLOT_STEP: отлучиться на «с 12:30 до 14:30» бывает нужно редко, зато
    вдвое меньше кнопок, и они помещаются в клавиатуру без листания.

    first — показывать только время после него: «по» не может быть раньше «с».
    """
    hours = []
    start = to_minutes(work_start())
    limit = to_minutes(work_end())

    while start <= limit:
        moment = to_time(start)
        if first is None or moment > first:
            hours.append(moment)
        start += 60

    return hours


def work_days(minutes, exclude_id=None):
    """Ближайшие рабочие дни, где есть хотя бы одно свободное окошко.

    Возвращает список строк-дат. Дни без окон в список не попадают —
    нет смысла показывать кнопку, за которой пусто.

    exclude_id — переносимая запись: день, целиком занятый ею одной, для
    этого клиента свободен, и в список он попасть обязан.
    """
    bookings = bookings_ahead()   # один запрос на весь перебор
    closures = closures_ahead()  # и один на закрытия
    days = []
    today = date.today()

    for shift in range(config.DAYS_AHEAD):
        day = today + timedelta(days=shift)

        if day.weekday() not in work_weekdays():
            continue  # выходной мастера

        key = day.strftime("%Y-%m-%d")
        if not free_slots(key, minutes, bookings, exclude_id, closures):
            continue  # день целиком занят

        days.append(key)
        if len(days) == config.DAYS_TO_SHOW:
            break

    return days


def busy_days(minutes, exclude_id=None):
    """Ближайшие рабочие дни, где окошка под эту процедуру НЕТ.

    Обратная сторона work_days(): там дни, куда можно записаться, здесь —
    те, на которые остаётся только подписаться и ждать чужой отмены.

    Сегодня в список не попадает (range начинается с 1): даже если кто-то
    отменится через час, запас MIN_LEAD_MINUTES до конца дня уже не влезет.
    """
    bookings = bookings_ahead()
    closures = closures_ahead()
    days = []
    today = date.today()

    for shift in range(1, config.DAYS_AHEAD):
        day = today + timedelta(days=shift)

        if day.weekday() not in work_weekdays():
            continue  # выходной мастера

        key = day.strftime("%Y-%m-%d")
        if free_slots(key, minutes, bookings, exclude_id, closures):
            continue  # окошки есть — это день для записи, а не для подписки

        if closed_all_day(key, closures):
            continue  # мастер не принимает: ждать тут нечего

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


def move_booking(booking_id, user_id, day, start, minutes, service, length,
                 density, price_from, price_to):
    """Перенести запись на другое время. Возвращает новую запись или None.

    Отмена и новая запись одним неделимым действием — в этом весь смысл.
    Если делать по очереди («сначала отмени, потом запишись заново»), между
    двумя шагами старое время уже улетит подписчикам, а нового может не
    оказаться: клиент останется вообще без записи. Здесь же не сложилось —
    значит не изменилось ничего.

    Старая запись получает статус MOVED, а не CANCELLED: в истории мастера
    «перенёс» и «передумал» — разные события.

    None означает, что время заняли, пока клиент выбирал, или что переносить
    уже нечего. Оба случая для диалога одинаковы: показать свежие окошки.
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

    # Та же логика, что и у новой записи: до процедуры меньше суток —
    # спрашивать «придёте?» уже не о чем.
    if minutes_left(booking) <= config.CONFIRM_BEFORE_HOURS * 60:
        booking["status"] = "CONFIRMED"

    columns = ["user_id", "date", "start", "minutes", "service", "length",
               "density", "price_from", "price_to", "status"]

    with db.transaction():
        old = db.query_one(
            f"SELECT id FROM bookings WHERE id = ? AND user_id = ? "
            f"AND status IN ({placeholders(ACTIVE_STATUSES)})",
            (booking_id, user_id) + ACTIVE_STATUSES,
        )
        if old is None:
            return None  # чужая, отменённая или уже перенесённая

        # Себе самой запись не мешает: при переносе внутри одного дня она
        # занимает как раз то время, рядом с которым клиент и выбирает.
        # Без этого исключения перенос с 14:00 на 14:30 был бы невозможен.
        others = [other for other in bookings_on(day)
                  if other["id"] != booking_id]

        if not is_free(others, day, to_minutes(start), minutes):
            return None

        db.execute("UPDATE bookings SET status = 'MOVED' WHERE id = ?",
                   (booking_id,))

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


def cancel_by_master(booking_id, reason):
    """Отменить запись от лица мастера. Возвращает запись или None.

    Отдельный статус, а не CANCELLED: для мастера «клиент передумал»
    и «я сам отменил» — разные строки в истории, и путать их нельзя. Причина
    ложится в саму запись, чтобы её можно было показать и через неделю,
    а не только в том сообщении, которое клиент мог не прочитать.

    Владельца, в отличие от cancel_booking(), не проверяем: отменяет мастер,
    и любая активная запись ему подвластна. Условие по статусу внутри UPDATE
    оставляем — оно защищает от повторной отмены и от гонки с планировщиком.
    """
    changed = db.execute(
        f"UPDATE bookings SET status = 'CANCELLED_BY_MASTER', "
        f"cancel_reason = ? "
        f"WHERE id = ? AND status IN ({placeholders(ACTIVE_STATUSES)})",
        (reason, booking_id) + ACTIVE_STATUSES,
    )

    if not changed:
        return None

    return get_booking(booking_id)


def cancel_many_by_master(bookings, reason):
    """Отменить сразу несколько записей — одной причиной и одним действием.

    Так мастер закрывает день: пятнадцать записей отменяются вместе, а не по
    одной. Транзакция здесь не для скорости, а чтобы не получилось половины:
    день закрыт, а часть клиентов об этом не знает и приедет.

    Возвращает те записи, которые действительно отменились — им и пишем.
    """
    cancelled = []

    with db.transaction():
        for booking in bookings:
            changed = db.execute(
                f"UPDATE bookings SET status = 'CANCELLED_BY_MASTER', "
                f"cancel_reason = ? "
                f"WHERE id = ? AND status IN ({placeholders(ACTIVE_STATUSES)})",
                (reason, booking["id"]) + ACTIVE_STATUSES,
            )
            if changed:
                cancelled.append(dict(booking,
                                      status="CANCELLED_BY_MASTER",
                                      cancel_reason=reason))

    return cancelled


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


def due_day_reminders():
    """Записи, до которых остались часы: пора напомнить «сегодня, ждём».

    Отличий от due_reminders() два. Во-первых, статус подходит любой активный:
    к этому часу запись обычно уже CONFIRMED, но если автоотмена выключена
    в настройках, она может остаться и REMINDED — напомнить надо всё равно.
    Во-вторых, «уже напомнили» помнит не статус, а отдельная колонка: статус
    здесь не меняется, и записать признак больше некуда.
    """
    if config.DAY_REMINDER_HOURS is None:
        return []  # напоминание в день записи выключено в настройках

    now = datetime.now()
    limit = now + timedelta(hours=config.DAY_REMINDER_HOURS)

    return db.query(
        f"SELECT * FROM bookings WHERE day_reminded = 0 "
        f"AND status IN ({placeholders(ACTIVE_STATUSES)}) "
        f"AND date || ' ' || start > ? AND date || ' ' || start <= ? "
        f"ORDER BY date, start",
        ACTIVE_STATUSES + (moment_key(now), moment_key(limit)),
    )


def mark_day_reminded(booking_id):
    """Отметить, что напоминание в день записи отправлено.

    Возвращает True, если отметка поставлена именно сейчас. False означает,
    что кто-то успел раньше — например, планировщик после перезапуска пошёл
    по тому же списку. Условие day_reminded = 0 внутри UPDATE и делает эту
    проверку неделимой: второе напоминание не уйдёт.
    """
    return db.execute(
        "UPDATE bookings SET day_reminded = 1 "
        "WHERE id = ? AND day_reminded = 0",
        (booking_id,),
    ) > 0


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
    """Подтвердить записи, о которых бот спрашивал. Возвращает подтверждённые.

    Берём только те, что ждут ответа (ASKED_STATUS), и сразу все: клиент
    нажимает «Подтверждаю» в ответ на напоминание, и разбираться, к какой
    записи относилась кнопка (а сообщение могло быть и вчерашним), не нужно —
    обычно ждёт ответа одна, а если их две, подтвердить обе и есть то, чего
    клиент хочет.

    Запись в NEW кнопка не трогает, хотя формально она тоже не подтверждена.
    Про неё бот ещё не спрашивал, и если подтвердить её сейчас, напоминание
    за сутки по ней уже не придёт: клиент, нажавший «Подтверждаю» на запись
    завтра, молча остался бы без напоминания о записи через неделю.

    Сначала выбираем, что подтверждать, потом подтверждаем — и то и другое
    внутри одной транзакции: вернуть клиенту список записей нужно, а между
    выбором и правкой планировщик не должен успеть отменить одну из них.
    """
    now = moment_key(datetime.now())

    with db.transaction():
        confirmed = db.query(
            "SELECT * FROM bookings WHERE user_id = ? AND status = ? "
            "AND date || ' ' || start > ? "  # прошедшую подтверждать нечего
            "ORDER BY date, start",
            (user_id, ASKED_STATUS, now),
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
