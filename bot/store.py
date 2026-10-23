from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .cards import Card

# Таблицы progress, log и disputes остались в старых базах от повторения
# с проверкой ответов. Бот их больше не трогает и не удаляет.
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
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- добавленные админом через /adduser. админы из .env сюда не пишутся
CREATE TABLE IF NOT EXISTS users (
    id    BIGINT PRIMARY KEY,
    added TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- кнопка меню. колонка добавлена позже: старая база доращивается тут
ALTER TABLE cards ADD COLUMN IF NOT EXISTS theme TEXT NOT NULL DEFAULT '';
"""
# тема «всё вперемешку»
ALL = "*"
# фильтр по теме: для ALL условие истинно всегда
THEME_SQL = "(%s::text = '*' OR c.theme = %s::text)"


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

    async def _exec(self, sql: str, *args) -> int:
        async with self.pool.connection() as conn:
            return (await conn.execute(sql, args)).rowcount

    async def sync(self, cards: list[Card]) -> tuple[int, int]:
        """Карточки из файлов в базу. Убранные из файлов гасятся, а не
        удаляются: на них ссылается старая история."""
        async with self.pool.connection() as conn, conn.transaction():
            before = {r["id"] for r in await (await conn.execute("SELECT id FROM cards")).fetchall()}
            await conn.execute("UPDATE cards SET active = FALSE")
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO cards (id, q, a, src, kind, keys, need, topic, theme, ord, active)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)"
                    " ON CONFLICT (id) DO UPDATE SET q = excluded.q, a = excluded.a,"
                    " src = excluded.src, kind = excluded.kind, keys = excluded.keys,"
                    " need = excluded.need, topic = excluded.topic, theme = excluded.theme,"
                    " ord = excluded.ord, active = TRUE",
                    [(c.id, c.q, c.a, c.src, c.kind, Jsonb(list(c.keys)), c.need, c.topic, c.theme,
                      c.ord) for c in cards],
                )
            off = await (await conn.execute("SELECT COUNT(*) AS n FROM cards WHERE NOT active")).fetchone()
        return len({c.id for c in cards} - before), off["n"]

    async def themes(self) -> list[dict]:
        """Для меню: темы и сколько в каждой карточек."""
        return await self._all(
            "SELECT theme, COUNT(*) AS n FROM cards WHERE active GROUP BY theme ORDER BY theme")

    async def deck_size(self, theme: str) -> int:
        row = await self._one(f"SELECT COUNT(*) AS n FROM cards c WHERE c.active AND {THEME_SQL}",
                              theme, theme)
        return row["n"]

    async def card_at(self, theme: str, seed: str, n: int) -> dict | None:
        """n-я карточка колоды. Колода - карточки темы, перемешанные по seed:
        один seed на проход, поэтому без повторов, а новый проход - новый порядок."""
        return await self._one(
            f"SELECT c.* FROM cards c WHERE c.active AND {THEME_SQL}"
            " ORDER BY md5(c.id || %s) OFFSET %s LIMIT 1", theme, theme, seed, n)

    async def card(self, card_id: str) -> dict | None:
        return await self._one("SELECT * FROM cards WHERE id = %s", card_id)

    async def users(self) -> set[int]:
        return {r["id"] for r in await self._all("SELECT id FROM users")}

    async def add_user(self, uid: int) -> bool:
        return bool(await self._exec(
            "INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING", uid))

    async def del_user(self, uid: int) -> bool:
        return bool(await self._exec("DELETE FROM users WHERE id = %s", uid))

    async def get_meta(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM meta WHERE key = %s", key)
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._exec(
            "INSERT INTO meta (key, value) VALUES (%s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value", key, value)
