"""
YAML CRUD for per-watcher config files.

Each watcher lives in its own file:
    ~/.config/watcher/watchers/<8-hex-id>.yaml
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

WATCHERS_DIR = Path("~/.config/watcher/watchers").expanduser()


@dataclass
class WatcherConfig:
    id: str                  # 8-char hex (secrets.token_hex(4))
    name: str
    url: str
    interval: int = 30
    enabled: bool = True
    created_at: str = ""              # ISO-8601, filled by caller
    prompts: list[str] = field(default_factory=list)  # ordered cai filter prompts (chain)


def _load_prompts(data: dict) -> list[str]:
    """Read prompts from YAML data; supports both 'prompts' list and legacy 'prompt' string."""
    if "prompts" in data:
        raw = data["prompts"]
        if isinstance(raw, list):
            return [str(p) for p in raw if p]
    if "prompt" in data and data["prompt"]:
        return [str(data["prompt"])]
    return []


def _path(watcher_id: str) -> Path:
    return WATCHERS_DIR / f"{watcher_id}.yaml"


def load_all() -> list[WatcherConfig]:
    """Read every *.yaml in WATCHERS_DIR and return parsed WatcherConfig objects."""
    WATCHERS_DIR.mkdir(parents=True, exist_ok=True)
    watchers: list[WatcherConfig] = []
    for f in sorted(WATCHERS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text())
            if not isinstance(data, dict):
                continue
            watchers.append(
                WatcherConfig(
                    id=str(data["id"]),
                    name=str(data.get("name", data["id"])),
                    url=str(data["url"]),
                    interval=int(data.get("interval", 30)),
                    enabled=bool(data.get("enabled", True)),
                    created_at=str(data.get("created_at", "")),
                    prompts=_load_prompts(data),
                )
            )
        except Exception:
            pass  # skip malformed files silently
    return watchers


def save(w: WatcherConfig) -> None:
    """Write WatcherConfig to WATCHERS_DIR/<id>.yaml."""
    WATCHERS_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "id": w.id,
        "name": w.name,
        "url": w.url,
        "interval": w.interval,
        "enabled": w.enabled,
        "created_at": w.created_at,
        "prompts": w.prompts,
    }
    _path(w.id).write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False))


def delete(watcher_id: str) -> bool:
    """Remove WATCHERS_DIR/<watcher_id>.yaml. Returns True if file existed."""
    p = _path(watcher_id)
    if p.exists():
        p.unlink()
        return True
    return False


def get(watcher_id: str) -> Optional[WatcherConfig]:
    """Load a single watcher by ID; return None if not found."""
    p = _path(watcher_id)
    if not p.exists():
        return None
    data = yaml.safe_load(p.read_text())
    if not isinstance(data, dict):
        return None
    return WatcherConfig(
        id=str(data["id"]),
        name=str(data.get("name", data["id"])),
        url=str(data["url"]),
        interval=int(data.get("interval", 30)),
        enabled=bool(data.get("enabled", True)),
        created_at=str(data.get("created_at", "")),
        prompts=_load_prompts(data),
    )
