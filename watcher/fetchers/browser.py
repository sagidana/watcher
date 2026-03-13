"""
Persistent browser fetcher for a single watcher.

Keeps one Playwright browser context alive per watcher to avoid the overhead
of spawning a fresh browser on every poll cycle.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("watcher.fetcher.browser")


class BrowserFetcher:
    """
    One instance per watcher.  Call:
        await fetcher.start(url)
        text = await fetcher.fetch()   # call repeatedly
        await fetcher.close()
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._url: str = ""

    async def start(self, url: str) -> None:
        from playwright.async_api import async_playwright

        self._url = url

        self._playwright = await async_playwright().start()

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
        """Reload the page and return visible text content (normalised)."""
        assert self._page is not None

        try:
            await self._page.reload(wait_until="networkidle", timeout=30_000)
        except Exception:
            try:
                await self._page.reload(wait_until="domcontentloaded", timeout=20_000)
            except Exception as exc:
                # Browser/page was closed (crash, OOM, etc.) — attempt full restart
                log.warning("fetch [%s]: reload failed (%s), attempting browser restart", self._url, exc)
                try:
                    await self.close()
                    await self.start(self._url)
                except Exception as restart_exc:
                    log.error("fetch [%s]: browser restart failed: %s", self._url, restart_exc)
                    return ""

        try:
            content: str = await self._page.evaluate(
                """
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
                """,
                None,
            )
            log.debug(
                "[browser] extracted %d chars from %s",
                len(content), self._url,
            )
            return _normalise(content)
        except Exception as e:
            log.info(f"[browser] ({self._url}): exception {e}")

        return ""


    async def close(self) -> None:
        try:
            if self._page:
                log.info("[browser] closing page...")
                await self._page.close()
                log.info("[browser] page closed")
            if self._context:
                log.info("[browser] closing context...")
                await self._context.close()
                log.info("[browser] context closed")
            if self._browser:
                log.info("[browser] closing browser...")
                await self._browser.close()
                log.info("[browser] browser closed")
            if self._playwright:
                log.info("[browser] stopping playwright...")
                await self._playwright.stop()
                log.info("[browser] playwright stopped")
        except Exception:
            log.exception("[browser] error during close")
        finally:
            self._page = self._context = self._browser = self._playwright = None


def _normalise(text: str) -> str:
    """Strip and collapse internal whitespace."""
    text = text.strip()
    text = re.sub(r"[ \t]+", " ", text)       # collapse horizontal whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)    # collapse blank lines
    return text
