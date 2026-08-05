"""Проверка того, что состояние диалога переживает перезапуск бота.

main.py импортируется целиком, но vk_api подменён заглушкой из stub/: ни одно
сообщение во ВКонтакте не уходит, longpoll сразу пустой, и главный цикл
заканчивается на импорте. База подменяется ДО первого обращения — живая
bot.db не трогается.

«Перезапуск» изображаем так же, как это выглядит на самом деле: память процесса
(main.users) чистая, соединение с базой новое, а данные на месте.
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent

# Каталог с кодом бота — на уровень выше этого файла. Путь вычисляем от
# самого файла, а не от рабочего каталога: тесты запускают и из своей папки,
# и из корня проекта. Переменной окружения VK_BOT_DIR его можно задать вручную —
# например, когда код скопирован в другое место.
PROJECT = Path(os.environ.get("VK_BOT_DIR")
               or Path(__file__).resolve().parents[1])

sys.path.insert(0, str(HERE / "stub"))   # заглушка vk_api — раньше настоящего
sys.path.insert(0, str(PROJECT))

TEST_DB = HERE / "test_restart.db"
for suffix in ("", "-wal", "-shm"):
    Path(str(TEST_DB) + suffix).unlink(missing_ok=True)

import vk_api  # это заглушка: vk_api.SENT — список «отправленных» сообщений

import config
import db

db.DB_FILE = TEST_DB
assert db.DB_FILE != config.DB_FILE, "база теста должна быть отдельной!"
assert vk_api.__file__.startswith(str(HERE)), "vk_api должен быть заглушкой!"

LIVE_STAMP = (config.DB_FILE.stat().st_mtime if config.DB_FILE.exists()
              else None)

import schedule
import main

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


CLIENT = 555


def who(user_id):
    """Клиент как main.Client(platform, id) — так его ждёт диалог.

    Номер (CLIENT, OWNER) оставляем числом: он нужен и сырым — для запросов
    к базе и проверок vk_api.SENT. А в main.* уходит уже парой, где платформа
    у нас всегда «vk».
    """
    return main.Client("vk", user_id)


def msg(text):
    """Сообщение от клиента — ровно так, как его обрабатывает главный цикл."""
    vk_api.SENT.clear()
    main.handle_message(who(CLIENT),text)
    main.save_user(who(CLIENT))
    return [params["message"] for params in vk_api.SENT]


def restart():
    """Перезапуск бота: память чистая, соединение новое, база на месте."""
    main.users.clear()
    db.close()


def state():
    return main.get_user(who(CLIENT))["state"]


def saved():
    return db.load_dialog("vk", CLIENT)


def work_day(min_shift=3):
    for shift in range(min_shift, min_shift + 8):
        day = date.today() + timedelta(days=shift)
        if day.weekday() in config.WORK_DAYS:
            return day.isoformat()
    raise RuntimeError("не нашёлся рабочий день")


# --- 1. Главное меню строку не занимает -----------------------------------
print("\n1. Главное меню")

msg("привет")
check("клиент в меню", state() == main.MAIN_MENU)
check("в базе строки нет", saved() is None, str(saved()))
check("сохранение незнакомого клиента не падает",
      main.save_user(who(999)) is None)
check("и строки ему не создаёт", db.load_dialog("vk", 999) is None)


# --- 2. Каждый шаг оказывается в базе -------------------------------------
print("\n2. Шаги диалога сохраняются")

msg("записаться")
check("выбор процедуры сохранён", saved() == {"state": "SELECTING_SERVICE"},
      str(saved()))

msg("кератин")
check("процедура запомнилась", saved()["service"] == "keratin", str(saved()))
check("шаг — выбор длины", saved()["state"] == "SELECTING_LENGTH")

msg("длинные")
check("длина запомнилась", saved()["length"] == "long")

row = msg("густые") and saved()
print(f"     {row}")
check("густота запомнилась", row["density"] == "thick")
check("длительность посчитана и сохранена", row["minutes"] > 0, str(row))
check("цена сохранена числами",
      isinstance(row["price_from"], int) and isinstance(row["price_to"], int))
check("шаг — цена рассчитана", row["state"] == "PRICE_CALCULATED")

msg("записаться")
check("шаг — выбор дня", saved()["state"] == "SELECTING_DATE")


# --- 3. Перезапуск посреди выбора дня -------------------------------------
print("\n3. Перезапуск на выборе дня")

restart()
check("память пуста", main.users == {})
check("шаг вернулся из базы", state() == "SELECTING_DATE")
check("параметры процедуры вернулись",
      main.get_user(who(CLIENT))["service"] == "keratin")

DAY = work_day()
minutes = main.get_user(who(CLIENT))["minutes"]

answers = msg(schedule.day_label(DAY).lower())
print(f"     ответ бота: {answers[0][:50]!r}")
check("бот понял день, а не начал заново", state() == "SELECTING_TIME",
      f"{state()} / {answers}")
check("день сохранён", saved()["day"] == DAY, str(saved()))
check("страница сохранена числом", saved()["page"] == 0)


# --- 4. Перезапуск посреди выбора времени ---------------------------------
print("\n4. Перезапуск на выборе времени")

restart()
check("шаг вернулся", state() == "SELECTING_TIME")
check("день вернулся", main.get_user(who(CLIENT))["day"] == DAY)

slot = schedule.free_slots(DAY, minutes)[0]
msg(slot)
check("бот принял время", state() == "CONFIRMING", str(saved()))
check("время сохранено", saved()["time"] == slot, str(saved()))


# --- 5. Перезапуск перед подтверждением -----------------------------------
print("\n5. Перезапуск перед подтверждением")

restart()
check("шаг вернулся", state() == "CONFIRMING")

before = len(schedule.user_bookings("vk", CLIENT))
answers = msg("подтвердить запись")
print(f"     ответ бота: {answers[0][:50]!r}")
check("запись создалась", len(schedule.user_bookings("vk", CLIENT)) == before + 1)
check("клиент вернулся в меню", state() == main.MAIN_MENU)
check("строка диалога удалена", saved() is None, str(saved()))

restart()
check("после перезапуска клиент в меню", state() == main.MAIN_MENU)


# --- 6. Устаревшее состояние не восстанавливаем ---------------------------
print("\n6. Устаревший день")

YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
PROCEDURE = {"service": "cold", "length": "short", "density": "thin",
             "minutes": 90, "price_from": 2100, "price_to": 2400}

db.save_dialog("vk", CLIENT, {"state": "SELECTING_TIME", "day": YESTERDAY,
                        "page": 0, **PROCEDURE})
restart()
check("вчерашний день не поднимаем — в меню", state() == main.MAIN_MENU)
check("мусор из состояния не притащился",
      main.get_user(who(CLIENT)) == {"state": main.MAIN_MENU},
      str(main.get_user(who(CLIENT))))

db.save_dialog("vk", CLIENT, {"state": "SUB_CONFIRM", "sub_day": YESTERDAY,
                        **PROCEDURE})
restart()
check("устаревший день подписки — тоже в меню", state() == main.MAIN_MENU)

db.save_dialog("vk", CLIENT, {"state": "SELECTING_TIME", "day": work_day(),
                        "page": 1, **PROCEDURE})
restart()
check("будущий день поднимается как есть", state() == "SELECTING_TIME")
check("страница поднялась числом", main.get_user(who(CLIENT))["page"] == 1)


# --- 7. Состояние, выставленное фоновым потоком ---------------------------
print("\n7. Уведомление о свободном окошке")

main.users.pop(who(CLIENT), None)
db.forget_dialog("vk", CLIENT)

subscription = {"user_id": CLIENT, "date": DAY, **PROCEDURE}

vk_api.SENT.clear()
main.offer_free_slot(who(CLIENT), subscription)
print(f"     сообщений клиенту: {len(vk_api.SENT)}")

row = saved()
print(f"     {row}")
check("состояние от фонового потока сохранено", row is not None)
check("клиент на выборе времени", row and row["state"] == "SELECTING_TIME")
check("день из подписки", row and row["day"] == DAY)
check("параметры процедуры из подписки",
      row and (row["service"], row["minutes"], row["price_to"])
      == ("cold", 90, 2400), str(row))

restart()
check("после перезапуска шаг на месте", state() == "SELECTING_TIME")


# --- 8. Уборка брошенных диалогов ----------------------------------------
print("\n8. Уборка")

check("дата активности заполнена сегодняшним днём",
      db.query_one("SELECT seen_date FROM dialogs WHERE user_id = ?",
                   (CLIENT,))["seen_date"] == date.today().isoformat())

db.execute("UPDATE dialogs SET seen_date = ? WHERE user_id = ?",
           ((date.today() - timedelta(days=config.KEEP_DIALOG_DAYS + 1))
            .isoformat(), CLIENT))
check("брошенный диалог уборка убирает", db.cleanup() == 1)
check("и в базе его больше нет", saved() is None)

restart()
check("после уборки клиент начинает с меню", state() == main.MAIN_MENU)


# --- 9. Кнопки из старых сообщений ----------------------------------------
# Сообщения в переписке живут вечно, и клиент может нажать кнопку из
# позавчерашнего. Бот обязан её понять на любом шаге — и уж точно не молчать.
print("\n9. Кнопки из старых сообщений")

msg("привет")
msg("записаться")
msg("кератин")
answers = msg("записаться")   # кнопка из старого сообщения с меню
check("«Записаться» понята посреди выбора", state() == "SELECTING_SERVICE",
      f"{state()} / {answers}")

msg("кератин")
msg("длинные")
msg("густые")
answers = msg("записаться")
check("после расчёта цены ведёт сразу к дню", state() == "SELECTING_DATE",
      f"{state()} / {answers}")

msg("мои записи")
check("клиент на списке записей", state() == "MY_BOOKINGS", state())
answers = msg("записаться")
check("«Записаться» работает и из списка записей",
      state() == "SELECTING_SERVICE", f"{state()} / {answers}")

# Шаг, которого в коде больше нет: так выглядит строка, оставшаяся в базе
# от прошлой версии бота.
db.save_dialog("vk", CLIENT, {"state": "ШАГ_ИЗ_ПРОШЛОЙ_ВЕРСИИ"})
restart()
answers = msg("ага")
check("на непонятном шаге бот не молчит", answers != [], str(answers))
check("и возвращает клиента в меню", state() == main.MAIN_MENU, state())


# --- 10. Подсказки к длине и густоте --------------------------------------
print("\n10. Подсказки на экранах выбора")

main.users.clear()
db.forget_dialog("vk", CLIENT)

msg("записаться")
answers = msg("кератин")
check("на экране длины есть расшифровка", "до плеч" in " ".join(answers),
      str(answers))

answers = msg("длиные")   # опечатка
check("опечатка не выкидывает из шага", state() == "SELECTING_LENGTH", state())
check("и подсказки повторяются", "до плеч" in " ".join(answers), str(answers))

answers = msg("длинные")
check("на экране густоты своя расшифровка",
      "кожа головы" in " ".join(answers), str(answers))

check("вариант без подсказки просто пропускается",
      main.hints_text({"a": {"title": "Без", "price_k": 1}}) == "")


# --- 11. Непонятное сообщение в меню --------------------------------------
print("\n11. Непонятное сообщение в главном меню")

msg("в меню")
answers = msg("Здравствуйте, можно записаться на кератин?")
check("бот здоровается, а не «не понял»",
      "Привет" in " ".join(answers), str(answers))
check("адрес в приветствии есть", config.ADDRESS in " ".join(answers))
check("клиент остался в меню", state() == main.MAIN_MENU, state())

answers = msg("спасибо")
check("на второе непонятное отвечает так же",
      "Привет" in " ".join(answers), str(answers))


# --- 12. Расписание владельца ---------------------------------------------
print("\n12. Расписание владельца")

# Владельца задаём сами, а не берём из config.cfg: там он свой у каждой
# установки, а в опубликованном файле — ноль, и кабинет не открылся бы вовсе.
# Проверяем мы поведение бота, а не то, чей номер вписан в настройки.
OWNER = 900
config.OWNER_ID = OWNER


def owner(text):
    """Сообщение от владельца — с тем же разбором, что и у клиента."""
    vk_api.SENT.clear()
    main.handle_message(who(OWNER),text)
    main.save_user(who(OWNER))
    return " | ".join(params["message"] for params in vk_api.SENT)


def owner_state():
    return main.get_user(who(OWNER))["state"]


vk_api.SENT.clear()
main.show_menu(who(CLIENT))
check("клиенту кнопку кабинета не показывают",
      main.CABINET_BUTTON not in vk_api.SENT[-1]["keyboard"])

vk_api.SENT.clear()
main.show_menu(who(OWNER))
check("владельцу показывают",
      main.CABINET_BUTTON in vk_api.SENT[-1]["keyboard"])

check("клиент владельцем не считается", main.is_owner(who(CLIENT)) is False)
check("владелец считается", main.is_owner(who(OWNER)) is True)

vk_api.SENT.clear()
main.handle_message(who(CLIENT),"кабинет")
check("клиенту слово «кабинет» ничего не открывает",
      main.get_user(who(CLIENT))["state"] == main.MAIN_MENU)

DAY = work_day(4)
schedule.create_booking("vk", CLIENT,DAY, "10:00", 90, "cold", "short", "thin",
                        2100, 2400)

answer = owner("кабинет")
check("кабинет открылся", "Кабинет мастера" in answer, answer)
check("владелец на шаге кабинета", owner_state() == main.OWNER_CABINET)

answer = owner(main.SCHEDULE_BUTTON)
check("показан список дней", "дни с записями" in answer, answer)
check("владелец на шаге расписания", owner_state() == main.OWNER_SCHEDULE)

answer = owner(schedule.day_label(DAY))
check("день раскрылся по часам", "10:00" in answer, answer)
check("с ссылкой на клиента", f"vk.com/id{CLIENT}" in answer)

# Шаг владельца остался в базе, а владельцем этот номер быть перестал.
main.get_user(who(CLIENT))["state"] = main.OWNER_SCHEDULE
main.handle_message(who(CLIENT),schedule.day_label(DAY))
check("чужого с шага кабинета уводит в меню",
      main.get_user(who(CLIENT))["state"] == main.MAIN_MENU)


# --- 13. Уведомления владельцу --------------------------------------------
print("\n13. Уведомления владельцу")

vk_api.PEOPLE[CLIENT] = ("Мария", "Петрова")

vk_api.SENT.clear()
main.notify_owner("проверка")
to_owner = [params for params in vk_api.SENT
            if params["user_id"] == OWNER]
check("владельцу написали", len(to_owner) == 1, str(vk_api.SENT))
check("без клавиатуры", "keyboard" not in to_owner[0])

check("имя клиента подставилось",
      main.client_name(who(CLIENT)) == "Мария Петрова", main.client_name(who(CLIENT)))
check("незнакомый номер не роняет карточку",
      main.client_card(who(404)) == "Клиент: vk.com/id404",
      main.client_card(who(404)))

saved_owner = config.OWNER_ID
config.OWNER_ID = 0
vk_api.SENT.clear()
main.notify_owner("этого никто не увидит")
check("при OWNER_ID = 0 бот молчит", vk_api.SENT == [], str(vk_api.SENT))
config.OWNER_ID = saved_owner


# --- 14. Перенос записи переживает перезапуск -----------------------------
print("\n14. Перенос записи")

main.users.clear()
db.forget_dialog("vk", CLIENT)

# Записи от прошлых разделов убираем: перенос проверяем на одной, иначе
# «нажмите номер 1» выберет неизвестно какую из накопившихся.
for old in schedule.user_bookings("vk", CLIENT):
    schedule.cancel_booking(old["id"], "vk", CLIENT)

MOVE_FROM = work_day(4)
moving = schedule.create_booking("vk", CLIENT,MOVE_FROM, "10:00", 90, "cold",
                                 "short", "thin", 2100, 2400)

msg("мои записи")
msg("1")
answers = msg(main.MOVE_BOOKING)
check("после «Перенести» спрашивают день",
      "Выберите день" in " ".join(answers), str(answers))
check("перенос попал в базу", saved()["move_id"] is not None, str(saved()))

restart()
check("после перезапуска перенос помнится",
      main.get_user(who(CLIENT)).get("move_id") is not None)

NEW_DAY = work_day(6)
msg(schedule.day_label(NEW_DAY))
msg("12:00")
answers = msg("подтвердить запись")
check("сказано, что перенесла", "Перенесла запись" in " ".join(answers),
      str(answers))
check("активная запись одна и на новом дне",
      [(b["date"], b["start"]) for b in schedule.user_bookings("vk", CLIENT)]
      == [(NEW_DAY, "12:00")], str(schedule.user_bookings("vk", CLIENT)))
check("старая помечена перенесённой",
      schedule.get_booking(moving["id"])["status"] == "MOVED",
      schedule.get_booking(moving["id"])["status"])
check("старое время снова свободно",
      "10:00" in schedule.free_slots(MOVE_FROM, 90))
check("признак переноса забыт", "move_id" not in main.get_user(who(CLIENT)))


# --- 15. Кабинет мастера --------------------------------------------------
print("\n15. Кабинет мастера")

for old in schedule.user_bookings("vk", CLIENT):
    schedule.cancel_booking(old["id"], "vk", CLIENT)
db.execute("DELETE FROM closures")
main.users.clear()

vk_api.PEOPLE[CLIENT] = ("Мария", "Петрова")
CAB_DAY = work_day(4)
victim = schedule.create_booking("vk", CLIENT,CAB_DAY, "10:00", 90, "cold",
                                 "short", "thin", 2100, 2400)

# Отмена записи мастером: причина должна дойти до клиента как написана.
owner("кабинет")
owner(main.SCHEDULE_BUTTON)
owner(schedule.day_label(CAB_DAY))
answer = owner("1")
check("мастера спрашивают причину", "причину" in answer, answer)

vk_api.SENT.clear()
main.handle_message(who(OWNER),"Заболела, простите")
to_client = [params["message"] for params in vk_api.SENT
             if params["user_id"] == CLIENT]
check("клиент получил сообщение", to_client != [], str(to_client))
check("причина не потеряла регистр",
      "Заболела, простите" in " ".join(to_client), str(to_client))
check("статус записи — отмена мастером",
      schedule.get_booking(victim["id"])["status"] == "CANCELLED_BY_MASTER")
check("причина сохранена в записи",
      schedule.get_booking(victim["id"])["cancel_reason"]
      == "Заболела, простите")

# Закрытие дня с отменой того, что в нём есть.
CLOSE_DAY = work_day(6)
schedule.create_booking("vk", CLIENT,CLOSE_DAY, "10:00", 90, "cold", "short",
                        "thin", 2100, 2400)
owner("кабинет")
owner(main.CLOSE_BUTTON)
owner(main.CLOSE_WHOLE_DAY)
owner(schedule.day_label(CLOSE_DAY))
answer = owner("Уезжаю")
check("показано, скольких заденет", "1 запись" in answer, answer)

vk_api.SENT.clear()
main.handle_message(who(OWNER),main.CLOSE_YES)
check("клиенту сообщили о закрытии",
      any(params["user_id"] == CLIENT for params in vk_api.SENT))
check("день закрыт", schedule.closed_all_day(CLOSE_DAY))
check("записаться в него нельзя", CLOSE_DAY not in schedule.work_days(90))

# Черновик закрытия живёт только в памяти — перезапуск его теряет,
# и бот обязан это пережить, а не упасть.
owner("кабинет")
owner(main.CLOSE_BUTTON)
owner(main.CLOSE_WHOLE_DAY)
restart()
answer = owner(schedule.day_label(work_day(8)))
check("перезапуск посреди закрытия возвращает в кабинет",
      "Кабинет мастера" in answer, answer)
check("и ничего лишнего не закрылось",
      len(schedule.all_closures()) == 1, str(schedule.all_closures()))

owner(main.CLOSURES_BUTTON)
answer = owner("1")
check("закрытие снимается", "Открыла" in answer, answer)
check("день снова открыт", not schedule.closed_all_day(CLOSE_DAY))

# График работы.
owner("кабинет")
owner(main.WORK_BUTTON)
owner(main.WORK_START_BUTTON)
owner("11:00")
check("начало дня сохранилось", schedule.work_start() == "11:00")
owner(main.WORK_END_BUTTON)
answer = owner("09:00")
check("перевёрнутый день отвергнут", "Не получится" in answer, answer)
db.execute("DELETE FROM settings")

# Чужой на шаге кабинета — в меню, и ничего не делает.
main.get_user(who(CLIENT))["state"] = main.OWNER_CLOSE_KIND
main.handle_message(who(CLIENT),main.CLOSE_PAUSE)
check("клиента с шага кабинета уводит в меню",
      main.get_user(who(CLIENT))["state"] == main.MAIN_MENU)
check("и он ничего не закрыл", schedule.all_closures() == [])


# --- 16. Контакт запоминается ---------------------------------------------
print("\n16. Контакт запоминается")

# Без этой отметки не заработает связка аккаунтов: найти человека по
# «vk.com/chuul» можно только у себя — ВК по домену чужой номер не отдаёт.
vk_api.PEOPLE[CLIENT] = ("Мария", "Петрова", "chuul")
db.execute("DELETE FROM contacts")

main.remember_contact(who(CLIENT))
noted = db.get_contact("vk", CLIENT)
check("контакт записан", noted is not None)
check("имя из ВК", noted and noted["name"] == "Мария Петрова")
check("домен из ВК", noted and noted["handle"] == "chuul")
check("человек находится по домену",
      (db.find_contact("vk", "chuul") or {}).get("user_id") == CLIENT)

# Домен известен и день сегодняшний — второй раз ВК не дёргаем.
vk_api.PEOPLE[CLIENT] = ("Мария", "Иванова", "chuul")
main.remember_contact(who(CLIENT))
check("за день повторно ВК не спрашиваем",
      db.get_contact("vk", CLIENT)["name"] == "Мария Петрова")

# У страницы без короткого имени ВК отдаёт «id777» — тоже годная ссылка.
vk_api.PEOPLE[777] = ("Пётр", "Сидоров")
main.remember_contact(who(777))
check("без короткого имени домен как «idN»",
      db.get_contact("vk", 777)["handle"] == "id777")

vk_api.PEOPLE[CLIENT] = ("Мария", "Петрова")


# --- 17. Связка аккаунтов ВК и Telegram -----------------------------------
print("\n17. Связка аккаунтов ВК и Telegram")


class FakeTg:
    """Telegram-канал, который никуда не ходит: копит отправленное.

    Настоящий TgMessenger полез бы в сеть, а нам нужно проверить сам обмен:
    запрос ушёл тому, кого назвали, и подтверждение связало аккаунты.
    """
    platform = "tg"

    def __init__(self):
        self.sent = []

    def send(self, user_id, text, rows=None):
        self.sent.append((user_id, text))

    def contact(self, user_id):
        return ("Мария Т", "d_chul")


fake_tg = FakeTg()
main.bot._messengers["tg"] = fake_tg
TG = main.Client("tg", 777)

db.execute("DELETE FROM links")
db.save_contact("vk", CLIENT, "Мария Петрова", "chuul")
db.save_contact("tg", 777, "Мария Т", "d_chul")

msg(main.PROFILE_BUTTON)
check("профиль открылся", state() == main.PROFILE)
check("предлагает связать", main.LINK_BUTTON in vk_api.SENT[-1]["keyboard"])

msg(main.LINK_BUTTON)
check("бот ждёт ссылку", state() == main.LINK_HANDLE)
check("предупредили, что нужно писать из обоих",
      "Telegram" in vk_api.SENT[-1]["message"])

# Человек, который боту не писал, не найдётся — и процесс прерывается.
msg("@nobody")
check("незнакомого не нашли",
      "не найдено" in vk_api.SENT[-2]["message"], vk_api.SENT[-2]["message"])
check("вернулись в профиль", state() == main.PROFILE)
check("заявки не завели", db.link_for("vk", CLIENT) is None)

# А знакомого — находим по ссылке t.me/…
msg(main.LINK_BUTTON)
msg("https://t.me/d_chul")
check("заявка заведена", (db.link_for("vk", CLIENT) or {}).get("status")
      == "PENDING")
check("запрос ушёл в Telegram", len(fake_tg.sent) == 1)
check("в запросе видно, кто просит",
      "Мария Петрова" in fake_tg.sent[-1][1], fake_tg.sent[-1][1])
check("до подтверждения аккаунты не связаны",
      db.linked_identities("vk", CLIENT) == [("vk", CLIENT)])

# Подтверждение приходит со стороны Telegram.
main.handle_message(TG, main.LINK_YES)
linked = db.linked_identities("vk", CLIENT)
check("после «Да» аккаунты связаны", linked == [("vk", CLIENT), ("tg", 777)],
      str(linked))
check("инициатору сообщили",
      "подтвердил связку" in vk_api.SENT[-1]["message"],
      vk_api.SENT[-1]["message"])

# Второй раз связать нельзя: одна связка на аккаунт.
msg(main.PROFILE_BUTTON)
check("в профиле видно связку", "связан" in vk_api.SENT[-1]["message"])
check("теперь предлагают отвязать",
      main.UNLINK_BUTTON in vk_api.SENT[-1]["keyboard"])

msg(main.UNLINK_BUTTON)
check("после отвязки личность одна",
      db.linked_identities("vk", CLIENT) == [("vk", CLIENT)])
check("второму аккаунту сообщили об отвязке",
      "снята" in fake_tg.sent[-1][1])

# Отказ: заявка исчезает, связки нет.
msg(main.PROFILE_BUTTON)
msg(main.LINK_BUTTON)
msg("@d_chul")
main.handle_message(TG, main.LINK_NO)
check("после «Нет» связки нет",
      db.linked_identities("vk", CLIENT) == [("vk", CLIENT)])
check("и заявка убрана", db.link_for("vk", CLIENT) is None)

check("разбор ссылок", [main.parse_handle(t) for t in
                        ("@d_chul", "vk.com/chuul", "https://vk.ru/chuul/",
                         "t.me/d_chul", "vk.com/id555")]
      == ["d_chul", "chuul", "chuul", "d_chul", "id555"])

del main.bot._messengers["tg"]
db.execute("DELETE FROM links")


# --- 18. Живые данные -----------------------------------------------------
print("\n18. Живые данные")

check("боевая база не тронута",
      (config.DB_FILE.stat().st_mtime if config.DB_FILE.exists() else None)
      == LIVE_STAMP)

db.close()
print(f"\nИтого: ок {ok}, плохо {fail}")
sys.exit(1 if fail else 0)
