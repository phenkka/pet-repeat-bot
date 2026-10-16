import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.conninfo import make_conninfo


@dataclass(frozen=True)
class Config:
    token: str
    allowed: frozenset[int]
    tz: ZoneInfo
    remind_at: time
    nudge_at: time
    new_per_day: int
    max_per_day: int
    intervals: tuple[int, ...]
    cards_dir: Path
    db: str


def load() -> Config:
    # без белого списка не стартуем: бот без него отдаёт карточки любому
    allowed = frozenset(int(x) for x in os.environ["ALLOWED_USER_IDS"].split(",") if x.strip())
    if not allowed:
        raise SystemExit("ALLOWED_USER_IDS пуст")
    return Config(
        token=os.environ["BOT_TOKEN"],
        allowed=allowed,
        tz=ZoneInfo(os.environ.get("TZ_NAME", "Asia/Yekaterinburg")),
        remind_at=time.fromisoformat(os.environ.get("REMIND_AT", "13:00")),
        nudge_at=time.fromisoformat(os.environ.get("NUDGE_AT", "20:00")),
        # норма дня: возвраты, а оставшееся место добивается новыми
        new_per_day=int(os.environ.get("NEW_PER_DAY", "15")),
        max_per_day=int(os.environ.get("MAX_PER_DAY", "15")),
        intervals=tuple(int(x) for x in os.environ.get("INTERVALS", "1,3,7,21,60").split(",")),
        cards_dir=Path(os.environ.get("CARDS_DIR", "cards")),
        # пароль из файла (секрет compose), а не из переменной: env видно в docker inspect
        db=make_conninfo(
            host=os.environ.get("DB_HOST", "db"),
            dbname=os.environ.get("DB_NAME", "repeat"),
            user=os.environ.get("DB_USER", "repeat"),
            password=Path(os.environ.get("DB_PASSWORD_FILE", "/run/secrets/db_password")).read_text().strip(),
        ),
    )
