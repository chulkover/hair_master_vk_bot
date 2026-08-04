"""Заглушка vk_api: перехватывает отправку, никуда не ходит по сети."""
SENT = []

# Кого «знает» ВКонтакте: {номер: (имя, фамилия)}. Бот спрашивает имя, когда
# пишет мастеру о новой записи. Кого здесь нет — того для ВК не существует,
# и users.get на нём ведёт себя как настоящий.
PEOPLE = {}


class _Messages:
    def send(self, **params):
        SENT.append(params)


class _Users:
    def get(self, user_ids=None, **params):
        """Имя и фамилия — в том же виде, в каком их отдаёт настоящий VK.

        На несуществующем номере настоящий метод возвращает пустой список,
        и вызывающий код спотыкается, забирая из него первый элемент.
        Заглушка ведёт себя так же: бот обязан это переживать.
        """
        person = PEOPLE.get(int(user_ids))
        if person is None:
            return []
        return [{"id": int(user_ids),
                 "first_name": person[0],
                 "last_name": person[1]}]


class _Api:
    messages = _Messages()
    users = _Users()


class VkApi:
    def __init__(self, token=None):
        self.token = token

    def get_api(self):
        return _Api()
