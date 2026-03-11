"""
Send Telegram notifications when a watched element changes.
"""

from __future__ import annotations

import logging
from difflib import unified_diff

from aiogram import Bot

from .config import Settings
from .watchers_config import WatcherConfig

log = logging.getLogger("watcher.notifier")

_MAX_LINES = 10
_MAX_MSG = 4000  # Telegram max message length (4096, leave headroom)


def _split_diff_lines(old_text: str, new_text: str) -> tuple[list[str], list[str]]:
    """Return (added_lines, removed_lines) from a unified diff."""
    raw = list(unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm=""))
    added = [l[1:] for l in raw if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in raw if l.startswith("-") and not l.startswith("---")]
    return added, removed


def build_short_diff(old_text: str, new_text: str, max_lines: int = _MAX_LINES) -> str:
    """Return a compact diff string with added/removed lines (no context lines)."""
    added, removed = _split_diff_lines(old_text, new_text)
    parts: list[str] = []
    if added:
        parts.append("Added:\n" + "\n".join(f"+ {l}" for l in added[:max_lines]))
    if removed:
        parts.append("Removed:\n" + "\n".join(f"- {l}" for l in removed[:max_lines]))
    return "\n".join(parts) if parts else "(content changed but diff is empty)"


async def notify_change(
    settings: Settings,
    watcher: WatcherConfig,
    text: str,
) -> None:
    """Send a Telegram notification for a watcher change."""
    msg = f"{watcher.name}\n{watcher.url}\n\n{text}"

    if len(msg) > _MAX_MSG:
        msg = msg[:_MAX_MSG] + "\n…"

    bot = Bot(token=settings.telegram.token)
    try:
        await bot.send_message(
            chat_id=settings.telegram.chat_id,
            text=msg,
        )
        log.info("Notification sent for watcher %s", watcher.id)
    except Exception:
        log.exception("Failed to send Telegram notification for watcher %s", watcher.id)
    finally:
        await bot.session.close()


