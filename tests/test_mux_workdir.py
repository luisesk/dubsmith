"""Local-disk mux staging: workdir selection + atomic copy-back."""
import os
import shutil
import subprocess
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
    def fake_run(cmd, check=False):
        # Find the -o path; mkvmerge tempdir lives next to target
        for i, a in enumerate(cmd):
            if a == "-o":
                out = Path(cmd[i + 1])
                captured["out_dir"] = out.parent.parent  # tempdir lives in target's dir
                # Write a "merged" file > 90% of target
                out.write_bytes(b"\x00" * 1024)
        class R: returncode = 0
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
    def fake_run(cmd, check=False):
        for i, a in enumerate(cmd):
            if a == "-o":
                out = Path(cmd[i + 1])
                captured["out_parent"] = out.parent
                captured["tempdir_parent"] = out.parent.parent
                out.write_bytes(b"\x00" * 1024)
        class R: returncode = 0
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
    def fake_run(cmd, check=False):
        for i, a in enumerate(cmd):
            if a == "-o":
                Path(cmd[i + 1]).write_bytes(b"\x00" * 1024)
                captured["tempdir_parent"] = Path(cmd[i + 1]).parent.parent
        class R: returncode = 0
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


def test_inject_atomic_copyback_when_cross_fs(tmp_path, monkeypatch):
    """When workdir is 'cross-FS' (we mock _stat_same_fs to return False), the
    copy-back path is taken: result lands at final atomically via os.replace."""
    target_dir = tmp_path / "lib"; target_dir.mkdir()
    target = target_dir / "v.mkv"; _make_video(target, 1024)
    src = tmp_path / "a.mkv"; _make_video(src)
    workdir = tmp_path / "fast"; workdir.mkdir()

    monkeypatch.setattr(mux, "_stat_same_fs", lambda a, b: False)
    monkeypatch.setattr(mux.probe, "streams", lambda p: [])

    def fake_run(cmd, check=False):
        for i, a in enumerate(cmd):
            if a == "-o":
                Path(cmd[i + 1]).write_bytes(b"\x00" * 1024)
        class R: returncode = 0
        return R()

    monkeypatch.setattr(mux.subprocess, "run", fake_run)

    final_path = mux.inject(str(target), str(src), 200, lang="por", track_name="PT",
                            mux_workdir=str(workdir))
    assert Path(final_path).exists()
    # No leftover .dubsmith.* tempfile beside target
    assert not list(target_dir.glob(".dubsmith.*"))
