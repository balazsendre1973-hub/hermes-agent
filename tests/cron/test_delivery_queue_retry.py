"""Transient delivery failures re-queue with backoff instead of terminalizing ``failed``.

Covers the wiring of ``_transient_backoff`` / ``retry_attempts`` / ``next_retry_at``
(deliveries.db columns exist; before this card the helper was dead code, so every
429/DNS failure went terminal with no retry entry — card t_7dd92782).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

import cron.delivery_queue as queue

CET = timezone(timedelta(hours=1))
T0 = datetime(2026, 11, 13, 12, 0, 0, tzinfo=CET)

# The exact error shapes the production send path produced (deliveries.db rows
# 2026-11-07 18:32 429 / 2026-11-11 13:22 DNS):
ERROR_429 = (
    'Telegram API HTTP 429: {"ok": false, "error_code": 429, "description": '
    '"Too Many Requests: retry after 10", "parameters": {"retry_after": 10}} '
    "(target log:8832329921)"
)
ERROR_DNS = (
    "Telegram send failed: <urlopen error [Errno -3] Temporary failure in name "
    "resolution> (target log:8832329921)"
)


@pytest.fixture
def clock(monkeypatch):
    """Freeze ``_hermes_now`` so ``next_retry_at`` math is deterministic."""
    state = {"now": T0}
    monkeypatch.setattr(queue, "_hermes_now", lambda: state["now"])
    return state


def _advance(state, seconds: float) -> None:
    state["now"] = state["now"] + timedelta(seconds=seconds)


def test_429_requeues_with_retry_after_backoff_then_delivers(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-429", {"id": "job-429"}, "brief")
    failing = Mock(return_value=ERROR_429)

    # Attempt 1 fails with the 429 -> NOT terminalized, retry scheduled in retry_after seconds.
    assert queue.drain(failing) == 1
    row = queue.get_status("exec-429")
    assert row["status"] == "pending"
    assert row["retry_attempts"] == 1
    assert row["next_retry_at"] == (T0 + timedelta(seconds=10)).isoformat()
    assert "429" in row["error"]

    # Inside the backoff window nothing may be claimed (no hot-loop resend).
    good = Mock(return_value=None)
    assert queue.drain(good) == 0
    good.assert_not_called()

    # After the window the same row is re-claimed and delivered (settle happens once).
    _advance(clock, 10)
    assert queue.drain(good) == 1
    good.assert_called_once_with({"id": "job-429"}, "brief", False)
    row = queue.get_status("exec-429")
    assert row["status"] == "delivered"
    assert row["retry_attempts"] == 1
    assert row["next_retry_at"] == (T0 + timedelta(seconds=10)).isoformat()


def test_dns_failure_backs_off_exponentially_and_stops_at_max(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-dns", {"id": "job-dns"}, "brief")
    failing = Mock(return_value=ERROR_DNS)

    expected_delays = [5.0, 10.0, 20.0, 40.0, 80.0]
    for attempt, delay in enumerate(expected_delays, start=1):
        assert queue.drain(failing) == 1
        row = queue.get_status("exec-dns")
        assert row["status"] == "pending", f"attempt {attempt} must still be retryable"
        assert row["retry_attempts"] == attempt
        assert row["next_retry_at"] == (clock["now"] + timedelta(seconds=delay)).isoformat()
        _advance(clock, delay)

    # 6th failure: the 6th send was retry #6 > MAX_DELIVERY_RETRY_ATTEMPTS -> terminal.
    assert queue.drain(failing) == 1
    row = queue.get_status("exec-dns")
    assert row["status"] == "failed"
    assert row["retry_attempts"] == 5
    assert "name resolution" in row["error"]


def test_permanent_error_fails_closed_without_any_retry(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-cfg", {"id": "job-cfg"}, "brief")
    send = Mock(return_value="unknown platform 'operator-chat'")

    assert queue.drain(send) == 1
    row = queue.get_status("exec-cfg")
    assert row["status"] == "failed"
    assert row["retry_attempts"] == 0
    assert row["next_retry_at"] is None
    # and it is never re-claimed
    assert queue.drain(send) == 0
    send.assert_called_once()


def test_waiting_retry_does_not_block_other_pending_rows(tmp_path, monkeypatch, clock):
    """A row in backoff must not starve newer deliveries (filter, not head-of-line)."""
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-a-old", {"id": "job-a"}, "first")   # oldest: fails first
    failing = Mock(return_value=ERROR_429)
    assert queue.drain(failing) == 1                        # exec-a-old now waiting +10s
    assert queue.get_status("exec-a-old")["status"] == "pending"

    queue.enqueue("exec-b-new", {"id": "job-b"}, "second")
    good = Mock(return_value=None)
    assert queue.drain(good) == 1                           # exec-b-new flows immediately
    good.assert_called_once_with({"id": "job-b"}, "second", False)
    assert queue.get_status("exec-b-new")["status"] == "delivered"
    assert queue.get_status("exec-a-old")["status"] == "pending"

    # ... and exec-a-old still delivers once its window opens.
    _advance(clock, 10)
    assert queue.drain(good) == 1
    assert queue.get_status("exec-a-old")["status"] == "delivered"


def test_success_never_touches_retry_columns(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(queue, "DELIVERY_DB", tmp_path / "deliveries.db")
    queue.enqueue("exec-ok", {"id": "job-ok"}, "brief")
    assert queue.drain(Mock(return_value=None)) == 1
    row = queue.get_status("exec-ok")
    assert row["status"] == "delivered"
    assert row["retry_attempts"] == 0
    assert row["next_retry_at"] is None
