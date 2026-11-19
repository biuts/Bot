"""v2: ручная модерация по ID, варны, списки и история (поверх текущей архитектуры)."""
from __future__ import annotations

import html
import logging
import re
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatPermissions, Message

import keyboards as kb
from db import ChatSettings, Database
from keyboards import V2
from utils import fmt_for, fmt_minutes, is_chat_admin

logger = logging.getLogger(__name__)
router = Router(name="moderation")

GROUPS = F.chat.type.in_({"group", "supergroup"})
PRIVATE = F.chat.type == "private"
REASON_TTL = 120  # секунд на ввод причины
MAX_LIST = 40
PROTECTED = "❌ Нельзя наказывать администратора."

# Мут = запрет всех видов отправки (сообщения, медиа, стикеры/гифки, превью ссылок, опросы)
_SEND_FLAGS = (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews",
)
MUTED = ChatPermissions(**{f: False for f in _SEND_FLAGS})
ALL_ALLOWED = ChatPermissions(**{f: True for f in _SEND_FLAGS})

KIND_TITLES = {
    "mute": "🔇 Мут", "unmute": "🔊 Размут", "ban": "🚫 Бан", "unban": "✅ Разбан",
    "kick": "👢 Кик", "warn": "⚠️ Варн", "unwarn": "➖ Снят варн",
}

Action = Callable[..., Awaitable[str]]


class AskReason(StatesGroup):
    """Ожидание причины мута от админа (в группе)."""

    waiting = State()


class AskHistoryId(StatesGroup):
    """Ожидание ID для истории (в личке)."""

    waiting = State()


# ------------------------------------------------------------------ helpers
def _link(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a> (<code>{uid}</code>)'


def _reason(reason: str) -> str:
    return html.escape(reason) if reason else "—"


def _by(issuer: int) -> str:
    return "\nВыдал: " + ("анонимный админ" if issuer == 0 else f"<code>{issuer}</code>")


def _dt(dt: datetime | None, short: bool = False) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%d.%m %H:%M" if short else "%d.%m.%y %H:%M")


def _explain(exc: TelegramAPIError) -> str:
    e = str(exc).lower()
    if "participant_id_invalid" in e or "user not found" in e or "member not found" in e:
        return "пользователь с таким ID не найден"
    if "participant" in e:
        return "пользователь не в чате"
    if "supergroup" in e:
        return "это действие работает только в супергруппах"
    if "administrator of the chat" in e or "chat owner" in e:
        return "нельзя наказывать администратора"
    if "not enough rights" in e or "need administrator" in e or "forbidden" in e or "kicked" in e:
        return "у бота нет прав (он должен быть админом с правом «Блокировка пользователей»)"
    return str(exc)[:150]


async def _name(bot: Bot, chat_id: int, uid: int) -> str:
    try:
        return (await bot.get_chat_member(chat_id, uid)).user.full_name
    except TelegramAPIError:
        return str(uid)


async def _chat_settings(db: Database, chat) -> ChatSettings:
    cs = await db.get_chat(chat.id)
    if cs is None:
        await db.upsert_chat(chat.id, chat.title or "")
        cs = await db.get_chat(chat.id)
    assert cs is not None
    return cs


async def _log(bot: Bot, chat: ChatSettings, text: str) -> None:
    """Дублирует действие в лог-канал (если задан через /setlog)."""
    if not chat.log_chat_id:
        return
    try:
        await bot.send_message(chat.log_chat_id, f"📝 <b>{html.escape(chat.title)}</b>\n{text}")
    except TelegramAPIError as exc:
        logger.warning("Не удалось записать в лог-канал %s: %s", chat.log_chat_id, exc)


async def _issuer(bot: Bot, message: Message) -> int | None:
    """ID админа, выдавшего команду; 0 — анонимный админ; None — не админ."""
    sc = message.sender_chat
    if sc is not None and sc.id == message.chat.id:
        return 0
    user = message.from_user
    if user is None or user.is_bot:
        return None
    return user.id if await is_chat_admin(bot, message.chat.id, user.id, fresh=True) else None


def _target_and_rest(message: Message, args: str | None) -> tuple[int | None, str]:
    """ID цели: первым аргументом или из reply."""
    rest = (args or "").strip()
    parts = rest.split(maxsplit=1)
    if parts and re.fullmatch(r"\d{5,15}", parts[0]):
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and reply.sender_chat is None:
        return reply.from_user.id, rest
    return None, rest


_DUR = re.compile(r"^(\d+)([mмhчdд])$")


def _parse_duration(token: str) -> int | None:
    """30m / 2h / 1d (м/ч/д тоже) -> минуты; perm/навсегда -> 0; иначе None."""
    t = token.lower()
    if t in {"perm", "forever", "навсегда"}:
        return 0
    m = _DUR.match(t)
    if not m:
        return None
    mult = {"m": 1, "м": 1, "h": 60, "ч": 60, "d": 1440, "д": 1440}[m.group(2)]
    return int(m.group(1)) * mult or None


def _split_time_reason(rest: str) -> tuple[int | None, str]:
    parts = rest.split(maxsplit=1)
    if parts:
        minutes = _parse_duration(parts[0])
        if minutes is not None:
            return minutes, (parts[1] if len(parts) > 1 else "")
    return None, rest


async def _render(cb: CallbackQuery, text: str, markup=None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            logger.debug("edit_text: %s", exc)


async def _record(
    bot: Bot, db: Database, chat: ChatSettings, target: int, kind: str,
    reason: str, issuer: int, text: str, minutes: int | None = None,
) -> str:
    await db.add_history(chat.chat_id, target, kind, reason or None, issuer, minutes)
    await _log(bot, chat, text + _by(issuer))
    return text


# ------------------------------------------------------------- core actions
# Единая сигнатура: (bot, db, chat, target, reason, issuer) -> текст результата. Не бросают исключений.
async def apply_mute(
    bot: Bot, db: Database, chat: ChatSettings, target: int, reason: str, issuer: int, *, minutes: int = 0
) -> str:
    if await is_chat_admin(bot, chat.chat_id, target, fresh=True):
        return PROTECTED
    # until_date=None == навсегда (то же, что 0 в Bot API)
    until = None if minutes <= 0 else datetime.now(timezone.utc) + timedelta(minutes=minutes)
    try:
        await bot.restrict_chat_member(chat.chat_id, target, permissions=MUTED, until_date=until)
    except TelegramAPIError as exc:
        logger.warning("mute %s в %s: %s", target, chat.chat_id, exc)
        return f"❌ Не удалось замутить: {_explain(exc)}."
    name = await _name(bot, chat.chat_id, target)
    await db.set_mute(chat.chat_id, target, name, until, reason or None, issuer)
    text = f"🔇 {_link(target, name)} замьючен {fmt_for(minutes)}. Причина: {_reason(reason)}"
    return await _record(bot, db, chat, target, "mute", reason, issuer, text, minutes)


async def apply_unmute(bot: Bot, db: Database, chat: ChatSettings, target: int, reason: str, issuer: int) -> str:
    try:
        full = await bot.get_chat(chat.chat_id)
        perms = getattr(full, "permissions", None) or ALL_ALLOWED  # права чата по умолчанию
        await bot.restrict_chat_member(chat.chat_id, target, permissions=perms)
    except TelegramAPIError as exc:
        return f"❌ Не удалось снять мут: {_explain(exc)}."
    await db.remove_mute(chat.chat_id, target)
    name = await _name(bot, chat.chat_id, target)
    return await _record(bot, db, chat, target, "unmute", "", issuer, f"🔊 {_link(target, name)} размьючен.")


async def apply_ban(bot: Bot, db: Database, chat: ChatSettings, target: int, reason: str, issuer: int) -> str:
    if await is_chat_admin(bot, chat.chat_id, target, fresh=True):
        return PROTECTED
    try:
        await bot.ban_chat_member(chat.chat_id, target)
    except TelegramAPIError as exc:
        return f"❌ Не удалось заблокировать: {_explain(exc)}."
    await db.remove_mute(chat.chat_id, target)
    name = await _name(bot, chat.chat_id, target)
    text = f"🚫 {_link(target, name)} заблокирован. Причина: {_reason(reason)}"
    return await _record(bot, db, chat, target, "ban", reason, issuer, text)


async def apply_unban(bot: Bot, db: Database, chat: ChatSettings, target: int, reason: str, issuer: int) -> str:
    try:
        await bot.unban_chat_member(chat.chat_id, target, only_if_banned=True)
    except TelegramAPIError as exc:
        return f"❌ Не удалось разблокировать: {_explain(exc)}."
    name = await _name(bot, chat.chat_id, target)
    return await _record(bot, db, chat, target, "unban", "", issuer, f"✅ {_link(target, name)} разблокирован.")


async def apply_kick(bot: Bot, db: Database, chat: ChatSettings, target: int, reason: str, issuer: int) -> str:
    """Кик = бан + мгновенный разбан (пользователь может вернуться по ссылке)."""
    if await is_chat_admin(bot, chat.chat_id, target, fresh=True):
        return PROTECTED
    try:
        await bot.ban_chat_member(chat.chat_id, target)
        await bot.unban_chat_member(chat.chat_id, target, only_if_banned=True)
    except TelegramAPIError as exc:
        return f"❌ Не удалось исключить: {_explain(exc)}."
    await db.remove_mute(chat.chat_id, target)
    text = f"👢 <code>{target}</code> исключён из чата. Причина: {_reason(reason)}"
    return await _record(bot, db, chat, target, "kick", reason, issuer, text)


async def apply_warn(bot: Bot, db: Database, chat: ChatSettings, target: int, reason: str, issuer: int) -> str:
    if await is_chat_admin(bot, chat.chat_id, target, fresh=True):
        return PROTECTED
    count = await db.add_warn(chat.chat_id, target, reason or None, issuer)
    name = await _name(bot, chat.chat_id, target)
    text = (
        f"⚠️ {_link(target, name)} получил предупреждение {count}/{chat.warn_limit}. "
        f"Причина: {_reason(reason)}"
    )
    text = await _record(bot, db, chat, target, "warn", reason, issuer, text)
    if count >= chat.warn_limit:
        await db.clear_warns(chat.chat_id, target)  # счётчик обнуляется после наказания
        why = f"лимит предупреждений ({chat.warn_limit})"
        if chat.warn_action == "ban":
            sub = await apply_ban(bot, db, chat, target, why, 0)
        elif chat.warn_action == "kick":
            sub = await apply_kick(bot, db, chat, target, why, 0)
        else:
            sub = await apply_mute(bot, db, chat, target, why, 0, minutes=chat.mute_minutes)
        text += "\n" + sub
    return text


async def apply_unwarn(bot: Bot, db: Database, chat: ChatSettings, target: int, reason: str, issuer: int) -> str:
    if not await db.remove_last_warn(chat.chat_id, target):
        return f"У <code>{target}</code> нет предупреждений."
    left = await db.count_warns(chat.chat_id, target)
    text = f"➖ С <code>{target}</code> снято последнее предупреждение ({left}/{chat.warn_limit})."
    return await _record(bot, db, chat, target, "unwarn", "", issuer, text)


async def apply_clearwarns(bot: Bot, db: Database, chat: ChatSettings, target: int, reason: str, issuer: int) -> str:
    n = await db.clear_warns(chat.chat_id, target)
    if not n:
        return f"У <code>{target}</code> нет предупреждений."
    return await _record(
        bot, db, chat, target, "unwarn", "сброс всех", issuer, f"🧹 У <code>{target}</code> сброшено предупреждений: {n}."
    )


async def show_warns(bot: Bot, db: Database, chat: ChatSettings, target: int, reason: str, issuer: int) -> str:
    rows = await db.list_warns(chat.chat_id, target)
    if not rows:
        return f"У <code>{target}</code> нет предупреждений."
    lines = [f"⚠️ Предупреждения <code>{target}</code>: {len(rows)}/{chat.warn_limit}"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {_dt(r['created_at'])} — {_reason(r['reason'] or '')}")
    return "\n".join(lines)


async def history_text(db: Database, chat_id: int, uid: int) -> str:
    rows = await db.get_history(chat_id, uid, 15)
    if not rows:
        return f"📜 История <code>{uid}</code> пуста."
    lines = [f"📜 <b>История <code>{uid}</code></b> (последние 15, время UTC)"]
    for r in rows:
        kind = r["kind"]
        dur = f" {fmt_for(r['minutes'])}" if kind == "mute" and r["minutes"] is not None else ""
        by = "бот" if r["issued_by"] == 0 else f"<code>{r['issued_by']}</code>"
        lines.append(
            f"{_dt(r['created_at'])} — {KIND_TITLES.get(kind, kind)}{dur}\n"
            f"   {_reason(r['reason'] or '')} · выдал: {by}"
        )
    return "\n".join(lines)


async def show_history(bot: Bot, db: Database, chat: ChatSettings, target: int, reason: str, issuer: int) -> str:
    return await history_text(db, chat.chat_id, target)


async def mutelist_text(db: Database, chat_id: int) -> str:
    rows = await db.list_mutes(chat_id)
    if not rows:
        return "🔇 Активных мутов нет."
    lines = [f"🔇 <b>Активные муты: {len(rows)}</b> (время UTC)"]
    for r in rows[:MAX_LIST]:
        until = "навсегда" if r["until"] is None else "до " + _dt(r["until"], short=True)
        lines.append(f"• {html.escape(r['name'] or '?')} (<code>{r['user_id']}</code>) — {until}")
    if len(rows) > MAX_LIST:
        lines.append(f"…и ещё {len(rows) - MAX_LIST}")
    return "\n".join(lines)


# ----------------------------------------------------------------- команды
async def _run(
    message: Message, command: CommandObject, bot: Bot, db: Database, usage: str, action: Action
) -> None:
    issuer = await _issuer(bot, message)
    if issuer is None:
        return  # не админ — молча игнорируем
    target, rest = _target_and_rest(message, command.args)
    if target is None:
        await message.reply(usage)
        return
    chat = await _chat_settings(db, message.chat)
    try:
        text = await action(bot, db, chat, target, rest.strip()[:200], issuer)
    except Exception:
        logger.exception("Ошибка команды %s", command.command)
        text = "❌ Внутренняя ошибка, подробности в логах."
    await message.reply(text)


def _register(name: str, usage: str, action: Action) -> None:
    @router.message(GROUPS, Command(name))
    async def _handler(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
        await _run(message, command, bot, db, usage, action)


_register("unmute", "Использование: <code>/unmute ID</code>", apply_unmute)
_register("ban", "Использование: <code>/ban ID [причина]</code>", apply_ban)
_register("unban", "Использование: <code>/unban ID</code>", apply_unban)
_register("kick", "Использование: <code>/kick ID [причина]</code>", apply_kick)
_register("warn", "Использование: <code>/warn ID [причина]</code> или ответом на сообщение", apply_warn)
_register("unwarn", "Использование: <code>/unwarn ID</code>", apply_unwarn)
_register("clearwarns", "Использование: <code>/clearwarns ID</code>", apply_clearwarns)
_register("warns", "Использование: <code>/warns ID</code>", show_warns)
_register("history", "Использование: <code>/history ID</code>", show_history)


@router.message(GROUPS, Command("mute"))
async def cmd_mute(message: Message, command: CommandObject, bot: Bot, db: Database, state: FSMContext) -> None:
    """/mute ID [время] [причина]. Без времени — клавиатура выбора."""
    issuer = await _issuer(bot, message)
    if issuer is None:
        return
    target, rest = _target_and_rest(message, command.args)
    if target is None:
        await message.reply(
            "Использование: <code>/mute ID [время] [причина]</code>\n"
            "Время: <code>30m</code>, <code>2h</code>, <code>1d</code>, <code>perm</code>. "
            "Без времени — покажу кнопки."
        )
        return
    chat = await _chat_settings(db, message.chat)
    minutes, reason = _split_time_reason(rest)
    if minutes is None:
        await state.update_data(pre={"uid": target, "reason": rest[:200]} if rest else None)
        await message.reply(
            f"🔇 На сколько замутить <code>{target}</code>?",
            reply_markup=kb.duration_menu(chat.chat_id, "gm", target, cancel=True),
        )
        return
    await message.reply(await apply_mute(bot, db, chat, target, reason[:200], issuer, minutes=minutes))


@router.message(GROUPS, Command("mutelist"))
async def cmd_mutelist(message: Message, bot: Bot, db: Database) -> None:
    if await _issuer(bot, message) is None:
        return
    await message.reply(await mutelist_text(db, message.chat.id))


@router.message(GROUPS, Command("setlog"))
async def cmd_setlog(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """/setlog <id канала> — куда дублировать действия; /setlog off — выключить."""
    if await _issuer(bot, message) is None:
        return
    chat = await _chat_settings(db, message.chat)
    arg = (command.args or "").strip().lower()
    if arg in {"off", "0", "выкл"}:
        await db.set_chat_field(chat.chat_id, "log_chat_id", None)
        await message.reply("📝 Лог-канал выключен.")
    elif re.fullmatch(r"-?\d{5,20}", arg):
        try:
            await bot.send_message(int(arg), f"📝 Лог модерации «{html.escape(chat.title)}» подключён.")
        except TelegramAPIError as exc:
            await message.reply(f"❌ Не могу писать туда: {_explain(exc)}. Добавьте бота админом в канал.")
            return
        await db.set_chat_field(chat.chat_id, "log_chat_id", int(arg))
        await message.reply("📝 Лог-канал подключён.")
    else:
        await message.reply("Использование: <code>/setlog -100XXXXXXXXXX</code> или <code>/setlog off</code>")


# ------------------------------------------- мут через кнопки (группа, v2_gm/gr/gc)
@router.message(GROUPS, AskReason.waiting, F.text, ~F.text.startswith("/"))
async def got_reason(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    data = await state.get_data()
    if time.time() > data.get("expires", 0):
        await state.clear()
        raise SkipHandler  # причина просрочена — обычная обработка сообщения
    issuer = await _issuer(bot, message)
    if issuer is None:
        await state.clear()
        raise SkipHandler
    await state.clear()
    chat = await _chat_settings(db, message.chat)
    reply = await apply_mute(
        bot, db, chat, int(data["uid"]), (message.text or "")[:200], issuer, minutes=int(data["minutes"])
    )
    with suppress(TelegramAPIError):
        await message.delete()
    await message.answer(reply)


async def _admin_alert(cb: CallbackQuery, bot: Bot, chat_id: int) -> bool:
    if await is_chat_admin(bot, chat_id, cb.from_user.id, fresh=True):
        return True
    await cb.answer("Только для админов.", show_alert=True)
    return False


@router.callback_query(V2.filter(F.act == "gm"))
async def cb_pick_time(cb: CallbackQuery, callback_data: V2, state: FSMContext, bot: Bot, db: Database) -> None:
    if cb.message is None or not await _admin_alert(cb, bot, callback_data.chat_id):
        return
    target, minutes = callback_data.uid, int(callback_data.arg or 0)
    pre = (await state.get_data()).get("pre")
    if pre and pre.get("uid") == target:  # причина была указана в команде
        await state.update_data(pre=None)
        chat = await _chat_settings(db, cb.message.chat)
        await _render(cb, await apply_mute(bot, db, chat, target, pre["reason"], cb.from_user.id, minutes=minutes))
        await cb.answer()
        return
    await state.set_state(AskReason.waiting)
    await state.update_data(uid=target, minutes=minutes, expires=time.time() + REASON_TTL)
    await _render(
        cb,
        f"🔇 Мут <code>{target}</code> {fmt_for(minutes)}.\n"
        "Напишите причину сообщением (2 минуты) или нажмите кнопку.",
        kb.reason_menu(callback_data.chat_id, target, minutes),
    )
    await cb.answer()


@router.callback_query(V2.filter(F.act == "gr"))
async def cb_no_reason(cb: CallbackQuery, callback_data: V2, state: FSMContext, bot: Bot, db: Database) -> None:
    if cb.message is None or not await _admin_alert(cb, bot, callback_data.chat_id):
        return
    await state.clear()
    chat = await _chat_settings(db, cb.message.chat)
    text = await apply_mute(
        bot, db, chat, callback_data.uid, "", cb.from_user.id, minutes=int(callback_data.arg or 0)
    )
    await _render(cb, text)
    await cb.answer()


@router.callback_query(V2.filter(F.act == "gc"))
async def cb_cancel(cb: CallbackQuery, callback_data: V2, state: FSMContext, bot: Bot) -> None:
    if not await _admin_alert(cb, bot, callback_data.chat_id):
        return
    await state.clear()
    await _render(cb, "❌ Отменено.")
    await cb.answer()


# --------------------------------- кнопки в личке: меню настроек (v2_pm/md/wset/...)
async def _guard(cb: CallbackQuery, chat_id: int, bot: Bot, db: Database) -> ChatSettings | None:
    if not await is_chat_admin(bot, chat_id, cb.from_user.id, fresh=True):
        await cb.answer("Нет доступа: вы не админ этого чата.", show_alert=True)
        return None
    chat = await db.get_chat(chat_id)
    if chat is None or not chat.active:
        await cb.answer("Чат недоступен.", show_alert=True)
        return None
    return chat


def _warns_text(chat: ChatSettings) -> str:
    action = kb.WARN_ACTIONS.get(chat.warn_action, chat.warn_action)
    if chat.warn_action == "mute":
        action += f" ({fmt_minutes(chat.mute_minutes)})"
    return (
        "⚠️ <b>Варны</b>\n\n"
        f"Лимит предупреждений: <b>{chat.warn_limit}</b>\n"
        f"После лимита: <b>{action}</b>\n\n"
        "Выдача: <code>/warn ID причина</code> или ответом на сообщение."
    )


PRIV_ACTS = {"pm", "md", "mdur", "wset", "wl", "wa", "ml", "hist"}


@router.callback_query(V2.filter(F.act.in_(PRIV_ACTS)))
async def cb_private(cb: CallbackQuery, callback_data: V2, state: FSMContext, bot: Bot, db: Database) -> None:
    await state.clear()
    chat = await _guard(cb, callback_data.chat_id, bot, db)
    if chat is None:
        return
    act, cid, arg = callback_data.act, chat.chat_id, callback_data.arg

    if act in {"pm", "md"} and arg.isdigit():
        minutes = int(arg)
        await db.set_chat_field(cid, "mute_minutes", minutes)
        chat = await db.get_chat(cid) or chat
        if act == "pm":  # шаг 2 меню «Тип наказания»
            await _render(
                cb, f"🛡 <b>Тип наказания</b>: 🔇 Мут {fmt_for(minutes)}.", kb.punish_menu(cid, chat.punishment)
            )
        else:  # «Настройки чата» -> длительность по умолчанию
            await _render(
                cb,
                f"🔇 Мут по умолчанию: <b>{fmt_minutes(minutes)}</b>",
                kb.duration_menu(cid, "md", current=minutes, back_action="settings"),
            )
    elif act == "mdur":
        await _render(
            cb,
            "🔇 <b>Длительность мута по умолчанию</b>\nИспользуется для авто-наказаний и варнов.",
            kb.duration_menu(cid, "md", current=chat.mute_minutes, back_action="settings"),
        )
    elif act == "wset":
        await _render(cb, _warns_text(chat), kb.warn_settings_menu(chat))
    elif act == "wl" and arg.isdigit() and 1 <= int(arg) <= 20:
        await db.set_chat_field(cid, "warn_limit", int(arg))
        chat = await db.get_chat(cid) or chat
        await _render(cb, _warns_text(chat), kb.warn_settings_menu(chat))
    elif act == "wa" and arg in kb.WARN_ACTIONS:
        await db.set_chat_field(cid, "warn_action", arg)
        chat = await db.get_chat(cid) or chat
        await _render(cb, _warns_text(chat), kb.warn_settings_menu(chat))
    elif act == "ml":
        await _render(cb, await mutelist_text(db, cid), kb.back_to(cid, "stats"))
    elif act == "hist":
        await state.set_state(AskHistoryId.waiting)
        await state.update_data(chat_id=cid)
        await _render(cb, "📜 Пришлите числовой Telegram ID пользователя.", kb.back_to(cid, "stats"))
    await cb.answer()


@router.message(PRIVATE, AskHistoryId.waiting, F.text)
async def got_history_id(message: Message, state: FSMContext, bot: Bot, db: Database) -> None:
    data = await state.get_data()
    chat_id = int(data["chat_id"])
    user = message.from_user
    if user is None or not await is_chat_admin(bot, chat_id, user.id, fresh=True):
        await state.clear()
        await message.answer("Нет доступа к этому чату.")
        return
    txt = (message.text or "").strip()
    if not re.fullmatch(r"\d{5,15}", txt):
        await message.answer("Нужен числовой ID (только цифры). Попробуйте ещё раз.")
        return
    await state.clear()
    await message.answer(await history_text(db, chat_id, int(txt)), reply_markup=kb.back_to(chat_id, "stats"))
