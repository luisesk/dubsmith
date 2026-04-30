"""Library server integration. Supports Plex and Jellyfin."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class LibraryServer:
    """url + token + type ('plex'|'jellyfin'). type determines API shape."""

    def __init__(self, kind: str, url: str, token: str, library_section_id: str | int | None = None):
        self.kind = (kind or "plex").lower()
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.section_id = library_section_id

    def refresh_section(self) -> bool:
        if not self.url or not self.token:
            return False
        try:
            if self.kind == "plex":
                params = {"X-Plex-Token": self.token}
                if self.section_id is not None:
                    r = httpx.get(f"{self.url}/library/sections/{self.section_id}/refresh",
                                  params=params, timeout=10)
                else:
                    r = httpx.get(f"{self.url}/library/sections/all/refresh",
                                  params=params, timeout=10)
                return r.status_code in (200, 204)
            elif self.kind == "jellyfin":
                # Jellyfin: POST /Library/Refresh with X-Emby-Token header
                r = httpx.post(f"{self.url}/Library/Refresh",
                               headers={"X-Emby-Token": self.token,
                                        "Authorization": f'MediaBrowser Token="{self.token}"'},
                               timeout=10)
                return r.status_code in (200, 204)
        except Exception as e:
            log.warning("library refresh failed (%s): %s", self.kind, e)
        return False

    def test(self) -> dict:
        try:
            if self.kind == "plex":
                r = httpx.get(f"{self.url}/identity",
                              params={"X-Plex-Token": self.token}, timeout=10)
                return {"ok": r.status_code == 200, "status": r.status_code}
            elif self.kind == "jellyfin":
                r = httpx.get(f"{self.url}/System/Info/Public", timeout=10)
                return {"ok": r.status_code == 200, "version": r.json().get("Version") if r.status_code == 200 else None}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "unknown server type"}
