"""A gateway-drain owner refusal must not spend the card's failure budget.

While ``hermes update`` drained the gateway (2026-11-06) every freshly spawned worker
died at the owner handshake: ``kanban_worker_client._submit`` printed
``Kanban owner refused: Kanban owner silent for 192s: Gateway draining: restart_wait``
and exited 1, the dispatcher booked that as a *task* failure, and the breaker tripped on
a card whose own work never ran — 4 killed spawns, 2 false ``gave_up``/``blocked`` and a
false stall card in ~75 minutes (board ``btcusd-jev``, card ``t_6391481f``).

Pinned here:

* the host-side owner-handshake refusals (gateway/runtime drain, or the owner staying
  silent past ``OWNER_WAIT_S``) are recorded as ``infrastructure`` — no
  ``consecutive_failures``, no ``blocked``, however often they repeat;
* a plain nonzero exit still counts and still blocks at the limit;
* an owner refusal whose detail does not name the host (a final answer: body rejection,
  ``Gateway disconnected``, ``execution unknown``) still counts — only the forms that
  prove the HOST ended the attempt are exempted.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

# Verbatim from the incident's ``worker_output`` payloads (task_events 11:50:06 /
# 12:32:34 on btcusd-jev): the worker's whole output before its exit trailer.
DRAIN_OUTPUT = (
    "Session: local-dd016ce7efc55a11eb83a619af3a1e6c49ffeff7f6c191bbe05f65bfd3af708c\n"
    "Kanban owner refused: Kanban owner silent for 192s: Gateway draining: restart_wait\n"
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def _dead_worker(conn, tid: str, pid: int, output: str, rc: int = 1) -> None:
    """Claim ``tid`` for a worker that already exited ``rc`` with ``output`` in its log.

    The log is append-mode across attempts, exactly like production: each sweep reads
    the tail the newest attempt left behind.
    """
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
        (pid, int(time.time()) - 120, tid),
    )
    conn.commit()
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{output}{KANBAN_WORKER_EXIT_TRAILER}{rc}\n")


def test_drain_refusal_is_infrastructure_and_never_blocks(kanban_home):
    """The drain signature costs nothing: no counter, no ``blocked``, any number of times."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="drained", assignee="a")

        for attempt in range(4):
            _dead_worker(conn, tid, 72000 + attempt, DRAIN_OUTPUT)
            assert kbd.detect_crashed_workers(conn) == [tid]
            assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", []) == []
            task = kb.get_task(conn, tid)
            assert task.status == "ready", f"attempt {attempt} parked the card"
            assert task.consecutive_failures == 0, f"attempt {attempt} spent the budget"

        # The attempt is still on the board, tagged as infrastructure so the history
        # shows WHY nothing was charged.
        run = conn.execute(
            "SELECT outcome, metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert run["outcome"] == "crashed"
        assert kb._json_dict(run["metadata"]).get("infrastructure") is True

        event = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert event["kind"] == "crashed"
        assert kb._json_dict(event["payload"]).get("infrastructure") is True

        # ... and the blocker stays stamped for check_respawn_guard to space retries.
        assert "Gateway draining: restart_wait" in (kb.get_task(conn, tid).last_failure_error or "")


def test_plain_nonzero_exit_still_counts_and_blocks(kanban_home):
    """Control: a genuine crash spends the budget and trips at DEFAULT_FAILURE_LIMIT."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="genuine", assignee="a")

        # Spend the budget one crash at a time: ready until the limit, blocked at it.
        for attempt in range(kbd.DEFAULT_FAILURE_LIMIT - 1):
            _dead_worker(conn, tid, 73000 + attempt, f"Traceback (most recent call last):\n  boom {attempt}\n")
            kbd.detect_crashed_workers(conn)
            task = kb.get_task(conn, tid)
            assert (task.status, task.consecutive_failures) == ("ready", attempt + 1)

        _dead_worker(conn, tid, 73999, "Traceback (most recent call last):\n  final boom\n")
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert (task.status, task.consecutive_failures) == ("blocked", kbd.DEFAULT_FAILURE_LIMIT)
        assert tid in getattr(kbd.detect_crashed_workers, "_last_auto_blocked", [])


def test_owner_refusal_without_drain_detail_still_counts(kanban_home):
    """A FINAL owner answer (``Kanban owner refused:`` + a card-side reason) is not infra.

    ``_submit`` prints that same prefix for every refusal it catches, drain or not, so
    the host marker is what separates the two — without it the exit keeps counting.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="refused", assignee="a")

        for attempt in range(kbd.DEFAULT_FAILURE_LIMIT):
            _dead_worker(
                conn, tid, 74000 + attempt,
                "Kanban owner refused: invalid_request: task body rejected\n",
            )
            kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == kbd.DEFAULT_FAILURE_LIMIT
        assert task.status == "blocked"


def test_a_later_crash_does_not_inherit_an_earlier_attempts_drain_refusal(kanban_home):
    """The per-task log is append-only: attempt N-1 died at the handshake, attempt N
    crashed for its own reason and printed its own admission line. The new crash must
    still spend the budget — only the LAST attempt's words may decide."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale tail", assignee="a")

        _dead_worker(conn, tid, 75000, DRAIN_OUTPUT)
        kbd.detect_crashed_workers(conn)
        first = kb.get_task(conn, tid)
        assert first is not None and first.consecutive_failures == 0

        _dead_worker(
            conn, tid, 75001,
            "Session: local-5a0920bdaa1a7bba68cb9e5b398ab83f024e3add925a5b8f798440defaa64e30\n"
            "Provider said: HTTP 404 — OpenCode | Not Found\n",
        )
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.consecutive_failures == 1
        assert task.status == "ready"


def test_a_drain_refusal_after_an_unblock_stays_ready_and_uncharged(kanban_home):
    """The incident's sequence: the breaker trips, the card is unblocked (a deliberate
    unblock resets the retry budget), then the next spawn dies in the drain. The card
    must come back ``ready`` with nothing charged and the blocker stamped — not trip
    straight back to ``blocked`` on a refusal it never caused."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="at limit", assignee="a")

        for attempt in range(kbd.DEFAULT_FAILURE_LIMIT):
            _dead_worker(conn, tid, 76000 + attempt, "Traceback (most recent call last):\n  boom\n")
            kbd.detect_crashed_workers(conn)
        at_limit = kb.get_task(conn, tid)
        assert at_limit is not None
        assert (at_limit.status, at_limit.consecutive_failures) == ("blocked", kbd.DEFAULT_FAILURE_LIMIT)

        kb.unblock_task(conn, tid)
        unblocked = kb.get_task(conn, tid)
        assert unblocked is not None
        assert (unblocked.status, unblocked.consecutive_failures) == ("ready", 0)

        _dead_worker(conn, tid, 76002, DRAIN_OUTPUT)
        kbd.detect_crashed_workers(conn)
        after = kb.get_task(conn, tid)
        assert after is not None
        assert after.status == "ready"
        assert after.consecutive_failures == 0  # unchanged: no charge, no trip
        assert "Gateway draining: restart_wait" in (after.last_failure_error or "")


@pytest.mark.parametrize(
    "output, expected",
    [
        ("Kanban owner refused: Kanban owner silent for 192s: Gateway draining: restart_wait", True),
        ("Kanban owner refused: Gateway draining (update_paused)", True),
        ("Kanban owner silent for 185s: restart_wait", True),
        # Truncated tail of a longer log — the signature need not start the window.
        ("wner refused: Kanban owner silent for 187s: Gateway draining: restart_wait", True),
        # The runtime's own transient drain codes: the 2026-11-01 runtime drain tripped
        # the breaker on three healthy cards (btcusd-jev gave_up 02:02:04 / 02:06:07).
        ("Kanban owner refused: runtime_draining", True),
        # Silence of the owner is itself the diagnosis: _await_receipt only raises it
        # for a restartable host/transport exception.
        ("Kanban owner silent for 187s: TimeoutError", True),
        # Final owner answers / ordinary failures: never exempted.
        ("Kanban owner refused: invalid_request: task body rejected", False),
        ("Kanban owner refused: unauthorized", False),
        ("Kanban owner refused: Gateway disconnected; turn outcome is unknown. "
         "Resume the printed session ID to check whether it completed or was interrupted; "
         "do not resend the work.", False),
        ("Kanban owner refused: Kanban execution unknown; retained by owner", False),
        # A stale refusal from an EARLIER attempt must not cover a later crash: the
        # per-task log is append-only, so the window can open on the old refusal and
        # close on the new attempt's own output (its ``Session:`` line included).
        ("Kanban owner refused: Kanban owner silent for 192s: Gateway draining: restart_wait "
         "Session: local-cf969d5ab8748ebfa5401870a13b87582fee0ac756e8a5840ca764cfbd7dd0c7 "
         "Provider said: HTTP 404 — OpenCode | Not Found", False),
        ("pid 70001 exited with code 1 Worker's last output: 'boom'", False),
        ("", False),
    ],
)
def test_is_owner_drain_refusal_matches_only_the_host_signature(output, expected):
    assert kbd._is_owner_drain_refusal(output) is expected
