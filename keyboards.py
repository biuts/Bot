"""Inline-клавиатуры и callback-данные."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import ChatSettings, Word
from utils import DURATIONS, fmt_minutes

PUNISH_TITLES = {
    "warn": "⚠️ Предупреждение",
    "mute": "🔇 Мут",
    "kick": "👢 Кик",
    "ban": "🚫 Бан",
    "escalation": "📈 Эскалация (1→⚠️, 2→🔇, 3→🚫)",
}
MUTE_OPTIONS = (10, 60, 1440, 10080)
MAX_WORDS_SHOWN = 50


class Menu(CallbackData, prefix="m"):
    """Данные кнопок меню: действие, чат, аргумент."""

    action: str
    chat_id: int = 0
    arg: str = ""


def _b(text: str, action: str, chat_id: int, arg: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=Menu(action=action, chat_id=chat_id, arg=arg).pack())


def _back(chat_id: int) -> list[InlineKeyboardButton]:
    return [_b("⬅️ Назад", "main", chat_id)]


def open_settings(chat_id: int) -> InlineKeyboardMarkup:
    """Кнопка «Настроить бота» для приветствия."""
    return InlineKeyboardMarkup(inline_keyboard=[[_b("⚙️ Настроить бота", "main", chat_id)]])


def main_menu(chat_id: int) -> InlineKeyboardMarkup:
    """Главное меню чата."""
    rows = [
        [_b("📋 Список триггер-слов", "words", chat_id)],
        [_b("➕ Добавить слово", "wadd", chat_id)],
        [_b("🛡 Тип наказания", "punish", chat_id)],
        [_b("⚙️ Настройки чата", "settings", chat_id)],
        [_b("📊 Статистика", "stats", chat_id)],
        [_b("🔀 Другие чаты", "chats", chat_id)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chats_menu(chats: list[ChatSettings]) -> InlineKeyboardMarkup:
    """Список чатов администратора."""
    rows = [[_b(f"💬 {c.title or c.chat_id}", "main", c.chat_id)] for c in chats]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def words_menu(chat_id: int, words: list[Word]) -> InlineKeyboardMarkup:
    """Слова с кнопками удаления."""
    shown = words[:MAX_WORDS_SHOWN]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(shown), 2):
        rows.append([_b(f"❌ {w.word}", "wdel", chat_id, str(w.id)) for w in shown[i : i + 2]])
    rows.append([_b("➕ Добавить слово", "wadd", chat_id)])
    rows.append(_back(chat_id))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def punish_menu(chat_id: int, current: str) -> InlineKeyboardMarkup:
    """Выбор типа наказания."""
    rows = [
        [_b(("✅ " if mode == current else "") + title, "pset", chat_id, mode)]
        for mode, title in PUNISH_TITLES.items()
    ]
    rows.append(_back(chat_id))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_menu(chat: ChatSettings) -> InlineKeyboardMarkup:
    """Переключатели и длительность мута."""
    cid = chat.chat_id
    rows = [
        [_b(f"{'✅' if chat.delete_admin_msgs else '☑️'} Удалять сообщения админов", "tadm", cid)],
        [_b(f"{'✅' if chat.notify_admins else '☑️'} Уведомлять админов о нарушениях", "tnot", cid)],
        [
            _b(("✅ " if m == chat.mute_minutes else "") + fmt_minutes(m), "mute", cid, str(m))
            for m in MUTE_OPTIONS
        ],
        [_v(f"⚠️ Варны: {chat.warn_limit} → {WARN_ACTIONS.get(chat.warn_action, chat.warn_action)}", "wset", cid)],
        [_v("🔇 Другая длительность мута", "mdur", cid)],
        _back(cid),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_menu(chat_id: int) -> InlineKeyboardMarkup:
    """Только кнопка «Назад»."""
    return InlineKeyboardMarkup(inline_keyboard=[_back(chat_id)])


# ------------------------------------------------------------------ v2
class V2(CallbackData, prefix="v2"):
    """Callback-данные дополнения v2 (префикс v2, не пересекаются с Menu)."""

    act: str
    chat_id: int = 0
    uid: int = 0
    arg: str = ""


WARN_ACTIONS = {"mute": "🔇 Мут", "kick": "👢 Кик", "ban": "🚫 Бан"}
WARN_LIMITS = (2, 3, 4, 5)


def _v(text: str, act: str, chat_id: int, uid: int = 0, arg: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=V2(act=act, chat_id=chat_id, uid=uid, arg=arg).pack())


def duration_menu(
    chat_id: int, act: str, uid: int = 0, *, current: int | None = None,
    cancel: bool = False, back_action: str | None = None,
) -> InlineKeyboardMarkup:
    """Выбор длительности: 5м 15м 1ч 6ч 12ч 1д 3д 7д + навсегда."""
    btns = [
        _v(("✅ " if current == m else "") + fmt_minutes(m), act, chat_id, uid, str(m)) for m in DURATIONS
    ]
    rows = [btns[i : i + 3] for i in range(0, len(btns), 3)]
    rows.append([_v(("✅ " if current == 0 else "") + "♾ Навсегда", act, chat_id, uid, "0")])
    if cancel:
        rows.append([_v("❌ Отмена", "gc", chat_id, uid)])
    if back_action:
        rows.append([_b("⬅️ Назад", back_action, chat_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reason_menu(chat_id: int, uid: int, minutes: int) -> InlineKeyboardMarkup:
    """Шаг причины: пропустить или отменить."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_v("Без причины", "gr", chat_id, uid, str(minutes))],
            [_v("❌ Отмена", "gc", chat_id, uid)],
        ]
    )


def warn_settings_menu(chat: ChatSettings) -> InlineKeyboardMarkup:
    """Лимит варнов и действие после лимита."""
    cid = chat.chat_id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_v(("✅ " if n == chat.warn_limit else "") + str(n), "wl", cid, 0, str(n)) for n in WARN_LIMITS],
            [
                _v(("✅ " if a == chat.warn_action else "") + t, "wa", cid, 0, a)
                for a, t in WARN_ACTIONS.items()
            ],
            [_v("🔇 Длительность мута", "mdur", cid)],
            [_b("⬅️ Назад", "settings", cid)],
        ]
    )


def back_to(chat_id: int, action: str) -> InlineKeyboardMarkup:
    """Кнопка «Назад» на указанный экран меню."""
    return InlineKeyboardMarkup(inline_keyboard=[[_b("⬅️ Назад", action, chat_id)]])


def stats_menu(chat_id: int) -> InlineKeyboardMarkup:
    """Статистика + списки v2."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_v("🔇 Активные муты", "ml", chat_id)],
            [_v("📜 История по ID", "hist", chat_id)],
            _back(chat_id),
        ]
    )
