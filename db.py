"""Слой работы с SQLite (aiosqlite). Все данные изолированы по chat_id."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id           INTEGER PRIMARY KEY,
    title             TEXT    NOT NULL DEFAULT '',
    punishment        TEXT    NOT NULL DEFAULT 'escalation',
    mute_minutes      INTEGER NOT NULL DEFAULT 60,
    delete_admin_msgs INTEGER NOT NULL DEFAULT 0,
    notify_admins     INTEGER NOT NULL DEFAULT 1,
    active            INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS words (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    word    TEXT    NOT NULL,
    UNIQUE(chat_id, word)
);
CREATE TABLE IF NOT EXISTS chat_admins (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY(chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS violations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    username   TEXT,
    full_name  TEXT,
    word       TEXT NOT NULL,
    action     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_viol_chat_user ON violations(chat_id, user_id);
"""

_EDITABLE_FIELDS = {"punishment", "mute_minutes", "delete_admin_msgs", "notify_admins"}


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


class Database:
    """Асинхронная обёртка над SQLite."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Открывает соединение и создаёт схему."""
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        """Закрывает соединение."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ---- низкоуровневые помощники ----
    @property
    def _conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database.connect() не вызван"
        return self._db

    async def _exec(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = await self._conn.execute(sql, params)
        rowcount = cur.rowcount
        await cur.close()
        await self._conn.commit()
        return rowcount

    async def _all(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    @staticmethod
    def _to_chat(row: aiosqlite.Row) -> ChatSettings:
        return ChatSettings(
            chat_id=row["chat_id"],
            title=row["title"],
            punishment=row["punishment"],
            mute_minutes=row["mute_minutes"],
            delete_admin_msgs=bool(row["delete_admin_msgs"]),
            notify_admins=bool(row["notify_admins"]),
            active=bool(row["active"]),
        )

    # ---- чаты ----
    async def upsert_chat(self, chat_id: int, title: str) -> None:
        """Создаёт чат или реактивирует существующий."""
        await self._exec(
            "INSERT INTO chats(chat_id, title, active) VALUES(?, ?, 1) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, active=1",
            (chat_id, title),
        )

    async def get_chat(self, chat_id: int) -> ChatSettings | None:
        """Возвращает настройки чата или None."""
        row = await self._one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
        return self._to_chat(row) if row else None

    async def deactivate_chat(self, chat_id: int) -> None:
        """Помечает чат неактивным (бота выгнали)."""
        await self._exec("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))

    async def set_chat_field(self, chat_id: int, field: str, value: int | str) -> None:
        """Меняет одно из разрешённых полей настроек."""
        if field not in _EDITABLE_FIELDS:
            raise ValueError(f"Недопустимое поле: {field}")
        await self._exec(f"UPDATE chats SET {field}=? WHERE chat_id=?", (value, chat_id))

    async def migrate_chat(self, old_id: int, new_id: int) -> None:
        """Переносит данные при миграции группы в супергруппу."""
        await self._exec("DELETE FROM chats WHERE chat_id=?", (new_id,))
        await self._exec("UPDATE chats SET chat_id=? WHERE chat_id=?", (new_id, old_id))
        for table in ("words", "chat_admins"):
            await self._exec(f"UPDATE OR IGNORE {table} SET chat_id=? WHERE chat_id=?", (new_id, old_id))
            await self._exec(f"DELETE FROM {table} WHERE chat_id=?", (old_id,))
        await self._exec("UPDATE violations SET chat_id=? WHERE chat_id=?", (new_id, old_id))

    # ---- админы ----
    async def add_admin(self, chat_id: int, user_id: int) -> None:
        """Запоминает администратора чата (для уведомлений и меню)."""
        await self._exec(
            "INSERT OR IGNORE INTO chat_admins(chat_id, user_id) VALUES(?, ?)", (chat_id, user_id)
        )

    async def remove_admin(self, chat_id: int, user_id: int) -> None:
        """Удаляет админа из списка."""
        await self._exec("DELETE FROM chat_admins WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    async def get_chat_admin_ids(self, chat_id: int) -> list[int]:
        """ID админов чата, известных боту."""
        rows = await self._all("SELECT user_id FROM chat_admins WHERE chat_id=?", (chat_id,))
        return [r["user_id"] for r in rows]

    async def get_admin_chats(self, user_id: int) -> list[ChatSettings]:
        """Активные чаты, где пользователь известен как админ."""
        rows = await self._all(
            "SELECT c.* FROM chats c JOIN chat_admins a ON a.chat_id=c.chat_id "
            "WHERE a.user_id=? AND c.active=1 ORDER BY c.title",
            (user_id,),
        )
        return [self._to_chat(r) for r in rows]

    # ---- слова ----
    async def list_words(self, chat_id: int) -> list[Word]:
        """Список запрещённых слов чата."""
        rows = await self._all("SELECT id, word FROM words WHERE chat_id=? ORDER BY word", (chat_id,))
        return [Word(r["id"], r["word"]) for r in rows]

    async def add_words(self, chat_id: int, words: Sequence[str]) -> int:
        """Добавляет слова, возвращает число реально добавленных."""
        added = 0
        for w in words:
            added += max(
                await self._exec("INSERT OR IGNORE INTO words(chat_id, word) VALUES(?, ?)", (chat_id, w)), 0
            )
        return added

    async def delete_word(self, chat_id: int, word_id: int) -> bool:
        """Удаляет слово по id."""
        return await self._exec("DELETE FROM words WHERE id=? AND chat_id=?", (word_id, chat_id)) > 0

    # ---- нарушения ----
    async def count_violations(self, chat_id: int, user_id: int) -> int:
        """Сколько нарушений уже у пользователя в чате."""
        row = await self._one(
            "SELECT COUNT(*) AS c FROM violations WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        return int(row["c"]) if row else 0

    async def add_violation(
        self, chat_id: int, user_id: int, username: str | None, full_name: str, word: str, action: str
    ) -> None:
        """Записывает нарушение."""
        await self._exec(
            "INSERT INTO violations(chat_id, user_id, username, full_name, word, action) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, username, full_name, word, action),
        )

    async def get_stats(self, chat_id: int) -> Stats:
        """Статистика нарушений по чату."""
        total_row = await self._one("SELECT COUNT(*) AS c FROM violations WHERE chat_id=?", (chat_id,))
        users = await self._all(
            "SELECT user_id, MAX(full_name) AS n, MAX(username) AS u, COUNT(*) AS c FROM violations "
            "WHERE chat_id=? GROUP BY user_id ORDER BY c DESC LIMIT 10",
            (chat_id,),
        )
        words = await self._all(
            "SELECT word, COUNT(*) AS c FROM violations WHERE chat_id=? "
            "GROUP BY word ORDER BY c DESC LIMIT 5",
            (chat_id,),
        )
        return Stats(
            total=int(total_row["c"]) if total_row else 0,
            top_users=[
                (r["user_id"], f'{r["n"] or "?"}' + (f' (@{r["u"]})' if r["u"] else ""), r["c"])
                for r in users
            ],
            top_words=[(r["word"], r["c"]) for r in words],
        )
