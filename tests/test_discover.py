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


def test_probe_source_dubs_marks_available(monkeypatch):
    show_cfg = {"cr_seasons": {"1": "GR49C7EPD"}, "source": "crunchyroll"}

    def fake_probe(cr_id, source="crunchyroll"):
        return ["en", "es-419", "pt-BR", "ja"]

    monkeypatch.setattr(discover.downloader, "probe_season_dubs", fake_probe)
    out = discover._probe_source_dubs(show_cfg, "por")
    assert out["available"] is True
    assert out["with_lang"] == ["1"]
    assert out["source"] == "crunchyroll"


def test_probe_source_dubs_no_dub(monkeypatch):
    show_cfg = {"cr_seasons": {"1": "X", "2": "Y"}, "source": "crunchyroll"}

    monkeypatch.setattr(discover.downloader, "probe_season_dubs",
                        lambda cr_id, source="crunchyroll": ["en", "ja"])
    out = discover._probe_source_dubs(show_cfg, "por")
    assert out["available"] is False
    assert out["with_lang"] == []
    assert sorted(out["without_lang"]) == ["1", "2"]


def test_probe_source_dubs_partial_seasons(monkeypatch):
    show_cfg = {"cr_seasons": {"1": "X", "2": "Y"}, "source": "crunchyroll"}

    def fake_probe(cr_id, source="crunchyroll"):
        return ["pt-BR"] if cr_id == "Y" else ["en"]

    monkeypatch.setattr(discover.downloader, "probe_season_dubs", fake_probe)
    out = discover._probe_source_dubs(show_cfg, "por")
    assert out["available"] is True
    assert out["with_lang"] == ["2"]
    assert out["without_lang"] == ["1"]


def test_probe_source_dubs_collects_errors(monkeypatch):
    show_cfg = {"cr_seasons": {"1": "X"}, "source": "crunchyroll"}

    def fake_probe(cr_id, source="crunchyroll"):
        raise RuntimeError("boom")

    monkeypatch.setattr(discover.downloader, "probe_season_dubs", fake_probe)
    out = discover._probe_source_dubs(show_cfg, "por")
    assert out["available"] is False
    assert out["errors"][0]["error"] == "boom"


def test_scan_one_runs_source_probe_for_tracked_missing(monkeypatch, tmp_path):
    f = tmp_path / "ep.mkv"; f.write_bytes(b"x")
    sonarr = MagicMock()
    sonarr.episode_files.return_value = [{"path": str(f)}]
    series = {"id": 1, "title": "X"}
    show_cfg = {"cr_seasons": {"1": "ABC"}, "source": "crunchyroll"}

    monkeypatch.setattr(discover.probe, "audio_languages", lambda p: ["jpn"])
    monkeypatch.setattr(discover.downloader, "probe_season_dubs",
                        lambda cr_id, source="crunchyroll": ["pt-BR", "en"])
    row = discover._scan_one(series, sonarr, "por", ("", ""), {1}, show_cfg=show_cfg)
    assert row["has_target_in_first_ep"] is False
    assert row["source_available"] is True
    assert row["source_kind"] == "crunchyroll"


def test_scan_one_skips_source_probe_when_lib_already_has(monkeypatch, tmp_path):
    f = tmp_path / "ep.mkv"; f.write_bytes(b"x")
    sonarr = MagicMock()
    sonarr.episode_files.return_value = [{"path": str(f)}]
    series = {"id": 1, "title": "X"}
    show_cfg = {"cr_seasons": {"1": "ABC"}, "source": "crunchyroll"}

    called = []
    monkeypatch.setattr(discover.probe, "audio_languages", lambda p: ["por", "jpn"])
    monkeypatch.setattr(discover.downloader, "probe_season_dubs",
                        lambda *a, **k: called.append(1) or [])
    row = discover._scan_one(series, sonarr, "por", ("", ""), {1}, show_cfg=show_cfg)
    assert row["has_target_in_first_ep"] is True
    assert row["source_available"] is None
    assert called == []  # never called


def test_search_untracked_marks_available(monkeypatch):
    def fake_search(q, limit=1, source="crunchyroll"):
        return [{"show_id": "Z", "title": "Foo Bar",
                 "seasons": [{"cr_season_id": "GS1", "name": "S1",
                              "dub_langs": ["en", "pt-BR"]}]}]
    monkeypatch.setattr(discover.downloader, "search_show", fake_search)
    out = discover._search_untracked("Foo", "por")
    assert out["available"] is True
    assert out["matched_id"] == "Z"
    assert out["matched_title"] == "Foo Bar"
    assert "GS1" in out["with_lang"]


def test_search_untracked_no_results(monkeypatch):
    monkeypatch.setattr(discover.downloader, "search_show",
                        lambda q, limit=1, source="crunchyroll": [])
    out = discover._search_untracked("Nope", "por")
    assert out["available"] is False
    assert out["matched_id"] is None


def test_search_untracked_handles_exception(monkeypatch):
    monkeypatch.setattr(discover.downloader, "search_show",
                        lambda q, limit=1, source="crunchyroll": (_ for _ in ()).throw(RuntimeError("net")))
    out = discover._search_untracked("X", "por")
    assert out["available"] is False
    assert out["errors"][0]["error"] == "net"


def test_scan_one_searches_untracked_when_lib_missing(monkeypatch, tmp_path):
    f = tmp_path / "ep.mkv"; f.write_bytes(b"x")
    sonarr = MagicMock()
    sonarr.episode_files.return_value = [{"path": str(f)}]
    series = {"id": 7, "title": "Slime"}

    monkeypatch.setattr(discover.probe, "audio_languages", lambda p: ["jpn"])
    called = []

    def fake_search(q, limit=1, source="crunchyroll"):
        called.append(q)
        return [{"show_id": "QQ", "title": "Slime CR",
                 "seasons": [{"cr_season_id": "S1", "dub_langs": ["pt-BR"]}]}]

    monkeypatch.setattr(discover.downloader, "search_show", fake_search)
    row = discover._scan_one(series, sonarr, "por", ("", ""), tracked_ids=set())
    assert called == ["Slime"]
    assert row["source_available"] is True
    assert row["source_matched_title"] == "Slime CR"
    assert row["source_matched_id"] == "QQ"


def test_scan_all_uses_thread_pool(tmp_path, monkeypatch):
    """Hit ThreadPoolExecutor path. Verify all rows persist."""
    sonarr = MagicMock()
    sonarr.all_series.return_value = [{"id": i, "title": f"T{i}"} for i in range(8)]
    sonarr.episode_files.return_value = []
    out = discover.scan_all(sonarr, "por", ("", ""), set(), tmp_path,
                             max_workers=4, save_every=3)
    assert len(out["rows"]) == 8
    cache = json.loads((tmp_path / "discover.json").read_text())
    assert len(cache["rows"]) == 8
    assert cache["progress"]["done"] == 8
    assert cache["progress"]["total"] == 8


def test_summary_counts_actionable_bucket():
    rows = [
        {"probe_error": None, "has_target_in_first_ep": False, "tracked": True,
         "source_available": True, "source_probe_errors": []},
        {"probe_error": None, "has_target_in_first_ep": False, "tracked": True,
         "source_available": False, "source_probe_errors": []},
        {"probe_error": None, "has_target_in_first_ep": False, "tracked": False,
         "source_available": None, "source_probe_errors": []},
        {"probe_error": None, "has_target_in_first_ep": True, "tracked": True,
         "source_available": None, "source_probe_errors": []},
    ]
    s = discover.summary_counts(rows)
    assert s["actionable"] == 1
    assert s["missing_no_source"] == 1
    assert s["missing_unknown"] == 1
    assert s["complete"] == 1
