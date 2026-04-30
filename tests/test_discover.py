"""Discovery scanner: classification logic + cache I/O."""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import discover


def test_summary_counts_classifies_correctly():
    rows = [
        {"probe_error": None, "has_target_in_first_ep": True,  "tracked": True},
        {"probe_error": None, "has_target_in_first_ep": False, "tracked": True},
        {"probe_error": None, "has_target_in_first_ep": False, "tracked": False},
        {"probe_error": "boom", "has_target_in_first_ep": False, "tracked": False},
    ]
    s = discover.summary_counts(rows)
    assert s["total"] == 4
    assert s["complete"] == 1
    assert s["missing"] == 2
    assert s["errors"] == 1
    assert s["tracked"] == 2
    assert s["untracked"] == 2


def test_load_returns_empty_when_no_cache(tmp_path):
    d = discover.load(tmp_path)
    assert d["ts"] == 0
    assert d["rows"] == []


def test_save_and_load_roundtrip(tmp_path):
    payload = {"ts": 12345.0, "duration_s": 1.0, "target_lang": "por", "rows": [
        {"series_id": 1, "title": "X", "tracked": True,
         "has_target_in_first_ep": True, "probe_error": None,
         "first_ep_langs": ["por", "jpn"]},
    ]}
    discover._save(tmp_path, payload)
    out = discover.load(tmp_path)
    assert out["ts"] == 12345.0
    assert out["rows"][0]["title"] == "X"
    assert out["running"] is False


def test_scan_one_marks_missing_when_lang_absent(tmp_path):
    # File exists, ffprobe returns only jpn audio — target por should be missing.
    f = tmp_path / "ep.mkv"
    f.write_bytes(b"x")
    sonarr = MagicMock()
    sonarr.episode_files.return_value = [{"path": str(f)}]
    series = {"id": 99, "title": "S", "monitored": True, "statistics": {"episodeCount": 12}}

    with patch("src.discover.probe.audio_languages", return_value=["jpn"]):
        row = discover._scan_one(series, sonarr, "por", ("", ""), tracked_ids=set())
    assert row["series_id"] == 99
    assert row["first_ep_langs"] == ["jpn"]
    assert row["has_target_in_first_ep"] is False
    assert row["probe_error"] is None


def test_scan_one_handles_missing_file(tmp_path):
    sonarr = MagicMock()
    sonarr.episode_files.return_value = [{"path": "/nope/missing.mkv"}]
    series = {"id": 1, "title": "X"}
    row = discover._scan_one(series, sonarr, "por", ("", ""), set())
    assert row["probe_error"] == "file not found in container"


def test_scan_one_handles_no_files():
    sonarr = MagicMock()
    sonarr.episode_files.return_value = []
    series = {"id": 1, "title": "X"}
    row = discover._scan_one(series, sonarr, "por", ("", ""), set())
    assert row["probe_error"] == "no episode files"


def test_scan_one_marks_tracked():
    sonarr = MagicMock()
    sonarr.episode_files.return_value = []
    series = {"id": 7, "title": "T"}
    row = discover._scan_one(series, sonarr, "por", ("", ""), {7, 9})
    assert row["tracked"] is True
    row2 = discover._scan_one(series, sonarr, "por", ("", ""), {1, 2})
    assert row2["tracked"] is False


def test_scan_one_lang_match_cross_form(tmp_path):
    """ffprobe says 'pt' but target is 'por' → must match (lang.normalize)."""
    f = tmp_path / "ep.mkv"
    f.write_bytes(b"x")
    sonarr = MagicMock()
    sonarr.episode_files.return_value = [{"path": str(f)}]
    series = {"id": 1, "title": "X"}
    with patch("src.discover.probe.audio_languages", return_value=["pt", "jpn"]):
        row = discover._scan_one(series, sonarr, "por", ("", ""), set())
    assert row["has_target_in_first_ep"] is True


def test_scan_all_persists(tmp_path):
    sonarr = MagicMock()
    sonarr.all_series.return_value = [{"id": 1, "title": "X"}, {"id": 2, "title": "Y"}]
    sonarr.episode_files.return_value = []
    out = discover.scan_all(sonarr, "por", ("", ""), set(), tmp_path)
    assert len(out["rows"]) == 2
    assert out["target_lang"] == "por"
    cache = json.loads((tmp_path / "discover.json").read_text())
    assert len(cache["rows"]) == 2
