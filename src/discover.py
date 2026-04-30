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

from . import probe
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


def _scan_one(series: dict, sonarr, target_lang: str,
              path_remap: tuple[str, str], tracked_ids: set[int]) -> dict:
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
    return row


def scan_all(sonarr, target_lang: str, path_remap: tuple[str, str],
             tracked_ids: set[int], data_dir: Path | str) -> dict:
    """Run a full library scan and persist results."""
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
            rows = []
            for s in series_list:
                rows.append(_scan_one(s, sonarr, target_lang, path_remap, tracked_ids))
            payload = {
                "ts": time.time(),
                "started": started,
                "duration_s": round(time.time() - started, 1),
                "target_lang": target_lang,
                "rows": rows,
            }
            _save(data_dir, payload)
            log.info("discover: scanned %d series in %.1fs (%d errors, %d missing)",
                     len(rows), payload["duration_s"],
                     sum(1 for r in rows if r["probe_error"]),
                     sum(1 for r in rows if not r["has_target_in_first_ep"] and not r["probe_error"]))
            payload["running"] = False
            return payload
        finally:
            _RUNNING["value"] = False


def scan_in_background(sonarr, target_lang: str, path_remap, tracked_ids,
                        data_dir) -> bool:
    if _RUNNING["value"]:
        return False
    threading.Thread(
        target=scan_all,
        args=(sonarr, target_lang, path_remap, tracked_ids, data_dir),
        daemon=True, name="discover-scan",
    ).start()
    return True


def summary_counts(rows: list[dict]) -> dict:
    out = {"total": len(rows), "missing": 0, "complete": 0, "tracked": 0,
           "untracked": 0, "errors": 0}
    for r in rows:
        if r["probe_error"]:
            out["errors"] += 1
        elif r["has_target_in_first_ep"]:
            out["complete"] += 1
        else:
            out["missing"] += 1
        if r["tracked"]:
            out["tracked"] += 1
        else:
            out["untracked"] += 1
    return out
