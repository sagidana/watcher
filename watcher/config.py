"""
Load runtime settings from ~/.config/watcher/.env and settings.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

CONFIG_DIR = Path.home() / ".config" / "watcher"


@dataclass
class TelegramSettings:
    token: str
    chat_id: int
    # Seconds Telegram holds each getUpdates request open (1-55).
    # Exposed in ~/.config/watcher/settings.yaml under telegram.poll_timeout.
    poll_timeout: int = 30


@dataclass
class Settings:
    telegram: TelegramSettings
    log_level: str = "INFO"
    headed: bool = False


def load_settings() -> Settings:
    """Load .env then settings.yaml; return a fully-populated Settings object."""
    load_dotenv(CONFIG_DIR / ".env")

    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id_raw:
        raise RuntimeError(
            "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in "
            f"{CONFIG_DIR / '.env'}"
        )

    poll_timeout = 30
    log_level = "INFO"
    settings_file = CONFIG_DIR / "settings.yaml"
    if settings_file.exists():
        raw = yaml.safe_load(settings_file.read_text()) or {}
        poll_timeout = int(
            raw.get("telegram", {}).get("poll_timeout", poll_timeout)
        )
        poll_timeout = max(1, min(55, poll_timeout))  # Telegram hard limits
        log_level = str(raw.get("log_level", log_level)).upper()

    return Settings(
        telegram=TelegramSettings(
            token=token,
            chat_id=int(chat_id_raw),
            poll_timeout=poll_timeout,
        ),
        log_level=log_level,
    )
