"""Заглушка requests: перехватывает HTTP до Telegram, никуда не ходит по сети."""

SENT = []  # каждый post(): {"url": ..., "json": ...}

# Чем отвечать на следующий post(): функция (url, params) -> dict в формате
# Bot API ({"ok": True, "result": ...} или {"ok": False, ...}). По умолчанию —
# как будто Telegram всегда говорит «ок» и ничего нового не прислал.
HANDLER = lambda url, params: {"ok": True, "result": []}


class RequestException(Exception):
    pass


class _Response:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def post(url, json=None, timeout=None):
    SENT.append({"url": url, "json": json})
    return _Response(HANDLER(url, json))
