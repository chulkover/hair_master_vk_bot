"""Заглушка vk_api: перехватывает отправку, никуда не ходит по сети."""
from vk_api.exceptions import ApiError

SENT = []

# {user_id: код ошибки} — send() на этом номере вместо отправки бросит
# ApiError(code). Пусто по умолчанию: обычные тесты отправку не ловят.
FAIL = {}

# Кого «знает» ВКонтакте: {номер: (имя, фамилия)} или {номер: (имя, фамилия,
# домен)}. Бот спрашивает имя, когда пишет мастеру о новой записи, а домен —
# чтобы человека можно было найти по ссылке vk.com/chuul при связке аккаунтов.
# Кого здесь нет — того для ВК не существует, и users.get на нём ведёт себя
# как настоящий.
PEOPLE = {}


class _Messages:
    def send(self, **params):
        user_id = params.get("user_id")
        if user_id in FAIL:
            raise ApiError(code=FAIL[user_id])
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

        found = {"id": int(user_ids),
                 "first_name": person[0],
                 "last_name": person[1]}

        # Домен настоящий ВК отдаёт только когда его спросили через fields.
        # У страницы без короткого имени он выглядит как «id12345» — заглушка
        # ведёт себя так же, поэтому третий элемент в PEOPLE необязателен.
        if "domain" in (params.get("fields") or ""):
            found["domain"] = (person[2] if len(person) > 2
                               else f"id{int(user_ids)}")
        return [found]


class _Api:
    messages = _Messages()
    users = _Users()


class VkApi:
    def __init__(self, token=None):
        self.token = token

    def get_api(self):
        return _Api()
