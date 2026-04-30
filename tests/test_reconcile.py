"""Reconcile drops tracked shows whose Sonarr id no longer exists."""
from unittest.mock import MagicMock

from src import reconcile
from src.queue import Queue
from src.shows import ShowsStore


def test_reconcile_removes_missing(tmp_path):
    sonarr = MagicMock()
    sonarr.all_series.return_value = [{"id": 1}, {"id": 2}]

    shows = ShowsStore(tmp_path / "s.yml")
    shows.upsert(1, name="A", cr_seasons={"1": "X"})
    shows.upsert(2, name="B", cr_seasons={"1": "Y"})
    shows.upsert(99, name="ZOMBIE", cr_seasons={"1": "Z"})

    queue = Queue(tmp_path / "q.db")
    queue.upsert_pending(99, 1, 1, "/v.mkv")
    queue.upsert_pending(99, 1, 2, "/v.mkv")
    queue.upsert_pending(1, 1, 1, "/a.mkv")

    out = reconcile.run(sonarr, shows, queue)
    assert out["error"] is None
    assert out["checked"] == 3
    assert len(out["removed"]) == 1
    assert out["removed"][0]["series_id"] == 99
    assert out["removed"][0]["name"] == "ZOMBIE"
    assert out["queue_cleared"] == 2
    assert out["removed"][0]["queue_cleared"] == 2

    # show gone, but other shows + queue jobs untouched
    assert shows.get(99) is None
    assert shows.get(1) is not None
    assert queue.by_series(1, 1, 1) is not None


def test_reconcile_keeps_done_jobs_for_history(tmp_path):
    sonarr = MagicMock()
    sonarr.all_series.return_value = []

    shows = ShowsStore(tmp_path / "s.yml")
    shows.upsert(7, name="GONE", cr_seasons={"1": "X"})

    queue = Queue(tmp_path / "q.db")
    jid_pending = queue.upsert_pending(7, 1, 1, "/v.mkv")
    jid_done = queue.upsert_pending(7, 1, 2, "/v.mkv")
    queue.set_state(jid_done, "done")

    out = reconcile.run(sonarr, shows, queue)
    assert out["queue_cleared"] == 1  # only pending nuked
    assert queue.get(jid_pending) is None
    assert queue.get(jid_done) is not None  # done preserved


def test_reconcile_handles_sonarr_unreachable(tmp_path):
    sonarr = MagicMock()
    sonarr.all_series.side_effect = ConnectionError("nope")
    shows = ShowsStore(tmp_path / "s.yml")
    shows.upsert(1, name="X", cr_seasons={"1": "Y"})
    queue = Queue(tmp_path / "q.db")
    out = reconcile.run(sonarr, shows, queue)
    assert "sonarr unreachable" in out["error"]
    # No mutation when sonarr is down — don't risk false-positive deletes
    assert shows.get(1) is not None


def test_reconcile_noop_when_nothing_stale(tmp_path):
    sonarr = MagicMock()
    sonarr.all_series.return_value = [{"id": 1}, {"id": 2}]
    shows = ShowsStore(tmp_path / "s.yml")
    shows.upsert(1, name="A", cr_seasons={})
    shows.upsert(2, name="B", cr_seasons={})
    queue = Queue(tmp_path / "q.db")
    out = reconcile.run(sonarr, shows, queue)
    assert out["removed"] == []
    assert out["queue_cleared"] == 0
