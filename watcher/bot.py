"""
Telegram bot — long-polling receiver.

Only messages from the configured TELEGRAM_CHAT_ID are processed.
All other senders receive no response (silent drop).
"""

from __future__ import annotations

import asyncio
import contextlib
import html as _html
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from .config import Settings
from . import watchers_config as wc

log = logging.getLogger("watcher.bot")


# ── pending action tracker ──────────────────────────────────────────────────────
# Maps chat_id → current pending context dict.
# "action" key is one of: "edit_interval" | "edit_prompt_item" | "create_prompt"
# Extra keys: watcher_id, prompt_idx, ask_msg_id, return_to

_pending: dict[int, dict] = {}

# ── prompt UI state ─────────────────────────────────────────────────────────────
# Maps chat_id → {wid, prompt_msg_ids: list[int], add_msg_id: int}
# Tracks the per-prompt messages currently shown so they can be cleaned up.

_prompts_ui: dict[int, dict] = {}


# ── keyboard / text builders ───────────────────────────────────────────────────

def _done_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="✖ Done", callback_data="w:done")


def _watchers_list_kb(watchers: list[wc.WatcherConfig]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{'🟢' if w.enabled else '🔴'} {w.name}",
            callback_data=f"w:actions:{w.id}",
        )]
        for w in watchers
    ]
    rows.append([_done_btn()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _actions_kb(w: wc.WatcherConfig) -> InlineKeyboardMarkup:
    toggle = "🔴 Disable" if w.enabled else "🟢 Enable"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Modify",   callback_data=f"w:modify:{w.id}")],
        [InlineKeyboardButton(text=toggle,         callback_data=f"w:toggle:{w.id}")],
        [InlineKeyboardButton(text="🗑 Delete",    callback_data=f"w:delete:{w.id}")],
        [InlineKeyboardButton(text="◀ Watchers",  callback_data="w:list"),
         _done_btn()],
    ])


def _modify_kb(wid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Interval", callback_data=f"w:interval:{wid}")],
        [InlineKeyboardButton(text="📝 Prompts",  callback_data=f"w:prompts:{wid}")],
        [InlineKeyboardButton(text="◀ Back",      callback_data=f"w:actions:{wid}"),
         _done_btn()],
    ])


def _prompt_kb(wid: str, idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Delete", callback_data=f"w:del_prompt:{wid}:{idx}"),
        InlineKeyboardButton(text="✏️ Modify", callback_data=f"w:edit_prompt:{wid}:{idx}"),
    ]])


def _add_prompt_kb(wid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add prompt", callback_data=f"w:add_prompt:{wid}"),
         InlineKeyboardButton(text="◀ Back",        callback_data=f"w:modify:{wid}")],
        [_done_btn()],
    ])


def _input_cancel_kb(wid: str) -> InlineKeyboardMarkup:
    """Keyboard attached to bot 'ask for input' messages so the user can cancel."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖ Done", callback_data=f"w:cancel_input:{wid}"),
    ]])


async def _cleanup_prompts_ui(bot: Bot, chat_id: int) -> None:
    """Delete all tracked prompt UI messages for chat_id."""
    ui = _prompts_ui.pop(chat_id, None)
    if not ui:
        return
    for mid in ui.get("prompt_msg_ids", []):
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id, mid)
    if ui.get("add_msg_id"):
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id, ui["add_msg_id"])


async def _render_prompts(bot: Bot, chat_id: int, wid: str) -> None:
    """Send one message per prompt then an ➕ Add / ◀ Back message. Cleans up old UI first."""
    await _cleanup_prompts_ui(bot, chat_id)

    w = wc.get(wid)
    if w is None:
        return

    prompt_msg_ids: list[int] = []
    for i, p in enumerate(w.prompts):
        msg = await bot.send_message(
            chat_id, _html.escape(p),
            reply_markup=_prompt_kb(wid, i),
            parse_mode="HTML",
        )
        prompt_msg_ids.append(msg.message_id)

    name = _html.escape(w.name)
    summary = f"📝 <b>{name}</b> — {len(w.prompts)} prompt(s)" if w.prompts else f"📝 <b>{name}</b> — no prompts yet."
    add_msg = await bot.send_message(chat_id, summary, reply_markup=_add_prompt_kb(wid), parse_mode="HTML")

    _prompts_ui[chat_id] = {
        "wid": wid,
        "prompt_msg_ids": prompt_msg_ids,
        "add_msg_id": add_msg.message_id,
    }


def _watcher_info_text(w: wc.WatcherConfig) -> str:
    status = "🟢 enabled" if w.enabled else "🔴 disabled"
    return (
        f"<b>{_html.escape(w.name)}</b>\n"
        f"{_html.escape(w.url)}\n"
        f"Interval: {w.interval}s | {status}"
    )


# ── dispatcher ─────────────────────────────────────────────────────────────────

def _build_dispatcher(chat_id: int) -> Dispatcher:
    dp = Dispatcher()
    dp.message.filter(F.chat.id == chat_id)
    dp.callback_query.filter(F.message.chat.id == chat_id)

    # ── Commands ───────────────────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        log.info("cmd=start chat_id=%d user=%s", message.chat.id, message.from_user.username if message.from_user else None)
        _pending.pop(message.chat.id, None)
        await message.answer(
            "Watcher is running.\n\nCommands:\n/status — service status\n/watchers — manage watchers\n/help — help",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        log.info("cmd=status chat_id=%d user=%s", message.chat.id, message.from_user.username if message.from_user else None)
        await message.answer("Status: running")

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        log.info("cmd=help chat_id=%d user=%s", message.chat.id, message.from_user.username if message.from_user else None)
        await message.answer(
            "/start    — greeting\n"
            "/status   — service status\n"
            "/watchers — manage watchers\n"
            "/help     — this message"
        )

    @dp.message(Command("watchers"))
    async def cmd_watchers(message: Message) -> None:
        log.info("cmd=watchers chat_id=%d user=%s", message.chat.id, message.from_user.username if message.from_user else None)
        _pending.pop(message.chat.id, None)
        watchers = wc.load_all()
        if not watchers:
            await message.answer("No watchers configured.", reply_markup=ReplyKeyboardRemove())
            return
        await message.answer("Watchers:", reply_markup=_watchers_list_kb(watchers))

    # ── Done — close session at any stage ──────────────────────────────────────

    @dp.callback_query(F.data == "w:done")
    async def cb_done(query: CallbackQuery, bot: Bot) -> None:
        chat_id = query.message.chat.id  # type: ignore[union-attr]
        _pending.pop(chat_id, None)
        await _cleanup_prompts_ui(bot, chat_id)
        with contextlib.suppress(Exception):
            await query.message.delete()  # type: ignore[union-attr]
        await query.answer("Done.")

    # ── Cancel pending input ────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("w:cancel_input:"))
    async def cb_cancel_input(query: CallbackQuery, bot: Bot) -> None:
        chat_id = query.message.chat.id  # type: ignore[union-attr]
        wid = query.data.split(":", 2)[2]  # type: ignore[union-attr]
        pending = _pending.pop(chat_id, None)
        # Delete the ask message (the one carrying this Cancel button).
        with contextlib.suppress(Exception):
            await query.message.delete()  # type: ignore[union-attr]
        await query.answer("Cancelled.")
        return_to = (pending or {}).get("return_to", "prompts")
        if return_to == "prompts":
            await _render_prompts(bot, chat_id, wid)
        else:
            # return_to == "modify"
            w = wc.get(wid)
            if w:
                await bot.send_message(
                    chat_id,
                    f"⚙️ Modify <b>{_html.escape(w.name)}</b>:",
                    reply_markup=_modify_kb(wid),
                    parse_mode="HTML",
                )

    # ── Watcher list ───────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "w:list")
    async def cb_list(query: CallbackQuery, bot: Bot) -> None:
        chat_id = query.message.chat.id  # type: ignore[union-attr]
        _pending.pop(chat_id, None)
        await _cleanup_prompts_ui(bot, chat_id)
        watchers = wc.load_all()
        if not watchers:
            await query.message.edit_text("No watchers configured.")  # type: ignore[union-attr]
        else:
            await query.message.edit_text("Watchers:", reply_markup=_watchers_list_kb(watchers))  # type: ignore[union-attr]
        await query.answer()

    # ── Watcher actions ────────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("w:actions:"))
    async def cb_actions(query: CallbackQuery) -> None:
        _pending.pop(query.message.chat.id, None)  # type: ignore[union-attr]
        wid = query.data.split(":", 2)[2]  # type: ignore[union-attr]
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        await query.message.edit_text(  # type: ignore[union-attr]
            _watcher_info_text(w),
            reply_markup=_actions_kb(w),
            parse_mode="HTML",
        )
        await query.answer()

    @dp.callback_query(F.data.startswith("w:toggle:"))
    async def cb_toggle(query: CallbackQuery) -> None:
        wid = query.data.split(":", 2)[2]  # type: ignore[union-attr]
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        w.enabled = not w.enabled
        wc.save(w)
        await query.message.edit_text(  # type: ignore[union-attr]
            _watcher_info_text(w),
            reply_markup=_actions_kb(w),
            parse_mode="HTML",
        )
        await query.answer("Enabled." if w.enabled else "Disabled.")

    @dp.callback_query(F.data.startswith("w:delete:"))
    async def cb_delete(query: CallbackQuery) -> None:
        _pending.pop(query.message.chat.id, None)  # type: ignore[union-attr]
        wid = query.data.split(":", 2)[2]  # type: ignore[union-attr]
        w = wc.get(wid)
        name = _html.escape(w.name) if w else wid
        wc.delete(wid)
        remaining = wc.load_all()
        if remaining:
            await query.message.edit_text(  # type: ignore[union-attr]
                f"🗑 <b>{name}</b> deleted.\n\nWatchers:",
                reply_markup=_watchers_list_kb(remaining),
                parse_mode="HTML",
            )
        else:
            await query.message.edit_text(  # type: ignore[union-attr]
                f"🗑 <b>{name}</b> deleted.\n\nNo watchers remaining.",
                parse_mode="HTML",
            )
        await query.answer("Deleted.")

    # ── Modify menu ────────────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("w:modify:"))
    async def cb_modify(query: CallbackQuery, bot: Bot) -> None:
        chat_id = query.message.chat.id  # type: ignore[union-attr]
        _pending.pop(chat_id, None)
        wid = query.data.split(":", 2)[2]  # type: ignore[union-attr]
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        # Clean up individual prompt messages if navigating back from prompts view.
        ui = _prompts_ui.pop(chat_id, None)
        if ui:
            for mid in ui.get("prompt_msg_ids", []):
                with contextlib.suppress(Exception):
                    await bot.delete_message(chat_id, mid)
            # current message is the add/back message — edit it into the modify menu
        await query.message.edit_text(  # type: ignore[union-attr]
            f"⚙️ Modify <b>{_html.escape(w.name)}</b>:",
            reply_markup=_modify_kb(wid),
            parse_mode="HTML",
        )
        await query.answer()

    # ── Interval editing ───────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("w:interval:"))
    async def cb_interval(query: CallbackQuery) -> None:
        wid = query.data.split(":", 2)[2]  # type: ignore[union-attr]
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        ask = await query.message.answer(  # type: ignore[union-attr]
            f"Current interval: <b>{w.interval}s</b>\n\nSend the new interval in seconds:",
            reply_markup=_input_cancel_kb(wid),
            parse_mode="HTML",
        )
        _pending[query.message.chat.id] = {  # type: ignore[union-attr]
            "action": "edit_interval",
            "watcher_id": wid,
            "ask_msg_id": ask.message_id,
            "return_to": "modify",
        }
        await query.answer()

    # ── Prompts management ─────────────────────────────────────────────────────

    @dp.callback_query(F.data.startswith("w:prompts:"))
    async def cb_prompts(query: CallbackQuery, bot: Bot) -> None:
        wid = query.data.split(":", 2)[2]  # type: ignore[union-attr]
        if wc.get(wid) is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        _pending.pop(query.message.chat.id, None)  # type: ignore[union-attr]
        await query.message.delete()  # type: ignore[union-attr]
        await _render_prompts(bot, query.message.chat.id, wid)  # type: ignore[union-attr]
        await query.answer()

    @dp.callback_query(F.data.startswith("w:del_prompt:"))
    async def cb_del_prompt(query: CallbackQuery, bot: Bot) -> None:
        parts = query.data.split(":")  # type: ignore[union-attr]
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
        await query.message.delete()  # type: ignore[union-attr]
        await query.answer("🗑 Deleted.")
        await _render_prompts(bot, query.message.chat.id, wid)  # type: ignore[union-attr]

    @dp.callback_query(F.data.startswith("w:edit_prompt:"))
    async def cb_edit_prompt(query: CallbackQuery) -> None:
        parts = query.data.split(":")  # type: ignore[union-attr]
        wid, idx = parts[2], int(parts[3])
        w = wc.get(wid)
        if w is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        if idx < 0 or idx >= len(w.prompts):
            await query.answer("Prompt not found.", show_alert=True)
            return
        ask = await query.message.answer(  # type: ignore[union-attr]
            f"Current prompt {idx + 1}:\n\n<code>{_html.escape(w.prompts[idx])}</code>\n\nSend the new text:",
            reply_markup=_input_cancel_kb(wid),
            parse_mode="HTML",
        )
        _pending[query.message.chat.id] = {  # type: ignore[union-attr]
            "action": "edit_prompt_item",
            "watcher_id": wid,
            "prompt_idx": idx,
            "ask_msg_id": ask.message_id,
            "return_to": "prompts",
        }
        await query.answer()

    @dp.callback_query(F.data.startswith("w:add_prompt:"))
    async def cb_add_prompt(query: CallbackQuery) -> None:
        wid = query.data.split(":", 2)[2]  # type: ignore[union-attr]
        if wc.get(wid) is None:
            await query.answer("Watcher not found.", show_alert=True)
            return
        ask = await query.message.answer(  # type: ignore[union-attr]
            "Send the new prompt text:",
            reply_markup=_input_cancel_kb(wid),
        )
        _pending[query.message.chat.id] = {  # type: ignore[union-attr]
            "action": "create_prompt",
            "watcher_id": wid,
            "ask_msg_id": ask.message_id,
            "return_to": "prompts",
        }
        await query.answer()

    # ── Single message handler — dispatches by pending action ──────────────────

    @dp.message()
    async def handle_message(message: Message, bot: Bot) -> None:
        pending = _pending.get(message.chat.id)
        text = (message.text or "").strip()

        if pending is None:
            log.warning("unhandled message chat_id=%d user=%s text=%r",
                        message.chat.id,
                        message.from_user.username if message.from_user else None,
                        text)
            return

        action = pending["action"]
        ask_msg_id: int | None = pending.get("ask_msg_id")

        async def _cleanup_input() -> None:
            """Delete the bot's ask message and the user's reply."""
            if ask_msg_id:
                with contextlib.suppress(Exception):
                    await bot.delete_message(message.chat.id, ask_msg_id)
            with contextlib.suppress(Exception):
                await message.delete()

        # ── waiting for a new interval value ───────────────────────────────────
        if action == "edit_interval":
            if not text.isdigit() or int(text) <= 0:
                await message.answer("Please send a positive integer (seconds).")
                return
            w = wc.get(pending["watcher_id"])
            if w is None:
                _pending.pop(message.chat.id, None)
                await _cleanup_input()
                return
            w.interval = int(text)
            wc.save(w)
            _pending.pop(message.chat.id, None)
            await _cleanup_input()
            await bot.send_message(
                message.chat.id,
                f"⚙️ Modify <b>{_html.escape(w.name)}</b>:\n✅ Interval updated to <b>{w.interval}s</b>.",
                reply_markup=_modify_kb(w.id),
                parse_mode="HTML",
            )

        # ── waiting for replacement text for a specific prompt ─────────────────
        elif action == "edit_prompt_item":
            if not text:
                await message.answer("Please send non-empty text.")
                return
            wid = pending["watcher_id"]
            idx = pending["prompt_idx"]
            w = wc.get(wid)
            if w is None or idx >= len(w.prompts):
                _pending.pop(message.chat.id, None)
                await _cleanup_input()
                return
            w.prompts[idx] = text
            wc.save(w)
            _pending.pop(message.chat.id, None)
            await _cleanup_input()
            await _render_prompts(bot, message.chat.id, wid)

        # ── waiting for new prompt text to append ──────────────────────────────
        elif action == "create_prompt":
            if not text:
                await message.answer("Please send non-empty text.")
                return
            wid = pending["watcher_id"]
            w = wc.get(wid)
            if w is None:
                _pending.pop(message.chat.id, None)
                await _cleanup_input()
                return
            w.prompts.append(text)
            wc.save(w)
            _pending.pop(message.chat.id, None)
            await _cleanup_input()
            await _render_prompts(bot, message.chat.id, wid)

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
