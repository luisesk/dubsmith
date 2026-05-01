"""Library-wide audio coverage scanner.

Walks every Sonarr series, ffprobes one sample episode per series, classifies
each show by whether it has the target audio language. Results persist in
data/discover.json so the UI returns instantly; a background refresh updates them.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
import time
from pathlib import Path

from . import downloader, probe
from .lang import lang_matches

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()  # serializes scans
_RUNNING = {"value": False}


def cache_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "discover.json"


def load(data_dir: Path | str) -> dict:
    p = cache_path(data_dir)
    if not p.exists():
        return {"ts": 0, "running": is_running(), "rows": []}
    try:
        with _LOCK:
            with open(p) as f:
                d = json.load(f)
        d["running"] = is_running()
        return d
    except Exception as e:
        log.warning("discover cache read failed: %s", e)
        return {"ts": 0, "running": is_running(), "rows": []}


def is_running() -> bool:
    return _RUNNING["value"]


def _save(data_dir: Path | str, payload: dict) -> None:
    p = cache_path(data_dir)
    with _LOCK:
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f)
        tmp.replace(p)


def _probe_source_dubs(show_cfg: dict, target_lang: str) -> dict:
    """For a tracked show, probe each mapped season's source for the target dub.

    Returns: {available, with_lang: [season,...], without_lang: [season,...],
              errors: [{season, error},...], source: 'crunchyroll'|'hidive'|'adn',
              matched_title: None, matched_id: None}
    """
    cr_seasons = (show_cfg or {}).get("cr_seasons") or {}
    source = (show_cfg or {}).get("source", "crunchyroll")
    out = {
        "available": False,
        "with_lang": [], "without_lang": [], "errors": [],
        "source": source, "matched_title": None, "matched_id": None,
    }
    for season, cr_id in cr_seasons.items():
        try:
            langs = downloader.probe_season_dubs(cr_id, source=source)
        except Exception as e:
            out["errors"].append({"season": season, "error": str(e)[:200]})
            continue
        if any(lang_matches(l, target_lang) for l in langs):
            out["with_lang"].append(str(season))
        else:
            out["without_lang"].append(str(season))
    out["available"] = bool(out["with_lang"])
    return out


def _search_untracked(title: str, target_lang: str,
                      source: str = "crunchyroll") -> dict:
    """For an untracked show, search the source by title and report dub availability
    of the top match. Same shape as _probe_source_dubs, plus matched_* fields so
    the user can verify (and the setup wizard can deep-link).
    """
    out = {
        "available": False,
        "with_lang": [], "without_lang": [], "errors": [],
        "source": source, "matched_title": None, "matched_id": None,
    }
    if not title:
        return out
    try:
        results = downloader.search_show(title, limit=1, source=source)
    except Exception as e:
        out["errors"].append({"season": "?", "error": str(e)[:200]})
        return out
    if not results:
        return out
    top = results[0]
    out["matched_title"] = top.get("title")
    out["matched_id"] = top.get("show_id")
    for season in top.get("seasons") or []:
        cr_id = season.get("cr_season_id")
        langs = season.get("dub_langs") or []
        if any(lang_matches(l, target_lang) for l in langs):
            out["with_lang"].append(cr_id)
        else:
            out["without_lang"].append(cr_id)
    out["available"] = bool(out["with_lang"])
    return out


def _scan_one(series: dict, sonarr, target_lang: str,
              path_remap: tuple[str, str], tracked_ids: set[int],
              show_cfg: dict | None = None) -> dict:
    sid = series["id"]
    row = {
        "series_id": sid,
        "title": series.get("title", ""),
        "year": series.get("year"),
        "monitored": series.get("monitored", False),
        "total_eps": (series.get("statistics") or {}).get("episodeCount", 0),
        "tracked": sid in tracked_ids,
        "first_ep_langs": [],
        "first_ep_path": None,
        "has_target_in_first_ep": False,
        "probe_error": None,
        # source (CR/Hidive/ADN) availability
        "source_available": None,         # bool | None (None = not probed)
        "source_kind": None,              # 'crunchyroll'/'hidive'/'adn'
        "source_seasons_with_lang": [],
        "source_seasons_without_lang": [],
        "source_probe_errors": [],
        "source_matched_title": None,     # untracked rows: source-side matched show title
        "source_matched_id": None,        # untracked rows: source-side show id (for wizard)
    }
    try:
        files = sonarr.episode_files(sid)
    except Exception as e:
        row["probe_error"] = f"sonarr: {e}"
        return row
    if not files:
        row["probe_error"] = "no episode files"
        return row
    first = files[0]
    host_path = first.get("path", "") or ""
    local_path = host_path.replace(path_remap[0], path_remap[1], 1)
    row["first_ep_path"] = local_path
    if not Path(local_path).exists():
        row["probe_error"] = "file not found in container"
        return row
    try:
        langs = probe.audio_languages(local_path)
    except Exception as e:
        row["probe_error"] = str(e)[:200]
        return row
    row["first_ep_langs"] = langs
    row["has_target_in_first_ep"] = any(lang_matches(l, target_lang) for l in langs)

    # If library is missing the dub, probe the source.
    #   - Tracked: walk cr_seasons via mdnx -s <id>.
    #   - Untracked: mdnx --search <title>; take top match's first season's dubs.
    if not row["has_target_in_first_ep"]:
        if row["tracked"] and show_cfg:
            cr = _probe_source_dubs(show_cfg, target_lang)
        else:
            cr = _search_untracked(row["title"], target_lang)
        row["source_available"] = cr["available"]
        row["source_kind"] = cr["source"]
        row["source_seasons_with_lang"] = cr["with_lang"]
        row["source_seasons_without_lang"] = cr["without_lang"]
        row["source_probe_errors"] = cr["errors"]
        row["source_matched_title"] = cr.get("matched_title")
        row["source_matched_id"] = cr.get("matched_id")
    return row


def scan_all(sonarr, target_lang: str, path_remap: tuple[str, str],
             tracked_ids: set[int], data_dir: Path | str,
             tracked_cfg: dict | None = None,
             max_workers: int = 2,
             save_every: int = 25,
             pause_check=None) -> dict:
    """Run a full library scan and persist results.

    Probes ffprobe + source availability in parallel via ThreadPoolExecutor.
    `max_workers` bounds simultaneous mdnx subprocesses (default 4 — friendly
    to CR rate limits and leaves room for the worker pipeline).

    Saves a partial payload every `save_every` rows so an interrupted scan
    keeps progress.

    tracked_cfg: dict[series_id_int_or_str, show_yaml_entry]. Used to probe
    sources for dub availability on tracked rows.
    """
    if _RUNNING["value"]:
        log.info("discover.scan_all skipped: already running")
        return load(data_dir)
    with _RUN_LOCK:
        _RUNNING["value"] = True
        started = time.time()
        try:
            try:
                series_list = sonarr.all_series()
            except Exception as e:
                log.warning("discover: sonarr.all_series failed: %s", e)
                series_list = []
            tracked_cfg = tracked_cfg or {}

            def _task(s):
                sid = s["id"]
                cfg = tracked_cfg.get(sid) or tracked_cfg.get(str(sid))
                return _scan_one(s, sonarr, target_lang, path_remap,
                                 tracked_ids, show_cfg=cfg)

            rows: list[dict] = []
            done_count = 0
            total = len(series_list)
            workers = max(1, int(max_workers))
            log.info("discover: starting scan of %d series with %d workers", total, workers)

            def _wrapped(s):
                # Yield to the worker pipeline if it's actively chewing on a job:
                # NFS bandwidth is shared, and 4 parallel mdnx probes alongside
                # an active download spike load average to 35+. Wait until idle.
                if pause_check is not None:
                    backoff = 0
                    while pause_check():
                        if backoff < 30:
                            backoff += 5
                        time.sleep(backoff)
                return _task(s)

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                                      thread_name_prefix="disc") as ex:
                futures = {ex.submit(_wrapped, s): s for s in series_list}
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        row = fut.result()
                    except Exception as e:
                        s = futures[fut]
                        log.warning("discover: %s scan failed: %s", s.get("title"), e)
                        row = {
                            "series_id": s.get("id"),
                            "title": s.get("title", ""),
                            "tracked": s.get("id") in tracked_ids,
                            "probe_error": f"scan crashed: {e}",
                            "first_ep_langs": [], "has_target_in_first_ep": False,
                            "source_available": None,
                        }
                    rows.append(row)
                    done_count += 1
                    if done_count % save_every == 0 or done_count == total:
                        _save(data_dir, {
                            "ts": time.time(),
                            "started": started,
                            "duration_s": round(time.time() - started, 1),
                            "target_lang": target_lang,
                            "progress": {"done": done_count, "total": total},
                            "rows": rows,
                        })

            payload = {
                "ts": time.time(),
                "started": started,
                "duration_s": round(time.time() - started, 1),
                "target_lang": target_lang,
                "progress": {"done": done_count, "total": total},
                "rows": rows,
            }
            _save(data_dir, payload)
            actionable = sum(1 for r in rows if r.get("source_available"))
            log.info("discover: %d series in %.1fs · %d missing in lib · %d actionable · %d errors",
                     len(rows), payload["duration_s"],
                     sum(1 for r in rows if not r["has_target_in_first_ep"] and not r.get("probe_error")),
                     actionable,
                     sum(1 for r in rows if r.get("probe_error")))
            payload["running"] = False
            return payload
        finally:
            _RUNNING["value"] = False


def scan_in_background(sonarr, target_lang: str, path_remap, tracked_ids,
                        data_dir, tracked_cfg=None, max_workers: int = 2,
                        pause_check=None) -> bool:
    if _RUNNING["value"]:
        return False
    threading.Thread(
        target=scan_all,
        args=(sonarr, target_lang, path_remap, tracked_ids, data_dir,
              tracked_cfg, max_workers),
        kwargs={"pause_check": pause_check},
        daemon=True, name="discover-scan",
    ).start()
    return True


def summary_counts(rows: list[dict]) -> dict:
    out = {"total": len(rows), "missing": 0, "complete": 0, "tracked": 0,
           "untracked": 0, "errors": 0,
           "actionable": 0,         # missing in lib AND source has dub
           "missing_no_source": 0,  # missing in lib AND source missing too
           "missing_unknown": 0,    # missing in lib, source not yet probed (untracked)
           "source_errors": 0}
    for r in rows:
        if r["probe_error"]:
            out["errors"] += 1
        elif r["has_target_in_first_ep"]:
            out["complete"] += 1
        else:
            out["missing"] += 1
            sa = r.get("source_available")
            if sa is True:
                out["actionable"] += 1
            elif sa is False:
                if r.get("source_probe_errors"):
                    out["source_errors"] += 1
                else:
                    out["missing_no_source"] += 1
            else:
                out["missing_unknown"] += 1
        if r["tracked"]:
            out["tracked"] += 1
        else:
            out["untracked"] += 1
    return out
