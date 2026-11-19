"""Обработка событий в группах: онбординг, модерация."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.types import (
    Chat,
    ChatMember,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import keyboards as kb
from db import ChatSettings, Database
from matcher import WordMatcher
from utils import fmt_for, fmt_minutes, is_chat_admin

logger = logging.getLogger(__name__)
router = Router(name="group")

GROUPS = F.chat.type.in_({"group", "supergroup"})
MAX_AGE = timedelta(hours=48)  # Telegram не даёт удалять сообщения старше 48 ч
WARN_TTL = 15  # секунд до автоудаления публичных уведомлений

_bg_tasks: set[asyncio.Task[None]] = set()
_rights_warned: dict[int, float] = {}


# ----------------------------------------------------------------- helpers
def _spawn(coro: "asyncio.Future[None] | object") -> None:
    task = asyncio.ensure_future(coro)  # type: ignore[arg-type]
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _delete_later(bot: Bot, chat_id: int, message_id: int, delay: int = WARN_TTL) -> None:
    await asyncio.sleep(delay)
    with suppress(TelegramAPIError):
        await bot.delete_message(chat_id, message_id)


async def _say_temp(bot: Bot, chat_id: int, text: str) -> None:
    """Публичное уведомление, которое само исчезает."""
    try:
        sent = await bot.send_message(chat_id, text)
        _spawn(_delete_later(bot, chat_id, sent.message_id))
    except TelegramAPIError as exc:
        logger.warning("Не удалось отправить уведомление в %s: %s", chat_id, exc)


async def _dm(bot: Bot, user_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    """Личное сообщение; False, если пользователь не запускал бота."""
    for _ in range(2):
        try:
            await bot.send_message(user_id, text, reply_markup=markup)
            return True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
        except (TelegramForbiddenError, TelegramBadRequest):
            return False
        except TelegramAPIError as exc:
            logger.warning("DM %s не доставлен: %s", user_id, exc)
            return False
    return False


async def notify_admins(bot: Bot, db: Database, chat: ChatSettings, text: str) -> None:
    """Рассылает уведомление админам чата (если включено)."""
    if not chat.notify_admins:
        return
    for uid in await db.get_chat_admin_ids(chat.chat_id):
        if not await is_chat_admin(bot, chat.chat_id, uid):
            await db.remove_admin(chat.chat_id, uid)
            continue
        await _dm(bot, uid, text)


def _missing_rights(member: ChatMember) -> list[str]:
    """Каких прав не хватает боту."""
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return ["быть администратором"]
    missing = []
    if not getattr(member, "can_delete_messages", False):
        missing.append("удаление сообщений")
    if not getattr(member, "can_restrict_members", False):
        missing.append("блокировка пользователей (мут и бан)")
    return missing


def _deeplink_markup(username: str, chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Настроить бота", url=f"https://t.me/{username}?start=c{chat_id}")]
        ]
    )


async def _onboard(bot: Bot, db: Database, chat: Chat, missing: list[str]) -> None:
    """Шлёт каждому админу приветствие с кнопкой настройки."""
    title = html.escape(chat.title or "чат")
    rights = (
        "⚠️ <b>Мне не хватает прав:</b> " + ", ".join(missing) + ".\nВыдайте их в настройках администраторов группы."
        if missing
        else "✅ Все необходимые права выданы."
    )
    text = (
        f"👋 Я добавлен в «{title}».\n\n{rights}\n\n"
        "Нажмите «Настроить бота», чтобы задать триггер-слова и наказания — "
        "всё делается здесь, в личке, без команд в группе."
    )
    failed = 0
    for adm in await bot.get_chat_administrators(chat.id):
        if adm.user.is_bot:
            continue
        await db.add_admin(chat.id, adm.user.id)
        if not await _dm(bot, adm.user.id, text, kb.open_settings(chat.id)):
            failed += 1
    if failed or missing:
        me = await bot.me()
        note = "👋 Я модератор чата. Админы, нажмите кнопку и запустите бота, чтобы настроить его."
        if missing:
            note += "\n\n⚠️ Для работы мне нужны права: " + ", ".join(missing) + "."
        await bot.send_message(chat.id, note, reply_markup=_deeplink_markup(me.username or "", chat.id))


# ---------------------------------------------------------------- handlers
@router.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def on_bot_status(event: ChatMemberUpdated, bot: Bot, db: Database) -> None:
    """Бота добавили/повысили/выгнали."""
    new = event.new_chat_member
    if new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await db.deactivate_chat(event.chat.id)
        logger.info("Бот удалён из чата %s", event.chat.id)
        return
    if new.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return
    await db.upsert_chat(event.chat.id, event.chat.title or "")
    try:
        await _onboard(bot, db, event.chat, _missing_rights(new))
    except TelegramAPIError:
        logger.exception("Онбординг чата %s не удался", event.chat.id)


@router.message(GROUPS, F.migrate_to_chat_id)
async def on_migrate(message: Message, db: Database, matcher: WordMatcher) -> None:
    """Группа стала супергруппой — переносим данные на новый chat_id."""
    new_id = message.migrate_to_chat_id
    if new_id:
        await db.migrate_chat(message.chat.id, new_id)
        matcher.invalidate(message.chat.id)
        matcher.invalidate(new_id)
        logger.info("Чат %s мигрировал в %s", message.chat.id, new_id)


@router.message(GROUPS, Command("start"))
async def group_start(message: Message, bot: Bot, db: Database) -> None:
    """/start админа в группе -> меню настроек в личку."""
    user = message.from_user
    if user is None or not await is_chat_admin(bot, message.chat.id, user.id, fresh=True):
        return
    await db.upsert_chat(message.chat.id, message.chat.title or "")
    await db.add_admin(message.chat.id, user.id)
    sent = await _dm(
        bot,
        user.id,
        f"⚙️ Настройка чата «{html.escape(message.chat.title or '')}»",
        kb.open_settings(message.chat.id),
    )
    if sent:
        await message.reply("Отправил меню настроек вам в личные сообщения.")
    else:
        me = await bot.me()
        await message.reply(
            "Откройте личку со мной и нажмите Start:", reply_markup=_deeplink_markup(me.username or "", message.chat.id)
        )


async def _try_delete(message: Message) -> tuple[bool, str]:
    """Удаляет сообщение. Возвращает (успех, причина)."""
    try:
        await message.delete()
        return True, "ok"
    except TelegramBadRequest as exc:
        err = str(exc).lower()
        if "not found" in err:
            return True, "already_deleted"
        if "can't be deleted" in err:
            return False, "too_old"
        if "not enough rights" in err or "need administrator" in err:
            return False, "no_rights"
        return False, err
    except TelegramForbiddenError:
        return False, "no_rights"


def _decide(mode: str, count: int) -> str:
    """Выбор наказания по режиму и номеру нарушения."""
    if mode == "escalation":
        return "warn" if count <= 1 else "mute" if count == 2 else "ban"
    return mode


async def _punish(
    bot: Bot, db: Database, message: Message, chat: ChatSettings, action: str, count: int
) -> tuple[str, bool]:
    """Применяет наказание. Возвращает (описание, успех)."""
    user = message.from_user
    assert user is not None
    who = user.mention_html()
    try:
        if action == "warn":
            await _say_temp(bot, chat.chat_id, f"⚠️ {who}, сообщение удалено: запрещённое слово. Нарушение №{count}.")
            return "⚠️ предупреждение", True
        if action == "mute":
            # mute_minutes <= 0 -> навсегда (until_date=None)
            until = None if chat.mute_minutes <= 0 else datetime.now(timezone.utc) + timedelta(minutes=chat.mute_minutes)
            await bot.restrict_chat_member(
                chat.chat_id, user.id, permissions=ChatPermissions(can_send_messages=False), until_date=until
            )
            await db.set_mute(chat.chat_id, user.id, user.full_name, until, "запрещённое слово", 0)
            await _say_temp(
                bot, chat.chat_id, f"🔇 {who} в муте {fmt_for(chat.mute_minutes)} (нарушение №{count})."
            )
            return f"🔇 мут {fmt_for(chat.mute_minutes)}", True
        if action == "kick":
            await bot.ban_chat_member(chat.chat_id, user.id)
            await bot.unban_chat_member(chat.chat_id, user.id, only_if_banned=True)
            await db.remove_mute(chat.chat_id, user.id)
            await _say_temp(bot, chat.chat_id, f"👢 {who} исключён (нарушение №{count}).")
            return "👢 кик", True
        await bot.ban_chat_member(chat.chat_id, user.id)
        await db.remove_mute(chat.chat_id, user.id)
        await _say_temp(bot, chat.chat_id, f"🚫 {who} заблокирован (нарушение №{count}).")
        return "🚫 бан", True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("Наказание %s в %s не применено: %s", action, chat.chat_id, exc)
        return f"{action} — не удалось (проверьте права бота)", False


@router.message(GROUPS, F.text | F.caption)
@router.edited_message(GROUPS, F.text | F.caption)
async def moderate(message: Message, bot: Bot, db: Database, matcher: WordMatcher) -> None:
    """Проверяет сообщение на запрещённые слова и наказывает нарушителя."""
    try:
        await _moderate(message, bot, db, matcher)
    except Exception:  # хендлер не должен падать из-за одного сообщения
        logger.exception("Ошибка модерации сообщения %s в чате %s", message.message_id, message.chat.id)


async def _moderate(message: Message, bot: Bot, db: Database, matcher: WordMatcher) -> None:
    user = message.from_user
    if user is None or user.is_bot or message.sender_chat is not None:
        return  # анонимные админы, каналы, боты — пропускаем
    text = message.text or message.caption or ""
    chat_id = message.chat.id

    word = await matcher.find(db, chat_id, text)
    if word is None:
        return

    chat = await db.get_chat(chat_id)
    if chat is None:
        await db.upsert_chat(chat_id, message.chat.title or "")
        chat = await db.get_chat(chat_id)
        assert chat is not None

    is_admin = await is_chat_admin(bot, chat_id, user.id)
    if is_admin and not chat.delete_admin_msgs:
        return

    if datetime.now(timezone.utc) - message.date > MAX_AGE:
        logger.info("Сообщение %s старше 48 ч — удалить нельзя, пропускаю", message.message_id)
        return

    deleted, reason = await _try_delete(message)
    if not deleted:
        if reason == "no_rights" and time.monotonic() - _rights_warned.get(chat_id, 0) > 600:
            _rights_warned[chat_id] = time.monotonic()
            await notify_admins(
                bot, db, chat, f"⚠️ В «{html.escape(chat.title)}» я не могу удалять сообщения. Выдайте мне право удаления."
            )
        logger.warning("Не удалил сообщение %s (%s): %s", message.message_id, chat_id, reason)

    name = user.full_name
    if is_admin:
        await notify_admins(
            bot, db, chat,
            f"🗑 «{html.escape(chat.title)}»: удалено сообщение админа {html.escape(name)}, слово «{html.escape(word)}».",
        )
        return

    count = await db.count_violations(chat_id, user.id) + 1
    action = _decide(chat.punishment, count)
    description, _ok = await _punish(bot, db, message, chat, action, count)
    await db.add_violation(chat_id, user.id, user.username, name, word, action)

    uname = f" (@{user.username})" if user.username else ""
    await notify_admins(
        bot, db, chat,
        f"🚨 <b>{html.escape(chat.title)}</b>\n"
        f"Нарушитель: {html.escape(name)}{uname}\n"
        f"Слово: «{html.escape(word)}»\n"
        f"Нарушение №{count}\n"
        f"Наказание: {description}"
        + ("" if deleted else "\n⚠️ Сообщение удалить не удалось"),
    )
