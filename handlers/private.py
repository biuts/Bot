"""Меню настроек в личке с ботом."""
from __future__ import annotations

import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import keyboards as kb
from db import ChatSettings, Database
from keyboards import Menu
from matcher import WordMatcher, normalize
from utils import fmt_minutes, is_chat_admin

logger = logging.getLogger(__name__)
router = Router(name="private")
router.message.filter(F.chat.type == "private")


class AddWord(StatesGroup):
    """Ожидание слова от админа."""

    waiting = State()


def _parse_payload(args: str | None) -> int | None:
    """Разбирает deep-link payload вида c-1001234567890."""
    if args and args.startswith("c"):
        try:
            return int(args[1:])
        except ValueError:
            return None
    return None


def _main_text(chat: ChatSettings, words_count: int) -> str:
    return (
        f"⚙️ <b>Настройки чата «{html.escape(chat.title)}»</b>\n\n"
        f"Триггер-слов: <b>{words_count}</b>\n"
        f"Наказание: <b>{kb.PUNISH_TITLES.get(chat.punishment, chat.punishment)}</b>\n"
        f"Мут: <b>{fmt_minutes(chat.mute_minutes)}</b>"
    )


async def _chats_view(bot: Bot, db: Database, user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Список чатов, где пользователь всё ещё админ."""
    valid: list[ChatSettings] = []
    for chat in await db.get_admin_chats(user_id):
        if await is_chat_admin(bot, chat.chat_id, user_id):
            valid.append(chat)
        else:
            await db.remove_admin(chat.chat_id, user_id)
    if not valid:
        return (
            "У меня нет чатов, где вы админ.\n\nДобавьте меня в группу, назначьте администратором "
            "(права: удаление сообщений и блокировка пользователей) и нажмите /start в группе.",
            None,
        )
    return "Выберите чат для настройки:", kb.chats_menu(valid)


async def _render(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """edit_text, игнорируя «message is not modified»."""
    try:
        await cb.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


@router.message(CommandStart())
async def cmd_start(
    message: Message, command: CommandObject, state: FSMContext, bot: Bot, db: Database
) -> None:
    """/start в личке: сразу меню чата (deep link) или список чатов."""
    await state.clear()
    user = message.from_user
    if user is None:
        return
    chat_id = _parse_payload(command.args)
    if chat_id is not None:
        if not await is_chat_admin(bot, chat_id, user.id, fresh=True):
            await message.answer("Вы не администратор этого чата, либо меня в нём нет.")
            return
        chat = await db.get_chat(chat_id)
        if chat is None or not chat.active:
            await message.answer("Этот чат пока не подключён. Добавьте меня в группу.")
            return
        await db.add_admin(chat_id, user.id)
        words = await db.list_words(chat_id)
        await message.answer(_main_text(chat, len(words)), reply_markup=kb.main_menu(chat_id))
        return
    text, markup = await _chats_view(bot, db, user.id)
    await message.answer("👋 Я модерирую группы по списку запрещённых слов.\n\n" + text, reply_markup=markup)


@router.message(AddWord.waiting, F.text)
async def on_new_words(
    message: Message, state: FSMContext, bot: Bot, db: Database, matcher: WordMatcher
) -> None:
    """Принимает слова (по одному на строку или через запятую)."""
    data = await state.get_data()
    chat_id = int(data["chat_id"])
    if message.from_user is None or not await is_chat_admin(bot, chat_id, message.from_user.id, fresh=True):
        await state.clear()
        await message.answer("Нет доступа к этому чату.")
        return
    words = [
        w for w in (normalize(x) for x in re.split(r"[,\n;]+", message.text or "")) if 0 < len(w) <= 64
    ][:30]
    if not words:
        await message.answer("Не вижу слов. Пришлите слово (или несколько через запятую / с новой строки).")
        return
    added = await db.add_words(chat_id, words)
    matcher.invalidate(chat_id)
    await state.clear()
    all_words = await db.list_words(chat_id)
    await message.answer(
        f"✅ Добавлено новых слов: {added}\n<i>Совет: «слово*» ловит и все окончания.</i>",
        reply_markup=kb.words_menu(chat_id, all_words),
    )


@router.callback_query(Menu.filter())
async def on_menu(
    cb: CallbackQuery,
    callback_data: Menu,
    state: FSMContext,
    bot: Bot,
    db: Database,
    matcher: WordMatcher,
) -> None:
    """Единый обработчик всех кнопок меню."""
    await state.clear()
    action, chat_id, arg = callback_data.action, callback_data.chat_id, callback_data.arg
    user_id = cb.from_user.id

    if action == "chats":
        text, markup = await _chats_view(bot, db, user_id)
        await _render(cb, text, markup)
        await cb.answer()
        return

    if not await is_chat_admin(bot, chat_id, user_id, fresh=True):
        await cb.answer("Нет доступа: вы не админ этого чата.", show_alert=True)
        return
    chat = await db.get_chat(chat_id)
    if chat is None or not chat.active:
        await cb.answer("Чат недоступен.", show_alert=True)
        return
    await db.add_admin(chat_id, user_id)

    if action == "main":
        words = await db.list_words(chat_id)
        await _render(cb, _main_text(chat, len(words)), kb.main_menu(chat_id))

    elif action == "words":
        words = await db.list_words(chat_id)
        text = "📋 <b>Триггер-слова</b>\nНажмите на слово, чтобы удалить." if words else "Список пуст."
        if len(words) > kb.MAX_WORDS_SHOWN:
            text += f"\nПоказаны первые {kb.MAX_WORDS_SHOWN} из {len(words)}."
        await _render(cb, text, kb.words_menu(chat_id, words))

    elif action == "wdel":
        await db.delete_word(chat_id, int(arg))
        matcher.invalidate(chat_id)
        words = await db.list_words(chat_id)
        await _render(cb, "📋 <b>Триггер-слова</b>" if words else "Список пуст.", kb.words_menu(chat_id, words))

    elif action == "wadd":
        await state.set_state(AddWord.waiting)
        await state.update_data(chat_id=chat_id)
        await _render(
            cb,
            "➕ Пришлите слово сообщением.\nНесколько слов — через запятую или с новой строки.\n"
            "«слово*» — ловить и окончания (дурак*, дураки, дураками…).",
            kb.back_menu(chat_id),
        )

    elif action == "punish":
        await _render(
            cb,
            "🛡 <b>Тип наказания</b>\n\nЭскалация: 1-е нарушение — предупреждение, 2-е — мут, 3-е и далее — бан.",
            kb.punish_menu(chat_id, chat.punishment),
        )

    elif action == "pset" and arg in kb.PUNISH_TITLES:
        await db.set_chat_field(chat_id, "punishment", arg)
        await _render(cb, "🛡 <b>Тип наказания</b> обновлён.", kb.punish_menu(chat_id, arg))

    elif action in {"settings", "tadm", "tnot", "mute"}:
        if action == "tadm":
            await db.set_chat_field(chat_id, "delete_admin_msgs", int(not chat.delete_admin_msgs))
        elif action == "tnot":
            await db.set_chat_field(chat_id, "notify_admins", int(not chat.notify_admins))
        elif action == "mute" and arg.isdigit():
            await db.set_chat_field(chat_id, "mute_minutes", int(arg))
        chat = await db.get_chat(chat_id) or chat
        await _render(
            cb,
            "⚙️ <b>Настройки чата</b>\n\nДлительность мута — нижний ряд кнопок.\n"
            "Удаление сообщений админов: сообщение удаляется, но наказание админу не применяется.",
            kb.settings_menu(chat),
        )

    elif action == "stats":
        s = await db.get_stats(chat_id)
        lines = [f"📊 <b>Статистика «{html.escape(chat.title)}»</b>", f"Всего нарушений: <b>{s.total}</b>"]
        if s.top_users:
            lines.append("\n<b>Нарушители:</b>")
            lines += [f"{i}. {html.escape(label)} — {n}" for i, (_, label, n) in enumerate(s.top_users, 1)]
        if s.top_words:
            lines.append("\n<b>Частые слова:</b>")
            lines += [f"• {html.escape(w)} — {n}" for w, n in s.top_words]
        await _render(cb, "\n".join(lines), kb.back_menu(chat_id))

    await cb.answer()


@router.message()
async def fallback(message: Message) -> None:
    """Любое другое сообщение в личке."""
    await message.answer("Откройте меню настроек командой /start")
