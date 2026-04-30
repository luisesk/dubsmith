"""ffprobe cache key + eviction logic, no actual ffprobe call."""
import time
from unittest.mock import patch

from src import probe


def _fake_run(*args, **kwargs):
    class R:
        returncode = 0
        stdout = '{"streams":[{"codec_type":"audio","tags":{"language":"jpn"},"index":1}]}'
        stderr = ""
    return R()


def test_cache_hit_skips_subprocess(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    probe._CACHE.clear()
    with patch("src.probe.subprocess.run", side_effect=_fake_run) as m:
        probe.streams(str(f))
        probe.streams(str(f))
        probe.streams(str(f))
    assert m.call_count == 1


def test_cache_invalidated_on_mtime_change(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    probe._CACHE.clear()
    with patch("src.probe.subprocess.run", side_effect=_fake_run) as m:
        probe.streams(str(f))
        # tweak mtime forward
        st = f.stat()
        import os
        os.utime(f, (st.st_atime, st.st_mtime + 60))
        probe.streams(str(f))
    assert m.call_count == 2


def test_no_cache_flag_bypasses(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    probe._CACHE.clear()
    with patch("src.probe.subprocess.run", side_effect=_fake_run) as m:
        probe.streams(str(f))
        probe.streams(str(f), no_cache=True)
    assert m.call_count == 2


def test_cache_stats(tmp_path):
    probe._CACHE.clear()
    s = probe.cache_stats()
    assert s["entries"] == 0
    assert "max" in s and "ttl_s" in s
