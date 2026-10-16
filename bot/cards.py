import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# id уходит в callback_data, а у Telegram там предел 64 байта
ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
KINDS = {"С", "М"}


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
    ord: int


def load(cards_dir: Path) -> list[Card]:
    cards: list[Card] = []
    seen: set[str] = set()
    for path in sorted(cards_dir.glob("*.toml")):
        if path.name.startswith("_"):
            continue
        data = tomllib.loads(path.read_text(encoding="utf-8"))
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
            keys = raw.get("keys", [])
            if not keys or not all(isinstance(k, str) and k.strip() for k in keys):
                raise ValueError(f"{where}: keys - непустой список строк")
            need = raw.get("need")
            if need is not None and not 1 <= need <= len(keys):
                raise ValueError(f"{where}: need от 1 до числа keys")
            seen.add(cid)
            cards.append(Card(
                id=cid, q=raw["q"].strip(), a=raw["a"].strip(), src=raw["src"].strip(),
                kind=raw["kind"], keys=tuple(k.strip() for k in keys), need=need,
                topic=path.stem, ord=len(cards),
            ))
    return cards


def load_aliases(cards_dir: Path) -> dict[str, str]:
    path = cards_dir / "_aliases.toml"
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8")).get("alias", {})
    return {k.lower().replace("ё", "е"): v.lower() for k, v in data.items()}
