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
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Resolve cai from the same bin/ dir as this Python interpreter so the
# subprocess works even when systemd doesn't have pyenv shims on PATH.
_CAI_BIN = str(Path(sys.executable).parent / "cai")

import aiosqlite

from .config import Settings
from .fetchers.browser import BrowserFetcher
from .notifier import build_short_diff, notify_change
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
# cai filter
# ---------------------------------------------------------------------------

async def _cai_filter(watcher_id: str, prompt: str, diff: str):
    """
    Run the diff through `cai` with the watcher's prompt.
    Returns True if cai says the change is relevant, False otherwise.
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="watcher_diff_"
        ) as f:
            tmp_path = f.name
            f.write(diff)

        proc = await asyncio.create_subprocess_exec(
            _CAI_BIN,
            "--file", tmp_path,
            # "--system-prompt", 'in the case you have nothing you want to pass through, always return ine',
            "--",
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info("[watch:%s] cai prompt: %s", watcher_id, prompt)
        log.info("[watch:%s] cai diff:\n%s", watcher_id, diff)

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            log.warning("[watch:%s] cai timed out after 30s", watcher_id)
            return None

        log.debug("[watch:%s] cai stdout: %s", watcher_id, stdout.decode().strip())
        if stderr:
            log.debug("[watch:%s] cai stderr: %s", watcher_id, stderr.decode().strip())

        if proc.returncode != 0:
            log.warning(
                "[watch:%s] cai exited with code %d: %s",
                watcher_id, proc.returncode, stderr.decode().strip(),
            )
            return None

        result = stdout.decode().strip()
        log.info("[watch:%s] cai filter result: %s", watcher_id, result)
        return result or None

    except Exception:
        log.exception("[watch:%s] cai filter failed — notifying anyway", watcher_id)
        return f"(cai filter unavailable — content changed)\n{diff}"
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Per-watcher task
# ---------------------------------------------------------------------------

def is_content_empty(content) -> bool:
    if content is None: return True
    if len(content) == 0: return True
    content = content.strip()

    if content == "\"\"": return True
    if content.lower() == "none": return True
    if content.lower() == "false": return True

    return False

async def _watch_task(settings: Settings, watcher: WatcherConfig) -> None:
    log.info("[watch:%s] task started", watcher.id)
    fetcher = BrowserFetcher()

    try:
        log.info("[watch:%s] launching browser fetcher", watcher.id)
        await fetcher.start(watcher.url)
        log.info("[watch:%s] browser fetcher ready", watcher.id)
    except Exception:
        log.exception("[watch:%s] failed to start fetcher", watcher.id)
        await _record_run(watcher.id, "error", "Failed to start fetcher")
        return

    last_hash, last_text = await _get_snapshot(watcher.id)

    try:
        while True:
            changed = False
            try:
                log.debug("[watch:%s] fetching %s", watcher.id, watcher.url)
                text = await fetcher.fetch()
                log.debug("[watch:%s] fetched %d chars: %s", watcher.id, len(text), text[:200])
                h = hashlib.sha256(text.encode()).hexdigest()

                if last_hash is not None and h != last_hash:
                    changed = True
                    log.info("[watch:%s] change detected", watcher.id)
                    diff = build_short_diff(last_text or "", text)
                    if watcher.prompts:
                        content: Optional[str] = diff
                        for i, prompt in enumerate(watcher.prompts):
                            log.info("[watch:%s] running prompt %d/%d", watcher.id, i + 1, len(watcher.prompts))
                            content = await _cai_filter(watcher.id, prompt, content)
                            if not content:
                                log.info("[watch:%s] prompt %d/%d produced empty result — stopping chain", watcher.id, i + 1, len(watcher.prompts))
                                break

                            log.info("[watch:%s] prompt %d/%d produced result: %s", watcher.id, i + 1, len(watcher.prompts), content)
                        notification_text = content or None
                    else:
                        notification_text = diff
                    if not is_content_empty(notification_text):
                        await notify_change(settings, watcher, notification_text)

                await _save_snapshot(watcher.id, h, text)
                last_hash = h
                last_text = text
                await _record_run(watcher.id, "changed" if changed else "ok")

            except asyncio.CancelledError:
                log.info("[watch:%s] cancelled inside poll loop", watcher.id)
                raise
            except Exception:
                log.exception("[watch:%s] unexpected error during poll", watcher.id)
                await _record_run(watcher.id, "error", "Unexpected error — see logs")

            log.debug("[watch:%s] sleeping %ds", watcher.id, watcher.interval)
            await asyncio.sleep(watcher.interval)
    except asyncio.CancelledError:
        log.info("[watch:%s] task cancelled — entering finally", watcher.id)
        raise
    finally:
        log.info("[watch:%s] closing browser fetcher...", watcher.id)
        await fetcher.close()
        log.info("[watch:%s] browser fetcher closed", watcher.id)


# ---------------------------------------------------------------------------
# Engine: watches the watchers dir and manages tasks
# ---------------------------------------------------------------------------

def _config_changed(old: WatcherConfig, new: WatcherConfig) -> bool:
    return old.url != new.url or old.interval != new.interval or old.prompts != new.prompts


async def _cancel_task(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def run_engine(settings: Settings) -> None:
    """
    Continuously scan the watchers directory and maintain one asyncio task
    per enabled watcher.  Handles add/remove/disable/config-change dynamically.
    """
    log.info("Engine starting (rescan every %ds)", RESCAN_INTERVAL)
    tasks: dict[str, asyncio.Task] = {}
    configs: dict[str, WatcherConfig] = {}  # config each task was started with

    try:
        while True:
            current = {w.id: w for w in load_all() if w.enabled}

            # Cancel tasks for removed or disabled watchers
            for wid in list(tasks):
                if wid not in current:
                    log.info("Stopping watcher task: %s", wid)
                    await _cancel_task(tasks.pop(wid))
                    configs.pop(wid, None)

            # Start tasks for new watchers or restart tasks whose config changed
            for wid, watcher in current.items():
                if wid in tasks and not tasks[wid].done():
                    if _config_changed(configs[wid], watcher):
                        log.info("Config changed for watcher %s — restarting task", wid)
                        await _cancel_task(tasks.pop(wid))
                        configs.pop(wid, None)
                    else:
                        continue
                task = asyncio.create_task(
                    _watch_task(settings, watcher),
                    name=f"watch-{wid}",
                )
                tasks[wid] = task
                configs[wid] = watcher

            await asyncio.sleep(RESCAN_INTERVAL)

    except asyncio.CancelledError:
        log.info("[engine] cancelled — shutting down %d watch task(s)", len(tasks))
        for wid, task in tasks.items():
            log.info("[engine] cancelling watch task: %s", wid)
            task.cancel()
        log.info("[engine] waiting for all watch tasks to finish...")
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for wid, result in zip(tasks.keys(), results):
            log.info("[engine] watch task %s finished: %r", wid, result)
        log.info("[engine] all watch tasks done — re-raising CancelledError")
        raise
