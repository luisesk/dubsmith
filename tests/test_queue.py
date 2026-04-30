"""Tests for the SQLite job queue."""
import tempfile
from pathlib import Path

import pytest

from src.queue import Queue


@pytest.fixture
def q(tmp_path):
    return Queue(tmp_path / "test.db")


def test_upsert_and_claim(q):
    jid = q.upsert_pending(1, 1, 1, "/path/a.mkv")
    assert jid > 0
    j = q.claim_next()
    assert j is not None
    assert j.id == jid
    assert j.state == "downloading"
    assert j.attempts == 1


def test_claim_returns_none_when_empty(q):
    assert q.claim_next() is None


def test_upsert_dedupes(q):
    a = q.upsert_pending(7, 1, 5, "/x.mkv")
    b = q.upsert_pending(7, 1, 5, "/x.mkv")
    assert a == b


def test_state_transitions(q):
    jid = q.upsert_pending(2, 3, 4, "/p.mkv")
    q.claim_next()
    q.set_state(jid, "syncing")
    assert q.get(jid).state == "syncing"
    q.set_state(jid, "muxing")
    q.set_state(jid, "done")
    assert q.get(jid).completed_at is not None


def test_invalid_state_rejected(q):
    jid = q.upsert_pending(1, 1, 1, "/a")
    with pytest.raises(ValueError):
        q.set_state(jid, "exploded")


def test_reset_stale_running(q):
    jid = q.upsert_pending(5, 1, 1, "/a.mkv")
    q.claim_next()  # downloading
    n = q.reset_stale_running()
    assert n == 1
    assert q.get(jid).state == "pending"


def test_retry_failed_under_cap(q):
    jid = q.upsert_pending(9, 1, 1, "/a.mkv")
    q.claim_next()
    q.set_state(jid, "failed", last_error="boom")
    assert q.retry_failed(max_attempts=5) == 1
    assert q.get(jid).state == "pending"


def test_retry_failed_respects_cap(q):
    jid = q.upsert_pending(9, 1, 1, "/a.mkv")
    # cycle: claim→fail→retry to bump attempts past cap
    for _ in range(3):
        q.claim_next()
        q.set_state(jid, "failed")
        q.retry_failed(max_attempts=999)  # ensure back to pending each round
    q.claim_next()
    q.set_state(jid, "failed")
    # attempts now ≥ 4; cap of 2 must reject
    assert q.retry_failed(max_attempts=2) == 0


def test_progress_clamps(q):
    jid = q.upsert_pending(1, 1, 1, "/a")
    q.update_progress(jid, progress=2.0)
    assert q.get(jid).progress == 1.0
    q.update_progress(jid, progress=-1)  # indeterminate sentinel
    assert q.get(jid).progress == -1.0


def test_stats_groupings(q):
    q.upsert_pending(1, 1, 1, "/a")
    q.upsert_pending(1, 1, 2, "/b")
    s = q.stats()
    assert s.get("pending", 0) == 2
    per = q.stats_per_series()
    assert any(r["series_id"] == 1 for r in per)


def test_delete_where(q):
    q.upsert_pending(1, 1, 1, "/a")
    q.upsert_pending(1, 1, 2, "/b")
    n = q.delete_where(state="pending")
    assert n == 2
