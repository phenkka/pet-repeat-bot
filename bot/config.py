import os
from dataclasses import dataclass
from pathlib import Path

from psycopg.conninfo import make_conninfo


@dataclass(frozen=True)
class Config:
    token: str
    # админы из .env: всегда есть доступ, могут добавлять пользователей
    admins: frozenset[int]
    cards_dir: Path
    db: str


def load() -> Config:
    # без админа не стартуем: иначе добавить пользователей некому
    admins = frozenset(int(x) for x in os.environ["ALLOWED_USER_IDS"].split(",") if x.strip())
    if not admins:
        raise SystemExit("ALLOWED_USER_IDS пуст")
    return Config(
        token=os.environ["BOT_TOKEN"],
        admins=admins,
        cards_dir=Path(os.environ.get("CARDS_DIR", "cards")),
        # пароль из файла (секрет compose), а не из переменной: env видно в docker inspect
        db=make_conninfo(
            host=os.environ.get("DB_HOST", "db"),
            dbname=os.environ.get("DB_NAME", "repeat"),
            user=os.environ.get("DB_USER", "repeat"),
            password=Path(os.environ.get("DB_PASSWORD_FILE", "/run/secrets/db_password")).read_text().strip(),
        ),
    )
