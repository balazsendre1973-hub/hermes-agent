"""Dispatched-reviewer close without force (Option A, t_43e2d084).

The review lane CASes the card ``review -> running`` (``claim_review_task``);
the dispatched verifier must be able to close its OWN run with
``complete_task`` WITHOUT ``force=True``. Two fail-closed proofs gate it:

* the run's immutable ``claimed`` event says ``source_status=review``, and
* ``expected_run_id`` matches the card's current run (ownership).

Every other path stays refused: a normal (``ready``) claim, a missing event,
a ``None`` expected_run_id, a wrong run id, and the non-``running`` statuses
(``ready`` / ``triage`` / ``todo`` / ``blocked``). No path re-enables closing
those without review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _dispatched_review_card(conn, title: str = "dispatched review"):
    """Full flow: implementer claims -> review -> dispatched reviewer claims.

    Returns ``(task_id, review_run_id)``. The card ends in ``running`` with a
    ``claimed`` event carrying ``source_status=review`` — the exact state the
    review-lane dispatch produces (kanban_db_dispatch: lane == "review").
    """
    task_id = kb.create_task(conn, title=title, assignee="builder")
    implementation = kb.claim_task(conn, task_id, claimer="builder:1")
    assert implementation is not None
    assert kb.request_review(
        conn, task_id, summary="ready for review", reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    assert kb.get_task(conn, task_id).status == "review"
    # The dispatch's claim step (kanban_db_dispatch: lane == "review").
    review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
    assert review is not None
    assert kb.get_task(conn, task_id).status == "running"  # the CAS that caused the defect
    return task_id, review.current_run_id


def test_dispatched_reviewer_closes_own_run_without_force(conn):
    """THE live proof: dispatch -> claim -> complete (no force) -> done, events read back."""
    task_id, review_run = _dispatched_review_card(conn)

    ok = kb.complete_task(
        conn, task_id,
        summary="Approved after independent verification.",
        result="approved",
        expected_run_id=review_run,
        force=False,  # explicitly NOT force — the defect's fix
    )
    assert ok is True

    task = kb.get_task(conn, task_id)
    assert task.status == "done"

    # Events read back: the reviewer's claim and the completion.
    events = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()]
    assert "claimed" in events
    assert "completed" in events

    # The claimed event carries the review provenance the gate trusted.
    claimed = conn.execute(
        "SELECT payload, run_id FROM task_events "
        "WHERE task_id = ? AND kind = 'claimed' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    payload = json.loads(claimed["payload"])
    assert payload.get("source_status") == "review"
    assert claimed["run_id"] == review_run

    # The reviewer's run was closed with outcome=completed.
    run = conn.execute(
        "SELECT outcome, status FROM task_runs WHERE id = ?", (review_run,)
    ).fetchone()
    assert run["outcome"] == "completed"


def test_normal_ready_claim_still_refuses_direct_close(conn):
    """A non-review claim (source_status=ready) must NOT pass the new path."""
    task_id = kb.create_task(conn, title="normal card", assignee="worker")
    claimed = kb.claim_task(conn, task_id, claimer="worker:1")
    assert claimed is not None
    with pytest.raises(kb.ReviewRequiredError) as exc:
        kb.complete_task(
            conn, task_id, summary="shipped it", expected_run_id=claimed.current_run_id,
        )
    assert f"review required for {task_id}" in str(exc.value)
    assert kb.get_task(conn, task_id).status == "running"


def test_dispatched_review_close_requires_expected_run_id(conn):
    """expected_run_id=None (no ownership proof) refuses even with review provenance."""
    task_id, _review_run = _dispatched_review_card(conn)
    with pytest.raises(kb.ReviewRequiredError) as exc:
        kb.complete_task(conn, task_id, summary="no ownership proof")
    assert f"review required for {task_id}" in str(exc.value)
    assert kb.get_task(conn, task_id).status == "running"


def test_dispatched_review_close_requires_matching_run(conn):
    """A wrong expected_run_id (not the card's current run) refuses."""
    task_id, review_run = _dispatched_review_card(conn)
    with pytest.raises(kb.ReviewRequiredError):
        kb.complete_task(conn, task_id, summary="wrong run", expected_run_id=review_run + 999)
    assert kb.get_task(conn, task_id).status == "running"


def test_dispatched_review_close_fails_closed_on_missing_claimed_event(conn):
    """Missing claimed event => refuse (fail-closed)."""
    task_id, review_run = _dispatched_review_card(conn)
    with kb.write_txn(conn):
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind = 'claimed' AND run_id = ?",
            (task_id, review_run),
        )
    with pytest.raises(kb.ReviewRequiredError):
        kb.complete_task(conn, task_id, summary="missing event", expected_run_id=review_run)
    assert kb.get_task(conn, task_id).status == "running"


def test_non_running_statuses_still_refuse(conn):
    """ready / triage / blocked still refuse (no path re-enabled without review)."""
    ready_id = kb.create_task(conn, title="ready", assignee="w")
    with pytest.raises(kb.ReviewRequiredError):
        kb.complete_task(conn, ready_id, summary="x", expected_run_id=12345)
    assert kb.get_task(conn, ready_id).status == "ready"

    triage_id = kb.create_task(conn, title="triage", assignee="w")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (triage_id,))
    with pytest.raises(kb.ReviewRequiredError):
        kb.complete_task(conn, triage_id, summary="x", expected_run_id=12345)
    assert kb.get_task(conn, triage_id).status == "triage"

    blocked_id = kb.create_task(conn, title="blocked", assignee="w")
    kb.block_task(conn, blocked_id, reason="waiting", kind="needs_input")
    with pytest.raises(kb.ReviewRequiredError):
        kb.complete_task(conn, blocked_id, summary="x", expected_run_id=12345)
    assert kb.get_task(conn, blocked_id).status == "blocked"


def test_review_status_close_still_works_without_force(conn):
    """Regression: the normal review -> done path (no running involved) is unchanged."""
    task_id = kb.create_task(conn, title="manual", assignee="reviewer")
    assert kb.request_review(conn, task_id, summary="handoff")
    assert kb.complete_task(conn, task_id, summary="LGTM") is True
    assert kb.get_task(conn, task_id).status == "done"


def test_force_still_works_for_operator_override(conn):
    """force=True remains the operator escape hatch (unchanged semantics)."""
    task_id = kb.create_task(conn, title="cancel", assignee="worker")
    assert kb.complete_task(conn, task_id, result="cancelled", force=True) is True
    assert kb.get_task(conn, task_id).status == "done"
