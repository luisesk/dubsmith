"""Local shadow of Sonarr's series/episode catalog + on-disk poster cache.

Read-heavy UI (Library, Shows, Discover, show detail) traffic should never
need to touch live Sonarr. Periodic background sync refills the cache; the
worker pipeline still uses live Sonarr for write ops (rescan_series,
unmonitor_episode) since those have side-effects that need to be acknowledged.

Layout:
    /data/cache/sonarr.json        ← {ts, series, ep_files, episodes}
    /data/cache/images/{sid}-{kind}.jpg

Sync strategy:
    - Full library sync runs in background on startup + every 30 min.
    - Posters/fanarts are pre-fetched to disk during sync so the UI never
      has to do an expensive cold image fetch on first page load.
    - Sonarr webhook events (SeriesAdd / SeriesDelete) trigger targeted
      partial sync.

Concurrency:
    - sync() acquires _SYNC_LOCK so two scheduled syncs can't overlap.
    - Read methods are lock-free against an immutable snapshot dict; writes
      replace the whole dict atomically.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_SYNC_LOCK = threading.Lock()


class SonarrCache:
    def __init__(self, sonarr, data_dir: str | Path,
                 image_workers: int = 4):
        self.sonarr = sonarr
        self.data_dir = Path(data_dir)
        self.cache_path = self.data_dir / "cache" / "sonarr.json"
        self.images_dir = self.data_dir / "cache" / "images"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self._image_workers = image_workers
        self._snapshot: dict = self._load_disk() or {
            "ts": 0.0, "series": {}, "ep_files": {}, "episodes": {},
        }

    # ---------- public read API ----------
    def is_empty(self) -> bool:
        return not self._snapshot.get("series")

    def last_sync_ts(self) -> float:
        return float(self._snapshot.get("ts", 0))

    def all_series(self) -> list[dict]:
        return list(self._snapshot.get("series", {}).values())

    def series(self, sid: int) -> dict | None:
        return self._snapshot.get("series", {}).get(str(sid))

    def episode_files(self, sid: int) -> list[dict]:
        return self._snapshot.get("ep_files", {}).get(str(sid)) or []

    def episodes(self, sid: int) -> list[dict]:
        return self._snapshot.get("episodes", {}).get(str(sid)) or []

    def stats(self) -> dict:
        snap = self._snapshot
        return {
            "ts": snap.get("ts", 0),
            "series": len(snap.get("series", {})),
            "ep_files": sum(len(v) for v in snap.get("ep_files", {}).values()),
            "episodes": sum(len(v) for v in snap.get("episodes", {}).values()),
            "duration_s": snap.get("duration_s", 0),
        }

    # ---------- sync ----------
    def sync(self, prefetch_images: bool = True,
             max_series_workers: int = 4) -> dict:
        """Full Sonarr → local sync. Heavy: ~351 series × 2 calls + image fetches.

        Returns a stats dict. Safe to call concurrently — second caller
        no-ops if a sync is already running.
        """
        if not _SYNC_LOCK.acquire(blocking=False):
            log.info("sonarr cache sync already running; skipping")
            return self.stats()
        try:
            t0 = time.time()
            try:
                series_list = self.sonarr.all_series()
            except Exception as e:
                log.warning("sonarr cache sync: all_series failed: %s", e)
                return self.stats()
            new = {
                "ts": time.time(),
                "series": {},
                "ep_files": {},
                "episodes": {},
            }
            errors = []

            def _fetch_one(s):
                sid = s["id"]
                try:
                    efs = self.sonarr.episode_files(sid)
                except Exception as e:
                    errors.append(("ep_files", sid, str(e)[:100]))
                    efs = []
                try:
                    eps = self.sonarr.episodes(sid)
                except Exception as e:
                    errors.append(("episodes", sid, str(e)[:100]))
                    eps = []
                return sid, efs, eps

            # Fetch per-series payloads in parallel — bounded so we don't
            # hammer Sonarr.
            with ThreadPoolExecutor(max_workers=max(1, int(max_series_workers)),
                                    thread_name_prefix="sonarr-cache") as ex:
                futures = [ex.submit(_fetch_one, s) for s in series_list]
                for s, fut in zip(series_list, futures):
                    sid = s["id"]
                    new["series"][str(sid)] = s
                    try:
                        rsid, efs, eps = fut.result()
                        new["ep_files"][str(rsid)] = efs
                        new["episodes"][str(rsid)] = eps
                    except Exception as e:
                        errors.append(("future", sid, str(e)[:100]))
            new["duration_s"] = round(time.time() - t0, 1)
            new["errors"] = len(errors)
            self._replace(new)
            log.info("sonarr cache: synced %d series in %.1fs (%d errors)",
                     len(new["series"]), new["duration_s"], len(errors))

            # Pre-warm poster cache. Only for series whose poster we don't yet
            # have on disk.
            if prefetch_images:
                self._prefetch_images()

            return self.stats()
        finally:
            _SYNC_LOCK.release()

    def sync_in_background(self, **kwargs) -> threading.Thread:
        t = threading.Thread(
            target=self.sync, kwargs=kwargs, daemon=True,
            name="sonarr-cache-sync",
        )
        t.start()
        return t

    # ---------- internals ----------
    def _load_disk(self) -> dict | None:
        if not self.cache_path.exists():
            return None
        try:
            with open(self.cache_path) as f:
                return json.load(f)
        except Exception as e:
            log.warning("sonarr cache: read failed: %s", e)
            return None

    def _replace(self, snapshot: dict) -> None:
        self._snapshot = snapshot
        try:
            tmp = self.cache_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(snapshot, f)
            tmp.replace(self.cache_path)
        except OSError as e:
            log.warning("sonarr cache: write failed: %s", e)

    def _prefetch_images(self) -> int:
        """Pre-fetch posters that aren't yet on disk. Returns count fetched."""
        sonarr_url = getattr(self.sonarr, "base_url", "") or ""
        sonarr_key = ""
        try:
            sonarr_key = self.sonarr.client.headers.get("X-Api-Key", "") or ""
        except Exception:
            pass
        if not sonarr_url or not sonarr_key:
            return 0

        timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=2.0)
        client = httpx.Client(timeout=timeout, follow_redirects=True,
                              limits=httpx.Limits(max_connections=8,
                                                  max_keepalive_connections=4))

        def _fetch(sid_str: str, series: dict, kind: str) -> bool:
            cache_path = self.images_dir / f"{sid_str}-{kind}.jpg"
            if cache_path.exists():
                return False
            url = None
            for img in series.get("images", []) or []:
                if img.get("coverType") == kind:
                    url = img.get("remoteUrl") or img.get("url")
                    break
            if not url:
                return False
            try:
                if url.startswith("/"):
                    r = client.get(sonarr_url.rstrip("/") + url,
                                   headers={"X-Api-Key": sonarr_key})
                else:
                    r = client.get(url)
                if r.status_code == 200 and r.content:
                    cache_path.write_bytes(r.content)
                    return True
            except Exception as e:
                log.debug("image prefetch %s/%s: %s", sid_str, kind, e)
            return False

        n = 0
        try:
            with ThreadPoolExecutor(max_workers=self._image_workers,
                                    thread_name_prefix="img-prewarm") as ex:
                futs = []
                for sid_str, s in self._snapshot.get("series", {}).items():
                    futs.append(ex.submit(_fetch, sid_str, s, "poster"))
                    futs.append(ex.submit(_fetch, sid_str, s, "fanart"))
                for f in as_completed(futs):
                    try:
                        if f.result():
                            n += 1
                    except Exception:
                        pass
        finally:
            client.close()
        if n:
            log.info("sonarr cache: pre-fetched %d images", n)
        return n
