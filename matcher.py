"""Поиск запрещённых слов: регистронезависимо, с границами слов (в т.ч. для кириллицы)."""
from __future__ import annotations

import re

from db import Database, Word


def normalize(text: str) -> str:
    """Приводит текст к виду для сравнения: casefold и ё -> е."""
    return text.casefold().replace("ё", "е").strip()


def build_pattern(words: list[Word]) -> re.Pattern[str] | None:
    """Строит единый regex.

    Границы слов — через (?<!\\w) и (?!\\w); в Python 3 \\w юникодный,
    поэтому кириллица и цифры работают корректно. Слово с «*» на конце
    (например «дурак*») ловит и любые окончания: дураки, дураками...
    """
    parts: list[str] = []
    for w in sorted({normalize(x.word) for x in words}, key=len, reverse=True):
        if not w:
            continue
        if w.endswith("*"):
            core = re.escape(w.rstrip("*")).replace(r"\ ", r"\s+") + r"\w*"
        else:
            core = re.escape(w).replace(r"\ ", r"\s+")
        parts.append(core)
    if not parts:
        return None
    return re.compile(rf"(?<!\w)(?:{'|'.join(parts)})(?!\w)", re.UNICODE)


class WordMatcher:
    """Кэш скомпилированных паттернов по чатам."""

    def __init__(self) -> None:
        self._cache: dict[int, re.Pattern[str] | None] = {}

    def invalidate(self, chat_id: int) -> None:
        """Сбрасывает кэш чата (вызывать при изменении списка слов)."""
        self._cache.pop(chat_id, None)

    async def find(self, db: Database, chat_id: int, text: str) -> str | None:
        """Возвращает найденное запрещённое слово или None."""
        if chat_id not in self._cache:
            self._cache[chat_id] = build_pattern(await db.list_words(chat_id))
        pattern = self._cache[chat_id]
        if pattern is None:
            return None
        match = pattern.search(normalize(text))
        return match.group(0) if match else None
