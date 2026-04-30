"""Staging cleanup helpers."""
import os
import time
from pathlib import Path

from src import staging


def _make_episode(root: Path, cr: str, season: int, ep: int, mtime: float | None = None) -> Path:
    d = staging.episode_dir(root, cr, season, ep)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "video.mkv"
    f.write_bytes(b"x" * 1024)
    if mtime is not None:
        os.utime(d, (mtime, mtime))
        os.utime(f, (mtime, mtime))
    return d


def test_clean_episode_removes_dir_and_prunes_parents(tmp_path):
    root = tmp_path / "staging"
    _make_episode(root, "GR49C7EPD", 1, 1)
    freed = staging.clean_episode(root, "GR49C7EPD", 1, 1)
    assert freed > 0
    # episode dir gone
    assert not staging.episode_dir(root, "GR49C7EPD", 1, 1).exists()
    # parent S01 + cr-id pruned because empty
    assert not (root / "GR49C7EPD" / "S01").exists()
    assert not (root / "GR49C7EPD").exists()
    # but root remains
    assert root.exists()


def test_clean_keeps_sibling_episode(tmp_path):
    root = tmp_path / "staging"
    _make_episode(root, "X", 1, 1)
    _make_episode(root, "X", 1, 2)
    staging.clean_episode(root, "X", 1, 1)
    # ep 2 + parents preserved
    assert staging.episode_dir(root, "X", 1, 2).exists()
    assert (root / "X" / "S01").exists()


def test_clean_missing_dir_returns_zero(tmp_path):
    assert staging.clean_episode(tmp_path / "staging", "NOPE", 1, 1) == 0


def test_sweep_old_removes_aged_only(tmp_path):
    root = tmp_path / "staging"
    old = time.time() - 10 * 86400  # 10 days old
    _make_episode(root, "OLD", 1, 1, mtime=old)
    _make_episode(root, "FRESH", 1, 1)  # mtime now
    n = staging.sweep_old(root, max_age_days=7)
    assert n == 1
    assert not staging.episode_dir(root, "OLD", 1, 1).exists()
    assert staging.episode_dir(root, "FRESH", 1, 1).exists()


def test_sweep_aggressive_zero_age_kills_all(tmp_path):
    root = tmp_path / "staging"
    _make_episode(root, "A", 1, 1)
    _make_episode(root, "B", 1, 1)
    n = staging.sweep_old(root, max_age_days=0)
    assert n == 2
    # parent dirs pruned
    assert not (root / "A").exists()
    assert not (root / "B").exists()


def test_sweep_no_root(tmp_path):
    # missing root — must not raise
    assert staging.sweep_old(tmp_path / "missing", max_age_days=7) == 0


def test_disk_usage(tmp_path):
    root = tmp_path / "staging"
    _make_episode(root, "X", 1, 1)
    _make_episode(root, "X", 1, 2)
    u = staging.staging_disk_usage(root)
    assert u["episode_dirs"] == 2
    assert u["bytes"] >= 2048
