"""REGRESSION (t_34f3135c): a quota-flavoured ``last_failure_error`` stamp that
was superseded by a LATER, NON-FAILURE run must not park the card in
``blocker_auth`` forever.

The defect: the rate-limited requeue stamps
``pid N exited rate-limited (quota wall) — requeued without counting a failure``
into ``tasks.last_failure_error``. When the worker afterwards completed its
handoff (``outcome='review_requested'``), that stamp stayed behind: the newer
run wrote no error of its own, so the stale quota text survived and
``check_respawn_guard`` matched ``_RESPAWN_BLOCKER_RE`` against it — with
``latest_outcome='review_requested'`` the ``rate_limit_cooldown`` escape does
not apply and the ``crashed`` exemption does not apply either, so the card was
parked as ``blocker_auth`` forever. The whole review lane (6/6 cards) became
unspawnable.

The invariant this test encodes: the guard must judge the CURRENT failure
state, not a stale text left behind by a run that has since been superseded by
a non-failure outcome.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (same shape as test_kanban_db.py)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


QUOTA_STAMP = (
    "pid 1272817 exited rate-limited (quota wall) — requeued without counting "
    "a failure [owner admission settled failed]"
)


def _seed_review_lane(conn, tid):
    """Put a card in the review lane with a quota stamp and a review_requested run.

    Mirrors the live production shape exactly:
      * run#1: ``outcome='rate_limited'`` with the quota text as its error
      * run#2: ``outcome='review_requested'`` with NO error (the handoff wrote
        no failure of its own) -> the quota stamp on the task row is now stale
      * task row: ``status='review'``, ``last_failure_error`` = the quota text
        from run#1, ``consecutive_failures=0``
    """
    kb.claim_task(conn, tid)
    rl_run_id = kb.get_task(conn, tid).current_run_id
    conn.execute(
        "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
        "ended_at=?, error=? WHERE id=?",
        (5_000_000, QUOTA_STAMP, rl_run_id),
    )
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
        (QUOTA_STAMP, tid),
    )
    conn.commit()

    # The implementer then handed the card off for review: a NEW run whose
    # outcome is 'review_requested' and whose error is NULL. The stale quota
    # stamp is never overwritten.
    kb.claim_task(conn, tid)
    rr_run_id = kb.get_task(conn, tid).current_run_id
    conn.execute(
        "UPDATE task_runs SET outcome='review_requested', status='review', "
        "ended_at=?, error=NULL WHERE id=?",
        (5_000_100, rr_run_id),
    )
    conn.execute(
        "UPDATE tasks SET status='review', current_run_id=NULL, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL WHERE id=?",
        (tid,),
    )
    conn.commit()
    return tid


def test_review_lane_quota_stamp_superseded_by_review_requested_respawns(kanban_home):
    """The review lane must not be parked by a STALE quota stamp.

    ``check_respawn_guard`` may only read the stamp as a live blocker while it
    still describes the CURRENT run. Once a later, non-failure run has
    superseded it, the guard must let the card spawn.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review-lane card", assignee="verifier")
        _seed_review_lane(conn, tid)

        row = conn.execute(
            "SELECT status, last_failure_error, consecutive_failures FROM tasks WHERE id=?",
            (tid,),
        ).fetchone()
        # Precondition: the stale stamp is still on the row.
        assert row["status"] == "review"
        assert row["last_failure_error"] == QUOTA_STAMP
        assert row["consecutive_failures"] == 0

        assert kbd.check_respawn_guard(conn, tid, lane="review") is None


def test_ready_lane_quota_stamp_superseded_by_review_requested_respawns(kanban_home):
    """Same stale stamp on the READY lane must not park the card either."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ready-lane card", assignee="coder")
        _seed_review_lane(conn, tid)
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None


def test_genuine_auth_wall_still_returns_blocker_auth(kanban_home):
    """A LIVE auth wall (the stamp still describes the current run) stays guarded.

    This is the constraint from the card: dropping ``blocker_auth`` entirely
    would let a real 401/403/quota wall burn worker slots every tick. The
    shape is the production one: the spawn itself failed with the auth error,
    so the run's OWN error is the auth text and the stamp repeats it.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="real auth wall", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='spawn_failed', status='failed', ended_at=?, "
            "error='401 auth failed' WHERE id=?",
            (5_000_000, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error='401 auth failed' WHERE id=?",
            (tid,),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid, lane="ready") == "blocker_auth"


def test_genuine_quota_wall_still_returns_blocker_auth(kanban_home):
    """The quota text that IS the current run's error must stay guarded.

    The fix must key off the stamp's FRESHNESS, not off the presence of quota
    words: a reclaim that failed on a quota wall is a real wall.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="real quota wall", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='reclaimed', status='reclaimed', ended_at=?, "
            "error='429 quota exceeded for this account' WHERE id=?",
            (5_000_000, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error='429 quota exceeded for this account' WHERE id=?",
            (tid,),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid, lane="ready") == "blocker_auth"


def test_crash_outcome_keeps_its_existing_exemption(kanban_home):
    """A crash's captured output stays context, not a diagnosis (#117097).

    Unchanged by this fix: the ``crashed`` outcome was already exempt from
    ``blocker_auth`` before the stale-stamp hole was closed.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="crashed card", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='crashed', status='failed', ended_at=?, "
            "error='401 auth failed' WHERE id=?",
            (5_000_000, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error='401 auth failed' WHERE id=?",
            (tid,),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None


def test_rate_limited_cooldown_path_is_unchanged(kanban_home, monkeypatch):
    """The ``rate_limited`` cooldown escape must keep behaving exactly as before."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl card", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=?, error=? WHERE id=?",
            (5_000_000, QUOTA_STAMP, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
            (QUOTA_STAMP, tid),
        )
        conn.commit()

        import hermes_cli.kanban_db as _kb

        monkeypatch.setattr(_kb.time, "time", lambda: 5_000_100)
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        monkeypatch.setattr(_kb.time, "time", lambda: 5_000_400)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_completed_run_superseding_a_quota_stamp_respawns(kanban_home):
    """A card that COMPLETED after a quota stamp is not parked by that stamp.

    ``complete_task`` clears the stamp on the success path, but a run that
    ended in another non-failure outcome (here: completed, stamped by hand to
    model a card whose success was recorded without the clear) must still be
    judged on the CURRENT run, not on the leftover text.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="completed after quota", assignee="coder")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='done', ended_at=?, "
            "error=NULL WHERE id=?",
            (5_000_100, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
            (QUOTA_STAMP, tid),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid, lane="ready") is None
