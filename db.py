"""Хранение данных: соединение с SQLite, схема и состояние диалога.

Ниже этого файла — только config.py. Про VK и про расписание здесь ничего не
знают: db.py отвечает на вопрос «где лежат данные», а не «что они значат».

В базе лежит ровно то, что и так ходит по коду: дата строкой «2026-08-03»,
время — «14:00», статус — «CONFIRMED». Перекодировки между базой и словарями
нет, поэтому в данные можно смотреть глазами:

    sqlite3 bot.db "select * from bookings"

Место экономить не нужно: при десятке записей в день база не выходит за
несколько сотен килобайт, и держит её в этих пределах не устройство схемы,
а уборка старых строк — cleanup() ниже.

Новое поле добавляется через ALTER TABLE ADD COLUMN, а не переписыванием всех
данных, как было с текстовыми файлами. Версия схемы лежит в PRAGMA
user_version — по ней будущая миграция поймёт, что чинить.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, timedelta

import config

# Путь отдельной переменной, а не config.DB_FILE по месту: так тесты
# подменяют базу одной строкой, не трогая настройки.
DB_FILE = config.DB_FILE

SCHEMA_VERSION = 1


# =========================================================================
# 1. Схема
# =========================================================================
# Имена колонок совпадают с ключами словарей в коде («date», «start»,
# «status»), поэтому строка из базы — это уже готовая запись, а словарь
# сохраняется без переименований.

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id         INTEGER PRIMARY KEY,   -- он же rowid: номера выдаёт сама база
    user_id    INTEGER NOT NULL,      -- VK ID клиента
    date       TEXT    NOT NULL,      -- 2026-08-03
    start      TEXT    NOT NULL,      -- 14:00 — начало процедуры
    minutes    INTEGER NOT NULL,      -- длительность процедуры, без уборки
    service    TEXT    NOT NULL,      -- ключ из config.SERVICES
    length     TEXT    NOT NULL,
    density    TEXT    NOT NULL,
    price_from INTEGER NOT NULL,
    price_to   INTEGER NOT NULL,
    status     TEXT    NOT NULL
        CHECK (status IN ('NEW', 'REMINDED', 'CONFIRMED',
                          'CANCELLED', 'EXPIRED'))
);

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id    INTEGER NOT NULL,
    date       TEXT    NOT NULL,      -- день, окошка в котором клиент ждёт
    minutes    INTEGER NOT NULL,      -- окно короче не подойдёт
    service    TEXT    NOT NULL,
    length     TEXT    NOT NULL,
    density    TEXT    NOT NULL,
    price_from INTEGER NOT NULL,      -- цена на момент подписки
    price_to   INTEGER NOT NULL,
    PRIMARY KEY (user_id, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS dialogs (
    user_id    INTEGER PRIMARY KEY,
    state      TEXT    NOT NULL,      -- MAIN_MENU, SELECTING_TIME, ...
    service    TEXT,
    length     TEXT,
    density    TEXT,
    minutes    INTEGER,
    price_from INTEGER,
    price_to   INTEGER,
    day        TEXT,                  -- выбранный день записи
    time       TEXT,                  -- выбранное время начала
    page       INTEGER,               -- страница со свободными окошками
    cancel_id  INTEGER,               -- какую запись отменяем
    sub_day    TEXT,                  -- день, на который подписываем
    seen_date  TEXT    NOT NULL       -- последняя активность, нужна для уборки
);
"""

# Порядок колонок в PRIMARY KEY подписок не случаен: пара «клиент + день» —
# это и есть подписка, поэтому база сама не даст подписаться на день дважды.
# WITHOUT ROWID означает, что таблица хранится прямо по этому ключу, без
# отдельного скрытого номера строки.

# Индексов нет ни одного, кроме первичных ключей. Записей в базе — тысячи,
# полный проход по такой таблице занимает микросекунды, а каждый индекс нужно
# поддерживать при каждой вставке. Понадобятся, когда мастеров станет много.

# Поля состояния диалога — те же ключи, что лежат в словаре пользователя
# в main.py. Списком, потому что по нему собираются и запрос сохранения,
# и словарь при загрузке.
DIALOG_FIELDS = [
    "state",
    "service",
    "length",
    "density",
    "minutes",
    "price_from",
    "price_to",
    "day",
    "time",
    "page",
    "cancel_id",
    "sub_day",
]


# =========================================================================
# 2. Соединение
# =========================================================================
# sqlite3 не разрешает пользоваться одним соединением из разных потоков,
# а потоков у нас два: диалог с клиентами и планировщик. Поэтому соединение
# у каждого потока своё, а WAL позволяет им работать одновременно — тот,
# кто пишет, не заставляет читающего ждать.

_local = threading.local()


def connect():
    """Соединение текущего потока. Первое обращение создаёт базу и схему."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    # timeout — сколько ждать освобождения базы, если её занял другой поток.
    # Без него вместо ожидания сразу прилетает «database is locked».
    #
    # isolation_level=None отключает «умное» управление транзакциями внутри
    # sqlite3: каждое выражение применяется сразу, а там, где нужна
    # неразрывность, транзакция открывается явно — см. transaction().
    conn = sqlite3.connect(DB_FILE, timeout=5, isolation_level=None)

    # Строки приходят объектом Row: к колонкам можно обращаться по имени.
    conn.row_factory = sqlite3.Row

    # Работает только на пустой базе, поэтому идёт до создания таблиц:
    # после удаления старых записей место возвращается системе порциями,
    # без VACUUM, которому нужно временно вдвое больше диска.
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")

    # Журнал вперёд-записи: читающий поток видит последнее целое состояние
    # базы, пока пишущий работает. Настройка хранится в самом файле базы,
    # то есть задаётся один раз за её жизнь.
    conn.execute("PRAGMA journal_mode = WAL")

    # Чтобы файл журнала не разрастался: после каждой сборки он обрезается
    # до 64 КБ.
    conn.execute("PRAGMA journal_size_limit = 65536")

    # Ждать подтверждения диска на каждой записи. При десятке записей в день
    # это ничего не стоит, зато отключение света не съедает последнюю запись.
    conn.execute("PRAGMA synchronous = FULL")

    conn.executescript(SCHEMA)

    # На свежей базе номер версии — 0. Ставим свой, чтобы будущая миграция
    # могла отличить старую схему от новой и не гадать.
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    _local.conn = conn
    return conn


def close():
    """Закрыть соединение этого потока. Нужно тестам, боту — нет."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# =========================================================================
# 3. Запросы
# =========================================================================

def query(sql, params=()):
    """Выполнить SELECT и вернуть список обычных словарей.

    Не Row, а именно dict: словарь можно менять и дополнять, а Row только
    читать. Дальше по коду записи как раз дополняются — например, номером
    в списке клиента.
    """
    rows = connect().execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def query_one(sql, params=()):
    """Первая строка запроса или None, если не нашлось."""
    row = connect().execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def execute(sql, params=()):
    """Выполнить INSERT/UPDATE/DELETE и вернуть число затронутых строк.

    Это число заменяет прежнюю проверку «нашли ли мы такую строку»:
    UPDATE ... WHERE id = ? AND status = 'NEW' либо срабатывает, либо нет,
    и второй раз читать базу для этого не надо.
    """
    return connect().execute(sql, params).rowcount


def insert(sql, params=()):
    """Выполнить INSERT и вернуть номер добавленной строки."""
    return connect().execute(sql, params).lastrowid


@contextmanager
def transaction():
    """Несколько выражений, которые обязаны примениться вместе.

    Нужно там, где сначала читаем, а потом пишем по прочитанному: между
    проверкой «время свободно» и вставкой записи не должен влезть другой
    поток. IMMEDIATE берёт базу на запись сразу, а не на первом UPDATE, —
    иначе двое читающих дошли бы до записи одновременно.
    """
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# =========================================================================
# 4. Состояние диалога
# =========================================================================
# Пока живёт в памяти main.py и теряется при перезапуске бота. Здесь уже
# готовое место, куда его переложить: одна строка на клиента, целиком.

def load_dialog(user_id):
    """Состояние клиента или None, если бот его ещё не видел.

    Пустые колонки в словарь не попадают. В памяти диалог устроен так же:
    ключ появляется, когда клиент дошёл до этого шага. Иначе, например,
    user.get("page", 0) вернул бы None вместо нуля.
    """
    row = query_one("SELECT * FROM dialogs WHERE user_id = ?", (user_id,))
    if row is None:
        return None
    return {field: row[field] for field in DIALOG_FIELDS
            if row[field] is not None}


def save_dialog(user_id, data):
    """Запомнить состояние клиента, заменив прежнее целиком.

    Полей немного и меняются они все вместе, поэтому дописывать по одному
    незачем: INSERT OR REPLACE просто кладёт новую строку вместо старой.
    """
    columns = ["user_id"] + DIALOG_FIELDS + ["seen_date"]
    values = ([user_id]
              + [data.get(field) for field in DIALOG_FIELDS]
              + [date.today().isoformat()])

    execute(
        f"INSERT OR REPLACE INTO dialogs ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})",
        values,
    )


def forget_dialog(user_id):
    """Забыть клиента совсем. True — если было что забывать."""
    return execute("DELETE FROM dialogs WHERE user_id = ?", (user_id,)) > 0


# =========================================================================
# 5. Уборка
# =========================================================================

def cleanup():
    """Удалить то, что уже никому не нужно. Возвращает число строк.

    Именно эта функция, а не устройство схемы, держит размер базы: без неё
    записи копились бы годами. Мастеру история нужна, но не вся: полгода
    прошедших записей — это и «когда клиент был в прошлый раз», и «сколько
    было отмен», а всё, что старше, не открывают никогда.

    Подписки живут до конца своего дня, а диалоги — пока клиент не пропал
    на KEEP_DIALOG_DAYS: незаконченный выбор времени месячной давности
    возвращать смысла нет, клиент всё равно начнёт с меню.
    """
    today = date.today()
    old_bookings = today - timedelta(days=config.KEEP_HISTORY_DAYS)
    old_dialogs = today - timedelta(days=config.KEEP_DIALOG_DAYS)

    removed = execute("DELETE FROM bookings WHERE date < ?",
                      (old_bookings.isoformat(),))
    removed += execute("DELETE FROM subscriptions WHERE date < ?",
                       (today.isoformat(),))
    removed += execute("DELETE FROM dialogs WHERE seen_date < ?",
                       (old_dialogs.isoformat(),))

    if removed:
        # Освободившиеся страницы возвращаем системе. Без этого файл базы
        # остаётся размером с самый толстый свой день.
        connect().execute("PRAGMA incremental_vacuum")

    return removed
