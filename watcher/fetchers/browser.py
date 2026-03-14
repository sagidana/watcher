"""
Browser fetcher backed by a single shared persistent Playwright context.

All watchers share one Chromium profile at ~/.config/watcher/browser-profile/
so that cookies/sessions (e.g. Google login) are preserved across restarts and
visible to every watcher.

Playwright locks a user-data-dir to a single process, so a module-level
singleton holds the context; each BrowserFetcher owns only its page.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from watcher.config import CONFIG_DIR

log = logging.getLogger("watcher.fetcher.browser")

PROFILE_DIR: Path = CONFIG_DIR / "browser-profile"

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Extracts visible text nodes by walking the DOM in-browser (single IPC call).
_EXTRACT_JS = """
() => {
    const texts = [];
    const walker = document.createTreeWalker(
        document.body,
        NodeFilter.SHOW_TEXT,
        {
            acceptNode(node) {
                let el = node.parentElement;
                while (el) {
                    const s = window.getComputedStyle(el);
                    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0')
                        return NodeFilter.FILTER_REJECT;
                    el = el.parentElement;
                }
                return NodeFilter.FILTER_ACCEPT;
            }
        }
    );
    let node;
    while ((node = walker.nextNode())) {
        const t = node.textContent.trim();
        if (t) texts.push(t);
    }
    return texts.join('\\n');
}
"""

# ---------------------------------------------------------------------------
# Shared persistent context (module-level singleton)
# ---------------------------------------------------------------------------

_lock: asyncio.Lock | None = None
_playwright = None
_context = None


def _get_lock() -> asyncio.Lock:
    """Return the module-level lock, creating it lazily inside a running loop."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _get_shared_context(headless: bool = True):
    """Return the shared persistent browser context, launching it if needed."""
    global _playwright, _context

    async with _get_lock():
        if _context is None:
            from playwright.async_api import async_playwright

            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            log.info("[browser] launching persistent context (profile: %s)", PROFILE_DIR)
            _playwright = await async_playwright().start()
            _context = await _playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=headless,
                user_agent=_USER_AGENT,
            )

    return _context


async def close_shared_browser() -> None:
    """Shut down the shared context.  Call once at process exit."""
    global _playwright, _context

    if _context is not None:
        try:
            await _context.close()
        except Exception:
            log.exception("[browser] error closing shared context")
        finally:
            _context = None

    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            log.exception("[browser] error stopping playwright")
        finally:
            _playwright = None


# ---------------------------------------------------------------------------
# Per-watcher fetcher (owns only a page)
# ---------------------------------------------------------------------------


class BrowserFetcher:
    """
    One instance per watcher.  Call:
        await fetcher.start(url)
        text = await fetcher.fetch()   # call repeatedly
        await fetcher.close()

    All instances share a single Chromium profile so logins persist globally.
    """

    def __init__(self) -> None:
        self._page = None
        self._url: str = ""
        self._headless: bool = True

    async def start(self, url: str, headless: bool = True) -> None:
        self._url = url
        self._headless = headless

        context = await _get_shared_context(headless=headless)
        self._page = await context.new_page()

        try:
            from playwright_stealth import stealth_async  # type: ignore
            await stealth_async(self._page)
        except ImportError:
            pass

        await self._goto(self._url)

    async def _goto(self, url: str) -> None:
        """Navigate to url, falling back to domcontentloaded if networkidle times out."""
        assert self._page is not None
        try:
            await self._page.goto(url, wait_until="networkidle", timeout=30_000)
            return
        except Exception:
            pass

        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except Exception as exc:
            log.warning("_goto [%s]: both navigation strategies failed: %s", url, exc)
            raise

    async def _reload(self) -> None:
        """Reload the current page, falling back to domcontentloaded if networkidle times out."""
        assert self._page is not None
        try:
            await self._page.reload(wait_until="networkidle", timeout=30_000)
            return
        except Exception:
            pass

        await self._page.reload(wait_until="domcontentloaded", timeout=20_000)

    async def _reopen_page(self) -> None:
        """Close the current page and open a fresh one from the shared context."""
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None

        context = await _get_shared_context(headless=self._headless)
        self._page = await context.new_page()

        try:
            from playwright_stealth import stealth_async  # type: ignore
            await stealth_async(self._page)
        except ImportError:
            pass

    async def fetch(self) -> str:
        """Reload the page and return visible text content (normalised)."""
        assert self._page is not None

        try:
            await self._reload()
        except Exception as exc:
            log.warning("fetch [%s]: reload failed (%s), reopening page", self._url, exc)
            try:
                await self._reopen_page()
                await self._goto(self._url)
            except Exception as reopen_exc:
                log.error("fetch [%s]: page reopen failed: %s", self._url, reopen_exc)
                return ""

        try:
            content: str = await self._page.evaluate(_EXTRACT_JS, None)
            log.debug("[browser] '%s' -> extracted %s", self._url, content)
            return _normalise(content)
        except Exception:
            log.exception("[browser] extraction failed (%s)", self._url)
            return ""

    async def close(self) -> None:
        """Close this watcher's page.  The shared context/browser stays open."""
        try:
            if self._page:
                await self._page.close()
        except Exception:
            log.exception("[browser] error closing page for %s", self._url)
        finally:
            self._page = None


def _normalise(text: str) -> str:
    """Strip and collapse internal whitespace."""
    text = text.strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text
