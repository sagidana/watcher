"""
Monitoring engine.

run_engine() scans ~/.config/watcher/watchers/ every 10 s and keeps one
asyncio task per enabled watcher.  Each task polls its target at the
configured interval and sends a Telegram notification when the content changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Optional

import aiosqlite

from .config import Settings
from .fetchers.browser import BrowserFetcher, ElementNotFoundError
from .notifier import notify_change
from .watchers_config import WatcherConfig, load_all

log = logging.getLogger("watcher.engine")

DB_PATH = Path("~/.config/watcher/state.db").expanduser()
RESCAN_INTERVAL = 10  # seconds between directory scans


# ---------------------------------------------------------------------------
# DB helpers (async)
# ---------------------------------------------------------------------------

async def _get_snapshot(watcher_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return (hash, text) for watcher_id, or (None, None) if not seen yet."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT hash, snapshot FROM snapshots WHERE watcher_id = ?", (watcher_id,)
        ) as cur:
            row = await cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


async def _save_snapshot(watcher_id: str, h: str, text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO snapshots (watcher_id, hash, snapshot, checked_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(watcher_id) DO UPDATE SET
                hash       = excluded.hash,
                snapshot   = excluded.snapshot,
                checked_at = excluded.checked_at
            """,
            (watcher_id, h, text),
        )
        await db.commit()


async def _record_run(watcher_id: str, status: str, detail: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO runs (watcher_id, status, detail) VALUES (?, ?, ?)",
            (watcher_id, status, detail or None),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Per-watcher task
# ---------------------------------------------------------------------------

async def _watch_task(settings: Settings, watcher: WatcherConfig) -> None:
    log.info("Starting watcher: %s (%s)", watcher.name, watcher.id)
    fetcher = BrowserFetcher()

    try:
        await fetcher.start(watcher.url, watcher.selector)
    except Exception:
        log.exception("Failed to start fetcher for watcher %s", watcher.id)
        await _record_run(watcher.id, "error", "Failed to start fetcher")
        return

    last_hash, last_text = await _get_snapshot(watcher.id)

    while True:
        changed = False
        try:
            text = await fetcher.fetch()
            h = hashlib.sha256(text.encode()).hexdigest()

            if last_hash is not None and h != last_hash:
                changed = True
                log.info("Change detected in watcher %s", watcher.id)
                await notify_change(settings, watcher, last_text or "", text)

            await _save_snapshot(watcher.id, h, text)
            last_hash = h
            last_text = text
            await _record_run(watcher.id, "changed" if changed else "ok")

        except ElementNotFoundError as exc:
            log.warning("Selector not found for watcher %s: %s", watcher.id, exc)
            await _record_run(watcher.id, "error", str(exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error polling watcher %s", watcher.id)
            await _record_run(watcher.id, "error", "Unexpected error — see logs")

        await asyncio.sleep(watcher.interval)


# ---------------------------------------------------------------------------
# Engine: watches the watchers dir and manages tasks
# ---------------------------------------------------------------------------

async def run_engine(settings: Settings) -> None:
    """
    Continuously scan the watchers directory and maintain one asyncio task
    per enabled watcher.  Handles add/remove/disable dynamically.
    """
    log.info("Engine starting (rescan every %ds)", RESCAN_INTERVAL)
    tasks: dict[str, asyncio.Task] = {}

    try:
        while True:
            current = {w.id: w for w in load_all() if w.enabled}

            # Cancel tasks for removed or disabled watchers
            for wid in list(tasks):
                if wid not in current:
                    log.info("Stopping watcher task: %s", wid)
                    tasks[wid].cancel()
                    try:
                        await tasks[wid]
                    except (asyncio.CancelledError, Exception):
                        pass
                    del tasks[wid]

            # Start tasks for new watchers
            for wid, watcher in current.items():
                if wid not in tasks or tasks[wid].done():
                    task = asyncio.create_task(
                        _watch_task(settings, watcher),
                        name=f"watch-{wid}",
                    )
                    tasks[wid] = task

            await asyncio.sleep(RESCAN_INTERVAL)

    except asyncio.CancelledError:
        log.info("Engine shutting down — cancelling %d task(s)", len(tasks))
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        raise
