"""Credential pre-flight before kanban worker spawn.

Structural fix for the ``managed_worker_failed`` class: a ready card whose
assignee profile lacks its provider key must be parked ``blocked`` /
``needs_input`` (reason ``<profile> / <provider> / hiányzó: <ENV>``) BEFORE
claim/spawn -- no worker started, no crash, no retry loop -- instead of dying
inside the agent constructor and requeueing forever.

Rate-limit/quota AuthErrors and pre-flight-internal exceptions fail open and
let the card through; only an affirmative missing-credential verdict blocks.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.auth import AuthError
from hermes_cli.auth_constants import CODEX_RATE_LIMITED_CODE


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_profile(home: Path, name: str, env: str = "") -> None:
    (home / "profiles" / name).mkdir(parents=True, exist_ok=True)
    (home / "profiles" / name / ".env").write_text(env)


def _row(provider: str | None, model: str | None) -> sqlite3.Row:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE tasks (provider_override, model_override)")
    con.execute("INSERT INTO tasks VALUES (?, ?)", (provider, model))
    return con.execute("SELECT provider_override, model_override FROM tasks").fetchone()


def _ready_task(conn, assignee: str, provider: str, model: str) -> str:
    return kb.create_task(
        conn,
        title="pre-flight probe",
        assignee=assignee,
        provider_override=provider,
        model_override=model,
        initial_status="ready",
    )


def _missing_key(provider: str = "nvidia", env_var: str = "NVIDIA_API_KEY") -> AuthError:
    return AuthError(
        f"No usable credentials found for provider '{provider}'. Set {env_var}.",
        provider=provider,
        code="missing_api_key",
    )


def _rate_limited() -> AuthError:
    return AuthError(
        "upstream quota wall",
        provider="nvidia",
        code=CODEX_RATE_LIMITED_CODE,
        relogin_required=False,
    )


def _patch_resolver(monkeypatch, fake):
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fake)


# ---------------------------------------------------------------------------
# Unit level: the gate function itself, hermetic (no live provider, no network)
# ---------------------------------------------------------------------------


def test_preflight_blocks_missing_assignee_profile_key(kanban_home, monkeypatch, all_assignees_spawnable):
    _make_profile(kanban_home, "preflight_neg")  # .env without any provider key
    touched: list[str] = []
    from hermes_constants import set_hermes_home_override as _real_set

    def spy(path):
        touched.append(str(path))
        return _real_set(path)

    monkeypatch.setattr("hermes_constants.set_hermes_home_override", spy)
    _patch_resolver(monkeypatch, lambda **kw: (_ for _ in ()).throw(_missing_key()))

    reason = kbd._preflight_check_assignee_credentials("preflight_neg", _row("nvidia", "nvidia/llama-3.1-8b"))

    assert reason == "preflight_neg / nvidia / hiányzó: NVIDIA_API_KEY"
    # Profile-scoped: the probe ran in the ASSIGNEE profile's HERMES_HOME, not
    # in the dispatcher's home where a key might legitimately live for another card.
    assert any("profiles/preflight_neg" in p for p in touched)
    assert not any(p.rstrip("/") == str(kanban_home) for p in touched)


def test_preflight_rate_limit_auth_error_fails_open(kanban_home, monkeypatch):
    _make_profile(kanban_home, "preflight_neg")
    attempted = []

    def fake(**kw):
        attempted.append(kw)
        raise _rate_limited()

    _patch_resolver(monkeypatch, fake)

    reason = kbd._preflight_check_assignee_credentials("preflight_neg", _row("nvidia", "nvidia/llama-3.1-8b"))

    assert reason is None
    assert attempted  # the probe ran; only the verdict failed open


def test_preflight_internal_exception_fails_open(kanban_home, monkeypatch):
    _patch_resolver(monkeypatch, lambda **kw: (_ for _ in ()).throw(RuntimeError("probe exploded")))

    assert kbd._preflight_check_assignee_credentials("preflight_neg", _row("nvidia", "nvidia/llama-3.1-8b")) is None


def test_preflight_configured_fallback_chain_fails_open(kanban_home, monkeypatch):
    _make_profile(kanban_home, "preflight_neg")
    resolved = []
    monkeypatch.setattr(
        "hermes_cli.fallback_config.get_fallback_chain", lambda cfg: [{"provider": "openrouter"}]
    )
    _patch_resolver(monkeypatch, lambda **kw: resolved.append(kw) or None)

    assert kbd._preflight_check_assignee_credentials("preflight_neg", _row("nvidia", "nvidia/llama-3.1-8b")) is None
    assert resolved == []  # a usable fallback chain means resolution is not attempted here


def test_preflight_no_provider_configured_passes(kanban_home, monkeypatch):
    assert kbd._preflight_check_assignee_credentials("preflight_neg", _row(None, None)) is None


# ---------------------------------------------------------------------------
# Integration level: dispatch_once parks the card and never calls spawn_fn
# ---------------------------------------------------------------------------


def _dispatch_once_with(conn, spawn_fn):
    return kbd.dispatch_once(conn, spawn_fn=spawn_fn)


def test_dispatch_parks_missing_key_card_blocked_no_spawn(kanban_home, monkeypatch, all_assignees_spawnable):
    _make_profile(kanban_home, "preflight_neg")
    _patch_resolver(monkeypatch, lambda **kw: (_ for _ in ()).throw(_missing_key()))

    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = _ready_task(conn, "preflight_neg", "nvidia", "nvidia/llama-3.1-8b")
        res = _dispatch_once_with(conn, fake_spawn)

    assert spawns == []                      # no worker process started
    assert tid not in [s[0] for s in res.spawned]
    assert tid in res.preflight_blocked
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    events = kb.list_events(conn, tid)
    blocked = [e for e in events if e.kind == "blocked"]
    assert blocked, f"no blocked task_event for {tid}: {events!r}"
    assert "preflight_neg / nvidia / hiányzó: NVIDIA_API_KEY" in str(blocked[-1].payload)


def test_dispatch_spawns_when_key_present(kanban_home, monkeypatch, all_assignees_spawnable):
    _make_profile(
        kanban_home, "preflight_pos", "NVIDIA_API_KEY=dummy-test-key-not-real-offline-probe\n"
    )
    _patch_resolver(monkeypatch, lambda **kw: {"provider": "nvidia", "api_mode": "anthropic_messages"})

    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = _ready_task(conn, "preflight_pos", "nvidia", "nvidia/llama-3.1-8b")
        res = _dispatch_once_with(conn, fake_spawn)

    assert spawns == [tid]                   # spawn path runs normally
    assert tid not in res.preflight_blocked
    assert kb.get_task(conn, tid).status != "blocked"


def test_dispatch_fail_open_internal_exception_still_spawns(kanban_home, monkeypatch, all_assignees_spawnable):
    _make_profile(kanban_home, "preflight_neg")
    _patch_resolver(monkeypatch, lambda **kw: (_ for _ in ()).throw(RuntimeError("probe exploded")))

    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = _ready_task(conn, "preflight_neg", "nvidia", "nvidia/llama-3.1-8b")
        res = _dispatch_once_with(conn, fake_spawn)

    assert spawns == [tid]
    assert tid not in res.preflight_blocked


def test_dispatch_rate_limit_auth_error_still_spawns(kanban_home, monkeypatch, all_assignees_spawnable):
    _make_profile(kanban_home, "preflight_neg")
    _patch_resolver(monkeypatch, lambda **kw: (_ for _ in ()).throw(_rate_limited()))

    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = _ready_task(conn, "preflight_neg", "nvidia", "nvidia/llama-3.1-8b")
        res = _dispatch_once_with(conn, fake_spawn)

    assert spawns == [tid]
    assert tid not in res.preflight_blocked
