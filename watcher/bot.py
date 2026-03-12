"""
Telegram bot — long-polling receiver.

Only messages from the configured TELEGRAM_CHAT_ID are processed.
All other senders receive no response (silent drop).
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .config import Settings
from . import watchers_config as wc

log = logging.getLogger("watcher.bot")


# ── FSM states ─────────────────────────────────────────────────────────────────

class WatcherStates(StatesGroup):
    edit_interval    = State()   # waiting for user to type a new interval (seconds)
    edit_prompts     = State()   # viewing / managing prompts for a watcher
    edit_prompt_item = State()   # waiting for replacement text for one prompt
    create_prompt    = State()   # waiting for new prompt text to append


# ── keyboard / text builders ───────────────────────────────────────────────────

def _watchers_list_kb(watchers: list[wc.WatcherConfig]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{'🟢' if w.enabled else '🔴'} {w.name}",
            callback_data=f"w:actions:{w.id}",
        )]
        for w in watchers
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _actions_kb(w: wc.WatcherConfig) -> InlineKeyboardMarkup:
    toggle = "🔴 Disable" if w.enabled else "🟢 Enable"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Modify",   callback_data=f"w:modify:{w.id}")],
        [InlineKeyboardButton(text=toggle,         callback_data=f"w:toggle:{w.id}")],
        [InlineKeyboardButton(text="🗑 Delete",    callback_data=f"w:delete:{w.id}")],
        [InlineKeyboardButton(text="◀ Watchers",  callback_data="w:list")],
    ])


def _modify_kb(wid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Interval", callback_data=f"w:interval:{wid}")],
        [InlineKeyboardButton(text="📝 Prompts",  callback_data=f"w:prompts:{wid}")],
        [InlineKeyboardButton(text="◀ Back",      callback_data=f"w:actions:{wid}")],
    ])


def _prompts_inline_kb(wid: str, prompts: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🗑 Remove {i + 1}", callback_data=f"w:del_prompt:{wid}:{i}")]
        for i in range(len(prompts))
    ]
    rows.append([InlineKeyboardButton(text="◀ Back", callback_data=f"w:modify:{wid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _prompts_reply_kb(prompts: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    if prompts:
        edit_btns = [KeyboardButton(text=f"✏️ {i + 1}") for i in range(len(prompts))]
        rows = [edit_btns[i:i + 4] for i in range(0, len(edit_btns), 4)]
    rows.append([KeyboardButton(text="➕ Create"), KeyboardButton(text="✖ Done")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _prompts_text(w: wc.WatcherConfig) -> str:
    name = _html.escape(w.name)
    if not w.prompts:
        return f"📝 <b>{name}</b> — no prompts defined."
    lines = [f"📝 Prompts for <b>{name}</b>:"]
    for i, p in enumerate(w.prompts):
        lines.append(f"\n{i + 1}. {_html.escape(p)}")
    return "\n".join(lines)


def _watcher_info_text(w: wc.WatcherConfig) -> str:
    status = "🟢 enabled" if w.enabled else "🔴 disabled"
    return (
        f"<b>{_html.escape(w.name)}</b>\n"
        f"{_html.escape(w.url)}\n"
        f"Interval: {w.interval}s | {status}"
    )


# ── dispatcher ─────────────────────────────────────────────────────────────────

def _build_dispatcher(chat_id: int) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.filter(F.chat.id == chat_id)
    dp.callback_query.filter(F.message.chat.id == chat_id)

    # ── Commands ───────────────────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        log.info("cmd=start chat_id=%d user=%s", message.chat.id, message.from_user.username)
        await state.clear()
        await message.answer(
            "Watcher is running.\n\nCommands:\n/status — service status\n/watchers — manage watchers\n/help — help",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        log.info("cmd=status chat_id=%d user=%s", message.chat.id, message.from_user.username)
        await message.answer("Status: running")

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        log.info("cmd=help chat_id=%d user=%s", message.chat.id, message.from_user.username)
        await message.answer(
            "/start    — greeting\n"
            "/status   — service status\n"
            "/watchers — manage watchers\n"
            "/help     — this message"
        )

    @dp.message(Command("watchers"))
    async def cmd_watchers(message: Message, state: FSMContext) -> None:
        log.info("cmd=watchers chat_id=%d user=%s", message.chat.id, message.from_user.username)
        await state.clear()
        watchers = wc.load_all()
        if not watchers:
            await message.answer("No watchers configured.", reply_markup=ReplyKeyboardRemove())
            return
        await message.answer("Watchers:", reply_markup=_watchers_list_kb(watchers))

    # ── Watcher list ───────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "w:list")
    async def cb_list(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        watchers = wc.load_all()
        if not watchers:
            await query.message.edit_text("No watchers configured.")
        else:
            await query.message.edit_text("Watchers:", reply_markup=_watchers_list_kb(watchers))
        await query.answer()

    # ── Watcher actions ────────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("w:actions:"))
    async def cb_actions(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        wid = query.data.split(":", 2)[2]
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        await query.message.edit_text(
            _watcher_info_text(w),
            reply_markup=_actions_kb(w),
            parse_mode="HTML",
        )
        await query.answer()

    @dp.callback_query(F.data.startswith("w:toggle:"))
    async def cb_toggle(query: CallbackQuery) -> None:
        wid = query.data.split(":", 2)[2]
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        w.enabled = not w.enabled
        wc.save(w)
        await query.message.edit_text(
            _watcher_info_text(w),
            reply_markup=_actions_kb(w),
            parse_mode="HTML",
        )
        await query.answer("Enabled." if w.enabled else "Disabled.")

    @dp.callback_query(F.data.startswith("w:delete:"))
    async def cb_delete(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        wid = query.data.split(":", 2)[2]
        w = wc.get(wid)
        name = _html.escape(w.name) if w else wid
        wc.delete(wid)
        remaining = wc.load_all()
        if remaining:
            await query.message.edit_text(
                f"🗑 <b>{name}</b> deleted.\n\nWatchers:",
                reply_markup=_watchers_list_kb(remaining),
                parse_mode="HTML",
            )
        else:
            await query.message.edit_text(
                f"🗑 <b>{name}</b> deleted.\n\nNo watchers remaining.",
                parse_mode="HTML",
            )
        await query.answer("Deleted.")

    # ── Modify menu ────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("w:modify:"))
    async def cb_modify(query: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        wid = query.data.split(":", 2)[2]
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        await query.message.edit_text(
            f"⚙️ Modify <b>{_html.escape(w.name)}</b>:",
            reply_markup=_modify_kb(wid),
            parse_mode="HTML",
        )
        await query.answer()

    # ── Interval editing ───────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("w:interval:"))
    async def cb_interval(query: CallbackQuery, state: FSMContext) -> None:
        wid = query.data.split(":", 2)[2]
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        await state.set_state(WatcherStates.edit_interval)
        await state.update_data(watcher_id=wid)
        await query.message.answer(
            f"Current interval: <b>{w.interval}s</b>\n\nSend the new interval in seconds:",
            parse_mode="HTML",
        )
        await query.answer()

    @dp.message(WatcherStates.edit_interval)
    async def msg_interval(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text.isdigit() or int(text) <= 0:
            await message.answer("Please send a positive integer (seconds).")
            return
        data = await state.get_data()
        wid = data["watcher_id"]
        w = wc.get(wid)
        if w is None:
            await state.clear()
            await message.answer("Watcher no longer exists.")
            return
        w.interval = int(text)
        wc.save(w)
        await state.clear()
        await message.answer(f"✅ Interval updated to <b>{w.interval}s</b>.", parse_mode="HTML")

    # ── Prompts management ─────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("w:prompts:"))
    async def cb_prompts(query: CallbackQuery, state: FSMContext) -> None:
        wid = query.data.split(":", 2)[2]
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        await query.message.edit_text(
            _prompts_text(w),
            reply_markup=_prompts_inline_kb(wid, w.prompts),
            parse_mode="HTML",
        )
        await state.set_state(WatcherStates.edit_prompts)
        await state.update_data(watcher_id=wid, prompts_msg_id=query.message.message_id)
        await query.message.answer(
            "Use the buttons to edit or create prompts:",
            reply_markup=_prompts_reply_kb(w.prompts),
        )
        await query.answer()

    @dp.callback_query(F.data.startswith("w:del_prompt:"))
    async def cb_del_prompt(query: CallbackQuery, state: FSMContext) -> None:
        parts = query.data.split(":")
        wid, idx = parts[2], int(parts[3])
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        if idx < 0 or idx >= len(w.prompts):
            await query.answer("Prompt already removed.", show_alert=True)
            return
        w.prompts.pop(idx)
        wc.save(w)
        await query.message.edit_text(
            _prompts_text(w),
            reply_markup=_prompts_inline_kb(wid, w.prompts),
            parse_mode="HTML",
        )
        await state.update_data(watcher_id=wid, prompts_msg_id=query.message.message_id)
        await query.message.answer("🗑 Prompt removed.", reply_markup=_prompts_reply_kb(w.prompts))
        await query.answer()

    # -- Reply keyboard messages while in edit_prompts state --

    @dp.message(WatcherStates.edit_prompts, F.text.regexp(r"^✏️\s*(\d+)$"))
    async def msg_select_prompt(message: Message, state: FSMContext) -> None:
        m = re.match(r"^✏️\s*(\d+)$", message.text or "")
        idx = int(m.group(1)) - 1
        data = await state.get_data()
        wid = data["watcher_id"]
        w = wc.get(wid)
        if w is None or idx < 0 or idx >= len(w.prompts):
            await message.answer("Invalid selection.")
            return
        await state.set_state(WatcherStates.edit_prompt_item)
        await state.update_data(prompt_idx=idx)
        await message.answer(
            f"Editing prompt {idx + 1}:\n\n<code>{_html.escape(w.prompts[idx])}</code>\n\nSend the new text:",
            parse_mode="HTML",
        )

    @dp.message(WatcherStates.edit_prompts, F.text == "➕ Create")
    async def msg_create_start(message: Message, state: FSMContext) -> None:
        await state.set_state(WatcherStates.create_prompt)
        await message.answer("Send the new prompt text:")

    @dp.message(WatcherStates.edit_prompts, F.text == "✖ Done")
    async def msg_prompts_done(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        wid = data.get("watcher_id", "")
        await state.clear()
        await message.answer("Done.", reply_markup=ReplyKeyboardRemove())
        w = wc.get(wid)
        if w:
            await message.answer(
                f"⚙️ Modify <b>{_html.escape(w.name)}</b>:",
                reply_markup=_modify_kb(wid),
                parse_mode="HTML",
            )

    @dp.message(WatcherStates.edit_prompts)
    async def msg_edit_prompts_unhandled(message: Message) -> None:
        await message.answer("Use the keyboard buttons or send ✖ Done to finish.")

    # -- Replace a specific prompt --

    @dp.message(WatcherStates.edit_prompt_item)
    async def msg_replace_prompt(message: Message, state: FSMContext, bot: Bot) -> None:
        new_text = (message.text or "").strip()
        if not new_text:
            await message.answer("Please send non-empty text.")
            return
        data = await state.get_data()
        wid = data["watcher_id"]
        idx = data["prompt_idx"]
        prompts_msg_id = data.get("prompts_msg_id")
        w = wc.get(wid)
        if w is None or idx >= len(w.prompts):
            await state.set_state(WatcherStates.edit_prompts)
            await message.answer("Watcher or prompt no longer exists.")
            return
        w.prompts[idx] = new_text
        wc.save(w)
        await state.set_state(WatcherStates.edit_prompts)
        await state.update_data(watcher_id=wid)
        if prompts_msg_id:
            try:
                await bot.edit_message_text(
                    _prompts_text(w),
                    chat_id=message.chat.id,
                    message_id=prompts_msg_id,
                    reply_markup=_prompts_inline_kb(wid, w.prompts),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        await message.answer(f"✅ Prompt {idx + 1} updated.", reply_markup=_prompts_reply_kb(w.prompts))

    # -- Append a new prompt --

    @dp.message(WatcherStates.create_prompt)
    async def msg_create_prompt(message: Message, state: FSMContext, bot: Bot) -> None:
        new_text = (message.text or "").strip()
        if not new_text:
            await message.answer("Please send non-empty text.")
            return
        data = await state.get_data()
        wid = data["watcher_id"]
        prompts_msg_id = data.get("prompts_msg_id")
        w = wc.get(wid)
        if w is None:
            await state.clear()
            await message.answer("Watcher no longer exists.", reply_markup=ReplyKeyboardRemove())
            return
        w.prompts.append(new_text)
        wc.save(w)
        await state.set_state(WatcherStates.edit_prompts)
        await state.update_data(watcher_id=wid)
        if prompts_msg_id:
            try:
                await bot.edit_message_text(
                    _prompts_text(w),
                    chat_id=message.chat.id,
                    message_id=prompts_msg_id,
                    reply_markup=_prompts_inline_kb(wid, w.prompts),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        await message.answer("✅ Prompt added.", reply_markup=_prompts_reply_kb(w.prompts))

    # ── Fallback ───────────────────────────────────────────────────────────────

    @dp.message()
    async def unhandled(message: Message) -> None:
        log.warning(
            "unhandled message chat_id=%d user=%s text=%r",
            message.chat.id,
            message.from_user.username,
            message.text,
        )

    return dp


async def run_bot(settings: Settings) -> None:
    """Start the bot and block until cancelled."""
    bot = Bot(token=settings.telegram.token)
    dp = _build_dispatcher(settings.telegram.chat_id)

    log.info(
        "[bot] starting (chat_id=%d, poll_timeout=%ds)",
        settings.telegram.chat_id,
        settings.telegram.poll_timeout,
    )
    try:
        await dp.start_polling(bot, polling_timeout=settings.telegram.poll_timeout)
        log.info("[bot] start_polling returned normally")
    except asyncio.CancelledError:
        log.info("[bot] start_polling cancelled — closing session")
        raise
    except Exception:
        log.exception("[bot] start_polling raised unexpected exception")
        raise
    finally:
        log.info("[bot] closing bot session...")
        await bot.session.close()
        log.info("[bot] session closed")
