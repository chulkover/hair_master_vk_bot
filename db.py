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

# Версия схемы. Поднимается, когда меняется набор таблиц или колонок.
#   1 — записи, подписки, диалоги;
#   2 — напоминание в день записи (day_reminded), перенос (move_id, MOVED);
#   3 — мастер управляет расписанием: закрытые дни и часы (closures),
#       отмена записи мастером (cancel_reason, CANCELLED_BY_MASTER),
#       рабочий график в базе, а не только в настройках (settings);
#   4 — одна база на несколько мессенджеров: у диалогов, записей и подписок
#       появилась колонка platform («vk», «tg»), и человек опознаётся парой
#       (platform, user_id). Номера у ВК и Telegram свои и могут совпасть,
#       поэтому одного user_id для этого уже мало;
#   5 — связка аккаунтов: contacts (кто вообще писал боту и как его найти)
#       и links (заявки и подтверждённые связи ВК ↔ Telegram).
#
# Механизма миграций в проекте нет: данные учебные, и базу проще удалить.
# Но проверка версии при старте есть — см. connect(): база от старой версии
# не должна молча приехать в новый код.
SCHEMA_VERSION = 5


# =========================================================================
# 1. Схема
# =========================================================================
# Имена колонок совпадают с ключами словарей в коде («date», «start»,
# «status»), поэтому строка из базы — это уже готовая запись, а словарь
# сохраняется без переименований.

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id         INTEGER PRIMARY KEY,   -- он же rowid: номера выдаёт сама база
    platform   TEXT    NOT NULL DEFAULT 'vk',  -- мессенджер клиента: vk, tg
    user_id    INTEGER NOT NULL,      -- номер клиента в этом мессенджере
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
                          'CANCELLED', 'EXPIRED', 'MOVED',
                          'CANCELLED_BY_MASTER')),
    -- Напоминание в день процедуры уже отправлено. Живёт в базе, а не
    -- в памяти планировщика: перезапуск бота не должен приводить к тому,
    -- что клиенту напомнят второй раз.
    day_reminded INTEGER NOT NULL DEFAULT 0,
    -- Почему мастер отменил запись. Пусто у всех остальных: клиент причину
    -- не указывает, а бот при автоотмене и так знает, что случилось.
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS subscriptions (
    platform   TEXT    NOT NULL DEFAULT 'vk',  -- мессенджер клиента: vk, tg
    user_id    INTEGER NOT NULL,      -- номер клиента в этом мессенджере
    date       TEXT    NOT NULL,      -- день, окошка в котором клиент ждёт
    minutes    INTEGER NOT NULL,      -- окно короче не подойдёт
    service    TEXT    NOT NULL,
    length     TEXT    NOT NULL,
    density    TEXT    NOT NULL,
    price_from INTEGER NOT NULL,      -- цена на момент подписки
    price_to   INTEGER NOT NULL,
    PRIMARY KEY (platform, user_id, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS dialogs (
    platform   TEXT    NOT NULL DEFAULT 'vk',  -- мессенджер клиента: vk, tg
    user_id    INTEGER NOT NULL,      -- номер клиента в этом мессенджере
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
    move_id    INTEGER,               -- какую запись переносим
    sub_day    TEXT,                  -- день, на который подписываем
    seen_date  TEXT    NOT NULL,      -- последняя активность, нужна для уборки
    PRIMARY KEY (platform, user_id)
);

-- Когда мастер не принимает. Одной таблицей закрываются четыре разных
-- случая, потому что для расписания это одно и то же — отрезок времени,
-- в который записаться нельзя:
--
--   не работаю 5 августа   since = until = 05.08, часы пустые
--   уеду с 12 до 15        since = until,         часы заданы
--   отпуск на неделю       since < until,         часы пустые
--   пауза до отмены        until далеко впереди,  снимается вручную
--
-- Пустые start/finish означают «весь день целиком». Отдельного признака
-- для этого не нужно: пустое время и есть отсутствие границ.
CREATE TABLE IF NOT EXISTS closures (
    id     INTEGER PRIMARY KEY,
    since  TEXT NOT NULL,             -- 2026-08-05, первый закрытый день
    until  TEXT NOT NULL,             -- последний закрытый день, включительно
    start  TEXT,                      -- 12:00 или пусто
    finish TEXT,                      -- 15:00 или пусто
    reason TEXT NOT NULL              -- что сказать клиентам
);

-- Настройки, которые мастер меняет из переписки: рабочие дни и часы.
-- Ключ-значение, а не колонки: настроек три, меняются они по одной,
-- и заводить ради них таблицу с фиксированным набором полей — значит
-- править схему на каждую следующую.
--
-- Чего здесь нет — берётся из config.py. То есть настройки в коде остаются
-- значениями по умолчанию, а база их лишь перекрывает.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

-- Все, кто когда-либо писал боту, и то, по чему их можно найти.
--
-- Нужна ради связки аккаунтов. Человек присылает «@d_chul» или
-- «vk.com/chuul» — а по такому имени номер аккаунта взять неоткуда:
-- в Telegram по @username чужой id не узнать в принципе, там нет
-- такого метода. Зато имя и @username лежат в каждом входящем
-- сообщении. Поэтому запоминаем их на каждом сообщении: эта таблица
-- и есть ответ на вопрос «а писал ли нам такой человек».
--
-- Отсюда же берётся проверка «сообщение от пользователя не найдено»:
-- нет строки — значит человек боту не писал, и связывать не с чем.
CREATE TABLE IF NOT EXISTS contacts (
    platform  TEXT    NOT NULL,          -- vk, tg
    user_id   INTEGER NOT NULL,          -- номер в этом мессенджере
    name      TEXT    NOT NULL DEFAULT '',  -- «Мария Петрова», для показа
    handle    TEXT    NOT NULL DEFAULT '',  -- username в TG, домен в ВК, без @
    seen_date TEXT    NOT NULL,          -- когда писал в последний раз
    PRIMARY KEY (platform, user_id)
) WITHOUT ROWID;

-- Связки аккаунтов: заявка ждёт ответа (PENDING) или связь подтверждена
-- (CONFIRMED). Одна строка на пару.
--
-- a — кто попросил связать, b — кого спросили. Порядок важен только
-- пока заявка висит: подтверждать её должен именно b, а сообщение
-- «вас хотят связать» видит тоже он. После подтверждения стороны
-- равны, и связь ищется в обе стороны — см. linked_identities().
--
-- Записи, подписки и диалоги эта таблица не трогает: они как лежали
-- со своими platform и user_id, так и лежат. Связка лишь добавляет
-- к вопросу «чьё это» второй аккаунт, поэтому отвязка ничего не портит.
CREATE TABLE IF NOT EXISTS links (
    a_platform   TEXT    NOT NULL,
    a_id         INTEGER NOT NULL,
    b_platform   TEXT    NOT NULL,
    b_id         INTEGER NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('PENDING', 'CONFIRMED')),
    created_date TEXT    NOT NULL,       -- по нему протухает заявка
    PRIMARY KEY (a_platform, a_id, b_platform, b_id)
) WITHOUT ROWID;
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
    "move_id",
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

    problem = version_problem(conn)
    if problem:
        # Отпускаем файл перед остановкой. Боту это уже безразлично, но
        # открытое соединение держит базу занятой, а первое, что человеку
        # предложат с ней сделать, — как раз удалить. На Windows занятый
        # файл не удаляется.
        conn.close()

        # SystemExit, а не исключение: человеку нужен понятный текст,
        # а не трассировка стека — так же, как с отсутствующим токеном.
        raise SystemExit(problem)

    _local.conn = conn
    return conn


def version_problem(conn):
    """Сверить версию схемы в базе с той, которую ждёт код.

    Возвращает текст жалобы или None, если всё в порядке. Не останавливает
    бота сама: решение принимает connect(), которому перед остановкой нужно
    ещё закрыть соединение.

    На свежей базе номер версии — 0: ставим свой и работаем дальше.

    А вот база от старой версии — это остановка. CREATE TABLE IF NOT EXISTS
    видит таблицу с нужным именем и уходит, новых колонок не добавив: код
    продолжил бы работать и спотыкался бы на каждом запросе, где новая
    колонка упоминается. Молчаливая поломка хуже честного отказа
    запуститься — её замечают через неделю по жалобе клиента.

    Миграций в проекте нет намеренно: данные учебные, базу проще удалить.
    Когда в ней появятся настоящие клиенты, вместо этой остановки должен
    появиться перенос данных со старой схемы на новую.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    if version == 0:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return None

    if version == SCHEMA_VERSION:
        return None

    return (f"База {DB_FILE} сделана версией схемы {version}, "
            f"а код ждёт {SCHEMA_VERSION}.\n"
            "Переноса данных между версиями в боте нет.\n"
            "Если записи в базе учебные — удалите файл базы, "
            "бот создаст её заново.")


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
# Шаг, на котором стоит клиент, и всё, что он успел выбрать, — одна строка
# на клиента, целиком. Рабочая копия живёт в памяти main.py (словарь users),
# сюда она попадает явным save_user(), поэтому перезапуск бота не сбивает
# клиента с его шага.

def load_dialog(platform, user_id):
    """Состояние клиента или None, если бот его ещё не видел.

    Клиент опознаётся парой (platform, user_id): в общей базе номер сам по себе
    не уникален. Пустые колонки в словарь не попадают. В памяти диалог устроен
    так же: ключ появляется, когда клиент дошёл до этого шага. Иначе, например,
    user.get("page", 0) вернул бы None вместо нуля.
    """
    row = query_one(
        "SELECT * FROM dialogs WHERE platform = ? AND user_id = ?",
        (platform, user_id),
    )
    if row is None:
        return None
    return {field: row[field] for field in DIALOG_FIELDS
            if row[field] is not None}


def save_dialog(platform, user_id, data):
    """Запомнить состояние клиента, заменив прежнее целиком.

    Полей немного и меняются они все вместе, поэтому дописывать по одному
    незачем: INSERT OR REPLACE просто кладёт новую строку вместо старой.
    """
    columns = ["platform", "user_id"] + DIALOG_FIELDS + ["seen_date"]
    values = ([platform, user_id]
              + [data.get(field) for field in DIALOG_FIELDS]
              + [date.today().isoformat()])

    execute(
        f"INSERT OR REPLACE INTO dialogs ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})",
        values,
    )


def forget_dialog(platform, user_id):
    """Забыть клиента совсем. True — если было что забывать."""
    return execute(
        "DELETE FROM dialogs WHERE platform = ? AND user_id = ?",
        (platform, user_id),
    ) > 0


# =========================================================================
# 4а. Настройки, которые меняет мастер
# =========================================================================
# Всё, что мастер правит из переписки, а не в config.py. Значения хранятся
# строками: их всего три, а разбирать «10:00» или «0,1,2» умеет тот, кому
# они нужны, — см. schedule.py.

def get_setting(key):
    """Значение настройки или None, если мастер её не менял."""
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row is not None else None


def set_setting(key, value):
    """Запомнить настройку, заменив прежнюю."""
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)))


# =========================================================================
# 4б. Кто нам писал и связка аккаунтов
# =========================================================================
# Человек с одним и тем же телефоном может прийти и из ВК, и из Telegram.
# Для бота это два разных клиента: пары (platform, user_id) у них разные,
# и записи у каждого свои. Связка аккаунтов позволяет ему сказать «это тоже
# я» — и увидеть свои записи из любого мессенджера.
#
# Сами записи при этом не переписываются: связка добавляется рядом, отдельной
# строкой. Поэтому отвязка ничего не ломает — всё просто разъезжается обратно.

def save_contact(platform, user_id, name, handle):
    """Запомнить, что этот человек нам писал, и чем его можно найти.

    Зовётся на каждом входящем сообщении. Строка одна на человека
    и просто обновляется — история переписки нам не нужна, нужен ответ
    на вопрос «писал ли он вообще и какой у него номер».

    Пустое имя или handle прежнее значение НЕ затирают: узнать имя не всегда
    удаётся (ВК может не ответить, в Telegram человек мог убрать @username),
    и терять из-за этого то, что уже знаем, незачем.
    """
    execute(
        "INSERT INTO contacts (platform, user_id, name, handle, seen_date) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (platform, user_id) DO UPDATE SET "
        "    name   = CASE WHEN excluded.name   != '' "
        "                  THEN excluded.name   ELSE name   END, "
        "    handle = CASE WHEN excluded.handle != '' "
        "                  THEN excluded.handle ELSE handle END, "
        "    seen_date = excluded.seen_date",
        (platform, user_id, name or "", handle or "", date.today().isoformat()),
    )


def get_contact(platform, user_id):
    """Что мы знаем об этом человеке, или None, если он боту не писал."""
    return query_one(
        "SELECT * FROM contacts WHERE platform = ? AND user_id = ?",
        (platform, user_id),
    )


def find_contact(platform, handle):
    """Найти человека в этом мессенджере по имени вида «d_chul». Или None.

    Ради этого таблица и заведена: по @username или домену ВК спросить номер
    аккаунта у самого мессенджера нельзя, а у себя — можно, если запоминали.

    Регистр не важен: человек напишет «@D_Chul», а сохранено «d_chul».
    lower() в SQLite умеет только латиницу, но имена аккаунтов латиницей
    и пишутся — кириллицы в них не бывает.
    """
    if not handle:
        return None
    return query_one(
        "SELECT * FROM contacts "
        "WHERE platform = ? AND handle != '' AND lower(handle) = lower(?)",
        (platform, handle),
    )


def linked_identities(platform, user_id):
    """Все аккаунты этого человека: он сам плюс связанный, если связь есть.

    Возвращает список пар [(platform, user_id), ...] — всегда хотя бы одну,
    саму себя. Поэтому вызывающему коду не нужно разбирать случай «связи нет»:
    он получит список из одного элемента, и запрос выйдет ровно такой же,
    какой был до всякой связки.

    Ищем в обе стороны: кто кого попросил, после подтверждения уже неважно.
    """
    rows = query(
        "SELECT a_platform, a_id, b_platform, b_id FROM links "
        "WHERE status = 'CONFIRMED' "
        "  AND ((a_platform = ? AND a_id = ?) OR (b_platform = ? AND b_id = ?))",
        (platform, user_id, platform, user_id),
    )

    identities = [(platform, user_id)]
    for row in rows:
        for side in (("a_platform", "a_id"), ("b_platform", "b_id")):
            pair = (row[side[0]], row[side[1]])
            if pair != (platform, user_id):
                identities.append(pair)
    return identities


def link_for(platform, user_id):
    """Связка с участием этого человека — заявка или готовая. Или None.

    Одна связка на аккаунт: пока висит заявка или есть подтверждённая пара,
    вторую заводить нельзя. Иначе пришлось бы разбирать, кто с кем и через
    кого связан, — а для «у меня ВК и Telegram» это лишнее.
    """
    return query_one(
        "SELECT * FROM links "
        "WHERE (a_platform = ? AND a_id = ?) OR (b_platform = ? AND b_id = ?)",
        (platform, user_id, platform, user_id),
    )


def pending_for(platform, user_id):
    """Заявка, которую ждут именно от этого человека. Или None.

    Только сторона b: подтверждает тот, кого спросили, а не тот, кто попросил.
    """
    return query_one(
        "SELECT * FROM links "
        "WHERE b_platform = ? AND b_id = ? AND status = 'PENDING'",
        (platform, user_id),
    )


def add_link_request(a_platform, a_id, b_platform, b_id):
    """Завести заявку «a хочет связать с собой b». Ждёт подтверждения b."""
    execute(
        "INSERT OR REPLACE INTO links "
        "(a_platform, a_id, b_platform, b_id, status, created_date) "
        "VALUES (?, ?, ?, ?, 'PENDING', ?)",
        (a_platform, a_id, b_platform, b_id, date.today().isoformat()),
    )


def confirm_link(a_platform, a_id, b_platform, b_id):
    """Подтвердить заявку. True — если было что подтверждать.

    Статус в условии не для красоты: подтвердить можно только висящую заявку.
    Нажатие «Да» вторым разом уже ничего не меняет и вернёт False.
    """
    return execute(
        "UPDATE links SET status = 'CONFIRMED' "
        "WHERE a_platform = ? AND a_id = ? AND b_platform = ? AND b_id = ? "
        "  AND status = 'PENDING'",
        (a_platform, a_id, b_platform, b_id),
    ) > 0


def drop_links(platform, user_id):
    """Убрать связку этого человека — отказ от заявки или отвязка. Сколько убрали.

    Одной функцией, потому что для базы это одно и то же: строки больше нет.
    Записи и подписки остаются на своих аккаунтах — их связка не трогала.
    """
    return execute(
        "DELETE FROM links "
        "WHERE (a_platform = ? AND a_id = ?) OR (b_platform = ? AND b_id = ?)",
        (platform, user_id, platform, user_id),
    )


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

    Из связок убираются только заявки, на которые не ответили: подтверждённая
    связка живёт, пока человек сам её не снимет. Контакты не трогаем вовсе —
    строка на человека весит десятки байт, а без неё перестанет находиться
    тот, кто писал боту давно, и связать аккаунты станет нельзя.
    """
    today = date.today()
    old_bookings = today - timedelta(days=config.KEEP_HISTORY_DAYS)
    old_dialogs = today - timedelta(days=config.KEEP_DIALOG_DAYS)
    old_requests = today - timedelta(days=config.KEEP_LINK_REQUEST_DAYS)

    removed = execute("DELETE FROM bookings WHERE date < ?",
                      (old_bookings.isoformat(),))
    removed += execute("DELETE FROM subscriptions WHERE date < ?",
                       (today.isoformat(),))
    removed += execute("DELETE FROM dialogs WHERE seen_date < ?",
                       (old_dialogs.isoformat(),))
    removed += execute("DELETE FROM links WHERE status = 'PENDING' "
                       "AND created_date < ?",
                       (old_requests.isoformat(),))

    if removed:
        # Освободившиеся страницы возвращаем системе. Без этого файл базы
        # остаётся размером с самый толстый свой день.
        connect().execute("PRAGMA incremental_vacuum")

    return removed
