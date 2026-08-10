"""Проверка messenger.py: VkMessenger, TgMessenger, Bot — без сети.

vk_api и requests подменены заглушками из stub/: ни одно сообщение никуда
не уходит, а входящие для тестов кладутся в заглушку заранее.
"""

import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
PROJECT = Path(os.environ.get("VK_BOT_DIR") or Path(__file__).resolve().parents[1])

sys.path.insert(0, str(HERE / "stub"))   # заглушки vk_api и requests — раньше настоящих
sys.path.insert(0, str(PROJECT))

import vk_api               # заглушка
import requests             # заглушка
import config

assert vk_api.__file__.startswith(str(HERE)), "vk_api должен быть заглушкой!"
assert requests.__file__.startswith(str(HERE)), "requests должен быть заглушкой!"

import messenger
from messenger import Client, Incoming, MessengerError, VkMessenger, TgMessenger, Bot
from vk_api.longpoll import VkEventType

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


# =========================================================================
# 1. VkMessenger
# =========================================================================
print("\n1. VkMessenger")

vk = VkMessenger("vk", "fake-token")

vk.send(111, "привет")
check("send() дошёл", vk_api.SENT[-1]["message"] == "привет"
      and vk_api.SENT[-1]["user_id"] == 111)
check("без rows клавиатуры нет", "keyboard" not in vk_api.SENT[-1])

vk.send(111, "меню", rows=[["Записаться"], ["Мои записи", "Профиль"]])
check("rows превратились в клавиатуру ВК", "keyboard" in vk_api.SENT[-1])

vk_api.FAIL[912] = 912
try:
    vk.send(912, "х")
    check("ошибка 912 поднимает MessengerError", False)
except MessengerError as error:
    check("ошибка 912 поднимает MessengerError", True)
    check("подсказка про «Возможности ботов»", "Возможности ботов" in error.hint)

vk_api.FAIL[913] = 100
try:
    vk.send(913, "х")
    check("прочая ошибка тоже MessengerError", False)
except MessengerError as error:
    check("прочая ошибка тоже MessengerError", True)
    check("но без подсказки", error.hint == "")
vk_api.FAIL.clear()

vk_api.PEOPLE[201] = ("Мария", "Петрова", "chuul")
vk_api.PEOPLE[202] = ("Олег", "Иванов")  # без короткого имени
check("contact() с доменом", vk.contact(201) == ("Мария Петрова", "chuul"))
check("contact() без домена — id-заглушка", vk.contact(202) == ("Олег Иванов", "id202"))
check("contact() незнакомца — пусто", vk.contact(999) == ("", ""))
check("user_name делегирует в contact", vk.user_name(201) == "Мария Петрова")
check("user_link — по номеру", vk.user_link(201) == "https://vk.com/id201")
check("link() подписывает свою страницу разметкой ВК",
      vk.link("Мария Петрова", "https://vk.com/id201") == "[id201|Мария Петрова]",
      vk.link("Мария Петрова", "https://vk.com/id201"))
check("link() чужой адрес оставляет как есть",
      vk.link("Мария Т", "https://t.me/anna_k") == "Мария Т — https://t.me/anna_k",
      vk.link("Мария Т", "https://t.me/anna_k"))
check("служебные символы имени не ломают разметку",
      vk.link("А|Б]В", "https://vk.com/id201") == "[id201|А/Б)В]",
      vk.link("А|Б]В", "https://vk.com/id201"))

from vk_api import longpoll as vk_longpoll
vk_longpoll.EVENTS[:] = [
    {"type": VkEventType.MESSAGE_NEW, "to_me": True, "user_id": 301, "text": "привет"},
    {"type": VkEventType.MESSAGE_NEW, "to_me": False, "user_id": 302, "text": "не мне"},
    {"type": "other_event", "to_me": True, "user_id": 303, "text": "не сообщение"},
]
incoming = list(vk.listen())
check("listen() отдал только адресованное боту сообщение",
      incoming == [Incoming(Client("vk", 301), "привет")], str(incoming))
vk_longpoll.EVENTS.clear()


# =========================================================================
# 2. TgMessenger
# =========================================================================
print("\n2. TgMessenger")

tg = TgMessenger("tg", "fake-token")

requests.HANDLER = lambda url, params: {"ok": True, "result": []}
tg.send(111, "меню", rows=[["Записаться"], ["Профиль"]])
check("send() дошёл", requests.SENT[-1]["json"]["text"] == "меню"
      and requests.SENT[-1]["json"]["chat_id"] == 111)
check("rows -> reply-клавиатура",
      requests.SENT[-1]["json"]["reply_markup"]
      == {"keyboard": [["Записаться"], ["Профиль"]], "resize_keyboard": True})

tg.send(111, "в меню", rows=None)
check("rows=None снимает клавиатуру",
      requests.SENT[-1]["json"]["reply_markup"] == {"remove_keyboard": True})

requests.HANDLER = lambda url, params: {"ok": False, "error_code": 400,
                                        "description": "Bad Request: chat not found"}
try:
    tg.send(999, "х")
    check("отказ Telegram поднимает MessengerError", False)
except MessengerError:
    check("отказ Telegram поднимает MessengerError", True)


def _boom(url, params):
    raise requests.RequestException("сеть недоступна")


requests.HANDLER = _boom
try:
    tg.send(111, "х")
    check("обрыв сети поднимает MessengerError", False)
except MessengerError as error:
    check("обрыв сети поднимает MessengerError", True)
    check("сообщение про Telegram недоступен", "недоступен" in str(error))

check("contact() до первого сообщения — пусто", tg.contact(401) == ("", ""))
check("user_link() без username — ссылки нет", tg.user_link(401) == "")
check("user_link() берёт подсказанный username",
      tg.user_link(401, "d_chul") == "https://t.me/d_chul")

requests.HANDLER = lambda url, params: {"ok": True, "result": [
    {"update_id": 1, "message": {"sticker": {}}},  # без текста — мимо, но offset двигает
    {"update_id": 2,
     "message": {"text": "привет", "from": {"id": 401, "first_name": "Анна",
                                            "username": "anna_k"}}},
]}
incoming = []
for message in tg.listen():
    incoming.append(message)
    if len(incoming) == 1:
        requests.HANDLER = lambda url, params: {"ok": True, "result": []}
        break
check("listen() отдал только текстовое сообщение",
      incoming == [Incoming(Client("tg", 401), "привет")], str(incoming))
check("отправитель запомнен", tg.contact(401) == ("Анна", "anna_k"))
check("user_link() теперь по username", tg.user_link(401) == "https://t.me/anna_k")
check("своё имя из сообщения важнее подсказки",
      tg.user_link(401, "d_chul") == "https://t.me/anna_k")
check("link() — html-тег со ссылкой",
      tg.link("Анна", "https://t.me/anna_k")
      == '<a href="https://t.me/anna_k">Анна</a>', tg.link("Анна", "https://t.me/anna_k"))
check("угловые скобки в имени экранируются",
      tg.link("А<Б>", "https://t.me/anna_k")
      == '<a href="https://t.me/anna_k">А&lt;Б&gt;</a>', tg.link("А<Б>", "https://t.me/anna_k"))

requests.HANDLER = lambda url, params: {"ok": True, "result": []}
tg.send(111, "обычный текст")
check("без ссылки разметку не включаем",
      "parse_mode" not in requests.SENT[-1]["json"])
tg.send(111, f'Клиент: {tg.link("Анна", "https://t.me/anna_k")}')
check("со ссылкой включается parse_mode HTML",
      requests.SENT[-1]["json"].get("parse_mode") == "HTML")
check("offset сдвинулся за последнее из пачки", tg._offset == 3)


# =========================================================================
# 3. Bot: маршрутизация
# =========================================================================
print("\n3. Bot")

bot = Bot({"vk": vk, "tg": tg})

try:
    bot.by("wa")
    check("неизвестная платформа — ошибка", False)
except MessengerError:
    check("неизвестная платформа — ошибка", True)

requests.HANDLER = lambda url, params: {"ok": True, "result": []}
before_vk, before_tg = len(vk_api.SENT), len(requests.SENT)
bot.send(Client("vk", 111), "vk-привет")
bot.send(Client("tg", 111), "tg-привет")
check("bot.send() уехал в ВК", len(vk_api.SENT) == before_vk + 1)
check("bot.send() уехал в Telegram", len(requests.SENT) == before_tg + 1)
check("bot.contact() маршрутизируется", bot.contact(Client("vk", 201))
      == ("Мария Петрова", "chuul"))
check("bot.user_name() маршрутизируется", bot.user_name(Client("vk", 201))
      == "Мария Петрова")
check("bot.user_link() маршрутизируется", bot.user_link(Client("tg", 401))
      == "https://t.me/anna_k")
check("bot.link() размечает по получателю, а не по адресу",
      bot.link(Client("vk", 111), "Анна", "https://t.me/anna_k")
      == "Анна — https://t.me/anna_k",
      bot.link(Client("vk", 111), "Анна", "https://t.me/anna_k"))

vk_longpoll.EVENTS[:] = [
    {"type": VkEventType.MESSAGE_NEW, "to_me": True, "user_id": 501, "text": "один канал"},
]
only_vk = Bot({"vk": vk})
check("один мессенджер — поток без потоков", list(only_vk.listen())
      == [Incoming(Client("vk", 501), "один канал")])
vk_longpoll.EVENTS.clear()

# Несколько каналов: у ВК longpoll сразу отдаёт сообщение и замолкает, у TG —
# бесконечный поток пустых ответов (как настоящий getUpdates). listen() должен
# успеть донести сообщение из ВК, не застряв на Telegram.
vk_longpoll.EVENTS[:] = [
    {"type": VkEventType.MESSAGE_NEW, "to_me": True, "user_id": 601, "text": "оба канала"},
]
requests.HANDLER = lambda url, params: {"ok": True, "result": []}
merged = Bot({"vk": vk, "tg": tg})
got = next(iter(merged.listen()))
check("несколько каналов — сообщение из ВК дошло через очередь",
      got == Incoming(Client("vk", 601), "оба канала"), str(got))
vk_longpoll.EVENTS.clear()


# =========================================================================
# 4. create(): выбор мессенджеров по config.cfg
# =========================================================================
print("\n4. create()")

had_bot_section = config.SETTINGS.has_section("bot")
saved_platform = (config.SETTINGS.get("bot", "platform")
                  if config.SETTINGS.has_option("bot", "platform") else None)

if not config.SETTINGS.has_section("bot"):
    config.SETTINGS.add_section("bot")

config.SETTINGS.remove_option("bot", "platform")
default_bot = messenger.create()
check("без настройки — только ВК", list(default_bot._messengers) == ["vk"])

config.SETTINGS.set("bot", "platform", "vk, tg")
both_bot = messenger.create()
check("«vk, tg» поднимает оба",
      set(both_bot._messengers) == {"vk", "tg"})
check("это правильные классы",
      isinstance(both_bot._messengers["vk"], VkMessenger)
      and isinstance(both_bot._messengers["tg"], TgMessenger))

config.SETTINGS.set("bot", "platform", "vk, vk")
check("повтор в списке не дублируется",
      list(messenger.create()._messengers) == ["vk"])

config.SETTINGS.set("bot", "platform", "whatsapp")
try:
    messenger.create()
    check("неизвестный мессенджер в настройке — SystemExit", False)
except SystemExit:
    check("неизвестный мессенджер в настройке — SystemExit", True)

# Возвращаем настройки, как были — этот файл делит config.cfg с другими тестами.
if saved_platform is None:
    config.SETTINGS.remove_option("bot", "platform")
else:
    config.SETTINGS.set("bot", "platform", saved_platform)
if not had_bot_section:
    config.SETTINGS.remove_section("bot")


print(f"\nИтого: ок {ok}, плохо {fail}")
sys.exit(1 if fail else 0)
