"""Проверка schedule.py после переезда записей и подписок в базу.

Работает на своей базе в песочнице. Живые bot.db / schedule.txt /
subscribers.txt не трогаются: путь к базе подменяется ДО первого обращения
к ней, а файлы бот больше не открывает вовсе.
"""

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent

# Каталог с кодом бота — на уровень выше этого файла. Путь вычисляем от
# самого файла, а не от рабочего каталога: тесты запускают и из своей папки,
# и из корня проекта. Переменной окружения VK_BOT_DIR его можно задать вручную —
# например, когда код скопирован в другое место.
PROJECT = Path(os.environ.get("VK_BOT_DIR")
               or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(PROJECT))

TEST_DB = HERE / "test_schedule.db"

for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)

import config
import db
import schedule

db.DB_FILE = TEST_DB

assert db.DB_FILE != config.DB_FILE, "база теста должна быть отдельной!"

# Старые текстовые файлы бот больше не открывает, но они лежат рядом как копия
# данных до переезда — проверим заодно, что мы их не тронули. Если их уже
# удалили, проверять просто нечего.
LIVE_SCHEDULE = PROJECT / "schedule.txt"
LIVE_SUBS = PROJECT / "subscribers.txt"
LIVE_STAMPS = tuple(path.stat().st_mtime if path.exists() else None
                    for path in (LIVE_SCHEDULE, LIVE_SUBS))

# Боевая база уже существует (записи в неё перенесены), поэтому проверяем не
# «её нет», а «мы её не тронули» — по времени последнего изменения файла.
LIVE_DB_STAMP = (config.DB_FILE.stat().st_mtime if config.DB_FILE.exists()
                 else None)

ok = 0
fail = 0


def check(name, condition, detail=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  ок   {name}")
    else:
        fail += 1
        print(f"  ПЛОХО {name} {detail}")


CLIENT = 111
OTHER = 222


def work_days_ahead(count, min_shift=3):
    """count разных рабочих дней мастера, начиная не раньше min_shift."""
    days = []
    shift = min_shift
    while len(days) < count:
        day = date.today() + timedelta(days=shift)
        if day.weekday() in config.WORK_DAYS:
            days.append(day.isoformat())
        shift += 1
    return days


def work_day(min_shift):
    """Первый рабочий день мастера не раньше чем через min_shift дней."""
    return work_days_ahead(1, min_shift)[0]


DAY = work_day(3)
DAY2 = work_day(5)
print(f"\nдни для проверки: {DAY}, {DAY2}")


# --- 1. Создание записи ---------------------------------------------------
print("\n1. Создание записи")

booking = schedule.create_booking(CLIENT, DAY, "12:00", 120, "cold", "short",
                                  "thin", 2100, 2400)
print(f"     {booking}")
check("запись создана", booking is not None)
check("номер выдала база", booking["id"] == 1, str(booking))
check("статус NEW", booking["status"] == "NEW")
check("поля на месте", booking["date"] == DAY and booking["start"] == "12:00"
      and booking["minutes"] == 120)
check("в базе лежит то же самое",
      schedule.get_booking(1)["price_to"] == 2400)

# То же время второй раз — занято.
check("второй раз то же время нельзя",
      schedule.create_booking(OTHER, DAY, "12:00", 120, "cold", "short",
                              "thin", 2100, 2400) is None)

# 13:30 попадает внутрь 12:00–14:00, 14:00–14:30 — уборка.
check("наложение не проходит",
      schedule.create_booking(OTHER, DAY, "13:30", 90, "cold", "short",
                              "thin", 2100, 2400) is None)
check("сразу после уборки — можно",
      schedule.create_booking(OTHER, DAY, "14:30", 90, "cold", "short",
                              "thin", 2100, 2400) is not None)
check("в базе две записи",
      len(db.query("SELECT * FROM bookings")) == 2)


# --- 2. Свободные окошки --------------------------------------------------
print("\n2. Свободные окошки")

slots = schedule.free_slots(DAY, 90)
print(f"     {DAY}: {slots}")
check("занятое время не предлагается", "12:00" not in slots)
check("13:00 тоже занято (пересечётся)", "13:00" not in slots)
check("утро свободно", "10:00" in slots)
# Вторая запись 14:30–16:00 плюс уборка до 16:30 — раньше не начать.
check("после уборки свободно", "16:30" in slots)
check("уборка второй записи занята", "16:00" not in slots)

check("день с окошками попадает в список", DAY in schedule.work_days(90))
check("свободный день не попадает в busy_days",
      DAY not in schedule.busy_days(90))

# Длинная процедура в этот день уже не влезает — значит, день «занятой».
long_minutes = 8 * 60
check("для длинной процедуры день занят",
      DAY not in schedule.work_days(long_minutes))
check("и попадает в busy_days", DAY in schedule.busy_days(long_minutes))

check("has_bookings видит активную запись", schedule.has_bookings(DAY) is True)
check("в пустом дне записей нет", schedule.has_bookings(DAY2) is False)


# --- 3. Записи клиента ----------------------------------------------------
print("\n3. Записи клиента и лимит")

mine = schedule.user_bookings(CLIENT)
check("клиенту видна только его запись",
      len(mine) == 1 and mine[0]["user_id"] == CLIENT, str(mine))
check("счётчик совпадает со списком", schedule.active_count(CLIENT) == 1)
check("лимит не достигнут", schedule.limit_reached(CLIENT) is False)

# Прошедшая запись: в списке будущих её быть не должно.
db.insert(
    "INSERT INTO bookings (user_id, date, start, minutes, service, length, "
    "density, price_from, price_to, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
    (CLIENT, (date.today() - timedelta(days=2)).isoformat(), "10:00", 90,
     "cold", "short", "thin", 2100, 2400, "CONFIRMED"))
check("прошедшая запись не в списке будущих",
      len(schedule.user_bookings(CLIENT)) == 1)
check("а в полном списке — есть",
      len(schedule.user_bookings(CLIENT, only_future=False)) == 2)
check("в лимит прошедшая не идёт", schedule.active_count(CLIENT) == 1)

# Список должен приходить по возрастанию времени.
schedule.create_booking(CLIENT, DAY2, "10:00", 90, "cold", "short", "thin",
                        2100, 2400)
mine = schedule.user_bookings(CLIENT)
check("список по времени", [b["date"] for b in mine] == [DAY, DAY2], str(mine))


# --- 4. Отмена ------------------------------------------------------------
print("\n4. Отмена")

check("чужую запись не отменить", schedule.cancel_booking(1, OTHER) is None)
check("несуществующую тоже", schedule.cancel_booking(999, CLIENT) is None)

cancelled = schedule.cancel_booking(1, CLIENT)
check("своя отменяется", cancelled is not None and
      cancelled["status"] == "CANCELLED", str(cancelled))
check("второй раз отменять нечего", schedule.cancel_booking(1, CLIENT) is None)
check("строка осталась в истории", schedule.get_booking(1) is not None)
check("время снова свободно", "12:00" in schedule.free_slots(DAY, 90))
check("из списка клиента ушла", len(schedule.user_bookings(CLIENT)) == 1)


# --- 5. Статусы: напоминание и автоотмена ---------------------------------
print("\n5. Статусы")

fresh = schedule.create_booking(CLIENT, DAY, "12:00", 120, "cold", "short",
                                "thin", 2100, 2400)
reminded = schedule.mark_reminded(fresh["id"])
check("напоминание отмечено", reminded["status"] == "REMINDED", str(reminded))
check("второй раз не отмечается", schedule.mark_reminded(fresh["id"]) is None)

expired = schedule.expire_booking(fresh["id"])
check("неподтверждённая отменяется сама", expired["status"] == "EXPIRED")
check("подтверждённую автоотмена не берёт",
      schedule.expire_booking(fresh["id"]) is None)
check("время после автоотмены свободно",
      "12:00" in schedule.free_slots(DAY, 90))


# --- 6. Кому пора писать --------------------------------------------------
print("\n6. due_reminders / due_expired")

INSERT = ("INSERT INTO bookings (user_id, date, start, minutes, service, "
          "length, density, price_from, price_to, status) "
          "VALUES (?,?,?,?,?,?,?,?,?,?)")


def at(hours, status, user_id=333):
    """Запись через hours часов от текущего момента, минуя проверки."""
    moment = datetime.now() + timedelta(hours=hours)
    return db.insert(INSERT, (user_id, moment.strftime("%Y-%m-%d"),
                              moment.strftime("%H:%M"), 60, "cold", "short",
                              "thin", 2100, 2400, status))


soon = at(10, "NEW")               # через 10 часов — пора напоминать
later = at(5 * 24, "NEW")          # через 5 дней — рано
already = at(10, "REMINDED")       # напоминание уже ушло
past = at(-3, "NEW")               # прошедшая

due = [b["id"] for b in schedule.due_reminders()]
print(f"     напомнить: {due}")
check("близкая NEW попала", soon in due)
check("далёкая NEW не попала", later not in due)
check("REMINDED второй раз не берём", already not in due)
check("прошедшую не тревожим", past not in due)

expire_soon = at(6, "REMINDED")    # 6 часов < 12 — пора отменять
expire_late = at(20, "REMINDED")   # 20 часов — ещё ждём
expire_past = at(-3, "REMINDED")

due = [b["id"] for b in schedule.due_expired()]
print(f"     отменить: {due}")
check("подошедшая REMINDED попала", expire_soon in due)
check("далёкая REMINDED не попала", expire_late not in due)
check("прошедшую не отменяем", expire_past not in due)

saved = config.AUTOCANCEL_BEFORE_HOURS
config.AUTOCANCEL_BEFORE_HOURS = None
check("выключенная автоотмена ничего не возвращает",
      schedule.due_expired() == [])
config.AUTOCANCEL_BEFORE_HOURS = saved


# --- 7. Подтверждение -----------------------------------------------------
print("\n7. Подтверждение")

confirmed = schedule.confirm_bookings(333)
print(f"     подтверждено: {[(b['id'], b['status']) for b in confirmed]}")
check("подтвердились те, о которых спрашивали",
      {b["id"] for b in confirmed} == {already, expire_soon, expire_late},
      str([b["id"] for b in confirmed]))
check("в возвращённых словарях статус новый",
      all(b["status"] == "CONFIRMED" for b in confirmed))
check("в базе статус тоже новый",
      schedule.get_booking(already)["status"] == "CONFIRMED")
check("прошедшая не подтвердилась",
      schedule.get_booking(expire_past)["status"] == "REMINDED")

# Главное в этой проверке: кнопка «Подтверждаю» не должна молча подтверждать
# записи, о которых бот ещё не спрашивал, — иначе напоминание за сутки по ним
# уже не придёт.
check("запись без напоминания осталась в NEW",
      schedule.get_booking(soon)["status"] == "NEW")
check("далёкая запись тоже не тронута",
      schedule.get_booking(later)["status"] == "NEW")
check("и напоминание её по-прежнему ждёт",
      soon in [b["id"] for b in schedule.due_reminders()])
check("прошедшая NEW не подтвердилась",
      schedule.get_booking(past)["status"] == "NEW")
check("порядок по времени",
      [b["id"] for b in confirmed] == sorted(
          [b["id"] for b in confirmed],
          key=lambda i: schedule.booking_datetime(schedule.get_booking(i))))
check("подтверждать второй раз нечего", schedule.confirm_bookings(333) == [])


# --- 8. Подписки ----------------------------------------------------------
print("\n8. Подписки")

subscription = schedule.add_subscription(CLIENT, DAY2, 90, "cold", "short",
                                         "thin", 2100, 2400)
print(f"     {subscription}")
check("подписка создалась", subscription is not None)
check("параметры процедуры сохранены",
      schedule.day_subscribers(DAY2)[0] == subscription, str(subscription))
check("повторная на тот же день не создаётся",
      schedule.add_subscription(CLIENT, DAY2, 120, "botox", "short", "thin",
                                2500, 2900) is None)
check("дубль не появился в базе",
      len(db.query("SELECT * FROM subscriptions")) == 1)
check("первая подписка не переписана",
      schedule.day_subscribers(DAY2)[0]["service"] == "cold")
check("клиент подписан", schedule.is_subscribed(CLIENT, DAY2) is True)
check("на другой день не подписан", schedule.is_subscribed(CLIENT, DAY) is False)
check("другой клиент не подписан", schedule.is_subscribed(OTHER, DAY2) is False)

# Другой клиент на тот же день — это другая подписка, ключ не мешает.
check("другой клиент на тот же день — можно",
      schedule.add_subscription(OTHER, DAY2, 90, "cold", "short", "thin",
                                2100, 2400) is not None)
check("день видит обоих", len(schedule.day_subscribers(DAY2)) == 2)
check("клиенту видна только своя",
      len(schedule.user_subscriptions(CLIENT)) == 1)

# Лимит: MAX_SUBSCRIPTIONS дней и ни одним больше. Одна подписка у клиента
# уже есть, поэтому добираем на единицу меньше лимита.
free_days = work_days_ahead(config.MAX_SUBSCRIPTIONS, 9)

for day in free_days[:config.MAX_SUBSCRIPTIONS - 1]:
    check(f"подписка на {day} создалась",
          schedule.add_subscription(CLIENT, day, 90, "cold", "short", "thin",
                                    2100, 2400) is not None)

check("лимит набран",
      schedule.subscriptions_count(CLIENT) == config.MAX_SUBSCRIPTIONS,
      str(schedule.user_subscriptions(CLIENT)))
check("сверх лимита не подписаться",
      schedule.add_subscription(CLIENT, free_days[-1], 90, "cold", "short",
                                "thin", 2100, 2400) is None)
check("лишняя подписка в базу не попала",
      schedule.is_subscribed(CLIENT, free_days[-1]) is False)
check("лимит виден снаружи",
      schedule.subscriptions_limit_reached(CLIENT) is True)
check("у другого клиента свой лимит",
      schedule.subscriptions_limit_reached(OTHER) is False)

check("список по возрастанию даты",
      [s["date"] for s in schedule.user_subscriptions(CLIENT)]
      == sorted(s["date"] for s in schedule.user_subscriptions(CLIENT)))

# Прошедшая подписка: в выдаче её нет, в базе до уборки лежит.
db.execute(
    "INSERT INTO subscriptions (user_id, date, minutes, service, length, "
    "density, price_from, price_to) VALUES (?,?,?,?,?,?,?,?)",
    (OTHER, (date.today() - timedelta(days=1)).isoformat(), 90, "cold",
     "short", "thin", 2100, 2400))
check("вчерашняя подписка не в списке",
      len(schedule.user_subscriptions(OTHER)) == 1)
check("и в лимит не идёт", schedule.subscriptions_count(OTHER) == 1)
check("уборка её убирает", db.cleanup() == 1)

check("подписка снимается", schedule.remove_subscription(CLIENT, DAY2) is True)
check("снимать дважды нечего",
      schedule.remove_subscription(CLIENT, DAY2) is False)
check("после снятия можно подписаться заново",
      schedule.add_subscription(CLIENT, DAY2, 90, "cold", "short", "thin",
                                2100, 2400) is not None)
check("подписки клиента остались в базе",
      schedule.subscriptions_count(CLIENT) == config.MAX_SUBSCRIPTIONS)


# --- 9. Перенос записи ----------------------------------------------------
print("\n9. Перенос записи")

MOVER = 900
MOVE_DAY, OTHER_DAY = work_days_ahead(2, min_shift=7)

moved = schedule.create_booking(MOVER, MOVE_DAY, "10:00", 90, "cold",
                                "short", "thin", 2100, 2400)

check("своё время занято для чужих",
      "10:30" not in schedule.free_slots(MOVE_DAY, 90))
check("и свободно для себя",
      "10:30" in schedule.free_slots(MOVE_DAY, 90, exclude_id=moved["id"]))
check("without() убирает ровно одну запись",
      len(schedule.without(schedule.bookings_on(MOVE_DAY), moved["id"]))
      == len(schedule.bookings_on(MOVE_DAY)) - 1)
check("without(None) не меняет список",
      schedule.without([{"id": 1}], None) == [{"id": 1}])

fresh = schedule.move_booking(moved["id"], MOVER, OTHER_DAY, "12:00", 90,
                              "cold", "short", "thin", 2100, 2400)
check("перенос состоялся", fresh is not None)
check("у новой записи свой номер", fresh and fresh["id"] != moved["id"])
check("старая помечена MOVED",
      schedule.get_booking(moved["id"])["status"] == "MOVED")
check("MOVED время не занимает", "10:00" in schedule.free_slots(MOVE_DAY, 90))
check("активная запись одна", schedule.active_count(MOVER) == 1)

check("дважды перенести ту же запись нельзя",
      schedule.move_booking(moved["id"], MOVER, MOVE_DAY, "10:00", 90, "cold",
                            "short", "thin", 2100, 2400) is None)
check("чужую запись перенести нельзя",
      schedule.move_booking(fresh["id"], MOVER + 1, MOVE_DAY, "10:00", 90,
                            "cold", "short", "thin", 2100, 2400) is None)
check("за конец рабочего дня не переносит",
      schedule.move_booking(fresh["id"], MOVER, OTHER_DAY, "19:30", 90,
                            "cold", "short", "thin", 2100, 2400) is None)
check("после неудачи запись осталась активной",
      schedule.get_booking(fresh["id"])["status"] in schedule.ACTIVE_STATUSES)

inside = schedule.move_booking(fresh["id"], MOVER, OTHER_DAY, "12:30", 90,
                               "cold", "short", "thin", 2100, 2400)
check("перенос внутри дня на соседнее время", inside is not None)
check("и он действительно сдвинул", inside and inside["start"] == "12:30")


# --- 10. Напоминание в день записи ----------------------------------------
print("\n10. Напоминание в день записи")

SOON = 950
in_two_hours = datetime.now() + timedelta(hours=2)

# Пишем прямо в базу: create_booking() на сегодня-через-два-часа не пустила бы
# (MIN_LEAD_MINUTES), а проверяем мы здесь очередь напоминаний, а не запись.
db.execute(
    "INSERT INTO bookings (user_id, date, start, minutes, service, length, "
    "density, price_from, price_to, status) "
    "VALUES (?, ?, ?, 90, 'cold', 'short', 'thin', 2100, 2400, 'CONFIRMED')",
    (SOON, in_two_hours.strftime("%Y-%m-%d"), in_two_hours.strftime("%H:%M")),
)
soon_id = db.query_one("SELECT id FROM bookings WHERE user_id = ?",
                       (SOON,))["id"]

due = [booking["id"] for booking in schedule.due_day_reminders()]
check("запись через два часа попала в очередь", soon_id in due, str(due))
check("дальняя запись в очередь не попала", inside["id"] not in due)

check("отметка ставится", schedule.mark_day_reminded(soon_id) is True)
check("второй раз не ставится", schedule.mark_day_reminded(soon_id) is False)
check("после отметки очередь пуста",
      soon_id not in [b["id"] for b in schedule.due_day_reminders()])

db.execute("UPDATE bookings SET day_reminded = 0, status = 'CANCELLED' "
           "WHERE id = ?", (soon_id,))
check("отменённой записи не напоминают",
      soon_id not in [b["id"] for b in schedule.due_day_reminders()])

saved_hours = config.DAY_REMINDER_HOURS
config.DAY_REMINDER_HOURS = None
check("выключенное напоминание никого не ищет",
      schedule.due_day_reminders() == [])
config.DAY_REMINDER_HOURS = saved_hours


# --- 11. Расписание мастера -----------------------------------------------
print("\n11. Расписание мастера")

days = dict(schedule.days_with_bookings())
check("день с записью в списке есть", OTHER_DAY in days, str(days))
check("день, где осталась только MOVED, не попал", MOVE_DAY not in days,
      str(days))

listed = schedule.day_bookings(OTHER_DAY)
check("день отдаёт свои записи", listed != [])
check("только активные",
      all(item["status"] in schedule.ACTIVE_STATUSES for item in listed))
check("и по возрастанию времени",
      [item["start"] for item in listed] == sorted(item["start"]
                                                   for item in listed))
check("пустой день отдаёт пустой список",
      schedule.day_bookings("2000-01-01") == [])


# --- 12. Закрытое время ---------------------------------------------------
print("\n12. Закрытое время")

CLOSED_DAY, FREE_DAY = work_days_ahead(2, min_shift=10)

check("пока ничего не закрыто", schedule.all_closures() == [])
check("день открыт", not schedule.closed_all_day(CLOSED_DAY))

whole = schedule.add_closure(CLOSED_DAY, CLOSED_DAY, None, None, "Заболела")
check("закрытие добавилось", whole["id"] > 0)
check("день закрыт целиком", schedule.closed_all_day(CLOSED_DAY))
check("окошек в нём нет", schedule.free_slots(CLOSED_DAY, 90) == [])
check("в список дней для записи не попал",
      CLOSED_DAY not in schedule.work_days(90))
check("и для подписки тоже не попал",
      CLOSED_DAY not in schedule.busy_days(90))
check("причина показывается",
      schedule.closure_reason(CLOSED_DAY) == "Заболела")
check("соседний день не тронут", not schedule.closed_all_day(FREE_DAY))

check("закрытие снимается", schedule.remove_closure(whole["id"]) is True)
check("дважды снять нельзя", schedule.remove_closure(whole["id"]) is False)
check("день снова открыт", schedule.free_slots(CLOSED_DAY, 90) != [])

part = schedule.add_closure(CLOSED_DAY, CLOSED_DAY, "12:00", "15:00", "Врач")
free = schedule.free_slots(CLOSED_DAY, 90)
check("до отлучки записаться можно", "10:00" in free, str(free))
check("внутрь отлучки нельзя", "13:00" not in free, str(free))
check("впритык перед ней тоже нельзя", "11:00" not in free, str(free))
check("после отлучки можно", "15:00" in free, str(free))
check("день целиком не закрыт", not schedule.closed_all_day(CLOSED_DAY))
check("отрезок посчитан в минутах",
      schedule.closed_intervals(CLOSED_DAY) == [(720, 900)],
      str(schedule.closed_intervals(CLOSED_DAY)))

inside = schedule.create_booking(CLIENT + 7, CLOSED_DAY, "10:00", 90, "cold",
                                 "short", "thin", 2100, 2400)
caught = schedule.bookings_in_closure(CLOSED_DAY, CLOSED_DAY, "12:00", "15:00")
check("запись до отлучки под неё не попала",
      inside["id"] not in [b["id"] for b in caught], str(caught))
caught = schedule.bookings_in_closure(CLOSED_DAY, CLOSED_DAY, None, None)
check("а при закрытии всего дня — попала",
      inside["id"] in [b["id"] for b in caught], str(caught))

cancelled = schedule.cancel_many_by_master(caught, "Закрываю день")
check("отменились разом", len(cancelled) == len(caught), str(len(cancelled)))
check("статус — отмена мастером",
      schedule.get_booking(inside["id"])["status"] == "CANCELLED_BY_MASTER")
check("причина легла в запись",
      schedule.get_booking(inside["id"])["cancel_reason"] == "Закрываю день")
check("второй раз отменять нечего",
      schedule.cancel_many_by_master(caught, "ещё раз") == [])

schedule.remove_closure(part["id"])

single = schedule.create_booking(CLIENT + 8, FREE_DAY, "10:00", 90, "cold",
                                 "short", "thin", 2100, 2400)
done = schedule.cancel_by_master(single["id"], "Перенесли салон")
check("одиночная отмена сработала", done is not None)
check("с причиной", done and done["cancel_reason"] == "Перенесли салон")
check("время освободилось", "10:00" in schedule.free_slots(FREE_DAY, 90))
check("повторно отменить нельзя",
      schedule.cancel_by_master(single["id"], "ещё") is None)


# --- 13. Рабочий график из базы -------------------------------------------
print("\n13. Рабочий график")

check("по умолчанию берётся из настроек",
      schedule.work_start() == config.WORK_START
      and schedule.work_end() == config.WORK_END
      and schedule.work_weekdays() == config.WORK_DAYS)

schedule.set_work_schedule(start="11:00")
check("начало дня переехало в базу", schedule.work_start() == "11:00")
check("и раньше него не записаться",
      "10:00" not in schedule.free_slots(FREE_DAY, 90))
check("конец дня остался из настроек",
      schedule.work_end() == config.WORK_END)

schedule.set_work_schedule(days=[0, 1])
check("рабочие дни читаются списком",
      schedule.work_weekdays() == [0, 1], str(schedule.work_weekdays()))
check("среда стала выходной",
      all(date.fromisoformat(day).weekday() in (0, 1)
          for day in schedule.work_days(90)))

hours = schedule.work_hours()
check("часы для кнопок начинаются с рабочего", hours[0] == "11:00", str(hours))
check("и не выходят за конец дня", hours[-1] <= config.WORK_END, str(hours))
check("«по» показывает только позже начала",
      all(hour > "13:00" for hour in schedule.work_hours(first="13:00")))

upcoming = schedule.upcoming_work_days(3)
check("ближайшие рабочие дни идут по порядку",
      upcoming == sorted(upcoming) and len(upcoming) == 3, str(upcoming))
check("и все — рабочие",
      all(date.fromisoformat(day).weekday() in (0, 1) for day in upcoming))

# Возвращаем настройки, чтобы следующие разделы считали как раньше.
db.execute("DELETE FROM settings")
check("после сброса снова из config",
      schedule.work_weekdays() == config.WORK_DAYS)


# --- 14. Живые данные не тронуты ------------------------------------------
print("\n14. Живые данные")

check("schedule.txt не изменился",
      (LIVE_SCHEDULE.stat().st_mtime if LIVE_SCHEDULE.exists() else None)
      == LIVE_STAMPS[0])
check("subscribers.txt не изменился",
      (LIVE_SUBS.stat().st_mtime if LIVE_SUBS.exists() else None)
      == LIVE_STAMPS[1])
check("боевая база не тронута",
      (config.DB_FILE.stat().st_mtime if config.DB_FILE.exists() else None)
      == LIVE_DB_STAMP)

db.close()
print(f"\nИтого: ок {ok}, плохо {fail}")
sys.exit(1 if fail else 0)
