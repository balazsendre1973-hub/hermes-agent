"""Review-gate contract for ``complete_task`` (kanban task t_67ccdc03, option B).

Closure may happen ONLY from ``status == 'review'`` (verifier approval) or via
``force=True``. Every other status (ready/running/blocked/triage/todo/scheduled)
refuses with a named, actionable error instead of closing the card.

Deviations from the old contract are intentional: workers hand off with
``kanban_request_review`` and the verifier closes with ``kanban_complete``;
there is no self-completion of a running card anymore.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _refusal_message(exc: Exception) -> str:
    return str(exc)


def test_ready_task_refuses_direct_complete_with_named_message(conn):
    """A never-claimed ``ready`` card cannot be completed directly anymore."""
    tid = kb.create_task(conn, title="ready card", assignee="worker")
    assert kb.get_task(conn, tid).status == "ready"

    with pytest.raises(kb.ReviewRequiredError) as exc:
        kb.complete_task(conn, tid, summary="shipped it")

    msg = _refusal_message(exc.value)
    assert "kanban_complete refused" in msg
    assert f"review required for {tid}" in msg
    assert "(status=ready)" in msg
    assert "kanban_request_review" in msg
    assert "--force" in msg

    # Nothing changed: no completed event, status untouched.
    assert kb.get_task(conn, tid).status == "ready"
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
    ).fetchall()]
    assert "completed" not in kinds


@pytest.mark.parametrize("status", ["running", "blocked", "triage", "todo"])
def test_non_review_statuses_refuse_complete(conn, status: str):
    """running / blocked / triage / todo all hit the review gate."""
    tid = kb.create_task(conn, title=f"{status} card", assignee="worker")
    if status == "running":
        kb.claim_task(conn, tid)
    elif status == "blocked":
        kb.block_task(conn, tid, reason="waiting", kind="needs_input")
    elif status == "triage":
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        conn.commit()
    elif status == "todo":
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
        conn.commit()

    assert kb.get_task(conn, tid).status == status
    with pytest.raises(kb.ReviewRequiredError) as exc:
        kb.complete_task(conn, tid, summary="shipped it")
    assert f"(status={status})" in str(exc.value)
    assert kb.get_task(conn, tid).status == status


def test_review_approval_closes_to_done(conn):
    """``review`` -> ``done`` via complete_task is the single sanctioned path."""
    tid = kb.create_task(conn, title="approve me", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    assert kb.request_review(
        conn, tid, summary="implementation handoff",
        expected_run_id=claimed.current_run_id,
    )
    assert kb.get_task(conn, tid).status == "review"

    ok = kb.complete_task(conn, tid, summary="LGTM — merged", result="approved")
    assert ok is True
    task = kb.get_task(conn, tid)
    assert task.status == "done"
    events = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
    ).fetchall()]
    assert "completed" in events


def test_review_approval_without_evidence_still_closes(conn):
    """A human review approval needs no result/summary evidence (exempt from the
    empty-completion gate)."""
    tid = kb.create_task(conn, title="manual approval", assignee="reviewer")
    assert kb.request_review(conn, tid, summary="implementation handoff")
    assert kb.complete_task(conn, tid) is True
    assert kb.get_task(conn, tid).status == "done"


def test_force_bypasses_review_gate_for_cancellation(conn):
    """``force=True`` is the operator escape hatch (cancellations / reversals):
    it closes a ``ready`` card without a review."""
    tid = kb.create_task(conn, title="cancel me", assignee="worker")
    assert kb.complete_task(conn, tid, result="cancelled", force=True) is True
    assert kb.get_task(conn, tid).status == "done"


def test_force_bypasses_review_gate_on_live_run(conn):
    """``force=True`` also keeps overriding the live-claim fence (unchanged
    semantics, now additionally bypassing the review gate)."""
    import os

    import hermes_cli.kanban_db_dispatch as kbd

    tid = kb.create_task(conn, title="live card", assignee="coder")
    claimed = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    assert claimed is not None
    kbd._set_worker_pid(conn, tid, os.getpid())
    assert kb.complete_task(
        conn, tid, result="operator override", force=True,
    ) is True
    run = conn.execute(
        "SELECT ended_at, outcome FROM task_runs WHERE id = ?", (claimed.current_run_id,)
    ).fetchone()
    assert run["ended_at"] is not None and run["outcome"] == "completed"


def test_review_required_error_is_a_value_error(conn):
    """Tool/CLI handlers treat the refusal as recoverable (ValueError family)."""
    tid = kb.create_task(conn, title="x", assignee="worker")
    with pytest.raises(kb.ReviewRequiredError):
        kb.complete_task(conn, tid, summary="nope")
    assert issubclass(kb.ReviewRequiredError, ValueError)
