"""
Watcher service entrypoint.
Starts the scheduler and Telegram bot in a shared asyncio event loop.
"""

import asyncio
import logging
from pathlib import Path

LOG_FILE = Path("/tmp/watcher.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("watcher")


async def run() -> None:
    log.info("Watcher starting...")
    # Phase 2: scheduler and bot will be wired here
    log.info("Watcher running. Press Ctrl-C to stop.")
    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        pass
    log.info("Watcher stopped.")


if __name__ == "__main__":
    asyncio.run(run())
