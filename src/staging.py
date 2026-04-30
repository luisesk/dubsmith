"""Per-episode staging dir layout + janitor helpers.

Layout: {staging_root}/{cr_season_id}/S{NN}/E{NN}/<files from mdnx>
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)


def episode_dir(staging_root: str | Path, cr_season_id: str, season: int, episode: int) -> Path:
    return Path(staging_root) / cr_season_id / f"S{season:02d}" / f"E{episode:02d}"


def clean_episode(staging_root: str | Path, cr_season_id: str,
                  season: int, episode: int) -> int:
    """Remove the per-episode dir and prune empty parents (S## and CR-id) up to root.
    Returns bytes freed (best-effort).
    """
    root = Path(staging_root)
    d = episode_dir(root, cr_season_id, season, episode)
    if not d.exists():
        return 0
    freed = _dir_size(d)
    try:
        shutil.rmtree(d, ignore_errors=True)
        log.info("staging: cleaned %s (%.1f MB)", d, freed / 1024 / 1024)
    except Exception as e:
        log.warning("staging: rmtree failed %s: %s", d, e)
        return 0
    _prune_empty_parents(d.parent, stop_at=root)
    return freed


def sweep_old(staging_root: str | Path, max_age_days: float = 7.0) -> int:
    """Remove episode dirs (E## leaves) older than max_age_days. Returns count removed.
    Empty parent dirs are pruned. Skips the root dir itself.
    """
    root = Path(staging_root)
    if not root.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    # Walk top-down up to depth 3 (cr_id / season / ep)
    for cr_dir in [p for p in root.iterdir() if p.is_dir()]:
        for season_dir in [p for p in cr_dir.iterdir() if p.is_dir()]:
            for ep_dir in [p for p in season_dir.iterdir() if p.is_dir()]:
                try:
                    if ep_dir.stat().st_mtime < cutoff:
                        shutil.rmtree(ep_dir, ignore_errors=True)
                        removed += 1
                        log.info("staging janitor: removed stale %s", ep_dir)
                except OSError:
                    continue
    _prune_empty_parents_recursive(root)
    return removed


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _prune_empty_parents(start: Path, stop_at: Path) -> None:
    cur = start.resolve()
    stop = stop_at.resolve()
    while cur != stop and cur.exists():
        try:
            if not any(cur.iterdir()):
                cur.rmdir()
            else:
                break
        except OSError:
            break
        cur = cur.parent


def _prune_empty_parents_recursive(root: Path) -> None:
    """Walk tree bottom-up, remove dirs that became empty."""
    if not root.exists():
        return
    for d in sorted([p for p in root.rglob("*") if p.is_dir()],
                    key=lambda p: -len(p.parts)):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            continue


def staging_disk_usage(staging_root: str | Path) -> dict:
    """Total bytes + episode dir count."""
    root = Path(staging_root)
    if not root.exists():
        return {"bytes": 0, "episode_dirs": 0}
    n = 0
    for cr_dir in [p for p in root.iterdir() if p.is_dir()]:
        for season_dir in [p for p in cr_dir.iterdir() if p.is_dir()]:
            for ep_dir in [p for p in season_dir.iterdir() if p.is_dir()]:
                n += 1
    return {"bytes": _dir_size(root), "episode_dirs": n}
