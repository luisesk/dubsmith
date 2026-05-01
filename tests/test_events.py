"""EventBus pub/sub semantics."""
import asyncio

import pytest

from src.events import EventBus, format_sse


def test_publish_to_one_subscriber():
    bus = EventBus()
    sid, q = bus.subscribe()
    bus.publish("job", {"id": 1, "state": "done"})
    evt = q.get_nowait()
    assert evt["kind"] == "job"
    assert evt["data"]["id"] == 1


def test_multiple_subscribers_each_receive():
    bus = EventBus()
    s1, q1 = bus.subscribe()
    s2, q2 = bus.subscribe()
    bus.publish("job", {"id": 7})
    e1 = q1.get_nowait()
    e2 = q2.get_nowait()
    assert e1["data"]["id"] == 7
    assert e2["data"]["id"] == 7
    assert s1 != s2


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    sid, q = bus.subscribe()
    bus.unsubscribe(sid)
    bus.publish("job", {"id": 1})
    assert q.empty()


def test_replay_buffer():
    bus = EventBus(history=3)
    bus.publish("a", {"i": 1})
    bus.publish("a", {"i": 2})
    bus.publish("a", {"i": 3})
    bus.publish("a", {"i": 4})
    h = bus.replay()
    assert [e["data"]["i"] for e in h] == [2, 3, 4]


def test_subscriber_count():
    bus = EventBus()
    assert bus.subscriber_count() == 0
    s1, _ = bus.subscribe()
    s2, _ = bus.subscribe()
    assert bus.subscriber_count() == 2
    bus.unsubscribe(s1)
    assert bus.subscriber_count() == 1


def test_full_subscriber_queue_doesnt_block_others():
    """A slow consumer's queue filling up must not stop fast consumers."""
    bus = EventBus()
    # Tiny queue size for the slow one
    s1, slow = bus.subscribe()
    slow._maxsize = 1  # private-ish but works for asyncio.Queue
    s2, fast = bus.subscribe()
    bus.publish("a", {})
    bus.publish("a", {})  # slow's queue fills
    bus.publish("a", {})  # slow drops; fast still gets all 3
    assert fast.qsize() == 3


def test_format_sse_shape():
    s = format_sse({"ts": 1, "kind": "job", "data": {"id": 5}})
    assert s.startswith("event: job\n")
    assert "data: " in s
    assert s.endswith("\n\n")
