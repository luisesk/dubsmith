import time
from threading import Lock

import httpx


# Process-wide TTL cache shared across Sonarr instances.
# Key: (base_url, method, args). Avoids re-hitting Sonarr per page render.
_CACHE: dict = {}
_CACHE_LOCK = Lock()
_DEFAULT_TTL = 300.0  # 5 min — Sonarr data changes rarely; we get
                       # invalidations on rescan_series anyway.


def _cache_get(key):
    now = time.time()
    with _CACHE_LOCK:
        v = _CACHE.get(key)
        if v and now - v[0] < v[1]:
            return v[2]
    return None


def _cache_put(key, val, ttl=_DEFAULT_TTL):
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), ttl, val)


def cache_invalidate(prefix: str | None = None):
    with _CACHE_LOCK:
        if prefix is None:
            _CACHE.clear()
        else:
            for k in [k for k in _CACHE if isinstance(k, tuple) and prefix in str(k)]:
                _CACHE.pop(k, None)


class Sonarr:
    def __init__(self, url: str, api_key: str):
        self.base_url = url.rstrip("/")
        # Tight timeouts so a stale connection or slow remote fails fast.
        # connect=5s, read=10s, write=10s, pool=2s. Long-lived keepalive
        # connections are recycled if idle > 30s to dodge stale-conn hangs.
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"X-Api-Key": api_key},
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=2.0),
            limits=httpx.Limits(max_keepalive_connections=4,
                                max_connections=8,
                                keepalive_expiry=30.0),
        )

    def series(self, series_id: int) -> dict:
        key = (self.base_url, "series", series_id)
        v = _cache_get(key)
        if v is not None:
            return v
        v = self.client.get(f"/api/v3/series/{series_id}").raise_for_status().json()
        _cache_put(key, v)
        return v

    def episode_files(self, series_id: int) -> list[dict]:
        key = (self.base_url, "ep_files", series_id)
        v = _cache_get(key)
        if v is not None:
            return v
        r = self.client.get("/api/v3/episodefile", params={"seriesId": series_id})
        r.raise_for_status()
        v = r.json()
        _cache_put(key, v)
        return v

    def episodes(self, series_id: int) -> list[dict]:
        key = (self.base_url, "episodes", series_id)
        v = _cache_get(key)
        if v is not None:
            return v
        r = self.client.get("/api/v3/episode", params={"seriesId": series_id})
        r.raise_for_status()
        v = r.json()
        _cache_put(key, v)
        return v

    def rescan_series(self, series_id: int) -> dict:
        r = self.client.post(
            "/api/v3/command",
            json={"name": "RescanSeries", "seriesId": series_id},
        )
        r.raise_for_status()
        # Sonarr will pick up new files; invalidate so next read sees them
        cache_invalidate(prefix=f"{series_id}")
        return r.json()

    def unmonitor_episode(self, episode_id: int) -> dict:
        r = self.client.put(
            f"/api/v3/episode/{episode_id}",
            json={"monitored": False},
        )
        r.raise_for_status()
        return r.json()

    def find_episode_id(self, series_id: int, season: int, episode: int) -> int | None:
        for e in self.episodes(series_id):
            if e.get("seasonNumber") == season and e.get("episodeNumber") == episode:
                return e.get("id")
        return None

    def all_series(self) -> list[dict]:
        key = (self.base_url, "all_series")
        v = _cache_get(key)
        if v is not None:
            return v
        r = self.client.get("/api/v3/series")
        r.raise_for_status()
        v = r.json()
        # Full library — biggest payload, hottest cache. 10 min.
        _cache_put(key, v, ttl=600.0)
        return v

    def poster_url(self, series_id: int) -> str | None:
        try:
            s = self.series(series_id)
        except Exception:
            return None
        for img in s.get("images", []) or []:
            if img.get("coverType") == "poster":
                # Sonarr returns local-relative or remote
                u = img.get("remoteUrl") or img.get("url") or ""
                if u.startswith("/"):
                    u = self.client.base_url._uri_reference.copy_with(path=u).unicode_string()
                return u
        return None
