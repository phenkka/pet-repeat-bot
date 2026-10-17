import html
import re

CODE_RE = re.compile(r"`([^`]+)`")
MD_RE = re.compile(r"\.md\b")


def text(s: str) -> str:
    """Текст карточки в HTML Telegram: экранировать, `код` в <code>."""
    return CODE_RE.sub(r"<code>\1</code>", html.escape(s))


def question(card) -> str:
    return f"<b>{html.escape(card['topic'])}</b> · {card['kind']}\n\n{text(card['q'])}"


def source(card) -> str:
    # без «.md»: иначе Telegram делает из «файл.md» ссылку на домен .md
    src = MD_RE.sub("", card["src"])
    return f"<i>{html.escape(src)}</i>"


def verdict(card, head: str, days: int) -> str:
    return f"<b>{head}</b>, снова через {days} дн.\n\n<b>Ответ</b>\n{text(card['a'])}\n\n{source(card)}"
