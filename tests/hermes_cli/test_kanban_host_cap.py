"""Host-level concurrency accounting + review-lane fairness (OOF-30 review).

Three gaps found in review of the original memory-guard PR:

1. The standalone daemon path (``hermes kanban daemon --force`` /
   :func:`hermes_cli.kanban_db_dispatch.run_daemon`) never resolved
   ``kanban.max_in_progress`` at all — the one shipped entry point that
   could still fan out an entire backlog in a single tick.
2. ``max_in_progress`` was enforced per-board while the gateway dispatcher
   ticks every active board — N boards multiplied the host budget by N.
3. The ready loop consumed the entire shared spawn budget before the
   review loop ran, so a sustained ready backlog starved autonomous
   reviews indefinitely.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


# ---------------------------------------------------------------------------
# 1. Standalone daemon resolves max_in_progress (P1a)
# ---------------------------------------------------------------------------


def test_run_daemon_resolves_and_passes_max_in_progress(
    kanban_home, monkeypatch,
):
    """The daemon tick must pass a resolved cap into dispatch_once.

    Regression guard for the OOF-30 review finding: ``run_daemon`` only
    forwarded ``max_spawn`` — with no explicit ``--max`` (the shipped
    systemd shape) nothing capped the tick even though the gateway and
    ``hermes kanban dispatch`` paths both resolved the memory-derived
    default.
    """
    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    # No explicit config → the derived default must flow through.
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: None)
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 3)

    def on_tick(res):
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert captured.get("max_in_progress") == 3




def test_configured_max_in_progress_parsing(monkeypatch):
    import hermes_cli.config as cfgmod

    cases = [
        ({"kanban": {"max_in_progress": 4}}, 4),
        ({"kanban": {"max_in_progress": "5"}}, 5),
        ({"kanban": {"max_in_progress": 0}}, None),
        ({"kanban": {"max_in_progress": -2}}, None),
        ({"kanban": {"max_in_progress": "lots"}}, None),
        ({"kanban": {}}, None),
        ({}, None),
    ]
    for config, expected in cases:
        monkeypatch.setattr(
            cfgmod, "load_config_readonly", lambda c=config: c
        )
        assert kbd.configured_max_in_progress() == expected, config


# ---------------------------------------------------------------------------
# 2. max_in_progress counts running work on ALL boards (P1b)
# ---------------------------------------------------------------------------


def test_max_in_progress_counts_other_boards(
    kanban_home, all_assignees_spawnable,
):
    """Workers running on another board consume the same host budget."""
    kb.create_board("second")

    # Two workers already running on the second board.
    with kbc.connect(board="second") as conn:
        for title in ("busy-1", "busy-2"):
            tid = kb.create_task(conn, title=title, assignee="alice")
            assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Host budget (2) already consumed by the second board → nothing spawns.
    assert not spawns
    assert not res.spawned


def test_max_in_progress_partial_budget_across_boards(
    kanban_home, all_assignees_spawnable,
):
    kb.create_board("second")

    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # 1 running elsewhere + budget 2 → exactly one new spawn here.
    assert len(spawns) == 1
    assert len(res.spawned) == 1


def test_count_running_tasks_other_boards_fails_open(
    kanban_home, monkeypatch,
):
    """A broken board enumeration must not brick dispatch (returns 0)."""
    monkeypatch.setattr(
        kb, "list_boards",
        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert kbd.count_running_tasks_other_boards() == 0


def test_max_spawn_stays_per_board(kanban_home, all_assignees_spawnable):
    """``max_spawn`` keeps its historical per-board semantics."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=1,
        )

    # The other board's worker does NOT count against max_spawn.
    assert len(spawns) == 1
    assert len(res.spawned) == 1


# ---------------------------------------------------------------------------
# 3. Review lane cannot be starved by a sustained ready backlog (P2)
# ---------------------------------------------------------------------------


def _park_in_review(conn: sqlite3.Connection, title: str, assignee: str) -> str:
    tid = kb.create_task(conn, title=title, assignee=assignee)
    _set_task_status(conn, tid, "review")
    return tid


def test_review_lane_gets_reserved_slot_under_ready_backlog(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2", "ready-3"):
            kb.create_task(conn, title=title, assignee="alice")
        review_id = _park_in_review(conn, "review-me", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    spawned_ids = [s[0] for s in res.spawned]
    # Budget 2: one ready + the reserved review slot — never 2×ready.
    assert len(spawned_ids) == 2
    assert review_id in spawned_ids


def _guard_review_row(conn: sqlite3.Connection, review_id: str) -> dict:
    """Latest run ``rate_limited`` → ``check_respawn_guard`` returns a cooldown."""
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, "
            "started_at, ended_at) VALUES (?, 'reviewer', 'rate_limited', "
            "'rate_limited', ?, ?)",
            (review_id, now, now),
        )
    assert kbd.check_respawn_guard(conn, review_id, lane="review") == "rate_limit_cooldown"
    return {"max_in_progress": 1}


def _cap_review_row(conn: sqlite3.Connection, review_id: str) -> dict:
    """``reviewer`` already has one running worker → the review row is per-profile capped."""
    busy_id = kb.create_task(conn, title="busy", assignee="reviewer")
    assert kb.claim_task(conn, busy_id) is not None
    return {"max_in_progress": 2, "max_in_progress_per_profile": 1}


@pytest.mark.parametrize("make_unspawnable", [_guard_review_row, _cap_review_row])
def test_unspawnable_review_does_not_reserve_the_only_ready_slot(
    kanban_home, all_assignees_spawnable, monkeypatch, make_unspawnable,
):
    """A review card the review loop would refuse this tick (respawn guard,
    per-profile cap) must not consume the fairness reservation — otherwise the
    ready lane starves every tick while the reserved slot goes unused."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        ready_id = kb.create_task(conn, title="ready-now", assignee="alice")
        review_id = _park_in_review(conn, "review-unspawnable", "reviewer")
        caps = make_unspawnable(conn, review_id)
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns), **caps)

    assert [task_id for task_id, *_ in res.spawned] == [ready_id]


def test_unguarded_review_reserves_the_only_ready_slot(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A dispatchable review card still receives the single shared slot."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="ready-now", assignee="alice")
        review_id = _park_in_review(conn, "review-now", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=1,
        )

    assert [task_id for task_id, *_ in res.spawned] == [review_id]


def test_review_reservation_released_when_no_review_work(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2", "ready-3"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # No review work → ready lane keeps the full budget.
    assert len(res.spawned) == 2


def test_nonspawnable_review_does_not_tax_ready_budget(
    kanban_home, monkeypatch,
):
    """Review tasks parked for humans (no real profile) release the slot."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    # Only 'alice' is a real profile; the review assignee is a human lane.
    monkeypatch.setattr(
        profmod, "profile_exists", lambda name: name == "alice"
    )

    spawns: list = []
    with kbc.connect() as conn:
        for title in ("ready-1", "ready-2"):
            kb.create_task(conn, title=title, assignee="alice")
        _park_in_review(conn, "human-review", "some-human")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Human-lane review is not spawnable → no reservation, ready gets both.
    assert len(res.spawned) == 2


def test_review_budget_still_bounded_by_shared_cap(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The reservation caps the ready lane; it grants review no extra slots."""
    import hermes_cli.config as cfgmod
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )

    spawns: list = []
    with kbc.connect() as conn:
        kb.create_task(conn, title="ready-1", assignee="alice")
        for i in range(3):
            _park_in_review(conn, f"review-{i}", "reviewer")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=2,
        )

    # Budget 2 total across both lanes, reservation notwithstanding.
    assert len(res.spawned) == 2


# ---------------------------------------------------------------------------
# 4. A full-cap hold is reported and named, never silent (t_8038a077)
# ---------------------------------------------------------------------------


def _events(conn: sqlite3.Connection, task_id: str, kind: str) -> list:
    return [e for e in kb.list_events(conn, task_id) if e.kind == kind]


def test_host_cap_hold_is_reported_and_named(
    kanban_home, all_assignees_spawnable,
):
    """ACCEPTANCE: full default-board occupancy (``max_in_progress=1``, one
    running there) plus a ready card on ANOTHER board → the dispatch result
    carries an objective hold reason and the suppression line names it.

    This is the t_886b92d9 shape: 18:36–19:34 the default board filled the
    host cap, the other board's ready card got ``may_spawn=False`` every tick,
    and nothing anywhere said why — three reason-less "dispatcher stuck"
    warnings and an empty ``describe_suppression``.
    """
    # The default board takes the single host slot.
    with kbc.connect() as conn:
        busy_id = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, busy_id) is not None

    kb.create_board("second")
    spawns: list = []
    with kbc.connect(board="second") as conn:
        ready_id = kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=1, board="second",
        )

        # Objective, self-describing reason — not an empty result.
        assert not spawns
        assert not res.spawned
        assert res.held_back_capacity == (1, 1)
        assert res.held_back_max_spawn is None
        assert kbd.describe_suppression([res]) == "capacity=1/1"
        # The "dispatcher stuck" warning names it as its suffix …
        assert kbd.describe_stuck_suffix([res]) == " Last tick held back: capacity=1/1."
        # … and the waiting card itself says why, so `tail` needs no log hunt.
        held = _events(conn, ready_id, "held_back_capacity")
        assert len(held) == 1
        assert held[0].payload == {"cap": "max_in_progress", "used": 1, "limit": 1}


def test_held_back_capacity_event_is_written_once_per_hold(
    kanban_home, all_assignees_spawnable,
):
    """State-change-only: three saturated ticks leave ONE row, and the slot
    freeing on the other board still spawns exactly as before."""
    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        busy_id = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, busy_id) is not None

    spawns: list = []
    with kbc.connect() as conn:
        ready_id = kb.create_task(conn, title="queued", assignee="alice")
        for _ in range(3):
            res = kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=1,
            )
            assert res.held_back_capacity == (1, 1)
        held = _events(conn, ready_id, "held_back_capacity")
        assert len(held) == 1
        assert held[0].payload == {"cap": "max_in_progress", "used": 1, "limit": 1}

        # Free the other board's slot → next tick spawns, no extra hold row.
        with kbc.connect(board="second") as other:
            _set_task_status(other, busy_id, "done")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=1,
        )
        assert [task_id for task_id, *_ in res.spawned] == [ready_id]
        assert res.held_back_capacity is None
        assert len(_events(conn, ready_id, "held_back_capacity")) == 1


def test_max_spawn_hold_names_its_own_cap(kanban_home, all_assignees_spawnable):
    """The per-board ``max_spawn`` cap reports itself, not the host cap, so the
    operator knows which knob to move."""
    spawns: list = []
    with kbc.connect() as conn:
        busy_id = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, busy_id) is not None
        queued_id = kb.create_task(conn, title="queued", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_spawn=1,
        )

        assert not spawns
        assert res.held_back_max_spawn == (1, 1)
        assert res.held_back_capacity is None
        assert kbd.describe_suppression([res]) == "max_spawn=1/1"
        assert kbd.describe_stuck_suffix([res]) == " Last tick held back: max_spawn=1/1."
        held = _events(conn, queued_id, "held_back_capacity")
        assert len(held) == 1
        assert held[0].payload == {"cap": "max_spawn", "used": 1, "limit": 1}


def test_clean_tick_reports_no_hold(kanban_home, all_assignees_spawnable):
    """Empty hold stays suffix-less and spawn is untouched (no regression for
    a tick that simply had room)."""
    spawns: list = []
    with kbc.connect() as conn:
        ready_id = kb.create_task(conn, title="run-me", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=4,
        )

        assert spawns == [ready_id]
        assert len(res.spawned) == 1
        assert res.held_back_capacity is None
        assert res.held_back_max_spawn is None
        assert kbd.describe_suppression([res]) == ""
        assert kbd.describe_stuck_suffix([res]) == ""
        assert kbd.describe_stuck_suffix([]) == ""
        assert not _events(conn, ready_id, "held_back_capacity")
        # The pre-existing reasons still render exactly as before.
        assert kbd.describe_suppression(
            [kb.DispatchResult(memory_pressure="critical")]) == "memory_pressure=critical"
        assert kbd.describe_suppression(
            [kb.DispatchResult(skipped_locked=True)]) == "skipped_locked=1"
        assert kbd.describe_suppression([
            None, kb.DispatchResult(respawn_guarded=[("t_1", "active_pr")]),
        ]) == "active_pr=1"


def test_capacity_survives_an_idle_board_in_the_summary():
    """The gateway folds EVERY board's tick into one line: an idle board that
    sorts after the held one must not erase the reason."""
    held = kb.DispatchResult(held_back_capacity=(8, 8))
    idle = kb.DispatchResult()
    assert kbd.describe_suppression([held, idle]) == "capacity=8/8"
    assert kbd.describe_suppression([idle, held]) == "capacity=8/8"
    assert kbd.describe_stuck_suffix([idle, held]) == " Last tick held back: capacity=8/8."


def test_pinned_env_undercounts_other_boards_and_says_so(
    kanban_home, all_assignees_spawnable, monkeypatch, caplog,
):
    """Documented diagnostic trap: a ``HERMES_KANBAN_DB``-pinned process (a
    dispatched worker's shell — where dispatch probes are most likely to run)
    resolves every board to one file, so the cross-board count collapses to 0
    and the budget looks larger than the host's. The collapse is now flagged
    instead of silently believed (t_8038a077).
    """
    import logging

    kb.create_board("second")
    with kbc.connect(board="second") as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None

    # Unpinned — how the gateway itself runs — the other board's worker counts.
    assert kbd.count_running_tasks_other_boards("default") == 1
    assert kbd._other_boards_pin_shadowed is False

    # Pinned with explicit board intent (exactly what a dispatched worker
    # carries): every slug resolves to the pinned file → structural 0.
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="default")))
    monkeypatch.setattr(kb, "_explicit_board_intent_pinned", lambda: True)
    assert kbd.count_running_tasks_other_boards("default") == 0
    assert kbd._other_boards_pin_shadowed is True

    # A dispatch taken from that shell now says its numbers are not host-accurate.
    monkeypatch.setattr(kbd, "_pin_shadow_warned", False)
    spawns: list = []
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kbc.connect() as conn:
            kb.create_task(conn, title="queued", assignee="alice")
            res = kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=1,
            )
    assert "HERMES_KANBAN_DB pins every board to one file" in caplog.text
    # The lie, demonstrated: 0+0 < 1 → it spawns, where the unpinned gateway
    # would have held (1 running on the other board + 1 slot = full).
    assert len(spawns) == 1
    assert res.held_back_capacity is None
