"""Write-gate tests: machine checklist, automatic restore, SQL-level triggers,
and the shared model-existence module (hermes_cli.model_gate).

The gate adapts the ``memory_consolidation.py`` pattern (snapshot → write →
diff → checklist → restore + non-zero exit) to the kanban board, so these tests
pin each step of that pipeline plus the two extra layers the board needs:
pre-write input checks and the SQL triggers that catch direct DB writes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_write_gate as kg
from hermes_cli import model_gate


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (test_kanban_db pattern)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _with_profiles(home: Path, *names: str) -> None:
    """Materialize the profile registry so the assignee rule activates."""
    for name in names:
        profile = home / "profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "config.yaml").write_text("{}\n", encoding="utf-8")


def _scoped_provider() -> tuple[str, list[str]]:
    """First provider with a non-empty local catalog (dynamic: the catalogs are
    repo data that changes over time)."""
    from hermes_cli.models import static_catalog_providers

    for provider in static_catalog_providers():
        ids = sorted(model_gate.known_ids(provider))
        if ids:
            return provider, ids
    pytest.skip("no provider with a local catalog")


# --------------------------------------------------------------------------- #
# Layer 3: SQL triggers — direct DB writes fail too
# --------------------------------------------------------------------------- #

class TestSqlTriggers:
    def test_four_triggers_installed(self, board):
        with kbc.connect() as conn:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert {
            "trg_tasks_status_gate_insert",
            "trg_tasks_status_gate_update",
            "trg_tasks_block_kind_gate_insert",
            "trg_tasks_block_kind_gate_update",
        } <= names

    def test_direct_sql_illegal_status_update_blocked(self, board):
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            with pytest.raises(sqlite3.IntegrityError, match="write-gate"):
                conn.execute("UPDATE tasks SET status = 'bogus' WHERE id = ?", (tid,))
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] in kb.VALID_STATUSES

    def test_direct_sql_illegal_status_insert_blocked(self, board):
        with kbc.connect() as conn:
            with pytest.raises(sqlite3.IntegrityError, match="write-gate"):
                conn.execute(
                    "INSERT INTO tasks (id, title, status, created_at) VALUES ('t_bad', 'x', 'bogus', 1)"
                )
            assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id = 't_bad'").fetchone()[0] == 0

    def test_direct_sql_illegal_block_kind_blocked(self, board):
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            with pytest.raises(sqlite3.IntegrityError, match="write-gate"):
                conn.execute("UPDATE tasks SET block_kind = 'weird' WHERE id = ?", (tid,))

    def test_legal_direct_write_still_works(self, board):
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
            assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "todo"

    def test_triggers_contain_every_valid_status(self, board):
        """The SQL gate's NOT IN lists track VALID_STATUSES, not a snapshot
        from when the trigger was first created: ``CREATE TRIGGER IF NOT EXISTS``
        never replaced stale triggers, which is exactly how ``revoked-by-Fin``
        went missing and every direct write of it hit SQLITE_CONSTRAINT 19.
        Triggers are now dropped + recreated from VALID_STATUSES on every init,
        so the two layers cannot drift apart unnoticed.
        """
        with kbc.connect() as conn:
            rows = {
                name: sql
                for name, sql in conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'trg_tasks_status_gate%'"
                ).fetchall()
            }
        assert set(rows) == {"trg_tasks_status_gate_insert", "trg_tasks_status_gate_update"}
        for sql in rows.values():
            inside = sql[sql.index("NOT IN (") + len("NOT IN ("): sql.index(")", sql.index("NOT IN ("))]
            allowed = {piece.strip().strip("'") for piece in inside.split(",")}
            assert allowed == set(kb.VALID_STATUSES), sql

    @pytest.mark.parametrize("status", sorted(kb.VALID_STATUSES))
    def test_every_valid_status_passes_the_status_gate(self, board, status):
        """Direct SQL write of every VALID_STATUSES value passes the trigger,
        including ``revoked-by-Fin`` (previously SQLITE_CONSTRAINT 19)."""
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] == status


# --------------------------------------------------------------------------- #
# Layer 1: pre-write input checks (bad value never reaches the database)
# --------------------------------------------------------------------------- #

class TestPreWriteChecks:
    def test_unknown_assignee_rejected_on_create(self, board):
        _with_profiles(board, "coder")
        with kbc.connect() as conn:
            with pytest.raises(ValueError, match="ghost"):
                kb.create_task(conn, title="t", assignee="ghost")
            assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 't'").fetchone()[0] == 0

    def test_known_assignee_accepted_on_create(self, board):
        _with_profiles(board, "coder")
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee="coder")
        assert tid

    def test_unknown_assignee_rejected_on_assign(self, board):
        _with_profiles(board, "coder")
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            with pytest.raises(ValueError, match="ghost"):
                kb.assign_task(conn, tid, "ghost")
            row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["assignee"] is None

    def test_assignee_rule_fails_open_without_registry(self, board):
        """No <root>/profiles on disk (fresh install / isolated home): there is
        no registry to validate against, so any name passes — the gate must not
        reject work for lack of a registry."""
        assert not (board / "profiles").is_dir()
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee="any-name")
        assert tid

    def test_unknown_model_rejected_on_set_model_override(self, board):
        provider, _ids = _scoped_provider()
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            with pytest.raises(ValueError, match="no-such-model"):
                kb.set_model_override(conn, tid, "no-such-model-xyz", provider=provider)
            row = conn.execute("SELECT model_override FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["model_override"] is None

    def test_known_model_accepted_on_set_model_override(self, board):
        provider, ids = _scoped_provider()
        good = ids[0]
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            kb.set_model_override(conn, tid, good, provider=provider)
            row = conn.execute("SELECT model_override FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["model_override"] == good

    def test_unknown_model_rejected_on_create(self, board):
        provider, _ids = _scoped_provider()
        with kbc.connect() as conn:
            with pytest.raises(ValueError, match="no-such-model"):
                kb.create_task(conn, title="t", model_override="no-such-model-xyz",
                               provider_override=provider)
            assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 't'").fetchone()[0] == 0

    def test_malformed_reg_idempotency_key_rejected(self, board):
        for bad in ("reg:||", "reg:|t_abc|NEW", "reg:default|has space|NEW", "reg:|"):
            with kbc.connect() as conn:
                with pytest.raises(ValueError, match="idempotency|reg:"):
                    kb.create_task(conn, title=f"t-{bad}", idempotency_key=bad)

    def test_well_formed_reg_idempotency_key_accepted(self, board):
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", idempotency_key="reg:default|t_deadbeef|NEW")
        assert tid

    def test_free_form_idempotency_key_unconstrained(self, board):
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", idempotency_key="ticket-1")
        assert tid


# --------------------------------------------------------------------------- #
# Layer 2: post-write checklist → automatic restore (the memory_consolidation
# pattern: backup → write → diff → checklist → restore + non-zero exit)
# --------------------------------------------------------------------------- #

class TestPostWriteRestore:
    def test_violation_restores_row_and_raises(self, board, monkeypatch):
        _with_profiles(board, "coder")
        real_registry = kg.assignee_registry
        calls = {"n": 0}

        def flipping():
            calls["n"] += 1
            if calls["n"] <= 1:
                return real_registry()  # pre-check: coder exists
            return (frozenset({"someone-else"}), True)  # post-check: it vanished

        monkeypatch.setattr(kg, "assignee_registry", flipping)
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            with pytest.raises(kg.GateViolation) as excinfo:
                kb.assign_task(conn, tid, "coder")
            row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (tid,)).fetchone()

        violation = excinfo.value
        assert violation.restored is True
        assert any(v.startswith("assignee_unknown") for v in violation.violations)
        assert violation.kind == "assign"
        assert row["assignee"] is None  # restored to the pre-mutation state

    def test_create_violation_deletes_the_created_row(self, board, monkeypatch):
        """A create that lands in a violated state is rolled back completely:
        row, events and links are gone (restore = 'never happened')."""
        _with_profiles(board, "coder")
        real_registry = kg.assignee_registry
        calls = {"n": 0}

        def flipping():
            calls["n"] += 1
            if calls["n"] <= 1:
                return real_registry()
            return (frozenset({"someone-else"}), True)

        monkeypatch.setattr(kg, "assignee_registry", flipping)
        with kbc.connect() as conn:
            with pytest.raises(kg.GateViolation) as excinfo:
                kb.create_task(conn, title="ghost-created", assignee="coder")
            tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 'ghost-created'").fetchone()[0]
            events = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id NOT IN (SELECT id FROM tasks)"
            ).fetchone()[0]
        assert excinfo.value.restored is True
        assert tasks == 0
        assert events == 0  # no orphan events either

    def test_ledger_records_before_after_sha_and_diff(self, board, monkeypatch):
        _with_profiles(board, "coder")
        real_registry = kg.assignee_registry
        calls = {"n": 0}

        def flipping():
            calls["n"] += 1
            if calls["n"] <= 1:
                return real_registry()
            return (frozenset({"someone-else"}), True)

        monkeypatch.setattr(kg, "assignee_registry", flipping)
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            with pytest.raises(kg.GateViolation):
                kb.assign_task(conn, tid, "coder")
            entries = kg.ledger_read(conn)

        actions = [e["action"] for e in entries]
        assert "violation" in actions and "restored" in actions
        violation = next(e for e in entries if e["action"] == "violation")
        assert violation["before_sha256"] and violation["after_sha256"]
        assert violation["before_sha256"] != violation["after_sha256"]
        assert violation["diff"]
        # The backup survives the restore: the restore entry references the
        # violated-state hash, so the reversal itself can be undone.
        restored = next(e for e in entries if e["action"] == "restored")
        assert restored["before_sha256"] == violation["after_sha256"]

    def test_good_mutations_land_in_the_ledger_as_verified(self, board):
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            kb.edit_task(conn, tid, title="renamed")
            entries = kg.ledger_read(conn)
        verified = [e for e in entries if e["action"] == "verified"]
        assert {e["kind"] for e in verified} >= {"create", "edit"}
        for entry in verified:
            assert entry["after_sha256"]

    def test_gate_kill_switch_disables_python_checks(self, board, monkeypatch):
        _with_profiles(board, "coder")
        monkeypatch.setenv("HERMES_KANBAN_WRITE_GATE", "off")
        assert kg.enabled() is False
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee="ghost-while-off")
        assert tid


# --------------------------------------------------------------------------- #
# Checklist internals + the shared model module
# --------------------------------------------------------------------------- #

class TestChecklist:
    def test_check_task_row_flags_illegal_values(self, monkeypatch):
        monkeypatch.setattr(kg, "assignee_registry", lambda: (frozenset({"coder"}), True))
        row = {
            "status": "bogus",
            "block_kind": "weird",
            "workspace_kind": "spaceship",
            "assignee": "ghost",
            "model_override": None,
            "provider_override": None,
            "idempotency_key": None,
            "consecutive_failures": -1,
            "block_recurrences": 0,
        }
        rules = kg.check_task_row(row)
        assert any(r.startswith("status_illegal") for r in rules)
        assert any(r.startswith("block_kind_illegal") for r in rules)
        assert any(r.startswith("workspace_kind_illegal") for r in rules)
        assert any(r.startswith("assignee_unknown") for r in rules)
        assert any(r.startswith("consecutive_failures_negative") for r in rules)

    def test_check_task_row_clean_row_is_silent(self, monkeypatch):
        monkeypatch.setattr(kg, "assignee_registry", lambda: (frozenset({"coder"}), True))
        row = {
            "status": "ready",
            "block_kind": None,
            "workspace_kind": "scratch",
            "assignee": "coder",
            "model_override": None,
            "provider_override": None,
            "idempotency_key": "reg:default|t_abc12345|NEW",
            "consecutive_failures": 0,
            "block_recurrences": 0,
        }
        assert kg.check_task_row(row) == set()

    def test_idempotency_format_rule(self):
        assert kg.idempotency_format_error(None) is None
        assert kg.idempotency_format_error("ticket-1") is None  # only reg: keys are shaped
        assert kg.idempotency_format_error("reg:default|t_abc|NEW") is None
        assert kg.idempotency_format_error("reg:default|t_abc") is None  # kind optional
        assert kg.idempotency_format_error("reg:||") is not None
        assert kg.idempotency_format_error("reg:|t_abc|NEW") is not None
        assert kg.idempotency_format_error("reg:default|has space|NEW") is not None

    def test_event_chain_flags_unparseable_payload(self, board):
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'x', ?, 1)",
                (tid, "{not json"),
            )
            rules = kg.check_event_chain(conn, tid, -1)
        assert any(r.startswith("event_payload_unparseable") for r in rules)

    def test_events_remain_parseable_after_gated_writes(self, board):
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            kb.edit_task(conn, tid, title="t2")
            kb.block_task(conn, tid, reason="r", kind="needs_input")
            bad = 0
            total = 0
            for kind, payload in conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ?", (tid,)):
                total += 1
                if not kind:
                    bad += 1
                if payload is not None:
                    try:
                        json.loads(payload)
                    except ValueError:
                        bad += 1
        assert total >= 3
        assert bad == 0


class TestModelGateSharedModule:
    def test_verdicts_match_between_gate_and_preflight_entrypoint(self, board):
        """One module, two call sites: require_model (write time) must raise
        exactly when model_known (dispatch-time preflight) says False."""
        provider, ids = _scoped_provider()
        bad_ok, _ = model_gate.model_known("no-such-model-xyz", provider)
        good_ok, _ = model_gate.model_known(ids[0], provider)
        assert bad_ok is False
        assert good_ok is True
        with kbc.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee=None)
            with pytest.raises(ValueError):
                kg.require_model("no-such-model-xyz", provider)
            kg.require_model(ids[0], provider)  # must not raise
        assert tid

    def test_unknown_provider_catalog_fails_open(self):
        ok, reason = model_gate.model_known("whatever-model", "no-such-provider-xyz")
        assert ok is True
        assert "fail-open" in reason

    def test_empty_model_always_ok(self):
        assert model_gate.model_known("", None)[0] is True
        assert model_gate.model_known(None, "openai")[0] is True
