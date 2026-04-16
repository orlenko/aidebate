"""Unit tests for the priority-aware EventBuffer used by the SSE layer."""

from __future__ import annotations

import queue
import threading
import time

import pytest

from aidebate.web.server import EventBuffer


def test_fifo_order_under_capacity() -> None:
    buf = EventBuffer(maxsize=8)
    for i in range(5):
        buf.put({"type": "pane", "i": i})
    for i in range(5):
        assert buf.get(timeout=0.1)["i"] == i


def test_overflow_drops_oldest_nonpriority() -> None:
    buf = EventBuffer(maxsize=3)
    buf.put({"type": "pane", "i": 0})
    buf.put({"type": "pane", "i": 1})
    buf.put({"type": "pane", "i": 2})
    buf.put({"type": "pane", "i": 3})  # overflow — oldest (i=0) gets dropped
    out = [buf.get(timeout=0.1) for _ in range(3)]
    assert [e["i"] for e in out] == [1, 2, 3]


def test_priority_event_not_dropped_under_pressure() -> None:
    buf = EventBuffer(maxsize=3)
    buf.put({"type": "pane", "i": 0})
    buf.put({"type": "verdict", "content": "VERDICT"})
    buf.put({"type": "pane", "i": 1})
    buf.put({"type": "pane", "i": 2})  # overflow — oldest pane (i=0) drops
    out = [buf.get(timeout=0.1) for _ in range(3)]
    types = [e["type"] for e in out]
    assert types == ["verdict", "pane", "pane"]


def test_priority_event_survives_even_mid_queue() -> None:
    """Verdict in the middle of pane events must survive an overflow."""
    buf = EventBuffer(maxsize=4)
    buf.put({"type": "pane", "i": 0})
    buf.put({"type": "pane", "i": 1})
    buf.put({"type": "verdict"})
    buf.put({"type": "pane", "i": 2})
    buf.put({"type": "pane", "i": 3})  # overflow — oldest non-priority drops
    out = [buf.get(timeout=0.1) for _ in range(4)]
    assert any(e.get("type") == "verdict" for e in out)
    # Pane i=0 was the oldest non-priority and should be gone.
    assert not any(e.get("i") == 0 for e in out if e.get("type") == "pane")


def test_all_priority_queue_overflows_gracefully() -> None:
    """If every slot is priority, accept the new event rather than losing one."""
    buf = EventBuffer(maxsize=2)
    buf.put({"type": "verdict"})
    buf.put({"type": "roast"})
    buf.put({"type": "status", "status": "done"})  # no evictable slot — append anyway
    out = [buf.get(timeout=0.1) for _ in range(3)]
    assert [e["type"] for e in out] == ["verdict", "roast", "status"]


def test_get_times_out_cleanly() -> None:
    buf = EventBuffer(maxsize=4)
    t0 = time.monotonic()
    with pytest.raises(queue.Empty):
        buf.get(timeout=0.05)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5  # didn't hang


def test_producer_wakes_waiting_consumer() -> None:
    buf = EventBuffer(maxsize=4)
    received: list[dict] = []

    def consume() -> None:
        received.append(buf.get(timeout=1.0))

    t = threading.Thread(target=consume)
    t.start()
    time.sleep(0.02)  # let the consumer block on the condition
    buf.put({"type": "verdict", "content": "hi"})
    t.join(timeout=1.0)
    assert received == [{"type": "verdict", "content": "hi"}]
