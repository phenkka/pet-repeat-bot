import html
import re

CODE_RE = re.compile(r"`([^`]+)`")
MD_RE = re.compile(r"\.md\b")


def text(s: str) -> str:
    """Текст карточки в HTML Telegram: экранировать, `код` в <code>."""
    return CODE_RE.sub(r"<code>\1</code>", html.escape(s))


def source(card) -> str:
    # без «.md»: иначе Telegram делает из «файл.md» ссылку на домен .md
    src = MD_RE.sub("", card["src"])
    return f"<i>{html.escape(src)}</i>"


def question(card, label: str) -> str:
    return f"<b>{html.escape(label)}</b>\n\n{text(card['q'])}"


def answer(card, label: str) -> str:
    # ответ дописывается в то же сообщение, под вопрос
    return f"{question(card, label)}\n\n<b>Ответ</b>\n{text(card['a'])}\n\n{source(card)}"
