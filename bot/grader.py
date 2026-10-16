import math
import re
from difflib import SequenceMatcher

# всё сводится к латинице: «дигест» и digest, «докер» и docker сравнимы
TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
})
TOKEN_RE = re.compile(r"[a-z0-9]+")
WORD_RE = re.compile(r"[a-zа-я0-9]+")

# свои сокращения: «прст» -> «просто». cards/_aliases.toml, правится по спорам
ALIASES: dict[str, str] = {}


def tokens(s: str) -> list[str]:
    out: list[str] = []
    for w in WORD_RE.findall(s.lower().replace("ё", "е")):
        out += TOKEN_RE.findall(ALIASES.get(w, w).translate(TRANSLIT))
    return out


def same(a: str, b: str) -> bool:
    if a == b:
        return True
    short = min(len(a), len(b))
    # короткие слова только точно: иначе «tok» сойдётся с чем попало
    if short < 4:
        return False
    prefix = len(next((a[:i] for i in range(short, 0, -1) if a[:i] == b[:i]), ""))
    # окончания и сокращения: «привилегий», «конф» от «конфигурация»
    if prefix >= 4 and prefix >= 0.6 * short:
        return True
    # опечатки: «привелегии», «гитликс»
    return SequenceMatcher(None, a, b).ratio() >= 0.8


def covered(alt: str, answer: list[str]) -> bool:
    words = tokens(alt)
    return bool(words) and all(any(same(w, t) for t in answer) for w in words)


def check(keys: list[str], need: int | None, answer: str) -> tuple[list[bool], bool]:
    """Пункт засчитан, если в ответе есть любой его вариант. Варианты через |,
    слова варианта ищутся в ответе в любом порядке."""
    words = tokens(answer)
    hits = [any(covered(alt, words) for alt in key.split("|")) for key in keys]
    need = need or math.ceil(len(keys) * 2 / 3)
    return hits, sum(hits) >= need


def label(key: str) -> str:
    return key.split("|")[0].strip()
