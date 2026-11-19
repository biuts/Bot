"""Вспомогательные функции."""
from __future__ import annotations

import time

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

_ADMIN_CACHE: dict[tuple[int, int], tuple[float, bool]] = {}


async def is_chat_admin(
    bot: Bot, chat_id: int, user_id: int, *, fresh: bool = False, ttl: float = 60.0
) -> bool:
    """Проверяет через getChatMember, админ ли пользователь (с кэшем на ttl секунд)."""
    key = (chat_id, user_id)
    now = time.monotonic()
    if not fresh:
        hit = _ADMIN_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        result = member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    except (TelegramBadRequest, TelegramForbiddenError):
        result = False
    if len(_ADMIN_CACHE) > 10_000:
        _ADMIN_CACHE.clear()
    _ADMIN_CACHE[key] = (now, result)
    return result


DURATIONS = (5, 15, 60, 360, 720, 1440, 4320, 10080)  # минуты для кнопок мута


def fmt_for(minutes: int) -> str:
    """«на 1 ч.» или «навсегда»."""
    return "навсегда" if minutes <= 0 else f"на {fmt_minutes(minutes)}"


def fmt_minutes(minutes: int) -> str:
    """Человекочитаемая длительность (0 = навсегда)."""
    if minutes <= 0:
        return "навсегда"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} дн."
    if minutes % 60 == 0:
        return f"{minutes // 60} ч."
    return f"{minutes} мин."
