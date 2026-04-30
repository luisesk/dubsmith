"""Local-disk mux staging: workdir selection + atomic copy-back."""
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from src import mux


def _make_video(p: Path, size: int = 1024) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * size)


def test_stat_same_fs_true_for_same_dir(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    assert mux._stat_same_fs(a, b) is True


def test_stat_same_fs_false_for_missing_path(tmp_path):
    assert mux._stat_same_fs(tmp_path / "nope", tmp_path) is False


def test_inject_uses_target_dir_when_workdir_none(tmp_path, monkeypatch):
    """No mux_workdir → tempdir lives next to the target."""
    target = tmp_path / "video.mkv"; _make_video(target)
    src = tmp_path / "audio.mkv"; _make_video(src)

    captured = {}
    def fake_run(cmd, **kwargs):
        # Find the -o path; mkvmerge tempdir lives next to target
        for i, a in enumerate(cmd):
            if a == "-o":
                out = Path(cmd[i + 1])
                captured["out_dir"] = out.parent.parent  # tempdir lives in target's dir
                # Write a "merged" file > 90% of target
                out.write_bytes(b"\x00" * 1024)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(mux.probe, "streams", lambda p: [])
    monkeypatch.setattr(mux.subprocess, "run", fake_run)
    final = mux.inject(str(target), str(src), 100, lang="por", track_name="PT")
    assert Path(final).exists()
    # tempdir parent was target_dir
    assert captured["out_dir"].resolve() == tmp_path.resolve()


def test_inject_uses_mux_workdir_when_set(tmp_path, monkeypatch):
    """mux_workdir → tempdir lives there, not next to the target."""
    target_dir = tmp_path / "lib"
    workdir = tmp_path / "fast"
    workdir.mkdir()
    target = target_dir / "v.mkv"; _make_video(target)
    src = tmp_path / "a.mkv"; _make_video(src)

    captured = {}
    def fake_run(cmd, **kwargs):
        for i, a in enumerate(cmd):
            if a == "-o":
                out = Path(cmd[i + 1])
                captured["out_parent"] = out.parent
                captured["tempdir_parent"] = out.parent.parent
                out.write_bytes(b"\x00" * 1024)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(mux.probe, "streams", lambda p: [])
    monkeypatch.setattr(mux.subprocess, "run", fake_run)
    final = mux.inject(str(target), str(src), 100, lang="por", track_name="PT",
                       mux_workdir=str(workdir))
    assert Path(final).exists()
    assert captured["tempdir_parent"].resolve() == workdir.resolve()


def test_inject_falls_back_when_workdir_unwritable(tmp_path, monkeypatch):
    """If workdir mkdir fails, behave as if it weren't set."""
    target = tmp_path / "v.mkv"; _make_video(target)
    src = tmp_path / "a.mkv"; _make_video(src)

    captured = {}
    def fake_run(cmd, **kwargs):
        for i, a in enumerate(cmd):
            if a == "-o":
                Path(cmd[i + 1]).write_bytes(b"\x00" * 1024)
                captured["tempdir_parent"] = Path(cmd[i + 1]).parent.parent
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    # Force mkdir to fail
    real_mkdir = Path.mkdir
    def fail_mkdir(self, *a, **k):
        if "bad" in str(self):
            raise OSError("read-only")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(mux.probe, "streams", lambda p: [])
    monkeypatch.setattr(mux.subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    final = mux.inject(str(target), str(src), 100, lang="por", track_name="PT",
                       mux_workdir=str(tmp_path / "bad"))
    assert Path(final).exists()
    # Falls back to target_dir
    assert captured["tempdir_parent"].resolve() == tmp_path.resolve()


def test_large_file_skips_local_staging(tmp_path, monkeypatch):
    """Files > _LARGE_FILE_BYTES should write directly to target FS even when
    mux_workdir is set."""
    target = tmp_path / "huge.mkv"
    target.write_bytes(b"x")  # tiny on disk; we mock stat

    src = tmp_path / "a.mkv"; _make_video(src)
    workdir = tmp_path / "fast"; workdir.mkdir()

    # Pretend the target is 8 GB
    real_stat = Path.stat
    big = 8 * 1024**3
    def fake_stat(self, *a, **k):
        s = real_stat(self, *a, **k)
        if str(self) == str(target):
            class S:
                st_size = big
                st_mtime = s.st_mtime
                st_dev = s.st_dev
                def __getattr__(self, name): return getattr(s, name)
            return S()
        return s
    monkeypatch.setattr(Path, "stat", fake_stat)

    captured = {}
    def fake_run(cmd, **kwargs):
        for i, a in enumerate(cmd):
            if a == "-o":
                Path(cmd[i + 1]).write_bytes(b"\x00" * 1024)
                captured["tempdir_parent"] = Path(cmd[i + 1]).parent.parent
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(mux.probe, "streams", lambda p: [])
    monkeypatch.setattr(mux.subprocess, "run", fake_run)
    # Avoid the 90% size sanity check tripping (target=8GB, out_tmp=1kb)
    monkeypatch.setattr(mux.os.path, "getsize", lambda p: big)

    final = mux.inject(str(target), str(src), 100, lang="por", track_name="PT",
                       mux_workdir=str(workdir))
    assert Path(final).exists()
    # Even though workdir was set, large file means target_dir was used.
    assert captured["tempdir_parent"].resolve() == tmp_path.resolve()


def test_orphan_sweep_removes_stale_tempfiles(tmp_path):
    """`.dubsmith.*.mkv` orphans older than 30min should get cleaned."""
    import os
    fresh = tmp_path / ".dubsmith.999.fresh.mkv"
    stale = tmp_path / ".dubsmith.111.stale.mkv"
    keep = tmp_path / "regular.mkv"
    for p in (fresh, stale, keep):
        p.write_bytes(b"x" * 100)
    # Backdate stale to 1h ago
    old = time.time() - 3700
    os.utime(stale, (old, old))

    n = mux._sweep_orphan_tempfiles(tmp_path)
    assert n == 1
    assert not stale.exists()
    assert fresh.exists()  # fresh stays
    assert keep.exists()   # non-tempfile untouched


def test_inject_runs_orphan_sweep(tmp_path, monkeypatch):
    """Mux should call the orphan sweeper before running mkvmerge."""
    target = tmp_path / "v.mkv"; _make_video(target)
    src = tmp_path / "a.mkv"; _make_video(src)
    swept = []
    monkeypatch.setattr(mux, "_sweep_orphan_tempfiles",
                        lambda d: swept.append(d) or 0)

    def fake_run(cmd, **kwargs):
        for i, a in enumerate(cmd):
            if a == "-o":
                Path(cmd[i + 1]).write_bytes(b"\x00" * 1024)
        class R:
            returncode = 0; stdout = ""; stderr = ""
        return R()

    monkeypatch.setattr(mux.probe, "streams", lambda p: [])
    monkeypatch.setattr(mux.subprocess, "run", fake_run)
    mux.inject(str(target), str(src), 100, lang="por", track_name="PT")
    assert swept and swept[0].resolve() == tmp_path.resolve()


def test_inject_atomic_copyback_when_cross_fs(tmp_path, monkeypatch):
    """When workdir is 'cross-FS' (we mock _stat_same_fs to return False), the
    copy-back path is taken: result lands at final atomically via os.replace."""
    target_dir = tmp_path / "lib"; target_dir.mkdir()
    target = target_dir / "v.mkv"; _make_video(target, 1024)
    src = tmp_path / "a.mkv"; _make_video(src)
    workdir = tmp_path / "fast"; workdir.mkdir()

    monkeypatch.setattr(mux, "_stat_same_fs", lambda a, b: False)
    monkeypatch.setattr(mux.probe, "streams", lambda p: [])

    def fake_run(cmd, **kwargs):
        for i, a in enumerate(cmd):
            if a == "-o":
                Path(cmd[i + 1]).write_bytes(b"\x00" * 1024)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(mux.subprocess, "run", fake_run)

    final_path = mux.inject(str(target), str(src), 200, lang="por", track_name="PT",
                            mux_workdir=str(workdir))
    assert Path(final_path).exists()
    # No leftover .dubsmith.* tempfile beside target
    assert not list(target_dir.glob(".dubsmith.*"))
