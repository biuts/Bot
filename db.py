"""Слой работы с PostgreSQL (asyncpg). Все данные изолированы по chat_id."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

import asyncpg

# Telegram chat_id / user_id не влезают в int32 -> BIGINT.
# Булевы поля оставлены INTEGER (0/1), чтобы не менять остальной код.
SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id           BIGINT PRIMARY KEY,
    title             TEXT    NOT NULL DEFAULT '',
    punishment        TEXT    NOT NULL DEFAULT 'escalation',
    mute_minutes      INTEGER NOT NULL DEFAULT 60,
    delete_admin_msgs INTEGER NOT NULL DEFAULT 0,
    notify_admins     INTEGER NOT NULL DEFAULT 1,
    active            INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS words (
    id      BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    word    TEXT   NOT NULL,
    UNIQUE(chat_id, word)
);
CREATE TABLE IF NOT EXISTS chat_admins (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS violations (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    username   TEXT,
    full_name  TEXT,
    word       TEXT NOT NULL,
    action     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_viol_chat_user ON violations(chat_id, user_id);

-- v2
ALTER TABLE chats ADD COLUMN IF NOT EXISTS warn_limit   INTEGER NOT NULL DEFAULT 3;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS warn_action  TEXT    NOT NULL DEFAULT 'mute';
ALTER TABLE chats ADD COLUMN IF NOT EXISTS log_chat_id  BIGINT;
CREATE TABLE IF NOT EXISTS warns (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    reason     TEXT,
    issued_by  BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_warns_chat_user ON warns(chat_id, user_id);
CREATE TABLE IF NOT EXISTS mod_history (
    id         BIGSERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    kind       TEXT   NOT NULL,
    reason     TEXT,
    issued_by  BIGINT NOT NULL DEFAULT 0,
    minutes    INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_hist_chat_user ON mod_history(chat_id, user_id);
CREATE TABLE IF NOT EXISTS mutes (
    chat_id   BIGINT NOT NULL,
    user_id   BIGINT NOT NULL,
    name      TEXT,
    until     TIMESTAMPTZ,
    reason    TEXT,
    issued_by BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY(chat_id, user_id)
);
"""

_EDITABLE_FIELDS = {
    "punishment", "mute_minutes", "delete_admin_msgs", "notify_admins",
    "warn_limit", "warn_action", "log_chat_id",
}


@dataclass(slots=True)
class ChatSettings:
    """Настройки одного чата."""

    chat_id: int
    title: str
    punishment: str
    mute_minutes: int
    delete_admin_msgs: bool
    notify_admins: bool
    active: bool
    warn_limit: int = 3
    warn_action: str = "mute"
    log_chat_id: int | None = None


@dataclass(slots=True)
class Word:
    """Запрещённое слово."""

    id: int
    word: str


@dataclass(slots=True)
class Stats:
    """Агрегированная статистика нарушений."""

    total: int
    top_users: list[tuple[int, str, int]]  # user_id, подпись, количество
    top_words: list[tuple[str, int]]
    actions: dict[str, int] = field(default_factory=dict)  # mute/ban/warn/kick -> число


def _affected(status: str) -> int:
    """Число затронутых строк из статуса asyncpg ('INSERT 0 1', 'UPDATE 3', ...)."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except ValueError:
        return 0


class Database:
    """Асинхронная обёртка над PostgreSQL (пул соединений)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Открывает пул и создаёт схему."""
        dsn = self._dsn
        local = "localhost" in dsn or "127.0.0.1" in dsn
        # Внешний URL Render требует SSL
        ssl = None if ("sslmode=" in dsn or local) else "require"
        self._pool = await asyncpg.create_pool(
            dsn, ssl=ssl, min_size=1, max_size=5, command_timeout=30
        )
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        """Закрывает пул."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ---- низкоуровневые помощники ----
    @property
    def _p(self) -> asyncpg.Pool:
        assert self._pool is not None, "Database.connect() не вызван"
        return self._pool

    async def _exec(self, sql: str, *params: Any) -> int:
        return _affected(await self._p.execute(sql, *params))

    async def _all(self, sql: str, *params: Any) -> list[asyncpg.Record]:
        return list(await self._p.fetch(sql, *params))

    async def _one(self, sql: str, *params: Any) -> asyncpg.Record | None:
        return await self._p.fetchrow(sql, *params)

    @staticmethod
    def _to_chat(row: asyncpg.Record) -> ChatSettings:
        return ChatSettings(
            chat_id=row["chat_id"],
            title=row["title"],
            punishment=row["punishment"],
            mute_minutes=row["mute_minutes"],
            delete_admin_msgs=bool(row["delete_admin_msgs"]),
            notify_admins=bool(row["notify_admins"]),
            active=bool(row["active"]),
            warn_limit=row["warn_limit"],
            warn_action=row["warn_action"],
            log_chat_id=row["log_chat_id"],
        )

    # ---- чаты ----
    async def upsert_chat(self, chat_id: int, title: str) -> None:
        """Создаёт чат или реактивирует существующий."""
        await self._exec(
            "INSERT INTO chats(chat_id, title, active) VALUES($1, $2, 1) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title, active=1",
            chat_id,
            title,
        )

    async def get_chat(self, chat_id: int) -> ChatSettings | None:
        """Возвращает настройки чата или None."""
        row = await self._one("SELECT * FROM chats WHERE chat_id=$1", chat_id)
        return self._to_chat(row) if row else None

    async def deactivate_chat(self, chat_id: int) -> None:
        """Помечает чат неактивным (бота выгнали)."""
        await self._exec("UPDATE chats SET active=0 WHERE chat_id=$1", chat_id)

    async def set_chat_field(self, chat_id: int, field: str, value: int | str | None) -> None:
        """Меняет одно из разрешённых полей настроек."""
        if field not in _EDITABLE_FIELDS:
            raise ValueError(f"Недопустимое поле: {field}")
        await self._exec(f"UPDATE chats SET {field}=$1 WHERE chat_id=$2", value, chat_id)

    async def migrate_chat(self, old_id: int, new_id: int) -> None:
        """Переносит данные при миграции группы в супергруппу (в одной транзакции)."""
        async with self._p.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM chats WHERE chat_id=$1", new_id)
                await conn.execute("UPDATE chats SET chat_id=$1 WHERE chat_id=$2", new_id, old_id)
                await conn.execute(
                    "INSERT INTO words(chat_id, word) SELECT $1::bigint, word FROM words "
                    "WHERE chat_id=$2 ON CONFLICT DO NOTHING",
                    new_id,
                    old_id,
                )
                await conn.execute("DELETE FROM words WHERE chat_id=$1", old_id)
                await conn.execute(
                    "INSERT INTO chat_admins(chat_id, user_id) SELECT $1::bigint, user_id "
                    "FROM chat_admins WHERE chat_id=$2 ON CONFLICT DO NOTHING",
                    new_id,
                    old_id,
                )
                await conn.execute("DELETE FROM chat_admins WHERE chat_id=$1", old_id)
                await conn.execute("UPDATE violations SET chat_id=$1 WHERE chat_id=$2", new_id, old_id)
                for table in ("warns", "mod_history", "mutes"):
                    await conn.execute(f"UPDATE {table} SET chat_id=$1 WHERE chat_id=$2", new_id, old_id)

    # ---- админы ----
    async def add_admin(self, chat_id: int, user_id: int) -> None:
        """Запоминает администратора чата (для уведомлений и меню)."""
        await self._exec(
            "INSERT INTO chat_admins(chat_id, user_id) VALUES($1, $2) ON CONFLICT DO NOTHING",
            chat_id,
            user_id,
        )

    async def remove_admin(self, chat_id: int, user_id: int) -> None:
        """Удаляет админа из списка."""
        await self._exec("DELETE FROM chat_admins WHERE chat_id=$1 AND user_id=$2", chat_id, user_id)

    async def get_chat_admin_ids(self, chat_id: int) -> list[int]:
        """ID админов чата, известных боту."""
        rows = await self._all("SELECT user_id FROM chat_admins WHERE chat_id=$1", chat_id)
        return [r["user_id"] for r in rows]

    async def get_admin_chats(self, user_id: int) -> list[ChatSettings]:
        """Активные чаты, где пользователь известен как админ."""
        rows = await self._all(
            "SELECT c.* FROM chats c JOIN chat_admins a ON a.chat_id=c.chat_id "
            "WHERE a.user_id=$1 AND c.active=1 ORDER BY c.title",
            user_id,
        )
        return [self._to_chat(r) for r in rows]

    # ---- слова ----
    async def list_words(self, chat_id: int) -> list[Word]:
        """Список запрещённых слов чата."""
        rows = await self._all("SELECT id, word FROM words WHERE chat_id=$1 ORDER BY word", chat_id)
        return [Word(r["id"], r["word"]) for r in rows]

    async def add_words(self, chat_id: int, words: Sequence[str]) -> int:
        """Добавляет слова, возвращает число реально добавленных."""
        added = 0
        for w in words:
            added += await self._exec(
                "INSERT INTO words(chat_id, word) VALUES($1, $2) ON CONFLICT DO NOTHING", chat_id, w
            )
        return added

    async def delete_word(self, chat_id: int, word_id: int) -> bool:
        """Удаляет слово по id."""
        return await self._exec("DELETE FROM words WHERE id=$1 AND chat_id=$2", word_id, chat_id) > 0

    # ---- нарушения ----
    async def count_violations(self, chat_id: int, user_id: int) -> int:
        """Сколько нарушений уже у пользователя в чате."""
        row = await self._one(
            "SELECT COUNT(*) AS c FROM violations WHERE chat_id=$1 AND user_id=$2", chat_id, user_id
        )
        return int(row["c"]) if row else 0

    async def add_violation(
        self, chat_id: int, user_id: int, username: str | None, full_name: str, word: str, action: str
    ) -> None:
        """Записывает нарушение."""
        await self._exec(
            "INSERT INTO violations(chat_id, user_id, username, full_name, word, action) "
            "VALUES($1, $2, $3, $4, $5, $6)",
            chat_id,
            user_id,
            username,
            full_name,
            word,
            action,
        )

    async def get_stats(self, chat_id: int) -> Stats:
        """Статистика нарушений по чату."""
        total_row = await self._one("SELECT COUNT(*) AS c FROM violations WHERE chat_id=$1", chat_id)
        users = await self._all(
            "SELECT user_id, MAX(full_name) AS n, MAX(username) AS u, COUNT(*) AS c FROM violations "
            "WHERE chat_id=$1 GROUP BY user_id ORDER BY c DESC LIMIT 10",
            chat_id,
        )
        words = await self._all(
            "SELECT word, COUNT(*) AS c FROM violations WHERE chat_id=$1 "
            "GROUP BY word ORDER BY c DESC LIMIT 5",
            chat_id,
        )
        return Stats(
            total=int(total_row["c"]) if total_row else 0,
            top_users=[
                (r["user_id"], f'{r["n"] or "?"}' + (f' (@{r["u"]})' if r["u"] else ""), r["c"])
                for r in users
            ],
            top_words=[(r["word"], r["c"]) for r in words],
            actions=await self.action_counts(chat_id),
        )

    # ================= v2: варны, история, муты =================
    async def add_warn(self, chat_id: int, user_id: int, reason: str | None, issued_by: int) -> int:
        """Добавляет варн, возвращает текущее число варнов."""
        async with self._p.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO warns(chat_id, user_id, reason, issued_by) VALUES($1, $2, $3, $4)",
                    chat_id, user_id, reason, issued_by,
                )
                return int(
                    await conn.fetchval(
                        "SELECT COUNT(*) FROM warns WHERE chat_id=$1 AND user_id=$2", chat_id, user_id
                    )
                )

    async def count_warns(self, chat_id: int, user_id: int) -> int:
        row = await self._one("SELECT COUNT(*) AS c FROM warns WHERE chat_id=$1 AND user_id=$2", chat_id, user_id)
        return int(row["c"]) if row else 0

    async def list_warns(self, chat_id: int, user_id: int) -> list[asyncpg.Record]:
        return await self._all(
            "SELECT reason, issued_by, created_at FROM warns WHERE chat_id=$1 AND user_id=$2 ORDER BY id",
            chat_id, user_id,
        )

    async def remove_last_warn(self, chat_id: int, user_id: int) -> bool:
        return (
            await self._exec(
                "DELETE FROM warns WHERE id=(SELECT id FROM warns WHERE chat_id=$1 AND user_id=$2 "
                "ORDER BY id DESC LIMIT 1)",
                chat_id, user_id,
            )
            > 0
        )

    async def clear_warns(self, chat_id: int, user_id: int) -> int:
        return await self._exec("DELETE FROM warns WHERE chat_id=$1 AND user_id=$2", chat_id, user_id)

    async def add_history(
        self, chat_id: int, user_id: int, kind: str, reason: str | None, issued_by: int, minutes: int | None = None
    ) -> None:
        """Ручные действия модераторов (авто-наказания за слова лежат в violations)."""
        await self._exec(
            "INSERT INTO mod_history(chat_id, user_id, kind, reason, issued_by, minutes) "
            "VALUES($1, $2, $3, $4, $5, $6)",
            chat_id, user_id, kind, reason, issued_by, minutes,
        )

    async def get_history(self, chat_id: int, user_id: int, limit: int = 15) -> list[asyncpg.Record]:
        """История наказаний: ручные + автоматические (по словам)."""
        return await self._all(
            "SELECT kind, reason, issued_by, minutes, created_at FROM ("
            " SELECT kind, reason, issued_by, minutes, created_at FROM mod_history"
            "  WHERE chat_id=$1 AND user_id=$2"
            " UNION ALL"
            " SELECT action, 'слово «' || word || '»', 0::bigint, NULL::integer, created_at FROM violations"
            "  WHERE chat_id=$1 AND user_id=$2"
            ") t ORDER BY created_at DESC LIMIT $3",
            chat_id, user_id, limit,
        )

    async def set_mute(
        self, chat_id: int, user_id: int, name: str | None, until: datetime | None,
        reason: str | None, issued_by: int,
    ) -> None:
        """Запоминает активный мут (until=None — навсегда)."""
        await self._exec(
            "INSERT INTO mutes(chat_id, user_id, name, until, reason, issued_by) VALUES($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET name=EXCLUDED.name, until=EXCLUDED.until, "
            "reason=EXCLUDED.reason, issued_by=EXCLUDED.issued_by",
            chat_id, user_id, name, until, reason, issued_by,
        )

    async def remove_mute(self, chat_id: int, user_id: int) -> None:
        await self._exec("DELETE FROM mutes WHERE chat_id=$1 AND user_id=$2", chat_id, user_id)

    async def list_mutes(self, chat_id: int) -> list[asyncpg.Record]:
        """Активные муты (истёкшие удаляются)."""
        await self._exec("DELETE FROM mutes WHERE chat_id=$1 AND until IS NOT NULL AND until<=now()", chat_id)
        return await self._all(
            "SELECT user_id, name, until, reason FROM mutes WHERE chat_id=$1 "
            "ORDER BY until NULLS LAST, user_id",
            chat_id,
        )

    async def action_counts(self, chat_id: int) -> dict[str, int]:
        """Сколько мутов/банов/варнов/киков было в чате."""
        rows = await self._all(
            "SELECT kind, COUNT(*) AS c FROM ("
            " SELECT kind FROM mod_history WHERE chat_id=$1"
            " UNION ALL SELECT action AS kind FROM violations WHERE chat_id=$1"
            ") t GROUP BY kind",
            chat_id,
        )
        return {r["kind"]: int(r["c"]) for r in rows}
