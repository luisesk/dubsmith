"""User settings persisted to data/settings.yml. Distinct from immutable config.yml."""
from __future__ import annotations

import threading
from pathlib import Path

import yaml

_LOCK = threading.Lock()

DEFAULTS = {
    "sonarr": {
        "url": "",
        "api_key": "",
        "unmonitor_after_mux": False,
        "rescan_after_mux": True,
        "webhook_secret": "",
    },
    "library_server": {
        "type": "plex",            # plex | jellyfin
        "url": "",
        "token": "",
        "section_id": "",
    },
    "sync": {
        "algorithm": "cross-corr",     # cross-corr | manual | silence-detect
        "confidence_threshold": 80,    # %, scaled 0-100
        "auto_mux": True,
        "keep_original_audio": True,
    },
    "library": {
        "show_posters": True,
    },
    "concurrency": {
        "downloads": 2,
        "muxes": 1,
        "sync": 2,
    },
    "ui": {
        "theme": "dark",
        "density": "comfortable",
    },
}


class SettingsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(DEFAULTS)

    def load(self) -> dict:
        with _LOCK:
            with open(self.path) as f:
                cur = yaml.safe_load(f) or {}
            # merge defaults for any new keys
            return _deepmerge(DEFAULTS, cur)

    def save(self, data: dict) -> None:
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                yaml.safe_dump(data, f, sort_keys=False)
            tmp.replace(self.path)

    def update(self, section: str, **fields) -> dict:
        d = self.load()
        d.setdefault(section, {}).update(fields)
        self.save(d)
        return d


def _deepmerge(default: dict, override: dict) -> dict:
    out = dict(default)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deepmerge(out[k], v)
        else:
            out[k] = v
    return out
