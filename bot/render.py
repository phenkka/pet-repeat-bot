import html
import re

CODE_RE = re.compile(r"`([^`]+)`")


def text(s: str) -> str:
    """Текст карточки в HTML Telegram: экранировать, `код` в <code>."""
    return CODE_RE.sub(r"<code>\1</code>", html.escape(s))


def question(card) -> str:
    return f"<b>{html.escape(card['topic'])}</b> · {card['kind']}\n\n{text(card['q'])}"


def source(card) -> str:
    # в <code>: иначе Telegram делает из «файл.md» ссылку на домен .md
    return f"<code>{html.escape(card['src'])}</code>"


def answer(card) -> str:
    return f"{question(card)}\n\n<b>Ответ</b>\n{text(card['a'])}\n\n{source(card)}"


def verdict(card, ok: bool, days: int) -> str:
    head = "Засчитано" if ok else "Не засчитано"
    return f"<b>{head}</b>, снова через {days} дн.\n\n<b>Ответ</b>\n{text(card['a'])}\n\n{source(card)}"
