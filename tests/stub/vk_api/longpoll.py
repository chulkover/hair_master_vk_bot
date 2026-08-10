class VkEventType:
    MESSAGE_NEW = "message_new"


# Что longpoll «увидит»: тесты кладут сюда словари вида
# {"type": VkEventType.MESSAGE_NEW, "user_id": 1, "text": "привет"}.
# Пусто по умолчанию — поток сразу пустой, как раньше.
EVENTS = []


class _Event:
    def __init__(self, data):
        self.type = data["type"]
        self.to_me = data.get("to_me", True)
        self.user_id = data.get("user_id")
        self.text = data.get("text")


class VkLongPoll:
    def __init__(self, session):
        pass

    def listen(self):
        return iter([_Event(event) for event in EVENTS])
