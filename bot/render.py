import html
import re

CODE_RE = re.compile(r"`([^`]+)`")


def text(s: str) -> str:
    """Текст карточки в HTML Telegram: экранировать, `код` в <code>."""
    return CODE_RE.sub(r"<code>\1</code>", html.escape(s))


def question(card) -> str:
    return f"<b>{html.escape(card['topic'])}</b> · {card['kind']}\n\n{text(card['q'])}"


def answer(card) -> str:
    return f"{question(card)}\n\n<b>Ответ</b>\n{text(card['a'])}\n\n<i>{html.escape(card['src'])}</i>"


def verdict(card, labels: list[str], hits: list[bool], ok: bool, days: int) -> str:
    points = "\n".join(f"{'✅' if h else '❌'} {text(l)}" for l, h in zip(labels, hits))
    head = "Засчитано" if ok else "Не засчитано"
    return f"<b>{head}</b>, снова через {days} дн.\n{points}\n\n<b>Эталон</b>\n{text(card['a'])}\n\n<i>{html.escape(card['src'])}</i>"
