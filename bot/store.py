from datetime import date, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .cards import Card

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id     TEXT PRIMARY KEY,
    q      TEXT NOT NULL,
    a      TEXT NOT NULL,
    src    TEXT NOT NULL,
    kind   TEXT NOT NULL,
    keys   JSONB NOT NULL,
    need   INTEGER,
    topic  TEXT NOT NULL,
    ord    INTEGER NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE
);
-- строки нет: карточка ещё не показывалась
CREATE TABLE IF NOT EXISTS progress (
    card_id    TEXT PRIMARY KEY REFERENCES cards(id),
    box        INTEGER NOT NULL,
    due        DATE NOT NULL,
    introduced DATE NOT NULL
);
CREATE TABLE IF NOT EXISTS log (
    id      BIGSERIAL PRIMARY KEY,
    card_id TEXT NOT NULL REFERENCES cards(id),
    day     DATE NOT NULL,
    ok      BOOLEAN NOT NULL,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- свой ответ: по промахам дописываются варианты в keys
    answer  TEXT
);
CREATE INDEX IF NOT EXISTS log_day ON log (day);
-- «Оспорить»: ответ не засчитан, а ты считаешь его верным
CREATE TABLE IF NOT EXISTS disputes (
    id      BIGSERIAL PRIMARY KEY,
    card_id TEXT NOT NULL REFERENCES cards(id),
    day     DATE NOT NULL,
    answer  TEXT NOT NULL,
    status  TEXT NOT NULL DEFAULT 'open',
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (card_id, day)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, conninfo: str):
        # пул сам переподключается, если база перезапустилась
        self.pool = AsyncConnectionPool(
            conninfo, min_size=1, max_size=3, open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
            check=AsyncConnectionPool.check_connection,
        )

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=60)
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        await self.pool.close()

    async def _one(self, sql: str, *args) -> dict | None:
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, args)).fetchone()

    async def _all(self, sql: str, *args) -> list[dict]:
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, args)).fetchall()

    async def _count(self, sql: str, *args) -> int:
        row = await self._one(sql, *args)
        return next(iter(row.values()))

    async def sync(self, cards: list[Card]) -> tuple[int, int]:
        """Карточки из файлов в базу. Прогресс не трогается: правка текста
        карточки не сбрасывает её интервал. Убранные из файлов гасятся,
        а не удаляются, чтобы вернуть их можно было с историей."""
        async with self.pool.connection() as conn, conn.transaction():
            before = {r["id"] for r in await (await conn.execute("SELECT id FROM cards")).fetchall()}
            await conn.execute("UPDATE cards SET active = FALSE")
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO cards (id, q, a, src, kind, keys, need, topic, ord, active)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)"
                    " ON CONFLICT (id) DO UPDATE SET q = excluded.q, a = excluded.a,"
                    " src = excluded.src, kind = excluded.kind, keys = excluded.keys,"
                    " need = excluded.need, topic = excluded.topic, ord = excluded.ord, active = TRUE",
                    [(c.id, c.q, c.a, c.src, c.kind, Jsonb(list(c.keys)), c.need, c.topic, c.ord)
                     for c in cards],
                )
            off = await (await conn.execute("SELECT COUNT(*) AS n FROM cards WHERE NOT active")).fetchone()
        return len({c.id for c in cards} - before), off["n"]

    async def answered(self, today: date) -> int:
        return await self._count("SELECT COUNT(*) FROM log WHERE day = %s", today)

    async def _new_left(self, today: date, new_per_day: int) -> int:
        started = await self._count("SELECT COUNT(*) FROM progress WHERE introduced = %s", today)
        return max(0, new_per_day - started)

    async def next_card(self, today: date, new_per_day: int, max_per_day: int) -> dict | None:
        """Сначала возвраты, самые старые первыми. Затем новые вперемешку по
        темам: тема, из которой показано меньше всего, при равенстве порядок
        тем свой на каждый день. Внутри темы - по порядку файла."""
        if await self.answered(today) >= max_per_day:
            return None
        row = await self._one(
            "SELECT c.* FROM cards c JOIN progress p ON p.card_id = c.id"
            " WHERE c.active AND p.due <= %s ORDER BY p.due, random() LIMIT 1", today)
        if row or not await self._new_left(today, new_per_day):
            return row
        return await self._one(
            "WITH seen AS (SELECT c.topic, COUNT(*) AS n FROM progress p"
            " JOIN cards c ON c.id = p.card_id GROUP BY c.topic)"
            " SELECT c.* FROM cards c LEFT JOIN progress p ON p.card_id = c.id"
            " LEFT JOIN seen s ON s.topic = c.topic"
            " WHERE c.active AND p.card_id IS NULL"
            " ORDER BY COALESCE(s.n, 0), md5(c.topic || %s), c.ord LIMIT 1", today.isoformat())

    async def to_do(self, today: date, new_per_day: int, max_per_day: int) -> int:
        """Сколько карточек бот покажет сегодня, если пройти всё."""
        due = await self._count(
            "SELECT COUNT(*) FROM cards c JOIN progress p ON p.card_id = c.id"
            " WHERE c.active AND p.due <= %s", today)
        fresh = await self._count(
            "SELECT COUNT(*) FROM cards c LEFT JOIN progress p ON p.card_id = c.id"
            " WHERE c.active AND p.card_id IS NULL")
        left = max(0, max_per_day - await self.answered(today))
        return min(left, due + min(fresh, await self._new_left(today, new_per_day)))

    async def grade(self, card_id: str, ok: bool, today: date, intervals: tuple[int, ...],
                    answer: str | None = None) -> int | None:
        """Ставит оценку, возвращает интервал в днях. None, если карточка
        сегодня уже оценена (двойное нажатие) или ей ещё рано (кнопка
        в старом сообщении)."""
        async with self.pool.connection() as conn, conn.transaction():
            done = await (await conn.execute(
                "SELECT 1 FROM log WHERE card_id = %s AND day = %s", (card_id, today))).fetchone()
            if done:
                return None
            row = await (await conn.execute(
                "SELECT box, due FROM progress WHERE card_id = %s FOR UPDATE", (card_id,))).fetchone()
            if row and row["due"] > today:
                return None
            box = row["box"] if row else 0
            # ошибся: сброс на +1 и лестница интервалов заново
            days = intervals[min(box, len(intervals) - 1)] if ok else intervals[0]
            await conn.execute(
                "INSERT INTO progress (card_id, box, due, introduced) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (card_id) DO UPDATE SET box = excluded.box, due = excluded.due",
                (card_id, box + 1 if ok else 0, today + timedelta(days=days), today))
            await conn.execute(
                "INSERT INTO log (card_id, day, ok, answer) VALUES (%s, %s, %s, %s)",
                (card_id, today, ok, answer))
        return days

    async def downgrade(self, card_id: str, today: date, intervals: tuple[int, ...]) -> int | None:
        """«Всё равно не знал»: засчитанное сегодня переводится в незнание.
        Обратного хода нет, иначе проверка теряет смысл."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE log SET ok = FALSE WHERE card_id = %s AND day = %s AND ok", (card_id, today))
            if not cur.rowcount:
                return None
            await conn.execute(
                "UPDATE progress SET box = 0, due = %s WHERE card_id = %s",
                (today + timedelta(days=intervals[0]), card_id))
        return intervals[0]

    async def dispute(self, card_id: str, today: date) -> int | None:
        """Спор по сегодняшнему незасчитанному ответу. Оценку не меняет:
        засчитать задним числом нельзя, разбор только по ответу."""
        row = await self._one(
            "INSERT INTO disputes (card_id, day, answer)"
            " SELECT card_id, day, answer FROM log"
            " WHERE card_id = %s AND day = %s AND NOT ok AND answer IS NOT NULL"
            " ON CONFLICT (card_id, day) DO NOTHING RETURNING id", card_id, today)
        return row["id"] if row else None

    async def open_disputes(self) -> list[dict]:
        return await self._all(
            "SELECT d.id, d.card_id, d.day, d.answer, c.keys, c.need"
            " FROM disputes d JOIN cards c ON c.id = d.card_id"
            " WHERE d.status = 'open' ORDER BY d.id")

    async def close_dispute(self, dispute_id: int, status: str) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE disputes SET status = %s WHERE id = %s AND status = 'open'", (status, dispute_id))
            return bool(cur.rowcount)

    async def card(self, card_id: str) -> dict | None:
        return await self._one("SELECT * FROM cards WHERE id = %s", card_id)

    async def stats(self, today: date) -> dict:
        return {
            "active": await self._count("SELECT COUNT(*) FROM cards WHERE active"),
            "unseen": await self._count(
                "SELECT COUNT(*) FROM cards c LEFT JOIN progress p ON p.card_id = c.id"
                " WHERE c.active AND p.card_id IS NULL"),
            "ok_today": await self._count("SELECT COUNT(*) FROM log WHERE day = %s AND ok", today),
            "fail_today": await self._count("SELECT COUNT(*) FROM log WHERE day = %s AND NOT ok", today),
            "due_tomorrow": await self._count(
                "SELECT COUNT(*) FROM cards c JOIN progress p ON p.card_id = c.id"
                " WHERE c.active AND p.due <= %s", today + timedelta(days=1)),
            "disputes": await self._count("SELECT COUNT(*) FROM disputes WHERE status = 'open'"),
            "boxes": [(r["box"], r["n"]) for r in await self._all(
                "SELECT p.box, COUNT(*) AS n FROM progress p JOIN cards c ON c.id = p.card_id"
                " WHERE c.active GROUP BY p.box ORDER BY p.box")],
        }

    async def get_meta(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM meta WHERE key = %s", key)
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO meta (key, value) VALUES (%s, %s)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
