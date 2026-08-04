import json


class VkKeyboardColor:
    SECONDARY = "secondary"


class VkKeyboard:
    """Повторяет только то, что использует бот: ряды и кнопки."""

    def __init__(self, one_time=False):
        self.rows = [[]]

    def add_line(self):
        if len(self.rows) >= 10:
            raise ValueError("Max rows reached")
        self.rows.append([])

    def add_button(self, label, color=None):
        if len(self.rows[-1]) >= 5:
            raise ValueError("Max buttons in row reached")
        self.rows[-1].append(label)

    def get_keyboard(self):
        return json.dumps(self.rows, ensure_ascii=False)
