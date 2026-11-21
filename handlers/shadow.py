"""Теневое удаление сообщений выбранных пользователей (управляет только OWNER_ID)."""
from __future__ import annotations

import html
import logging
import os
import re
from logging.handlers import RotatingFileHandler

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db import Database

# --- тихий лог только в файл (в чат и в общий лог ничего не уходит) ---
log = logging.getLogger("shadow")
log.propagate = False
log.setLevel(logging.INFO)
if not log.handlers:
    _fh = RotatingFileHandler(
        os.getenv("SHADOW_LOG", "shadow.log"), maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_fh)

router = Router(name="shadow")

CMD_RE = re.compile(r"^/夏日四(?:@\w+)?(?:\s|$)")
_cache: dict[int, str] = {}  # user_id -> reason


def owner_id() -> int:
    """OWNER_ID из окружения (читается лениво, после load_dotenv). 0 = владельца нет."""
    try:
        return int(os.getenv("OWNER_ID", "0"))
    except ValueError:
        return 0


def _allowed_chats() -> set[int]:
    """SHADOW_CHATS=id1,id2 — если пусто, работает везде, где бот админ."""
    raw = os.getenv("SHADOW_CHATS", "")
    out: set[int] = set()
    for x in raw.split(","):
        x = x.strip()
        if x.lstrip("-").isdigit():
            out.add(int(x))
    return out


class ShadowCb(CallbackData, prefix="shadow_"):
    """Кнопки в списке «в тени»."""

    action: str
    user_id: int = 0


async def init(db: Database) -> None:
    """Создаёт таблицу и загружает кэш. Вызвать один раз до старта polling."""
    await db._exec(
        "CREATE TABLE IF NOT EXISTS shadow_mutes ("
        "user_id BIGINT PRIMARY KEY, reason TEXT, set_by BIGINT, "
        "chat_id BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    rows = await db._all("SELECT user_id, COALESCE(reason, '') AS reason FROM shadow_mutes")
    _cache.clear()
    _cache.update({r["user_id"]: r["reason"] for r in rows})
    log.info("shadow: загружено %d записей", len(_cache))


async def _toggle(db: Database, user_id: int, reason: str, set_by: int) -> bool:
    """Переключает состояние. True — теперь в тени, False — снят."""
    if user_id in _cache:
        await db._exec("DELETE FROM shadow_mutes WHERE user_id=$1", user_id)
        _cache.pop(user_id, None)
        return False
    await db._exec(
        "INSERT INTO shadow_mutes(user_id, reason, set_by, chat_id) VALUES($1, $2, $3, 0) "
        "ON CONFLICT(user_id) DO UPDATE SET reason=EXCLUDED.reason, set_by=EXCLUDED.set_by",
        user_id,
        reason,
        set_by,
    )
    _cache[user_id] = reason
    return True


async def _list_view(db: Database) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = await db._all("SELECT user_id, reason, created_at FROM shadow_mutes ORDER BY created_at")
    if not rows:
        return "Список пуст.", None
    lines = ["🌑 <b>В тени:</b>"]
    kb: list[list[InlineKeyboardButton]] = []
    for r in rows:
        reason = html.escape(r["reason"] or "—")
        lines.append(f"<code>{r['user_id']}</code> — {reason} ({r['created_at'].strftime('%Y-%m-%d')})")
        kb.append([InlineKeyboardButton(
            text=f"✖️ {r['user_id']}",
            callback_data=ShadowCb(action="off", user_id=r["user_id"]).pack(),
        )])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


class IsShadowed(BaseFilter):
    """Сообщение от пользователя из списка в разрешённом групповом чате."""

    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        if user is None or user.id not in _cache or message.chat.type not in ("group", "supergroup"):
            return False
        chats = _allowed_chats()
        return not chats or message.chat.id in chats


# --- удаление: роутер подключён первым, совпавший хендлер останавливает дальнейшую обработку ---
@router.message(IsShadowed())
@router.edited_message(IsShadowed())
async def shadow_delete(message: Message) -> None:
    """Молча удаляет сообщение. Любые ошибки (нет прав, уже удалено, >48 ч) — только в файл."""
    try:
        await message.delete()
    except TelegramAPIError as exc:
        log.info("не удалено chat=%s msg=%s: %s", message.chat.id, message.message_id, exc)
    except Exception:
        log.exception("shadow_delete")


# --- команда (только личка). Telegram не считает «/夏日四» bot_command-ом, поэтому ловим как текст ---
@router.message(F.chat.type == "private", F.text.regexp(CMD_RE))
async def shadow_cmd(message: Message, db: Database) -> None:
    """/夏日四 <id> [причина] | list. Не-владельцу — «Неизвестная команда»."""
    try:
        user = message.from_user
        if user is None or user.id != owner_id() or owner_id() == 0:
            await message.answer("Неизвестная команда")
            return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 2:
            await message.answer("Формат: /夏日四 &lt;id&gt; [причина] | list")
            return
        if parts[1].lower() == "list":
            text, markup = await _list_view(db)
            await message.answer(text, reply_markup=markup)
            return
        if not parts[1].lstrip("-").isdigit():
            await message.answer("Неверный ID")
            return
        target = int(parts[1])
        if target == user.id:
            await message.answer("Нельзя применить к себе")
            return
        reason = parts[2].strip()[:200] if len(parts) > 2 else ""
        on = await _toggle(db, target, reason, user.id)
        log.info("owner=%s target=%s on=%s reason=%r", user.id, target, on, reason)
        await message.answer(f"{'🌑 В тени' if on else '☀️ Снят'}: <code>{target}</code>")
    except Exception:
        log.exception("shadow_cmd")


@router.callback_query(ShadowCb.filter())
async def shadow_cb(cb: CallbackQuery, callback_data: ShadowCb, db: Database) -> None:
    """Кнопки списка. Чужим — пустой ответ без пояснений."""
    try:
        if cb.from_user.id != owner_id() or owner_id() == 0:
            await cb.answer()
            return
        if callback_data.action == "off" and callback_data.user_id in _cache:
            await _toggle(db, callback_data.user_id, "", cb.from_user.id)
        text, markup = await _list_view(db)
        if cb.message:
            await cb.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        await cb.answer()
    except TelegramAPIError as exc:
        log.info("shadow_cb: %s", exc)
        await cb.answer()
    except Exception:
        log.exception("shadow_cb")
