"""Fail-closed routes exercised by the trial_model() change (t_2f00675a).

The core contract tests live in test_empty_model_fallback.py (trial_model /
_resolve_gateway_model); this file exercises the two caller sites that must
translate the model-resolution failure into a clear, domain-specific error
instead of propagating the bare exception or a misleading ``storage_unavailable``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest


class TestCronJobModelResolution:
    """Cron job config: no model anywhere → the existing ``no model`` RuntimeError.

    ``cron.scheduler._load_cron_job_config`` catches :exc:`TrialModelUnavailable`
    (from ``trial_model``) and feeds ``model = ''`` into its own guard, so the user
    sees the cron-specific message with job / HERMES_MODEL / config context — not the
    generic "no welcome trial" wording.
    """

    def test_job_without_any_model_raises_cron_error(self):
        from hermes_cli import config_backend, config_effective
        import hermes_cli.anon_auth as anon_auth
        from cron.scheduler import _load_cron_job_config

        job = {"model": None}  # no per-job pin, falls through to trial
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(config_backend, "config_exists", lambda _path: True)
            mp.setattr(config_effective, "load_user_config_effective", lambda _path: {})
            # No welcome trial active: the trial rung fails closed.
            mp.setattr(anon_auth, "free_inference_active", lambda: False)
            with pytest.raises(RuntimeError) as exc:
                _load_cron_job_config(job, "t_2f00675a", "no-model-job")
            msg = str(exc.value)
            assert "no model configured" in msg
            assert "t_2f00675a" in msg
            assert "no-model-job" in msg
            # The bare trial exception never surfaces to the job runtime.
            assert "TrialModelUnavailable" not in msg
            # The welcome-trial wording is swallowed by the cron guard.
            assert "welcome trial" not in msg


class TestKanbanStorageUnavailableRoute:
    """Kanban worker build: no model + no trial → clear RuntimeError, not ``storage_unavailable``.

    ``gateway/session_kanban.build_kanban_policy`` is the exact route that used to flow
    ``''`` into ``policy.model`` and surface as ``storage_unavailable`` (``t_605d71ef``,
    ``t_f59435a5``). Now it raises a ``RuntimeError`` carrying the kanban task id, board and
    assignee so the operator knows the profile simply has no model configured.
    """

    @pytest.fixture
    def fake_task(self, tmp_path):
        from hermes_cli.kanban_db import Task

        return Task(
            id="t_xxxxxxxx",
            title="fail-closed kanban test",
            body="should raise a clear no-model error",
            assignee="coder",
            status="running",
            priority=0,
            created_by="fin",
            created_at=1_700_000_000_000,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=str(tmp_path / "workspaces" / "t_xxxxxxxx"),
            claim_lock="abc123",
            claim_expires=None,
            tenant=None,
            branch_name=None,
            project_id=None,
            result=None,
            idempotency_key=None,
            consecutive_failures=0,
            worker_pid=None,
            last_failure_error=None,
            max_runtime_seconds=None,
            last_heartbeat_at=None,
            current_run_id=1,
            workflow_template_id=None,
            current_step_key=None,
            skills=None,
            model_override=None,
            provider_override=None,
            reasoning_effort=None,
            max_retries=None,
            goal_mode=False,
            goal_max_turns=None,
            session_id=None,
            block_kind=None,
        )

    @pytest.fixture
    def claim_lock(self):
        return "abc123"

    @pytest.fixture
    def fake_conn(self, fake_task, claim_lock):
        import sqlite3

        def fake_execute(sql, params=None):
            if "task_runs" in sql:
                row = dict(id=1, task_id=fake_task.id, status="running", claim_lock=claim_lock)
            elif "task_events" in sql and "owner_admitted" in sql:
                row = dict(payload=json.dumps(["default", fake_task.id, 1], separators=(",", ":")))
            else:
                raise AssertionError(f"unexpected query: {sql[:90]}")
            return type("FakeCursor", (), {"fetchone": lambda self: row})()

        class FakeConn:
            row_factory = None

            def execute(self, sql, params=None):
                return fake_execute(sql, params)

            def close(self):
                pass

        return FakeConn()

    def test_no_model_raises_clear_kanban_error(self, tmp_path, fake_task, claim_lock):
        from hermes_cli import kanban_db as kb
        import hermes_cli.anon_auth as anon_auth
        import sqlite3
        from gateway.session_kanban import build_kanban_policy

        class FakeAuthority:
            profile_id = str(tmp_path / "profiles" / "coder")

        class FakeActor:
            profile_id = str(tmp_path / "profiles" / "coder")
            capabilities = {"session:create"}

        connection = type(
            "FakeConnection", (), {"authority": FakeAuthority(), "actor": FakeActor(), "native_owner": True}
        )()

        params = {"board": "default", "task_id": fake_task.id, "run_id": 1, "claim_lock": claim_lock}
        config = {}  # no model configured anywhere

        # The production owner_admitted payload is a DICT with a 'request_id' key
        # (see gateway/session_kanban.py:156-158); bound[0] is index-able like sqlite3.Row.
        expected_request_id = "kanban:" + json.dumps(["default", fake_task.id, 1], separators=(",", ":"))

        class FakeRow(dict):
            def __getitem__(self, key):
                if isinstance(key, int):
                    return list(self.values())[key]
                return super().__getitem__(key)

        class FakeCursor:
            def execute(self, sql, *args):
                if "task_runs" in sql:
                    self._row = dict(id=1, task_id=fake_task.id, status="running", claim_lock=claim_lock)
                elif "task_events" in sql and "owner_admitted" in sql:
                    self._row = {"payload": json.dumps({"request_id": expected_request_id})}
                else:
                    raise AssertionError(f"unexpected query: {sql[:90]}")
                return self

            def fetchone(self):
                return FakeRow(self._row)

        class FakeConn:
            def execute(self, sql, *args):
                return FakeCursor().execute(sql, *args)

            def close(self):
                pass

        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("HERMES_HOME", str(tmp_path))
            # Isolated profile home with identity markers (satisfies named_profile_is_live).
            (tmp_path / "profiles").mkdir(parents=True)
            coder_home = tmp_path / "profiles" / "coder"
            coder_home.mkdir()
            (coder_home / "config.yaml").write_text("_config_version: 1\n", encoding="utf-8")
            (coder_home / ".env").write_text("", encoding="utf-8")
            mp.setattr(kb, "_require_slug", lambda slug: slug)
            mp.setattr(kb, "workspaces_root", lambda board: tmp_path / "workspaces")
            mp.setattr(kb, "kanban_db_path", lambda board: tmp_path / "kanban.db")
            mp.setattr(kb, "build_worker_context", lambda conn, task_id: json.dumps({"task_id": task_id}))
            mp.setattr(kb, "get_task", lambda conn, task_id: fake_task)
            mp.setattr(kb, "_retry_status_for_run", lambda conn, task_id, run_id: "ready")
            mp.setattr(sqlite3, "connect", lambda *a, **k: FakeConn())
            # Real config.yaml on disk has no model; the welcome trial is inactive.
            mp.setattr(anon_auth, "free_inference_active", lambda: False)

            workspace = tmp_path / "workspaces" / fake_task.id
            workspace.mkdir(parents=True)
            (workspace / "test.txt").write_text("x")

            with pytest.raises(RuntimeError) as exc:
                build_kanban_policy(connection, params, config)
            msg = str(exc.value)
            assert fake_task.id in msg, msg
            assert "default" in msg
            assert "coder" in msg
            assert "no model" in msg.lower()
            # The misleading storage error must never appear.
            assert "storage_unavailable" not in msg, msg
