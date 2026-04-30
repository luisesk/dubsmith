"""Streaming source registry: which services Dubsmith can pull dubs from."""
from __future__ import annotations

import threading
from pathlib import Path

import yaml

_LOCK = threading.Lock()

DEFAULT_SOURCES = {
    "crunchyroll": {
        "name": "Crunchyroll",
        "service": "crunchy",
        "languages": ["JA", "EN", "ES", "PT", "DE", "FR"],
        "connected": False,
        "user": None,
    },
    "hidive": {
        "name": "Hidive",
        "service": "hidive",
        "languages": ["JA", "EN"],
        "connected": False,
        "user": None,
    },
    "adn": {
        "name": "AnimationDigitalNetwork",
        "service": "ADN",
        "languages": ["JA", "FR"],
        "connected": False,
        "user": None,
    },
}


class SourcesStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(DEFAULT_SOURCES)

    def load(self) -> dict:
        with _LOCK:
            with open(self.path) as f:
                return yaml.safe_load(f) or {}

    def save(self, data: dict) -> None:
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                yaml.safe_dump(data, f, sort_keys=False)
            tmp.replace(self.path)

    def set_connected(self, key: str, user: str) -> None:
        d = self.load()
        if key in d:
            d[key]["connected"] = True
            d[key]["user"] = user
            self.save(d)

    def disconnect(self, key: str) -> None:
        d = self.load()
        if key in d:
            d[key]["connected"] = False
            d[key]["user"] = None
            self.save(d)
