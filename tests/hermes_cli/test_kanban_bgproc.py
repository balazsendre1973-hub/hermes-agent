"""A live background process parks the retry instead of spawning a twin — t_639b6d1d.

Measured event (Fin, 2026-11-06 16:00): while ``t_8e9d4fd0``'s ``mem_vec``
backfill ran, the retry worker launched ``timeout 900 ... indexer.py --index``
twice beside the live card; the Fin killed both and the card lost two hours.
The dispatcher must therefore check, *before* a ``crashed``/``timed_out`` ->
``ready`` transition and again before a spawn, whether a process the card's
previous run left behind is still alive — and park ``blocked (transient)``
instead of requeueing.

Two pid marks are read (``hermes_cli/kanban_bgproc``): the ``task_runs``
``worker_pid`` retained on the closed run row, and the ``proc_*`` records of
the process-registry checkpoint.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_bgproc as bg
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

DEAD_PID = 999_999_999  # far beyond pid_max: provably not running


def _checkpoint_file() -> Path:
    """The first path ``kanban_bgproc`` itself reads — agreement by construction."""
    paths = bg._checkpoint_paths()
    assert paths, "the active home must resolve a processes.json slot"
    return paths[0]


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    conn = kbc.connect(tmp_path / "kanban.db")
    bg._CHECKPOINT_CACHE.clear()
    try:
        yield conn
    finally:
        conn.close()
        bg._CHECKPOINT_CACHE.clear()
        _checkpoint_file().unlink(missing_ok=True)


@pytest.fixture
def live_pid() -> int:
    """A pid that is alive, is not this process, and is not the dead worker.

    The dispatcher excludes its own pid from every mark (otherwise every sweep
    would "find" the dispatcher), so the test borrows its parent: guaranteed
    alive for the whole run and never the pid under test.
    """
    from gateway.status import get_process_start_time

    pid = os.getppid()
    assert pid and pid != os.getpid()
    assert get_process_start_time(pid) is not None
    return pid


def _dead_worker_running(conn, *, max_runtime=None) -> str:
    """A ``running`` card whose worker is gone (the crash/timeout pre-state)."""
    tid = kb.create_task(conn, title="backfill", assignee="worker",
                         max_runtime_seconds=max_runtime)
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, DEAD_PID)
    old = int(time.time()) - 3600
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_started_at = ?, started_at = ?, claim_expires = ? WHERE id = ?",
            (1, old, old, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, tid),
        )
    return tid


def _write_checkpoint(task_id: str, pid: int) -> Path:
    """A ``proc_*`` record the card's own footprint points back at.

    The worker's stdout redirect carries the card's workspace path, which is
    how a human (and the gate) ties a background process to a card when the
    registry only knows the spawning session.
    """
    from gateway.status import get_process_start_time

    path = _checkpoint_file()
    path.write_text(json.dumps([{
        "session_id": "proc_000000000001",
        "command": ("cd /home/endre/.hermes/memoria-v2 && python3 indexer.py --index "
                    f"> /home/endre/.hermes/kanban/workspaces/{task_id}/indexer.log"),
        "cwd": "/home/endre/.hermes/memoria-v2",
        "pid": pid,
        "host_start_time": get_process_start_time(pid),
        "task_id": "default",
        "owner_task_id": "session-not-a-card",
    }]), encoding="utf-8")
    bg._CHECKPOINT_CACHE.clear()
    return path


def _drop_checkpoint() -> None:
    _checkpoint_file().unlink(missing_ok=True)
    bg._CHECKPOINT_CACHE.clear()


def _events(conn, tid) -> list[str]:
    return [event.kind for event in kb.list_events(conn, tid)]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_retained_task_runs_pid_is_a_mark(board) -> None:
    """A run row still carrying a live worker pid answers for its card."""
    conn = board
    tid = kb.create_task(conn, title="job", assignee="worker")
    kb.claim_task(conn, tid)  # opens the run row the mark lives on
    kbd._set_worker_pid(conn, tid, os.getppid())
    marks = bg.live_background_processes(conn, tid, exclude_pid=DEAD_PID)
    assert [m["pid"] for m in marks] == [os.getppid()]
    assert marks[0]["source"] == "task_runs"

    # The same mark with a foreign fingerprint is a recycled PID, never ours.
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET worker_started_at = 1 WHERE task_id = ?", (tid,))
    assert bg.live_background_processes(conn, tid) == []


def test_dead_worker_pid_is_not_a_mark(board) -> None:
    tid = _dead_worker_running(board)
    assert bg.live_background_processes(board, tid, exclude_pid=DEAD_PID) == []


def test_checkpoint_record_is_matched_by_the_cards_footprint(board, live_pid) -> None:
    tid = kb.create_task(board, title="job", assignee="worker")
    _write_checkpoint(tid, live_pid)
    marks = bg.live_background_processes(board, tid, exclude_pid=DEAD_PID)
    assert [m["pid"] for m in marks] == [live_pid]
    assert marks[0]["source"] == "process_registry"
    assert marks[0]["session_id"] == "proc_000000000001"


def test_checkpoint_record_of_another_card_is_ignored(board, live_pid) -> None:
    tid = kb.create_task(board, title="job", assignee="worker")
    _write_checkpoint(tid, live_pid)
    other = kb.create_task(board, title="other", assignee="worker")
    assert bg.live_background_processes(board, other, exclude_pid=DEAD_PID) == []


def test_recycled_checkpoint_pid_is_ignored(board, live_pid) -> None:
    """A live number with a foreign start tick is a stranger, not our process."""
    from gateway.status import get_process_start_time

    tid = kb.create_task(board, title="job", assignee="worker")
    path = _write_checkpoint(tid, live_pid)
    records = json.loads(path.read_text(encoding="utf-8"))
    records[0]["host_start_time"] = (get_process_start_time(live_pid) or 0) + 999_999
    path.write_text(json.dumps(records), encoding="utf-8")
    bg._CHECKPOINT_CACHE.clear()
    assert bg.live_background_processes(board, tid, exclude_pid=DEAD_PID) == []


def test_park_writes_the_block_and_the_event(board, live_pid) -> None:
    tid = kb.create_task(board, title="job", assignee="worker")
    _write_checkpoint(tid, live_pid)
    assert bg.park_for_live_background(board, tid, stage="spawn") is True
    task = kb.get_task(board, tid)
    assert task.status == "blocked"
    assert task.block_kind == "transient"
    kinds = _events(board, tid)
    assert "background_proc_alive" in kinds and "blocked" in kinds


def test_no_live_process_leaves_the_card_alone(board) -> None:
    tid = kb.create_task(board, title="job", assignee="worker")
    assert bg.park_for_live_background(board, tid, stage="spawn") is False
    assert kb.get_task(board, tid).status == "ready"


# ---------------------------------------------------------------------------
# Criterion 4 — the retry parks instead of spawning a twin
# ---------------------------------------------------------------------------

def test_crashed_reclaim_parks_blocked_transient_instead_of_ready(board, live_pid) -> None:
    """``crashed`` -> ``ready`` is replaced by ``blocked (transient)``."""
    tid = _dead_worker_running(board)
    _write_checkpoint(tid, live_pid)

    assert kbd.detect_crashed_workers(board) == []  # no requeue, no crash counted
    task = kb.get_task(board, tid)
    assert task.status == "blocked"
    assert task.block_kind == "transient"
    assert task.worker_pid is None and task.claim_lock is None
    assert "background_proc_alive" in _events(board, tid)
    # And the retry the dispatcher would have made cannot even be claimed.
    assert kb.claim_task(board, tid) is None

    # With the process gone the ordinary reclaim path is untouched.
    _drop_checkpoint()
    assert bg.live_background_processes(board, tid, exclude_pid=DEAD_PID) == []


def test_crashed_reclaim_without_a_live_process_is_unchanged(board) -> None:
    tid = _dead_worker_running(board)
    assert kbd.detect_crashed_workers(board) == [tid]
    assert kb.get_task(board, tid).status == "ready"


def test_timed_out_reclaim_parks_blocked_transient(board, live_pid) -> None:
    """``timed_out`` -> ``ready`` is replaced by ``blocked (transient)``."""
    tid = _dead_worker_running(board, max_runtime=1)
    _write_checkpoint(tid, live_pid)
    killed: list[tuple[int, int]] = []

    assert kbd.enforce_max_runtime(board, signal_fn=lambda pid, sig: killed.append((pid, sig))) == []
    task = kb.get_task(board, tid)
    assert task.status == "blocked"
    assert task.block_kind == "transient"
    assert task.worker_pid is None
    kinds = _events(board, tid)
    assert "timed_out" not in kinds and "background_proc_alive" in kinds
    # The kill is attempted before the park (the worker is already gone); the
    # point is that no requeue followed it.
    assert killed and killed[0][0] == DEAD_PID


def test_spawn_is_refused_while_a_background_process_lives(board, live_pid, monkeypatch) -> None:
    """The pre-spawn guard parks the card and never calls ``spawn_fn``."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = kb.create_task(board, title="backfill", assignee="worker")
    _write_checkpoint(tid, live_pid)
    spawned: list[str] = []
    result = kbd.DispatchResult()
    row = board.execute("SELECT id, assignee FROM tasks WHERE id = ?", (tid,)).fetchone()

    assert kbd._dispatch_lane_task(
        board, row, "worker", result, lane="ready", dry_run=False, ttl_seconds=60,
        board=None, failure_limit=3, spawn_fn=lambda *a, **k: spawned.append(a[0].id),
        per_profile_cap=None, per_profile_running={},
    ) is False

    assert spawned == []
    assert result.spawned == []
    assert (tid, "live_background_proc") in result.respawn_guarded
    task = kb.get_task(board, tid)
    assert task.status == "blocked"
    assert task.block_kind == "transient"


def test_dry_run_never_parks(board, live_pid, monkeypatch) -> None:
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = kb.create_task(board, title="backfill", assignee="worker")
    _write_checkpoint(tid, live_pid)
    result = kbd.DispatchResult()
    row = board.execute("SELECT id, assignee FROM tasks WHERE id = ?", (tid,)).fetchone()

    assert kbd._dispatch_lane_task(
        board, row, "worker", result, lane="ready", dry_run=True, ttl_seconds=60,
        board=None, failure_limit=3, spawn_fn=lambda *a, **k: None,
        per_profile_cap=None, per_profile_running={},
    ) is True
    assert result.spawned and result.spawned[0][0] == tid
    assert kb.get_task(board, tid).status == "ready"


# ---------------------------------------------------------------------------
# The helpers that moved with this rule
# ---------------------------------------------------------------------------

def test_survived_termination_and_defer_still_hold_the_claim(board) -> None:
    """``reclaim_deferred`` keeps a card ``running`` while its worker lives."""
    assert bg._worker_survived_termination({
        "host_local": True, "termination_attempted": True, "terminated": False,
    }) is True
    assert bg._worker_survived_termination({"terminated": True}) is False
    assert bg._worker_survived_termination({"host_local": False}) is False

    tid = kb.create_task(board, title="job", assignee="worker")
    kb.claim_task(board, tid)
    claim_lock = kb.get_task(board, tid).claim_lock
    before = int(time.time())
    bg._defer_reclaim_for_live_worker(
        board, tid, claim_lock, before, {"host_local": True}, reason="test_hold",
    )
    task = kb.get_task(board, tid)
    assert task.status == "running"
    assert task.claim_lock == claim_lock
    assert task.claim_expires >= before
    assert "reclaim_deferred" in _events(board, tid)
