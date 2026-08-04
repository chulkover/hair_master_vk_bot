class VkEventType:
    MESSAGE_NEW = "message_new"


class VkLongPoll:
    def __init__(self, session):
        pass

    def listen(self):
        return iter(())  # сразу пустой поток: цикл в main.py завершится
