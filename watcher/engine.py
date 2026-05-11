"""
Monitoring engine.

run_engine() scans ~/.config/watcher/watchers/ every 10 s and keeps one
asyncio task per enabled watcher.  Each task invokes `cai` at the configured
interval, runs the prompt chain, and sends the final cai response to Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

# Resolve cai from the same bin/ dir as this Python interpreter so the
# subprocess works even when systemd doesn't have pyenv shims on PATH.
_CAI_BIN = str(Path(sys.executable).parent / "cai")

from .config import Settings
from .notifier import notify_change
from .watchers_config import WatcherConfig, load_all

log = logging.getLogger("watcher.engine")

RESCAN_INTERVAL = 10  # seconds between directory scans


def _default_system_prompt() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"time is {now}, you are a professions web searcher and investigator"


def is_content_empty(content) -> bool:
    if content is None:
        return True
    if len(content) == 0:
        return True
    content = content.strip()

    if content == "\"\"":
        return True
    if content.lower() == "none":
        return True
    if content.lower() == "false":
        return True

    return False


# ---------------------------------------------------------------------------
# cai invocation
# ---------------------------------------------------------------------------

async def _run_cai(
    watcher_id: str,
    *,
    model: str,
    tools: list[str],
    system_prompt: str,
    prompt: str,
    input_file: Optional[str] = None,
    timeout: int = 600,
) -> Optional[str]:
    """
    Invoke cai with the given parameters and return stdout.
    Returns None on error/empty output.
    """
    args: list[str] = [_CAI_BIN, "--model", model]
    if tools:
        args += ["--tools", *tools]
    if system_prompt:
        args += ["--system-prompt", system_prompt]
    if input_file:
        args += ["--file", input_file]
    args += ["--", prompt]

    log.info("[watch:%s] running cai (model=%s tools=%s)", watcher_id, model, tools)
    log.debug("[watch:%s] cai prompt: %s", watcher_id, prompt)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            log.warning("[watch:%s] cai timed out after %ds", watcher_id, timeout)
            return None

        if stderr:
            log.debug("[watch:%s] cai stderr: %s", watcher_id, stderr.decode(errors="replace").strip())

        if proc.returncode != 0:
            log.warning(
                "[watch:%s] cai exited with code %d: %s",
                watcher_id, proc.returncode, stderr.decode(errors="replace").strip(),
            )
            return None

        result = stdout.decode(errors="replace").strip()
        log.debug("[watch:%s] cai stdout: %s", watcher_id, result)
        return result or None

    except Exception:
        log.exception("[watch:%s] cai invocation failed", watcher_id)
        return None


async def _run_prompt_chain(watcher: WatcherConfig) -> Optional[str]:
    """Run cai for each prompt in sequence; pass prior output via --file. Stop on empty."""
    if not watcher.prompts:
        return None

    system_prompt = watcher.system_prompt.strip() or _default_system_prompt()
    content: Optional[str] = None
    tmp_path: Optional[str] = None

    try:
        for i, prompt in enumerate(watcher.prompts):
            log.info("[watch:%s] running prompt %d/%d", watcher.id, i + 1, len(watcher.prompts))

            input_file: Optional[str] = None
            if i > 0 and content:
                # Hand the prior step's output to cai as --file input.
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False, prefix=f"watcher_{watcher.id}_"
                ) as f:
                    tmp_path = f.name
                    f.write(content)
                input_file = tmp_path

            content = await _run_cai(
                watcher.id,
                model=watcher.model,
                tools=watcher.tools,
                system_prompt=system_prompt,
                prompt=prompt,
                input_file=input_file,
            )

            # Clean up the per-step temp file before the next iteration.
            if tmp_path:
                try:
                    Path(tmp_path).unlink()
                except OSError:
                    pass
                tmp_path = None

            if is_content_empty(content):
                log.info("[watch:%s] prompt %d/%d produced empty result — stopping chain",
                         watcher.id, i + 1, len(watcher.prompts))
                return None

            log.info("[watch:%s] prompt %d/%d produced %d chars",
                     watcher.id, i + 1, len(watcher.prompts), len(content or ""))

        return content
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Per-watcher run
# ---------------------------------------------------------------------------

async def _run_once(settings: Settings, watcher: WatcherConfig) -> bool:
    """Run the prompt chain once and notify if it produced output. Returns True on notify."""
    output = await _run_prompt_chain(watcher)
    if is_content_empty(output):
        return False
    assert output is not None
    await notify_change(settings, watcher, output)
    return True


async def fetch_once(settings: Settings, watcher: WatcherConfig) -> str:
    """
    One-shot run outside the regular poll loop.
    Returns "ok" if a notification was sent, "empty" if not, or "error".
    """
    try:
        sent = await _run_once(settings, watcher)
        return "ok" if sent else "empty"
    except Exception:
        log.exception("[watch:%s] fetch_once failed", watcher.id)
        return "error"


async def _watch_task(settings: Settings, watcher: WatcherConfig) -> None:
    log.info("[watch:%s] task started", watcher.id)
    try:
        while True:
            try:
                await _run_once(settings, watcher)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[watch:%s] unexpected error during run", watcher.id)

            log.debug("[watch:%s] sleeping %ds", watcher.id, watcher.interval)
            await asyncio.sleep(watcher.interval)
    except asyncio.CancelledError:
        log.info("[watch:%s] task cancelled", watcher.id)
        raise


# ---------------------------------------------------------------------------
# Engine: watches the watchers dir and manages tasks
# ---------------------------------------------------------------------------

def _config_changed(old: WatcherConfig, new: WatcherConfig) -> bool:
    return (
        old.interval != new.interval
        or old.prompts != new.prompts
        or old.model != new.model
        or old.system_prompt != new.system_prompt
        or old.tools != new.tools
    )


async def _cancel_task(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _shutdown_tasks(tasks: dict[str, asyncio.Task]) -> None:
    log.info("[engine] cancelled — shutting down %d watch task(s)", len(tasks))
    for wid, task in tasks.items():
        log.info("[engine] cancelling watch task: %s", wid)
        task.cancel()
    log.info("[engine] waiting for all watch tasks to finish...")
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for wid, result in zip(tasks.keys(), results):
        log.info("[engine] watch task %s finished: %r", wid, result)
    log.info("[engine] all watch tasks done — re-raising CancelledError")


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
                    if not _config_changed(configs[wid], watcher):
                        continue
                    log.info("Config changed for watcher %s — restarting task", wid)
                    await _cancel_task(tasks.pop(wid))
                    configs.pop(wid, None)
                tasks[wid] = asyncio.create_task(
                    _watch_task(settings, watcher), name=f"watch-{wid}"
                )
                configs[wid] = watcher

            await asyncio.sleep(RESCAN_INTERVAL)

    except asyncio.CancelledError:
        await _shutdown_tasks(tasks)
        raise
