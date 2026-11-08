"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_running_with_open_parents_fires_only_while_running():
    """A running card whose parent is not terminal is flagged; the same graph
    on a ready/todo card (the gate is holding it) and a done parent are not."""
    graph = {"parents": [{"id": "t_parent", "title": "p", "status": "todo"}], "children": []}
    diags = kd.compute_task_diagnostics(_task(status="running", started_at=100), [], [], graph=graph)
    assert [d.kind for d in diags] == ["running_with_open_parents"]
    assert diags[0].data["open_parents"] == [{"id": "t_parent", "status": "todo"}]
    assert "hermes kanban unlink t_parent t_demo00" in diags[0].actions[0].payload["command"]
    assert kd.compute_task_diagnostics(_task(status="todo"), [], [], graph=graph) == []
    done_graph = {"parents": [{"id": "t_parent", "title": "p", "status": "done"}], "children": []}
    assert kd.compute_task_diagnostics(_task(status="running"), [], [], graph=done_graph) == []


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48








# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"


def test_stranded_in_ready_starts_at_the_crash_requeue_not_the_promotion():
    """A crash-requeue hands a `running` card back to `ready` WITHOUT a
    ready-entry event, so the strand clock must start there — not at
    ``promoted``. Regression for the 2026-11-06 16:14 phantom cards: a gateway
    restart (``worker_lost`` 16:12:43, ``crashed`` 16:14:22) left the clock on
    ``promoted`` (15:07), measuring a 99-second claim gap as 66 minutes."""
    now = 100_000
    task = _task(status="ready", assignee="coder", claim_lock=None)
    events = [
        _event("created", ts=now - 66 * 60),
        _event("promoted", ts=now - 65 * 60),
        _event("claimed", ts=now - 64 * 60),
        _event("worker_lost", ts=now - 110),
        _event("crashed", ts=now - 99),
    ]
    runs = [_run(outcome="crashed")]
    assert kd.compute_task_diagnostics(task, events, runs, now=now) == []

    # The same list minus the claim-loss kinds is the pre-fix behaviour: it
    # must flag, so this test cannot pass vacuously.
    legacy = [ev for ev in events if ev["kind"] not in {"crashed", "worker_lost"}]
    stranded = [d for d in kd.compute_task_diagnostics(task, legacy, [], now=now)
                if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1 and stranded[0].data["age_seconds"] >= 65 * 60


def test_stranded_in_ready_still_fires_when_a_crashed_card_sits_ready():
    """The new kinds only move the clock FORWARD: a card that crashed 45 min
    ago and has been claim-less since is exactly what the rule is for."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [
        _event("created", ts=now - 5 * 3600),
        _event("claimed", ts=now - 4 * 3600),
        _event("crashed", ts=now - 45 * 60),
        _event("respawn_guarded", ts=now - 40 * 60),  # in-run noise: no reset
    ]
    runs = [_run(outcome="crashed")]
    stranded = [d for d in kd.compute_task_diagnostics(task, events, runs, now=now)
                if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].data["age_seconds"] == 45 * 60


def test_run_end_floors_a_requeue_kind_nobody_enumerated_yet():
    """Belt and braces: a run that closed 10 min ago proves the card was worked
    on, so no unenumerated future requeue kind can resurrect ``created`` and
    manufacture a strand out of a 5-hour-old timestamp."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None,
                 created_at=now - 5 * 3600)
    events = [_event("created", ts=now - 5 * 3600),
              _event("some_future_requeue_kind", ts=now - 10 * 60)]
    runs = [{"id": 7, "outcome": "whatever", "ended_at": now - 10 * 60}]
    assert kd.compute_task_diagnostics(task, events, runs, now=now) == []
    # No closed run and no known ready-entry event: the created_at fallback
    # still over-flags rather than staying silent.
    stranded = [d for d in kd.compute_task_diagnostics(task, events, [], now=now)
                if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1


REGISTER_SYNC = Path.home() / ".hermes" / "profiles" / "logchan" / "scripts" / "task_register_sync.py"


@pytest.mark.skipif(not REGISTER_SYNC.is_file(),
                    reason="register mirror lives in the logchan profile")
def test_register_mirror_shares_the_canonical_ready_transition_kinds():
    """The register cannot import hermes_cli (it runs on the profile's own
    interpreter), so it copies the list — this locks the copy to the source
    (t_85b1f2e1: an uncoordinated half-fix would leave one side counting a
    crash-requeue and the other not)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_kanban_register_under_test", REGISTER_SYNC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.READY_TRANSITION_KINDS) == set(kd.READY_TRANSITION_KINDS)


# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True
