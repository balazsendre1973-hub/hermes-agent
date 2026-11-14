"""block_task live-claim ownership fence (t_c9e4fa2b).

Same hole class as t_43e2d084's complete_task fix: a mutation path that trusted
a mutable field instead of requiring the caller's own ``expected_run_id``.

``block_task`` now refuses to park a *live-claimed* running card when
``expected_run_id is None`` (LiveClaimError), mirroring complete_task and
request_review. The owner (expected_run_id matches current_run_id) and an
explicit ``force=True`` still clear the claim; ready cards and stale claims
block as before.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _live_claimed_running(conn, title: str = "t") -> str:
    """Create, claim, and mark live a running task (this process = worker)."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    # Publish our pid as the executing worker so the claim is live
    # (_claim_is_live: worker_pid set + _worker_alive). "unverified" fingerprint
    # keeps the existence answer, which is exactly right for our own pid.
    pid = os.getpid()
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
            (pid, kbd.UNVERIFIED_WORKER_FINGERPRINT, tid),
        )
    return tid


# ---------------------------------------------------------------------------
# Ownership fence
# ---------------------------------------------------------------------------


def test_block_refuses_live_claim_without_run_id(kanban_home: Path) -> None:
    """A live-claimed running card cannot be blocked with expected_run_id=None."""
    with kbc.connect_closing() as conn:
        tid = _live_claimed_running(conn)
        with pytest.raises(kb.LiveClaimError):
            kb.block_task(conn, tid, reason="a stranger parks the live card")
        # Nothing changed: claim and run intact.
        t = kb.get_task(conn, tid)
        assert t.status == "running"
        assert t.worker_pid == os.getpid()
        assert t.claim_lock is not None


def test_block_allows_owner_with_matching_run_id(kanban_home: Path) -> None:
    """The owner (expected_run_id == current_run_id) can still block its own run."""
    with kbc.connect_closing() as conn:
        tid = _live_claimed_running(conn)
        run_id = kb.get_task(conn, tid).current_run_id
        assert run_id is not None
        assert kb.block_task(
            conn, tid, reason="owner blocks own run", expected_run_id=run_id,
        ) is True
        assert kb.get_task(conn, tid).status == "blocked"


def test_block_refuses_wrong_run_id(kanban_home: Path) -> None:
    """expected_run_id that does not match the card's current run is refused."""
    with kbc.connect_closing() as conn:
        tid = _live_claimed_running(conn)
        assert kb.block_task(conn, tid, reason="wrong run", expected_run_id=999999) is False
        assert kb.get_task(conn, tid).status == "running"


# ---------------------------------------------------------------------------
# No regression: non-live cards still block as before
# ---------------------------------------------------------------------------


def test_block_ready_card_unclaimed(kanban_home: Path) -> None:
    """A ready (never claimed) card blocks exactly as today."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ready", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.block_task(conn, tid, reason="block a fresh ready card") is True
        assert kb.get_task(conn, tid).status == "blocked"


def test_block_stale_claim_clears_without_run_id(kanban_home: Path) -> None:
    """A running card whose worker is gone (stale claim) blocks without a run id."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="stale", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        # No worker_pid => claim not live => no fence, block clears the stale claim.
        assert kb.block_task(conn, tid, reason="stale claim") is True
        assert kb.get_task(conn, tid).status == "blocked"


def test_block_force_overrides_live_claim(kanban_home: Path) -> None:
    """force=True is the explicit operator escape hatch for a live claim."""
    with kbc.connect_closing() as conn:
        tid = _live_claimed_running(conn)
        assert kb.block_task(conn, tid, reason="operator override", force=True) is True
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# CLI surface: the refusal is a clean message, not a traceback
# ---------------------------------------------------------------------------


def test_block_cli_refuses_live_claim_with_message(
    kanban_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    with kbc.connect_closing() as conn:
        tid = _live_claimed_running(conn)
        args = argparse.Namespace(
            task_id=tid, ids=None, reason=["live", "claim"], kind=None, force=False,
        )
        rc = kanban_cli._cmd_block(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "cannot block" in err
        assert "a live worker is running it" in err
        assert "--force" in err
        # Card untouched.
        assert kb.get_task(conn, tid).status == "running"
