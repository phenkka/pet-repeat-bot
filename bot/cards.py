import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# id уходит в callback_data, а у Telegram там предел 64 байта
ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
KINDS = {"С", "М"}
# тема уходит в callback_data кнопки меню: «t:» + тема, предел 64 байта
THEME_MAX = 60


@dataclass(frozen=True)
class Card:
    id: str
    q: str
    a: str
    src: str
    kind: str
    keys: tuple[str, ...]
    need: int | None
    topic: str
    theme: str
    ord: int


def load(cards_dir: Path) -> list[Card]:
    cards: list[Card] = []
    seen: set[str] = set()
    for path in sorted(cards_dir.glob("*.toml")):
        if path.name.startswith("_"):
            continue
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        # кнопка меню. по умолчанию своя на файл
        theme = str(data.get("theme", path.stem)).strip()
        if not theme or len(theme.encode()) > THEME_MAX:
            raise ValueError(f"{path.name}: theme пустая или длиннее {THEME_MAX} байт")
        for raw in data.get("card", []):
            cid = raw.get("id", "")
            where = f"{path.name}: {cid or '?'}"
            if not ID_RE.match(cid):
                raise ValueError(f"{where}: id только a-z, 0-9 и дефис, до 40 знаков")
            if cid in seen:
                raise ValueError(f"{where}: id повторяется")
            for field in ("q", "a", "src"):
                if not raw.get(field, "").strip():
                    raise ValueError(f"{where}: пустое поле {field}")
            if raw.get("kind") not in KINDS:
                raise ValueError(f"{where}: kind должен быть С или М")
            # keys и need остались от проверки ответов: бот их не читает,
            # но хранит, чтобы проверку можно было вернуть без правки карточек
            keys = raw.get("keys", [])
            if not all(isinstance(k, str) for k in keys):
                raise ValueError(f"{where}: keys - список строк")
            need = raw.get("need")
            seen.add(cid)
            cards.append(Card(
                id=cid, q=raw["q"].strip(), a=raw["a"].strip(), src=raw["src"].strip(),
                kind=raw["kind"], keys=tuple(k.strip() for k in keys), need=need,
                topic=path.stem, theme=theme, ord=len(cards),
            ))
    return cards

