"""Library-wide audio coverage scanner.

Walks every Sonarr series, ffprobes one sample episode per series, classifies
each show by whether it has the target audio language. Results persist in
data/discover.json so the UI returns instantly; a background refresh updates them.
"""
from __future__ import annotations

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
              errors: [{season, error},...], source: 'crunchyroll'|'hidive'|'adn'}
    """
    cr_seasons = (show_cfg or {}).get("cr_seasons") or {}
    source = (show_cfg or {}).get("source", "crunchyroll")
    out = {
        "available": False,
        "with_lang": [], "without_lang": [], "errors": [],
        "source": source,
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
        "source_available": None,         # bool | None (None = not probed, e.g. untracked)
        "source_kind": None,              # 'crunchyroll'/'hidive'/'adn'
        "source_seasons_with_lang": [],
        "source_seasons_without_lang": [],
        "source_probe_errors": [],
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

    # If library is missing the dub AND show is tracked, probe its source for
    # whether the dub is actually fetchable. Untracked rows we skip — the user
    # has to set up CR mapping via the wizard first.
    if not row["has_target_in_first_ep"] and row["tracked"] and show_cfg:
        cr = _probe_source_dubs(show_cfg, target_lang)
        row["source_available"] = cr["available"]
        row["source_kind"] = cr["source"]
        row["source_seasons_with_lang"] = cr["with_lang"]
        row["source_seasons_without_lang"] = cr["without_lang"]
        row["source_probe_errors"] = cr["errors"]
    return row


def scan_all(sonarr, target_lang: str, path_remap: tuple[str, str],
             tracked_ids: set[int], data_dir: Path | str,
             tracked_cfg: dict | None = None) -> dict:
    """Run a full library scan and persist results.

    tracked_cfg: dict[series_id_int, show_yaml_entry]. Used to probe sources for
    dub availability on tracked rows.
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
            rows = []
            for s in series_list:
                sid = s["id"]
                cfg = tracked_cfg.get(sid) or tracked_cfg.get(str(sid))
                rows.append(_scan_one(s, sonarr, target_lang, path_remap,
                                       tracked_ids, show_cfg=cfg))
            payload = {
                "ts": time.time(),
                "started": started,
                "duration_s": round(time.time() - started, 1),
                "target_lang": target_lang,
                "rows": rows,
            }
            _save(data_dir, payload)
            actionable = sum(1 for r in rows if r.get("source_available"))
            log.info("discover: %d series in %.1fs · %d missing in lib · %d actionable on source · %d errors",
                     len(rows), payload["duration_s"],
                     sum(1 for r in rows if not r["has_target_in_first_ep"] and not r["probe_error"]),
                     actionable,
                     sum(1 for r in rows if r["probe_error"]))
            payload["running"] = False
            return payload
        finally:
            _RUNNING["value"] = False


def scan_in_background(sonarr, target_lang: str, path_remap, tracked_ids,
                        data_dir, tracked_cfg=None) -> bool:
    if _RUNNING["value"]:
        return False
    threading.Thread(
        target=scan_all,
        args=(sonarr, target_lang, path_remap, tracked_ids, data_dir, tracked_cfg),
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
