"""Проверка db.py. Работает на своей базе в песочнице.

Реальные schedule.txt / subscribers.txt / bot.db не трогаются: подменяем
db.DB_FILE ДО первого connect().
"""

import os
import sqlite3
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent

# Каталог с кодом бота — на уровень выше этого файла. Путь вычисляем от
# самого файла, а не от рабочего каталога: тесты запускают и из своей папки,
# и из корня проекта. Переменной окружения VK_BOT_DIR его можно задать вручную —
# например, когда код скопирован в другое место.
PROJECT = Path(os.environ.get("VK_BOT_DIR")
               or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(PROJECT))

TEST_DB = HERE / "test_bot.db"

# Чистый старт: убираем базу от прошлого запуска вместе с журналом.
for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)

import config
import db

db.DB_FILE = TEST_DB
assert db.DB_FILE != config.DB_FILE, "база теста должна быть отдельной!"

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


TODAY = date.today()


def day(shift):
    return (TODAY + timedelta(days=shift)).isoformat()


# --- 1. база и схема ------------------------------------------------------
print("\n1. Создание базы и схемы")

conn = db.connect()
check("файл базы создан", TEST_DB.exists())

tables = {row["name"] for row in
          db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
check("таблицы на месте",
      {"bookings", "subscriptions", "dialogs", "closures", "settings"}
      <= tables, str(sorted(tables)))

indexes = db.query("SELECT name, tbl_name FROM sqlite_master "
                   "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")
check("своих индексов нет", indexes == [], str(indexes))

pragmas = {
    "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
    "auto_vacuum": conn.execute("PRAGMA auto_vacuum").fetchone()[0],
    "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
    "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
    "journal_size_limit": conn.execute("PRAGMA journal_size_limit").fetchone()[0],
}
print(f"     PRAGMA: {pragmas}")
check("journal_mode = wal", pragmas["journal_mode"] == "wal")
check("auto_vacuum = 2 (incremental)", pragmas["auto_vacuum"] == 2)
check("synchronous = 2 (full)", pragmas["synchronous"] == 2)
check(f"user_version = {db.SCHEMA_VERSION}",
      pragmas["user_version"] == db.SCHEMA_VERSION)
check("journal_size_limit = 65536", pragmas["journal_size_limit"] == 65536)

# Повторный connect() в этом же потоке должен вернуть то же соединение.
check("соединение переиспользуется", db.connect() is conn)

# Колонки, появившиеся во второй версии схемы.
columns = {row["name"] for row in
           db.query("SELECT name FROM pragma_table_info('bookings')")}
check("у записи есть day_reminded", "day_reminded" in columns, str(columns))

dialog_columns = {row["name"] for row in
                  db.query("SELECT name FROM pragma_table_info('dialogs')")}
check("у диалога есть move_id", "move_id" in dialog_columns,
      str(dialog_columns))
check("move_id попал в список полей диалога",
      "move_id" in db.DIALOG_FIELDS)

# Третья версия схемы: мастер управляет расписанием.
check("у записи есть причина отмены", "cancel_reason" in columns, str(columns))
check("таблица закрытий на месте", "closures" in tables, str(sorted(tables)))
check("таблица настроек на месте", "settings" in tables, str(sorted(tables)))

check("настройки читаются и пишутся",
      db.get_setting("проверка") is None)
db.set_setting("проверка", "значение")
check("сохранённая настройка возвращается",
      db.get_setting("проверка") == "значение")
db.set_setting("проверка", "другое")
check("настройка перезаписывается",
      db.get_setting("проверка") == "другое")
db.execute("DELETE FROM settings WHERE key = 'проверка'")


# --- 2. записи ------------------------------------------------------------
print("\n2. Записи: вставка, чтение, номера")

INSERT = """
INSERT INTO bookings (user_id, date, start, minutes, service, length,
                      density, price_from, price_to, status)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

first = db.insert(INSERT, (111, day(3), "10:00", 180, "keratin", "long",
                           "thick", 6200, 7300, "NEW"))
second = db.insert(INSERT, (222, day(3), "14:00", 120, "botox", "medium",
                            "normal", 3600, 4200, "CONFIRMED"))
check("номера выдаёт база", (first, second) == (1, 2), f"{first}, {second}")

booking = db.query_one("SELECT * FROM bookings WHERE id = ?", (first,))
print(f"     запись: {booking}")
check("это обычный словарь", type(booking) is dict)
check("дата строкой как в коде", booking["date"] == day(3))
check("время строкой как в коде", booking["start"] == "10:00")
check("minutes числом", booking["minutes"] == 180)
check("словарь можно дополнить", (booking.update(number=1) or True))

check("query_one про несуществующее -> None",
      db.query_one("SELECT * FROM bookings WHERE id = 999") is None)
check("query возвращает список", len(db.query("SELECT * FROM bookings")) == 2)

# CHECK в схеме не даст записать статус с опечаткой.
try:
    db.insert(INSERT, (333, day(4), "10:00", 90, "cold", "short",
                       "thin", 2300, 2700, "CONFIRMD"))
    check("опечатка в статусе отвергнута", False, "вставка прошла!")
except Exception as error:
    check("опечатка в статусе отвергнута", True)
    print(f"     ({type(error).__name__}: {error})")

# MOVED появился во второй версии схемы: перенесённая запись.
moved_row = db.insert(INSERT, (333, day(4), "10:00", 90, "cold", "short",
                               "thin", 2300, 2700, "MOVED"))
check("статус MOVED схема принимает", moved_row > 0)
db.execute("DELETE FROM bookings WHERE id = ?", (moved_row,))


# --- 3. смена статуса одним UPDATE ---------------------------------------
print("\n3. Статусы: rowcount вместо перечитывания")

changed = db.execute(
    "UPDATE bookings SET status = 'REMINDED' "
    "WHERE id = ? AND status = 'NEW'", (first,))
check("напоминание отмечено", changed == 1, f"rowcount={changed}")

changed = db.execute(
    "UPDATE bookings SET status = 'REMINDED' "
    "WHERE id = ? AND status = 'NEW'", (first,))
check("второй раз не срабатывает", changed == 0, f"rowcount={changed}")

changed = db.execute(
    "UPDATE bookings SET status = 'CANCELLED' "
    "WHERE id = ? AND user_id = ?", (first, 999))
check("чужую запись не отменить", changed == 0, f"rowcount={changed}")


# --- 4. подписки ----------------------------------------------------------
print("\n4. Подписки: ключ «клиент + день»")

SUB_INSERT = """
INSERT INTO subscriptions (user_id, date, minutes, service, length,
                           density, price_from, price_to)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

db.execute(SUB_INSERT, (111, day(5), 180, "keratin", "long", "thick",
                        6200, 7300))
try:
    db.execute(SUB_INSERT, (111, day(5), 120, "botox", "medium", "normal",
                            3600, 4200))
    check("двух подписок на один день не бывает", False, "вставка прошла!")
except Exception as error:
    check("двух подписок на один день не бывает", True)
    print(f"     ({type(error).__name__}: {error})")

db.execute(SUB_INSERT, (222, day(5), 90, "cold", "short", "thin", 2300, 2700))
check("другой клиент на тот же день — можно",
      len(db.query("SELECT * FROM subscriptions")) == 2)


# --- 5. состояние диалога ------------------------------------------------
print("\n5. Диалог: сохранить, поднять, забыть")

check("незнакомый клиент -> None", db.load_dialog(555) is None)

db.save_dialog(555, {"state": "SELECTING_TIME", "service": "keratin",
                     "length": "long", "density": "thick", "minutes": 180,
                     "price_from": 6200, "price_to": 7300,
                     "day": day(3), "page": 1})
loaded = db.load_dialog(555)
print(f"     поднято: {loaded}")
check("состояние вернулось", loaded["state"] == "SELECTING_TIME")
check("параметры процедуры на месте", loaded["minutes"] == 180)
check("страница на месте", loaded["page"] == 1)
check("пустые поля в словарь не попали", "cancel_id" not in loaded,
      str(loaded))
check("get с запасным значением работает", loaded.get("page", 0) == 1)

db.save_dialog(555, {"state": "MAIN_MENU"})
loaded = db.load_dialog(555)
check("сохранение заменяет строку целиком", loaded == {"state": "MAIN_MENU"},
      str(loaded))
check("одна строка на клиента",
      len(db.query("SELECT * FROM dialogs")) == 1)

check("forget_dialog вернул True", db.forget_dialog(555) is True)
check("клиент забыт", db.load_dialog(555) is None)
check("забыть дважды нельзя", db.forget_dialog(555) is False)


# --- 6. транзакция --------------------------------------------------------
print("\n6. Транзакция откатывается целиком")

before = len(db.query("SELECT * FROM bookings"))
try:
    with db.transaction():
        db.insert(INSERT, (444, day(6), "11:00", 90, "cold", "short",
                           "thin", 2300, 2700, "NEW"))
        raise RuntimeError("что-то сломалось посреди записи")
except RuntimeError:
    pass
check("после ошибки записи нет",
      len(db.query("SELECT * FROM bookings")) == before)

with db.transaction():
    db.insert(INSERT, (444, day(6), "11:00", 90, "cold", "short",
                       "thin", 2300, 2700, "NEW"))
check("успешная транзакция сохранилась",
      len(db.query("SELECT * FROM bookings")) == before + 1)


# --- 7. два потока одновременно ------------------------------------------
print("\n7. Два потока пишут в базу")

errors = []


def worker(number):
    try:
        for i in range(20):
            with db.transaction():
                db.insert(INSERT, (number, day(7), f"{10 + i % 8}:00", 60,
                                   "cold", "short", "thin", 2300, 2700, "NEW"))
        db.close()  # у потока своё соединение, закрываем за собой
    except Exception as error:
        errors.append(f"поток {number}: {type(error).__name__}: {error}")


threads = [threading.Thread(target=worker, args=(700 + n,)) for n in range(4)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()

check("ошибок в потоках нет", not errors, str(errors))
added = db.query_one("SELECT count(*) AS n FROM bookings WHERE user_id >= 700")
check("все 80 записей на месте", added["n"] == 80, str(added))


# --- 8. уборка ------------------------------------------------------------
print("\n8. Уборка старого")

# Старая запись, вчерашняя подписка и диалог, брошенный давно.
db.insert(INSERT, (888, (TODAY - timedelta(days=config.KEEP_HISTORY_DAYS + 1))
                   .isoformat(), "10:00", 90, "cold", "short", "thin",
                   2300, 2700, "CANCELLED"))
db.execute(SUB_INSERT, (888, day(-1), 90, "cold", "short", "thin", 2300, 2700))
db.save_dialog(999, {"state": "MAIN_MENU"})
db.execute("UPDATE dialogs SET seen_date = ? WHERE user_id = 999",
           ((TODAY - timedelta(days=config.KEEP_DIALOG_DAYS + 1)).isoformat(),))

# И то, что уборка трогать не должна.
recent = db.insert(INSERT, (889, (TODAY - timedelta(days=10)).isoformat(),
                            "10:00", 90, "cold", "short", "thin",
                            2300, 2700, "CONFIRMED"))
db.save_dialog(998, {"state": "SELECTING_TIME"})

removed = db.cleanup()
print(f"     удалено строк: {removed}")
check("что-то удалилось", removed == 3, f"removed={removed}")
check("старая запись удалена",
      db.query_one("SELECT * FROM bookings WHERE user_id = 888") is None)
check("вчерашняя подписка удалена",
      db.query_one("SELECT * FROM subscriptions WHERE user_id = 888") is None)
check("брошенный диалог удалён", db.load_dialog(999) is None)
check("недавняя запись на месте",
      db.query_one("SELECT * FROM bookings WHERE id = ?", (recent,)) is not None)
check("живой диалог на месте", db.load_dialog(998) is not None)
check("свежая подписка на месте",
      len(db.query("SELECT * FROM subscriptions")) == 2)
check("уборка на убранной базе ничего не удаляет", db.cleanup() == 0)


# --- 9. Версия схемы ------------------------------------------------------
print("\n9. Версия схемы")

# База от прошлой версии не должна молча приехать в новый код: CREATE TABLE
# IF NOT EXISTS увидит таблицы с нужными именами и уйдёт, новых колонок
# не добавив. Бот обязан остановиться понятным текстом.
OLD_DB = HERE / "test_db_old.db"
for suffix in ("", "-wal", "-shm"):
    Path(str(OLD_DB) + suffix).unlink(missing_ok=True)

old_conn = sqlite3.connect(OLD_DB)
old_conn.executescript(db.SCHEMA)
old_conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
old_conn.commit()
old_conn.close()

live_db, db.DB_FILE = db.DB_FILE, OLD_DB
db.close()
try:
    db.connect()
    check("база от старой версии останавливает бота", False, "открылась!")
except SystemExit as error:
    check("база от старой версии останавливает бота", True)
    print(f"     ({error})")
except Exception as error:
    check("база от старой версии останавливает бота", False,
          f"{type(error).__name__}: {error}")

db.close()
db.DB_FILE = live_db
for suffix in ("", "-wal", "-shm"):
    Path(str(OLD_DB) + suffix).unlink(missing_ok=True)

conn = db.connect()
check("своя база после этого открывается",
      conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION)


# --- 10. сколько занимает -------------------------------------------------
print("\n10. Размер")

rows = db.query_one("SELECT count(*) AS n FROM bookings")["n"]
db.close()
size = TEST_DB.stat().st_size
wal = Path(str(TEST_DB) + "-wal")
print(f"     записей: {rows}")
print(f"     bot.db: {size} байт  ({size / max(rows, 1):.0f} байт на запись "
      f"со всеми служебными страницами)")
if wal.exists():
    print(f"     журнал: {wal.stat().st_size} байт")

print(f"\nИтого: ок {ok}, плохо {fail}")
sys.exit(1 if fail else 0)
