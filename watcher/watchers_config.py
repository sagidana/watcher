"""
YAML CRUD for per-watcher config files.

Each watcher lives in its own file:
    ~/.config/watcher/watchers/<8-hex-id>.yaml
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import logging
import yaml

log = logging.getLogger("watcher.engine")

WATCHERS_DIR = Path("~/.config/watcher/watchers").expanduser()


AVAILABLE_MODELS: list[str] = [
    "google/gemini-3-flash-preview",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-opus-4.6",
    "google/gemini-pro-latest",
    "openai/gpt-5.5",
    "google/gemma-4-31b-it",
]

DEFAULT_MODEL: str = AVAILABLE_MODELS[0]

AVAILABLE_TOOLS: list[str] = ["fetch_url"]

DEFAULT_TOOLS: list[str] = ["fetch_url"]


@dataclass
class WatcherConfig:
    id: str                  # 8-char hex (secrets.token_hex(4))
    name: str
    interval: int = 30
    enabled: bool = True
    created_at: str = ""              # ISO-8601, filled by caller
    prompts: list[str] = field(default_factory=list)  # ordered cai prompts (chain)
    model: str = DEFAULT_MODEL
    system_prompt: str = ""           # empty -> engine substitutes default at run time
    tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))


def _load_prompts(data: dict) -> list[str]:
    """Read prompts from YAML data; supports both 'prompts' list and legacy 'prompt' string."""
    if "prompts" in data:
        raw = data["prompts"]
        if isinstance(raw, list):
            return [str(p) for p in raw if p]
    if "prompt" in data and data["prompt"]:
        return [str(data["prompt"])]
    return []


def _load_tools(data: dict) -> list[str]:
    raw = data.get("tools")
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    if isinstance(raw, str) and raw.strip():
        return [t.strip() for t in raw.split(",") if t.strip()]
    return list(DEFAULT_TOOLS)


def _from_dict(data: dict) -> WatcherConfig:
    return WatcherConfig(
        id=str(data["id"]),
        name=str(data.get("name", data["id"])),
        interval=int(data.get("interval", 30)),
        enabled=bool(data.get("enabled", True)),
        created_at=str(data.get("created_at", "")),
        prompts=_load_prompts(data),
        model=str(data.get("model") or DEFAULT_MODEL),
        system_prompt=str(data.get("system_prompt", "")),
        tools=_load_tools(data),
    )


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
            watchers.append(_from_dict(data))
        except Exception as e:
            log.warning(f"failed to load config {f}: {e=}")

    return watchers


def save(w: WatcherConfig) -> None:
    """Write WatcherConfig to WATCHERS_DIR/<id>.yaml."""
    WATCHERS_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "id": w.id,
        "name": w.name,
        "interval": w.interval,
        "enabled": w.enabled,
        "created_at": w.created_at,
        "model": w.model,
        "system_prompt": w.system_prompt,
        "tools": w.tools,
        "prompts": w.prompts,
    }
    _path(w.id).write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False))


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
    return _from_dict(data)
