"""
Interactive element picker using a headed Playwright browser.

Flow:
1. Launch headed Chromium
2. After every page load, evaluate JS that injects a floating toolbar into <body>
3. User navigates freely, then clicks "Pick Element"
4. Hover highlights elements (blue outline)
5. Click to select → JS generates CSS selector → user confirms
6. Python receives selector via exposed function, closes browser
7. Returns PickResult(url, selector, title)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("watcher.picker")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class PickResult:
    url: str
    selector: str
    title: str


# ---------------------------------------------------------------------------
# JavaScript evaluated after each page load (DOM is guaranteed ready)
# ---------------------------------------------------------------------------

_TOOLBAR_JS = r"""
(function () {
  if (document.getElementById('__watcher_toolbar')) return;

  // ── inject <style> into <head> ────────────────────────────────────────
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
    #__watcher_toolbar .wt-selector {
      font-family: monospace !important;
      font-size: 11px !important;
      background: #313244 !important;
      border-radius: 4px !important;
      padding: 4px 6px !important;
      margin-bottom: 8px !important;
      word-break: break-all !important;
      min-height: 22px !important;
      color: #f38ba8 !important;
      display: none !important;
    }
    #__watcher_toolbar .wt-selector.visible { display: block !important; }
    #__watcher_toolbar button {
      background: #89b4fa !important;
      color: #1e1e2e !important;
      border: none !important;
      border-radius: 4px !important;
      padding: 5px 10px !important;
      cursor: pointer !important;
      font-size: 12px !important;
      font-weight: 600 !important;
      margin-right: 4px !important;
    }
    #__watcher_toolbar button:hover { background: #b4d0ff !important; }
    #__watcher_toolbar button.wt-danger { background: #f38ba8 !important; color: #1e1e2e !important; }
    #__watcher_toolbar button.wt-danger:hover { background: #f5a0b5 !important; }
    #__watcher_toolbar button.wt-ok { background: #a6e3a1 !important; color: #1e1e2e !important; }
    #__watcher_toolbar button.wt-ok:hover { background: #bff0ba !important; }
    #__watcher_overlay {
      position: fixed !important;
      pointer-events: none !important;
      z-index: 2147483646 !important;
      outline: 3px solid #89b4fa !important;
      outline-offset: 2px !important;
      background: rgba(137,180,250,.10) !important;
      border-radius: 2px !important;
      display: none !important;
      transition: left .07s ease, top .07s ease, width .07s ease, height .07s ease !important;
    }
    #__watcher_overlay.wt-selected {
      outline-color: #a6e3a1 !important;
      background: rgba(166,227,161,.15) !important;
    }
    #__watcher_tooltip {
      position: fixed !important;
      pointer-events: none !important;
      z-index: 2147483647 !important;
      background: #1e1e2e !important;
      color: #cdd6f4 !important;
      font-family: monospace !important;
      font-size: 11px !important;
      padding: 3px 7px !important;
      border-radius: 4px !important;
      white-space: nowrap !important;
      display: none !important;
      border: 1px solid #45475a !important;
    }
    #__watcher_tooltip .wt-tag { color: #89b4fa !important; }
    #__watcher_tooltip .wt-dims { color: #a6e3a1 !important; margin-left: 6px !important; }
  `;
  (document.head || document.documentElement).appendChild(style);

  // ── toolbar element ───────────────────────────────────────────────────
  const bar = document.createElement('div');
  bar.id = '__watcher_toolbar';
  bar.innerHTML = `
    <div class="wt-title">👁 Watcher</div>
    <div class="wt-status" id="__wt_status">Navigate to the page you want to monitor.</div>
    <div class="wt-selector" id="__wt_sel"></div>
    <div id="__wt_btns">
      <button id="__wt_pick">Pick Element</button>
    </div>
  `;
  document.body.appendChild(bar);

  // ── highlight overlay ─────────────────────────────────────────────────
  const overlay = document.createElement('div');
  overlay.id = '__watcher_overlay';
  document.body.appendChild(overlay);

  // ── tooltip (tag + dimensions) ────────────────────────────────────────
  const tooltip = document.createElement('div');
  tooltip.id = '__watcher_tooltip';
  tooltip.innerHTML = '<span class="wt-tag"></span><span class="wt-dims"></span>';
  document.body.appendChild(tooltip);

  // ── state ─────────────────────────────────────────────────────────────
  let pickedSelector = null;

  function setStatus(msg) {
    document.getElementById('__wt_status').textContent = msg;
  }

  function setButtons(html) {
    document.getElementById('__wt_btns').innerHTML = html;
    const pick    = document.getElementById('__wt_pick');
    const confirm = document.getElementById('__wt_confirm');
    const repick  = document.getElementById('__wt_repick');
    if (pick)    pick.addEventListener('click', startPicking);
    if (confirm) confirm.addEventListener('click', doConfirm);
    if (repick)  repick.addEventListener('click', startPicking);
  }

  // ── selector generator ────────────────────────────────────────────────
  function uniqueSelector(el) {
    if (!el || el === document.body || el === document.documentElement) return 'body';

    // 1. unique id?
    if (el.id) {
      const sel = '#' + CSS.escape(el.id);
      try { if (document.querySelectorAll(sel).length === 1) return sel; } catch(e) {}
    }

    // 2. walk up, building a path
    const parts = [];
    let cur = el;
    while (cur && cur !== document.documentElement && cur !== document.body) {
      let part = cur.tagName.toLowerCase();

      // up to 2 non-dynamic classes
      if (cur.classList && cur.classList.length) {
        const classes = Array.from(cur.classList)
          .filter(c => c.length < 40 && !/^(js-|is-|has-)/.test(c))
          .slice(0, 2);
        if (classes.length) part += '.' + classes.map(c => CSS.escape(c)).join('.');
      }

      // add nth-child when siblings share the same tag
      if (cur.parentElement) {
        const sameTag = Array.from(cur.parentElement.children)
          .filter(s => s.tagName === cur.tagName);
        if (sameTag.length > 1) {
          const pos = Array.from(cur.parentElement.children).indexOf(cur) + 1;
          part += `:nth-child(${pos})`;
        }
      }

      parts.unshift(part);

      try {
        const candidate = parts.join(' > ');
        if (document.querySelectorAll(candidate).length === 1) return candidate;
      } catch(e) {}

      cur = cur.parentElement;
    }

    return parts.join(' > ') || el.tagName.toLowerCase();
  }

  // ── picking mode ──────────────────────────────────────────────────────
  function startPicking() {
    setStatus('Hover over an element and click to select it.');
    setButtons('<button id="__wt_pick" class="wt-danger">Cancel</button>');
    document.getElementById('__wt_pick').addEventListener('click', cancelPicking);
    document.addEventListener('mouseover', onHover, true);
    document.addEventListener('click', onClick, true);
    document.body.style.cursor = 'crosshair';
  }

  function cancelPicking() {
    overlay.style.display = 'none';
    overlay.classList.remove('wt-selected');
    tooltip.style.display = 'none';
    document.removeEventListener('mouseover', onHover, true);
    document.removeEventListener('click', onClick, true);
    document.body.style.cursor = '';
    document.getElementById('__wt_sel').classList.remove('visible');
    setStatus('Navigate to the page you want to monitor.');
    setButtons('<button id="__wt_pick">Pick Element</button>');
  }

  function positionOverlay(el) {
    const r = el.getBoundingClientRect();
    overlay.style.left   = r.left   + window.scrollX + 'px';
    overlay.style.top    = r.top    + window.scrollY + 'px';
    overlay.style.width  = r.width  + 'px';
    overlay.style.height = r.height + 'px';
    return r;
  }

  function showTooltip(el, r) {
    const tag = el.tagName.toLowerCase() +
      (el.id ? '#' + el.id : '') +
      (el.classList.length ? '.' + Array.from(el.classList).slice(0,2).join('.') : '');
    tooltip.querySelector('.wt-tag').textContent = tag;
    tooltip.querySelector('.wt-dims').textContent =
      Math.round(r.width) + 'x' + Math.round(r.height);
    // position tooltip just above the overlay, clamp to viewport
    const ty = Math.max(0, r.top + window.scrollY - 24);
    tooltip.style.left = Math.min(r.left + window.scrollX, window.innerWidth - 200) + 'px';
    tooltip.style.top  = ty + 'px';
    tooltip.style.display = 'block';
  }

  function onHover(e) {
    const target = e.target;
    if (bar.contains(target) || target === overlay || target === tooltip) return;
    overlay.style.display = 'block';
    overlay.classList.remove('wt-selected');
    const r = positionOverlay(target);
    showTooltip(target, r);
  }

  function onClick(e) {
    const target = e.target;
    if (bar.contains(target) || target === overlay || target === tooltip) return;
    e.preventDefault();
    e.stopPropagation();

    document.removeEventListener('mouseover', onHover, true);
    document.removeEventListener('click', onClick, true);
    document.body.style.cursor = '';

    // keep overlay on the selected element, switch to green
    overlay.classList.add('wt-selected');
    const r = positionOverlay(target);
    showTooltip(target, r);

    pickedSelector = uniqueSelector(target);

    const selDiv = document.getElementById('__wt_sel');
    selDiv.textContent = pickedSelector;
    selDiv.classList.add('visible');
    setStatus('Element selected. Confirm or pick again.');
    setButtons(`
      <button id="__wt_confirm" class="wt-ok">Confirm</button>
      <button id="__wt_repick">Re-pick</button>
    `);
  }

  function doConfirm() {
    setStatus('Saving…');
    setButtons('');
    if (window.__watcherConfirm) {
      window.__watcherConfirm(pickedSelector);
    }
  }

  // ── initial button binding ────────────────────────────────────────────
  document.getElementById('__wt_pick').addEventListener('click', startPicking);
})();
"""


# ---------------------------------------------------------------------------
# Main picker coroutine
# ---------------------------------------------------------------------------

async def pick_element() -> PickResult:
    """
    Launch a headed browser, inject the picker toolbar after each page load,
    wait for the user to confirm a selection, then return PickResult.

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
    result_future: asyncio.Future[PickResult] = loop.create_future()

    async def inject_toolbar(page: Page) -> None:
        """Called after every page load to inject the toolbar into <body>."""
        try:
            await page.evaluate(_TOOLBAR_JS)
        except Exception:
            pass  # navigation may have already moved on; safe to ignore

    async def on_confirm(selector: str) -> None:
        if result_future.done():
            return
        url = page.url
        title = await page.title()
        result_future.set_result(PickResult(url=url, selector=selector, title=title))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()

        try:
            from playwright_stealth import stealth_async  # type: ignore
            page = await context.new_page()
            await stealth_async(page)
        except ImportError:
            page = await context.new_page()

        # Expose the Python callback (persists across navigations)
        await page.expose_function("__watcherConfirm", on_confirm)

        # Inject toolbar after every load (DOM is guaranteed ready at this point)
        page.on("load", lambda: asyncio.ensure_future(inject_toolbar(page)))

        # Open a start page so the toolbar appears immediately
        await page.goto("about:blank")
        await inject_toolbar(page)  # about:blank doesn't fire "load" reliably

        log.info("Picker browser opened — waiting for user selection")

        try:
            result = await asyncio.wait_for(result_future, timeout=3600)
        except asyncio.TimeoutError:
            raise RuntimeError("Element picker timed out after 1 hour.")

        await browser.close()

    log.info("Picker got selector=%r url=%r", result.selector, result.url)
    return result
