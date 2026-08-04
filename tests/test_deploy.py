"""Проверка того, что бот готов к запуску не на своём компьютере.

Пять вещей: файл настроек config.cfg, ключ доступа из переменной окружения,
путь к базе из переменной, часовой пояс мастера вместо UTC и уборка базы
раз в сутки.

config.py проверяем в отдельных процессах: он читает окружение и файлы при
импорте, поэтому подменять их надо ДО запуска Python. Копию config.py кладём
в свой каталог — иначе рядом всегда лежал бы живой token.txt и настоящий
config.cfg, и мы бы проверяли не то, что думаем.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent

# Каталог с кодом бота — на уровень выше этого файла. Путь вычисляем от
# самого файла, а не от рабочего каталога: тесты запускают и из своей папки,
# и из корня проекта. Переменной окружения VK_BOT_DIR его можно задать вручную —
# например, когда код скопирован в другое место.
PROJECT = Path(os.environ.get("VK_BOT_DIR")
               or Path(__file__).resolve().parents[1])

# Тот же интерпретатор, которым запущен сам тест: путь к venv на разных
# системах разный, а окружение нам нужно ровно то, где стоит vk_api.
PYTHON = sys.executable
STUB = HERE / "stub"
TEST_DB = HERE / "test_deploy.db"

for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)

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


def run(code, env=None, cwd=None):
    """Запустить код в отдельном процессе с чистым окружением."""
    environment = {"PATH": os.environ["PATH"]}
    environment.update(env or {})
    return subprocess.run([PYTHON, "-c", code], env=environment, cwd=cwd,
                          capture_output=True, text=True)


# --- 1. Файл настроек -----------------------------------------------------
print("\n1. Файл настроек")

# В config.cfg лежит то, что своё у каждой установки: адрес, телефон, VK ID
# мастера и имя файла с ключом доступа. Проверяем две вещи: значения
# действительно берутся оттуда, а забытая или испорченная строка
# останавливает бота с внятным текстом, а не роняет его трассировкой стека
# посреди рабочего дня.

SAMPLE = """
[community]
token_file = token.txt
owner_id = 12345

[salon]
address = ул. Тестовая, 1
phone = +7 000 000-00-00
"""


def put_settings(folder, text):
    """Положить рядом с копией config.py свой файл настроек."""
    (Path(folder) / "config.cfg").write_text(text, encoding="utf-8")


def instead(old, new):
    """Тот же образец настроек, но с одной изменённой строкой."""
    assert old in SAMPLE, old
    return SAMPLE.replace(old, new)


ASK_ALL = ("import config; print(config.OWNER_ID); print(config.ADDRESS); "
           "print(config.PHONE); print(config.TOKEN_FILE.name)")

with tempfile.TemporaryDirectory() as folder:
    shutil.copy(PROJECT / "config.py", folder)
    (Path(folder) / "token.txt").write_text("ключ", encoding="utf-8")
    put_settings(folder, SAMPLE)

    result = run(ASK_ALL, cwd=folder)
    check("значения берутся из config.cfg",
          result.stdout.splitlines() == ["12345", "ул. Тестовая, 1",
                                         "+7 000 000-00-00", "token.txt"],
          f"{result.stdout!r} {result.stderr[-200:]!r}")

    # Имя файла с ключом тоже настройка: положили ключ в другой файл —
    # бот должен взять его оттуда.
    (Path(folder) / "ключ.txt").write_text("другой-ключ", encoding="utf-8")
    put_settings(folder, instead("token_file = token.txt",
                                 "token_file = ключ.txt"))
    result = run("import config; print(config.VK_TOKEN)", cwd=folder)
    check("ключ читается из файла, названного в настройках",
          result.stdout.strip() == "другой-ключ",
          f"{result.stdout!r} {result.stderr[-200:]!r}")

    # Пустой owner_id — это «мастера нет», а не опечатка: так и оставляют
    # в чужой копии проекта, чтобы бот никому не писал.
    put_settings(folder, instead("owner_id = 12345", "owner_id ="))
    result = run("import config; print(config.OWNER_ID)", cwd=folder)
    check("пустой owner_id — это ноль", result.stdout.strip() == "0",
          f"{result.stdout!r} {result.stderr[-200:]!r}")

    # А вот «двести три» вместо номера — опечатка, и молчать о ней нельзя:
    # бот просто не писал бы мастеру, и никто бы не понял почему.
    put_settings(folder, instead("owner_id = 12345", "owner_id = я"))
    result = run("import config", cwd=folder)
    check("нечисловой owner_id останавливает бота", result.returncode != 0)
    check("в тексте названы и строка, и раздел",
          "owner_id" in result.stderr and "community" in result.stderr,
          result.stderr[-200:])

    # Забытая строка.
    put_settings(folder, instead("phone = +7 000 000-00-00", ""))
    result = run("import config", cwd=folder)
    check("без строки phone бот не запускается", result.returncode != 0)
    check("сказано, чего не хватает",
          "phone" in result.stderr and "salon" in result.stderr,
          result.stderr[-200:])
    check("это не трассировка стека", "Traceback" not in result.stderr,
          result.stderr[-200:])

    # Строка есть, но пустая — «собирался заполнить и забыл».
    put_settings(folder, instead("address = ул. Тестовая, 1", "address ="))
    result = run("import config", cwd=folder)
    check("пустой адрес останавливает бота", result.returncode != 0)
    check("сказано, какая строка пустая", "address" in result.stderr,
          result.stderr[-200:])

    # Решётка в середине строки — обычный символ, а не начало комментария.
    put_settings(folder, instead("ул. Тестовая, 1", "ул. Тестовая, 1 # 2"))
    result = run("import config; print(config.ADDRESS)", cwd=folder)
    check("решётка внутри значения остаётся в тексте",
          result.stdout.strip() == "ул. Тестовая, 1 # 2", repr(result.stdout))

    # Процент — тоже: подстановку значений мы выключили.
    put_settings(folder, instead("ул. Тестовая, 1", "Тестовая, 1, скидка 50%"))
    result = run("import config; print(config.ADDRESS)", cwd=folder)
    check("процент внутри значения не ломает разбор",
          result.stdout.strip().endswith("скидка 50%"), repr(result.stdout))

    # Испорченный файл: строка не в разделе.
    put_settings(folder, "адрес рядом с домом")
    result = run("import config", cwd=folder)
    check("непонятный файл настроек — понятная ошибка",
          result.returncode != 0 and "Traceback" not in result.stderr,
          result.stderr[-200:])

with tempfile.TemporaryDirectory() as folder:
    shutil.copy(PROJECT / "config.py", folder)  # config.cfg не создаём

    result = run("import config", cwd=folder)
    print(f"     сообщение: {result.stderr.strip()[:80]!r}")
    check("без config.cfg бот не запускается", result.returncode != 0)
    check("в тексте названо имя файла", "config.cfg" in result.stderr,
          result.stderr[-200:])
    check("это не трассировка стека", "Traceback" not in result.stderr,
          result.stderr[-200:])

# Живой файл настроек: если в нём нет нужных строк, бот не поднимется
# ни на чьём компьютере.
LIVE_SETTINGS = (PROJECT / "config.cfg").read_text(encoding="utf-8")
check("в живом config.cfg есть все строки",
      all(name in LIVE_SETTINGS
          for name in ("[community]", "token_file", "owner_id",
                       "[salon]", "address", "phone")))


# --- 2. Ключ доступа ------------------------------------------------------
print("\n2. Ключ доступа сообщества")

ASK = "import config; print(config.VK_TOKEN)"

with tempfile.TemporaryDirectory() as folder:
    shutil.copy(PROJECT / "config.py", folder)
    put_settings(folder, SAMPLE)
    (Path(folder) / "token.txt").write_text("ключ-из-файла\n", encoding="utf-8")

    result = run(ASK, cwd=folder)
    check("без переменной берётся token.txt",
          result.stdout.strip() == "ключ-из-файла",
          f"{result.stdout!r} {result.stderr[-200:]!r}")

    result = run(ASK, env={"VK_TOKEN": "ключ-из-переменной"}, cwd=folder)
    check("переменная важнее файла",
          result.stdout.strip() == "ключ-из-переменной", repr(result.stdout))

    result = run(ASK, env={"VK_TOKEN": "  ключ-с-пробелами  "}, cwd=folder)
    check("пробелы вокруг ключа обрезаются",
          result.stdout.strip() == "ключ-с-пробелами", repr(result.stdout))

    # Пустая переменная — это «не задана», а не «ключ из пустой строки».
    result = run(ASK, env={"VK_TOKEN": "   "}, cwd=folder)
    check("пустая переменная не перебивает файл",
          result.stdout.strip() == "ключ-из-файла", repr(result.stdout))

with tempfile.TemporaryDirectory() as folder:
    shutil.copy(PROJECT / "config.py", folder)  # token.txt не создаём
    put_settings(folder, SAMPLE)

    result = run(ASK, cwd=folder)
    print(f"     сообщение: {result.stderr.strip()[:80]!r}")
    check("без ключа вообще — понятная ошибка", result.returncode != 0)
    check("в тексте написано, что делать",
          "VK_TOKEN" in result.stderr and "token.txt" in result.stderr)
    check("это не трассировка стека", "Traceback" not in result.stderr,
          result.stderr[-200:])


# --- 3. Путь к базе -------------------------------------------------------
print("\n3. Путь к базе")

ASK_DB = "import config; print(config.DB_FILE)"

with tempfile.TemporaryDirectory() as folder:
    shutil.copy(PROJECT / "config.py", folder)
    put_settings(folder, SAMPLE)
    (Path(folder) / "token.txt").write_text("ключ", encoding="utf-8")

    result = run(ASK_DB, cwd=folder)
    check("по умолчанию база рядом с кодом",
          result.stdout.strip() == str(Path(folder) / "bot.db"),
          repr(result.stdout))

    # Сравниваем со str(Path(...)), а не с самой строкой: на Windows тот же
    # путь печатается через обратные слэши, и дело тут не в config.py.
    result = run(ASK_DB, env={"DB_FILE": "/data/bot.db"}, cwd=folder)
    check("переменная задаёт путь",
          result.stdout.strip() == str(Path("/data/bot.db")),
          repr(result.stdout))

    # Путь должен быть Path, а не строкой: db.py и тесты зовут .exists().
    # Имя класса зависит от системы — PosixPath или WindowsPath.
    result = run("import config; print(type(config.DB_FILE).__name__)",
                 env={"DB_FILE": "/data/bot.db"}, cwd=folder)
    check("путь остаётся Path",
          result.stdout.strip() == type(Path()).__name__,
          repr(result.stdout))


# --- 4. Часовой пояс ------------------------------------------------------
print("\n4. Часовой пояс")

# Изображаем хостинг: контейнер по UTC. main.py при старте должен перевести
# процесс в пояс мастера, иначе всё расписание уедет на три часа.
TZ_CHECK = f"""
import sys
sys.path.insert(0, {str(STUB)!r})
sys.path.insert(0, {str(PROJECT)!r})
import db
db.DB_FILE = {str(TEST_DB)!r}
import main
import time
print(time.tzname[0], -time.timezone // 60)
"""

# tzset() есть только на Unix, а хостинг у нас как раз Unix. Под Windows
# проверять нечего: там процесс живёт в системном поясе, и set_timezone()
# честно ничего не делает — на компьютере мастера пояс и так правильный.
if not hasattr(time, "tzset"):
    print("     пропускаю: tzset() есть только на Unix, "
          "под Windows пояс берётся системный")
else:
    result = run(TZ_CHECK, env={"TZ": "UTC", "VK_TOKEN": "ключ",
                                "DB_FILE": str(TEST_DB)})
    print(f"     процесс сообщил: {result.stdout.strip().splitlines()[-1:]}")
    last = (result.stdout.strip().splitlines()[-1]
            if result.stdout.strip() else "")
    check("пояс переключился на московский", last.startswith("MSK"),
          f"{last!r} {result.stderr[-300:]!r}")
    check("смещение +180 минут", last.endswith("180"), repr(last))

    # Без нашей настройки процесс остался бы в UTC — проверяем, что заглушка
    # окружения работает и мы действительно измеряем эффект от set_timezone().
    result = run("import time; print(time.tzname[0], -time.timezone // 60)",
                 env={"TZ": "UTC"})
    check("в UTC-окружении без main пояс остаётся UTC",
          result.stdout.strip() == "UTC 0", repr(result.stdout))


# --- 5. Уборка раз в сутки ------------------------------------------------
print("\n5. Уборка базы")

sys.path.insert(0, str(STUB))
sys.path.insert(0, str(PROJECT))

import vk_api  # заглушка

import config
import db

db.DB_FILE = TEST_DB
assert db.DB_FILE != config.DB_FILE, "база теста должна быть отдельной!"

LIVE_STAMP = (config.DB_FILE.stat().st_mtime if config.DB_FILE.exists()
              else None)

import schedule
import main

calls = []
real_cleanup = db.cleanup


def counting_cleanup():
    calls.append(date.today())
    return real_cleanup()


db.cleanup = counting_cleanup

main.last_cleanup = None
main.scheduler_tick()
check("первый проход планировщика убирается", len(calls) == 1, str(calls))
check("день уборки запомнен", main.last_cleanup == date.today())

main.scheduler_tick()
main.scheduler_tick()
check("в тот же день второй раз не убирается", len(calls) == 1, str(calls))

# Наступил следующий день.
main.last_cleanup = date.today() - timedelta(days=1)
main.scheduler_tick()
check("на следующий день убирается снова", len(calls) == 2, str(calls))

db.cleanup = real_cleanup

# А теперь настоящая уборка: она должна что-то удалить.
db.save_dialog(777, {"state": "SELECTING_TIME"})
db.execute("UPDATE dialogs SET seen_date = ? WHERE user_id = 777",
           ((date.today() - timedelta(days=config.KEEP_DIALOG_DAYS + 1))
            .isoformat(),))
main.last_cleanup = None
main.cleanup_once_a_day()
check("брошенный диалог удалён настоящей уборкой",
      db.load_dialog(777) is None)


# --- 6. Живые данные ------------------------------------------------------
print("\n6. Живые данные")

check("боевая база не тронута",
      (config.DB_FILE.stat().st_mtime if config.DB_FILE.exists() else None)
      == LIVE_STAMP)
check("живой файл с ключом на месте", config.TOKEN_FILE.exists())
check("живой config.cfg не тронут",
      (PROJECT / "config.cfg").read_text(encoding="utf-8") == LIVE_SETTINGS)

db.close()
print(f"\nИтого: ок {ok}, плохо {fail}")
sys.exit(1 if fail else 0)
