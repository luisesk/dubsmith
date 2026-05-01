"""Sonarr shadow cache: sync + read API."""
import json
from unittest.mock import MagicMock

from src.sonarr_cache import SonarrCache


def _fake_sonarr():
    s = MagicMock()
    s.base_url = "http://sonarr:8989"
    s.client = MagicMock()
    s.client.headers = {"X-Api-Key": "k"}
    s.all_series.return_value = [
        {"id": 1, "title": "A", "images": []},
        {"id": 2, "title": "B", "images": []},
    ]
    s.episode_files.side_effect = lambda sid: [{"id": 100 + sid, "seriesId": sid}]
    s.episodes.side_effect = lambda sid: [{"id": 200 + sid, "seriesId": sid,
                                            "seasonNumber": 1, "episodeNumber": 1}]
    return s


def test_sync_persists_and_serves_locally(tmp_path):
    sonarr = _fake_sonarr()
    c = SonarrCache(sonarr, tmp_path)
    assert c.is_empty()
    c.sync(prefetch_images=False)
    assert not c.is_empty()
    assert len(c.all_series()) == 2
    assert c.series(1)["title"] == "A"
    assert c.episode_files(2) == [{"id": 102, "seriesId": 2}]
    # Reload from disk
    c2 = SonarrCache(sonarr, tmp_path)
    assert c2.series(1)["title"] == "A"


def test_sync_records_stats(tmp_path):
    c = SonarrCache(_fake_sonarr(), tmp_path)
    c.sync(prefetch_images=False)
    s = c.stats()
    assert s["series"] == 2
    assert s["episodes"] == 2
    assert s["ep_files"] == 2
    assert s["ts"] > 0


def test_sync_skipped_if_already_running(tmp_path, monkeypatch):
    """Second concurrent sync no-ops."""
    sonarr = _fake_sonarr()
    c = SonarrCache(sonarr, tmp_path)
    c.sync(prefetch_images=False)
    # Pretend a sync is in progress by acquiring the module lock
    from src import sonarr_cache as mod
    mod._SYNC_LOCK.acquire()
    try:
        # This call should fast-return without invoking sonarr again
        before = sonarr.all_series.call_count
        out = c.sync(prefetch_images=False)
        assert sonarr.all_series.call_count == before
        assert out == c.stats()
    finally:
        mod._SYNC_LOCK.release()


def test_partial_sync_handles_per_series_errors(tmp_path):
    sonarr = _fake_sonarr()
    sonarr.episode_files.side_effect = lambda sid: (
        (_ for _ in ()).throw(RuntimeError("boom")) if sid == 1 else [{"id": 200 + sid, "seriesId": sid}]
    )
    c = SonarrCache(sonarr, tmp_path)
    c.sync(prefetch_images=False, max_series_workers=1)
    # Series 1 ep_files empty (failed), but series payload still present
    assert c.series(1)["title"] == "A"
    assert c.episode_files(1) == []
    assert c.episode_files(2) == [{"id": 202, "seriesId": 2}]


def test_read_api_returns_empty_for_unknown_sid(tmp_path):
    c = SonarrCache(_fake_sonarr(), tmp_path)
    c.sync(prefetch_images=False)
    assert c.series(999) is None
    assert c.episode_files(999) == []
    assert c.episodes(999) == []


def test_load_disk_handles_corrupt_json(tmp_path):
    p = tmp_path / "cache" / "sonarr.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    c = SonarrCache(_fake_sonarr(), tmp_path)
    assert c.is_empty()  # falls back gracefully
