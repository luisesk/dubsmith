"""Prowlarr search client — checks indexers for releases without grabbing them.

Used by upgrade_watcher to decide if a 4K release exists for an episode before
re-monitoring it in Sonarr (which would otherwise leave the episode in a
"wanted" state forever).
"""
from __future__ import annotations

import logging
import re
import time
from threading import Lock

import httpx

log = logging.getLogger(__name__)

# Tags we consider valid 2160p sources (excluding scene cam/cam-rip nonsense).
_VALID_2160P_RE = re.compile(
    r"\b(?:2160p|4k|uhd)\b", re.IGNORECASE,
)

# In-memory cache for has_2160p_release results — Nyaa indexers are flaky and
# repeated queries waste cycles. TTL 6h.
_HAS_4K_CACHE: dict[tuple, tuple[float, dict | None]] = {}
_HAS_4K_TTL = 6 * 3600.0
_HAS_4K_LOCK = Lock()


class Prowlarr:
    def __init__(self, url: str, api_key: str):
        self.base_url = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"X-Api-Key": self.api_key},
            # Nyaa via Flaresolverr can take 60-90s on cold cache, so be patient.
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=2.0),
        )

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def search(self, query: str, categories: list[int] | None = None,
               limit: int = 50) -> list[dict]:
        """Raw Prowlarr search. Returns list of release dicts."""
        if not self.is_configured():
            return []
        # Prowlarr expects categories as a repeated query parameter,
        # not a comma-separated value. Build via list of tuples so httpx
        # serializes correctly: ?categories=5040&categories=5045&...
        params: list[tuple[str, str]] = [("query", query), ("limit", str(limit))]
        for c in categories or []:
            params.append(("categories", str(c)))
        try:
            r = self.client.get("/api/v1/search", params=params)
            r.raise_for_status()
            return r.json() or []
        except Exception as e:
            log.warning("prowlarr search %r failed: %s", query, e)
            return []

    def has_2160p_release(self, titles: list[str] | str, season: int, episode: int,
                          min_seeders: int = 1) -> dict | None:
        """Check if a 2160p release exists for any of the given titles.

        `titles` is a list of search candidates (e.g. English + romanized JP);
        the first non-empty result wins. Returns the best-matching (most-seeded)
        release dict, or None. Result cached 6h on (titles[0], season, episode).
        """
        if isinstance(titles, str):
            titles = [titles]
        if not titles:
            return None
        key = (tuple(t.lower() for t in titles), int(season), int(episode))
        now = time.time()
        with _HAS_4K_LOCK:
            v = _HAS_4K_CACHE.get(key)
            if v and (now - v[0]) < _HAS_4K_TTL:
                return v[1]
        # Anime + TV HD/UHD categories. Prowlarr cats: 5040 HD, 5045 UHD, 5070 Anime.
        cats = [5040, 5045, 5070]
        candidates: list[dict] = []
        for show in titles:
            queries = [
                f"{show} S{season:02d}E{episode:02d} 2160p",
                f"{show} {episode:02d} 2160p",
            ]
            for q in queries:
                for r in self.search(q, categories=cats, limit=50):
                    title = r.get("title") or ""
                    if not _VALID_2160P_RE.search(title):
                        continue
                    # Sanity-match the episode number in the title — the indexer
                    # may return tangentially-related releases (different cour /
                    # different season). Look for SxxEyy or " - yy " (anime
                    # convention).
                    if not _episode_in_title(title, season, episode):
                        continue
                    seeders = int(r.get("seeders") or 0)
                    if seeders < min_seeders:
                        continue
                    candidates.append(r)
                time.sleep(0.2)
                if candidates:
                    break
            if candidates:
                break
        result = None
        if candidates:
            candidates.sort(key=lambda x: int(x.get("seeders") or 0), reverse=True)
            result = candidates[0]
        with _HAS_4K_LOCK:
            _HAS_4K_CACHE[key] = (now, result)
        return result


def _episode_in_title(title: str, season: int, episode: int) -> bool:
    """Heuristic: title mentions the right episode."""
    se = re.search(r"S(\d{1,2})E(\d{1,3})", title, re.IGNORECASE)
    if se:
        return int(se.group(1)) == season and int(se.group(2)) == episode
    # Anime convention: "Title - 05" / "Title 05" / "Title.05"
    # but only if season is 1 — otherwise "05" is ambiguous between cours.
    if season == 1:
        if re.search(rf"\b{episode:02d}\b", title):
            return True
    return False
