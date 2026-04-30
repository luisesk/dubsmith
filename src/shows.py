"""Mutable per-show config persisted to data/shows.yml.

Shape:
shows:
  37:
    name: "Campfire Cooking..."
    enabled: true
    cr_seasons:
      "1": GR49C7EPD
      "2": G649C71Q2
    season_offset:
      "2": 24
    target_audio: por      # ISO 639-2 code; falls back to global
    cr_dub_lang: por       # mdnx --dubLang flag value
"""
from __future__ import annotations

import threading
from pathlib import Path

import yaml

_LOCK = threading.Lock()


class ShowsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("shows: {}\n")

    def load(self) -> dict:
        with _LOCK:
            with open(self.path) as f:
                data = yaml.safe_load(f) or {}
            return data.get("shows") or {}

    def save(self, shows: dict) -> None:
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                yaml.safe_dump({"shows": shows}, f, sort_keys=True)
            tmp.replace(self.path)

    def get(self, series_id: int | str) -> dict | None:
        shows = self.load()
        return shows.get(int(series_id)) or shows.get(str(series_id))

    def upsert(self, series_id: int, **fields) -> dict:
        shows = self.load()
        key = int(series_id)
        existing = shows.get(key) or shows.get(str(key)) or {}
        # drop None values so we don't overwrite saved fields with null
        for k, v in list(fields.items()):
            if v is None:
                fields.pop(k)
        existing.update(fields)
        existing.setdefault("enabled", True)
        # normalize sub-dict keys to str for YAML round-trip
        cs = existing.get("cr_seasons") or {}
        existing["cr_seasons"] = {str(k): str(v) for k, v in cs.items()}
        so = existing.get("season_offset") or {}
        existing["season_offset"] = {str(k): int(v) for k, v in so.items()}
        # purge legacy str-int dup
        shows.pop(str(key), None)
        shows[key] = existing
        self.save(shows)
        return existing

    def delete(self, series_id: int) -> bool:
        shows = self.load()
        removed = bool(shows.pop(int(series_id), None) or shows.pop(str(series_id), None))
        if removed:
            self.save(shows)
        return removed

    def set_enabled(self, series_id: int, enabled: bool) -> None:
        self.upsert(series_id, enabled=enabled)
