"""Periodic check: episodes already dubbed at 1080p that now have a 4K release
available on Prowlarr. Re-monitors the episode in Sonarr and triggers a search,
which lets Sonarr grab the 4K release. Webhook handler then re-enqueues the mux
job so the 4K file gets the PT-BR dub track on top.

Also retries failed jobs whose error indicates the dub source was not yet
available (CR hadn't released the dub at first attempt). This is a separate
sweep from the regular failed-job retry which has a max_attempts cap.

Airdate filter: only episodes aired AFTER `upgrade_watcher.from_date` (in
settings.yml) are considered. Default: rollout date of 4K-upgrade scheme — keeps
the watcher away from the existing 800+ legacy 1080p library so we don't hammer
Nyaa or Crunchyroll trying to upgrade decade-old shows that won't ever get 4K.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time

from . import probe
from .lang import lang_matches
from .prowlarr import Prowlarr
from .queue import Queue
from .shows import ShowsStore
from .sonarr import Sonarr

log = logging.getLogger(__name__)

# Sonarr quality IDs for the WEB 2160p group + bluray UHD. Anything in this set
# means "already at or above 2160p" — no upgrade needed.
_QUALITY_2160P_IDS = {16, 19, 21, 1003}  # HDTV-2160p, BR-2160p, BR-2160p Remux, WEB 2160p group


def _file_is_2160p(ep_file: dict) -> bool:
    q = ((ep_file or {}).get("quality") or {}).get("quality") or {}
    qid = q.get("id")
    if qid in _QUALITY_2160P_IDS:
        return True
    name = (q.get("name") or "").lower()
    return "2160" in name or "uhd" in name or "4k" in name


def _file_has_target_dub(local_path: str, target_lang: str) -> bool:
    try:
        langs = probe.audio_languages(local_path)
    except Exception:
        return False
    return any(lang_matches(l, target_lang) for l in langs)


MAX_TRIGGERS_PER_SWEEP = 10
MAX_LOOKUPS_PER_SWEEP = 80
DEFAULT_FROM_DATE = "2026-05-09"  # rollout — earlier eps are out of scope


def _parse_aired(s: str | None) -> _dt.date | None:
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def check_and_trigger_upgrades(cfg: dict, queue: Queue, shows: ShowsStore,
                                sonarr: Sonarr, prowlarr: Prowlarr,
                                path_remap: tuple[str, str],
                                from_date: str | None = None,
                                series_id_filter: int | None = None,
                                episode_id_filter: int | None = None) -> dict:
    """Walk dubsmith-tracked shows; for each "done" episode at <2160p with
    target dub track present (and aired AFTER from_date), look for a 2160p
    release on Prowlarr; if found, re-monitor + EpisodeSearch in Sonarr.

    Returns a summary dict.
    """
    if not prowlarr.is_configured():
        log.info("upgrade_watcher: prowlarr not configured, skipping")
        return {"skipped": "prowlarr not configured"}

    sonarr_prefix, container_prefix = path_remap
    default_lang = cfg["target_language"]["audio"]
    cutoff = _parse_aired((from_date or DEFAULT_FROM_DATE) + "T00:00:00Z")
    tracked = shows.load()
    triggered: list[dict] = []
    checked = 0
    skipped_old = 0
    lookups = 0

    for sid_raw, show in tracked.items():
        if not show.get("enabled", True):
            continue
        # Per-show opt-out for the upgrade watcher (default on).
        if show.get("upgrade_4k") is False:
            continue
        sid = int(sid_raw)
        if series_id_filter is not None and sid != series_id_filter:
            continue
        target_lang = show.get("target_audio") or default_lang

        try:
            series = sonarr.series(sid)
            files = sonarr.episode_files(sid)
            eps = sonarr.episodes(sid)
        except Exception as e:
            log.warning("upgrade_watcher: sonarr fetch failed for sid=%d: %s", sid, e)
            continue
        primary_title = series.get("title") or show.get("name") or ""
        title_candidates = [primary_title]
        for alt in (series.get("alternateTitles") or []):
            t = alt.get("title")
            if t and t not in title_candidates:
                title_candidates.append(t)
        # ep_file_id -> file
        files_by_id = {f["id"]: f for f in files}

        for ep in eps:
            if episode_id_filter is not None and ep["id"] != episode_id_filter:
                continue
            if not ep.get("hasFile"):
                continue
            # Airdate gate — skip legacy library, only watch new eps.
            aired = _parse_aired(ep.get("airDateUtc") or ep.get("airDate"))
            if cutoff and (aired is None or aired < cutoff):
                skipped_old += 1
                continue
            fid = ep.get("episodeFileId") or 0
            f = files_by_id.get(fid)
            if not f:
                continue
            if _file_is_2160p(f):
                continue  # already 4K
            # Only candidates: file has the target dub (we already shipped 1080p+dub).
            host_path = f.get("path", "")
            local_path = host_path.replace(sonarr_prefix, container_prefix, 1)
            if not _file_has_target_dub(local_path, target_lang):
                # 1080p without dub yet — let the normal scanner enqueue mux.
                continue
            checked += 1
            if lookups >= MAX_LOOKUPS_PER_SWEEP:
                continue
            season = ep["seasonNumber"]
            episode_no = ep["episodeNumber"]
            try:
                rel = prowlarr.has_2160p_release(title_candidates, season, episode_no)
            except Exception as e:
                log.warning("prowlarr lookup failed for %s S%dE%d: %s",
                            primary_title, season, episode_no, e)
                continue
            lookups += 1
            if not rel:
                continue
            try:
                sonarr.set_episode_monitored(ep["id"], True)
                sonarr.episode_search([ep["id"]])
                triggered.append({
                    "series_id": sid, "title": primary_title,
                    "season": season, "episode": episode_no,
                    "release": rel.get("title"),
                    "seeders": rel.get("seeders"),
                })
                log.info("upgrade triggered: %s S%dE%d → %s (%s seeders)",
                         primary_title, season, episode_no, rel.get("title"),
                         rel.get("seeders"))
            except Exception as e:
                log.warning("trigger upgrade failed for sid=%d ep=%d: %s",
                            sid, ep["id"], e)
            if len(triggered) >= MAX_TRIGGERS_PER_SWEEP:
                break
        if len(triggered) >= MAX_TRIGGERS_PER_SWEEP:
            break
    return {"checked_eps_at_1080p": checked,
            "skipped_old": skipped_old,
            "lookups": lookups,
            "triggered": triggered,
            "from_date": (from_date or DEFAULT_FROM_DATE)}


def retry_dub_unavailable_failures(queue: Queue) -> int:
    """Reset 'failed' jobs whose error suggests CR hadn't released dub yet.
    No attempt cap — retries indefinitely until CR has the dub or operator
    cancels.
    """
    n = _reset_failed_matching(queue, ("dub", "version", "audio", "no episodes"))
    if n:
        log.info("retry sweep (dub unavailable): %d job(s) reset to pending", n)
    return n


def _reset_failed_matching(queue: Queue, patterns: tuple[str, ...]) -> int:
    """Reset failed jobs whose last_error contains any of the patterns."""
    import sqlite3
    total = 0
    now = time.time()
    with sqlite3.connect(queue.db_path, isolation_level=None, timeout=30) as c:
        for p in patterns:
            cur = c.execute(
                "UPDATE jobs SET state='pending', updated_at=? "
                "WHERE state='failed' AND last_error LIKE ?",
                (now, f"%{p}%"),
            )
            total += cur.rowcount
    return total
