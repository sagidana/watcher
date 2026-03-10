"""
Persistent browser fetcher for a single watcher.

Keeps one Playwright browser context alive per watcher to avoid the overhead
of spawning a fresh browser on every poll cycle.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("watcher.fetcher.browser")


class ElementNotFoundError(Exception):
    """Raised when the CSS selector matches nothing on the page."""


class BrowserFetcher:
    """
    One instance per watcher.  Call:
        await fetcher.start(url, selector)
        text = await fetcher.fetch()   # call repeatedly
        await fetcher.close()
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._url: str = ""
        self._selector: str = ""

    async def start(self, url: str, selector: str) -> None:
        from playwright.async_api import async_playwright

        self._url = url
        self._selector = selector

        self._playwright = await async_playwright().start()

        # Try stealth; fall back gracefully
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        try:
            from playwright_stealth import stealth_async  # type: ignore
            self._page = await self._context.new_page()
            await stealth_async(self._page)
        except ImportError:
            self._page = await self._context.new_page()

        await self._navigate()

    async def _navigate(self) -> None:
        assert self._page is not None
        try:
            await self._page.goto(self._url, wait_until="networkidle", timeout=30_000)
        except Exception:
            # Fallback: domcontentloaded is faster and more reliable for heavy SPAs
            try:
                await self._page.goto(self._url, wait_until="domcontentloaded", timeout=20_000)
            except Exception as exc:
                log.warning("Navigation fallback also failed: %s", exc)
                raise

    async def fetch(self) -> str:
        """Reload the page and return inner_text() of the selector (normalised)."""
        assert self._page is not None

        try:
            await self._page.reload(wait_until="networkidle", timeout=30_000)
        except Exception:
            await self._page.reload(wait_until="domcontentloaded", timeout=20_000)

        el = await self._page.query_selector(self._selector)
        if el is None:
            raise ElementNotFoundError(
                f"Selector {self._selector!r} not found on {self._url}"
            )

        raw = await el.inner_text()
        return _normalise(raw)

    async def close(self) -> None:
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            log.exception("Error closing BrowserFetcher")
        finally:
            self._page = self._context = self._browser = self._playwright = None


def _normalise(text: str) -> str:
    """Strip and collapse internal whitespace."""
    text = text.strip()
    text = re.sub(r"[ \t]+", " ", text)       # collapse horizontal whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)    # collapse blank lines
    return text
