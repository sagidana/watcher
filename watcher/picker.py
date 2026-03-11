"""
Interactive URL confirmer using a headed Playwright browser.

Flow:
1. Launch headed Chromium
2. After every page load, evaluate JS that injects a floating toolbar into <body>
3. User navigates freely, then clicks "Confirm" to select the current URL
4. Python receives the URL via exposed function, closes browser
5. Returns PickResult(url, title), or None if browser was closed without confirming
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("watcher.picker")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class PickResult:
    url: str
    title: str


# ---------------------------------------------------------------------------
# JavaScript evaluated after each page load (DOM is guaranteed ready)
# ---------------------------------------------------------------------------

_TOOLBAR_JS = r"""
(function () {
  if (document.getElementById('__watcher_toolbar')) return;

  const style = document.createElement('style');
  style.textContent = `
    #__watcher_toolbar {
      position: fixed !important;
      top: 12px !important;
      right: 12px !important;
      z-index: 2147483647 !important;
      background: #1e1e2e !important;
      color: #cdd6f4 !important;
      font-family: system-ui, sans-serif !important;
      font-size: 13px !important;
      border-radius: 8px !important;
      padding: 10px 14px !important;
      box-shadow: 0 4px 20px rgba(0,0,0,.6) !important;
      min-width: 260px !important;
      max-width: 380px !important;
      user-select: none !important;
      pointer-events: auto !important;
      line-height: 1.4 !important;
    }
    #__watcher_toolbar * { box-sizing: border-box !important; }
    #__watcher_toolbar .wt-title {
      font-weight: 700 !important;
      margin-bottom: 8px !important;
      color: #89b4fa !important;
    }
    #__watcher_toolbar .wt-status {
      font-size: 11px !important;
      color: #a6e3a1 !important;
      margin-bottom: 8px !important;
      min-height: 14px !important;
    }
    #__watcher_toolbar button {
      background: #a6e3a1 !important;
      color: #1e1e2e !important;
      border: none !important;
      border-radius: 4px !important;
      padding: 5px 10px !important;
      cursor: pointer !important;
      font-size: 12px !important;
      font-weight: 600 !important;
    }
    #__watcher_toolbar button:hover { background: #bff0ba !important; }
  `;
  (document.head || document.documentElement).appendChild(style);

  const bar = document.createElement('div');
  bar.id = '__watcher_toolbar';
  bar.innerHTML = `
    <div class="wt-title">👁 Watcher</div>
    <div class="wt-status">Navigate to the page you want to monitor, then click Confirm.</div>
    <button id="__wt_confirm">Confirm</button>
  `;
  document.body.appendChild(bar);

  document.getElementById('__wt_confirm').addEventListener('click', function () {
    if (window.__watcherConfirm) {
      window.__watcherConfirm();
    }
  });
})();
"""


# ---------------------------------------------------------------------------
# Main picker coroutine
# ---------------------------------------------------------------------------

async def pick_element() -> Optional[PickResult]:
    """
    Launch a headed browser, inject the confirm toolbar after each page load,
    wait for the user to confirm the current URL, then return PickResult.

    Returns None if the browser was closed without confirming.
    Raises RuntimeError if no display is available.
    """
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError(
            "No display found (DISPLAY / WAYLAND_DISPLAY not set).\n"
            "On WSL2, ensure WSLg is active (Windows 11 + wsl --update).\n"
            "Alternatively set DISPLAY=:0 if an X server is running."
        )

    from playwright.async_api import async_playwright, Page

    loop = asyncio.get_event_loop()
    result_future: asyncio.Future[Optional[PickResult]] = loop.create_future()

    async def inject_toolbar(page: Page) -> None:
        try:
            await page.evaluate(_TOOLBAR_JS)
        except Exception:
            pass  # navigation may have already moved on; safe to ignore

    async def on_confirm() -> None:
        if result_future.done():
            return
        url = page.url
        title = await page.title()
        result_future.set_result(PickResult(url=url, title=title))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()

        try:
            from playwright_stealth import stealth_async  # type: ignore
            page = await context.new_page()
            await stealth_async(page)
        except ImportError:
            page = await context.new_page()

        # Resolve future with None if browser is closed without confirming
        def on_disconnected():
            if not result_future.done():
                result_future.set_result(None)

        browser.on("disconnected", lambda _: on_disconnected())

        # Expose the Python callback (persists across navigations)
        await page.expose_function("__watcherConfirm", on_confirm)

        # Inject toolbar after every load (DOM is guaranteed ready at this point)
        page.on("load", lambda _: asyncio.ensure_future(inject_toolbar(page)))

        # Open a start page so the toolbar appears immediately
        await page.goto("about:blank")
        await inject_toolbar(page)  # about:blank doesn't fire "load" reliably

        log.info("Picker browser opened — waiting for user confirmation")

        try:
            result = await asyncio.wait_for(result_future, timeout=3600)
        except asyncio.TimeoutError:
            raise RuntimeError("Picker timed out after 1 hour.")

        if result is not None:
            await browser.close()

    if result is not None:
        log.info("Picker confirmed url=%r", result.url)
    else:
        log.info("Picker browser closed without confirming — no watcher added")

    return result
