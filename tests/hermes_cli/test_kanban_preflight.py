"""(b) stability layer — the kanban dispatcher preflight (`t_55e6fd81`).

Contract under test:

* a card carrying a broken pin (unknown ``model_override``, a model its
  provider does not offer — the measured ``gpt-5-nano`` → HTTP 400 class —, a
  missing credential, an unloadable skill, an unresolvable declared script) is
  parked ``blocked`` + reason **before** the worker spawns, with
  ``consecutive_failures`` untouched and no ``gave_up``;
* the four live models pass with **zero false positives**;
* transients (rate limit) and internal checker errors fail **open**.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_preflight as kp

# The four models the acceptance criteria name as "must never be blocked".
LIVE_MODELS = ["mimo-v2.6-flash", "minimax-m3.1-flash", "claude-haiku-5-5", "glm-5.3-flash"]

# A home-scoped catalog shaped like the real ``provider_models_cache.json``:
# the live models on their real provider, plus ``gpt-5-nano`` on a *different*
# provider — the "exists, but not on this backend" signature.
TEST_CATALOG = {
    "opencode-go": {
        "fp": None,
        "at": 0,
        "models": list(LIVE_MODELS),
    },
    "openai": {"fp": None, "at": 0, "models": ["gpt-5-nano"]},
}


def _clear_model_gate_caches() -> None:
    """``model_gate`` memoizes per process — never let one test's verdict leak."""
    from hermes_cli import model_gate

    model_gate._catalog_cache.clear()
    model_gate._result_cache.clear()


def _write_model_catalog(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "provider_models_cache.json"
    path.write_text(json.dumps(TEST_CATALOG), encoding="utf-8")
    return path


@pytest.fixture
def model_catalog():
    """Populate the active home's model cache (criterion 3 needs real knowledge)."""
    from hermes_constants import get_hermes_home

    _clear_model_gate_caches()
    path = _write_model_catalog(Path(get_hermes_home()))
    try:
        yield path
    finally:
        _clear_model_gate_caches()
        path.unlink(missing_ok=True)


class FakeTask:
    """Minimal ``kanban_db.Task`` stand-in: only the columns preflight reads."""

    def __init__(self, **kw):
        self.id = kw.get("id", "t_preflight")
        self.title = kw.get("title", "demo")
        self.body = kw.get("body")
        self.assignee = kw.get("assignee", "default")
        self.skills = kw.get("skills")
        self.model_override = kw.get("model_override")
        self.provider_override = kw.get("provider_override")
        self.script = kw.get("script")
        self.current_run_id = kw.get("current_run_id", 1)


@pytest.fixture
def provider_stub(monkeypatch):
    """Replace the read-only credential ladder with a controllable stub."""
    import hermes_cli.runtime_provider as rp

    state = {"calls": 0, "runtime": {"provider": ""}, "error": None}

    def _resolve(**kwargs):
        state["calls"] += 1
        if state["error"] is not None:
            raise state["error"]
        return dict(state["runtime"])

    monkeypatch.setattr(rp, "resolve_runtime_provider", _resolve)
    return state


@pytest.fixture
def skill_stub(monkeypatch):
    import tools.skills_tool as st

    state = {"payloads": {}}

    def _view(name, *args, **kwargs):
        payload = state["payloads"].get(name)
        if payload is None:
            return json.dumps({"success": False, "error": f"Skill '{name}' not found."})
        return json.dumps(payload)

    monkeypatch.setattr(st, "skill_view", _view)
    return state


# --------------------------------------------------------------------------- #
# Model (criterion 3: 0 false positives on the live models)
# --------------------------------------------------------------------------- #

def test_live_models_pass_with_no_provider_pin(model_catalog):
    for model in LIVE_MODELS:
        verdict = kp.check_model(FakeTask(model_override=model), None)
        assert verdict is None, f"{model} must pass, got {verdict}"


def test_live_models_pass_with_matching_provider(model_catalog):
    for model in LIVE_MODELS:
        verdict = kp.check_model(FakeTask(model_override=model), "opencode-go")
        assert verdict is None, f"{model} @ opencode-go must pass, got {verdict}"


def test_unknown_model_blocks_with_card_kind(model_catalog):
    verdict = kp.check_model(FakeTask(model_override="definitely-not-a-model-xyz"), None)
    assert verdict is not None
    kind, reason = verdict
    assert kind == kp.KIND_CARD_VALUE
    assert "definitely-not-a-model-xyz" in reason


def test_model_provider_mismatch_blocks_the_measured_signature(model_catalog):
    """``gpt-5-nano`` exists globally but not on the pinned backend — exactly
    the ``HTTP 400 Model is unavailable`` incident, caught pre-launch."""
    verdict = kp.check_model(FakeTask(model_override="gpt-5-nano"), "opencode-go")
    assert verdict is not None
    kind, reason = verdict
    assert kind == kp.KIND_CARD_VALUE
    assert "HTTP 400" in reason or "not a known model" in reason


def test_model_check_fails_open_without_any_catalog(monkeypatch):
    """A cache-less home can't tell 'unknown model' from 'unknown catalog' —
    blocking there would park healthy cards (fresh profile, isolated home)."""
    import hermes_constants as hc

    monkeypatch.setattr(hc, "get_hermes_home", lambda: Path("/nonexistent/home"))
    assert kp.check_model(FakeTask(model_override="definitely-not-a-model-xyz"), None) is None


def test_model_check_fails_open_when_the_gate_raises(monkeypatch):
    from hermes_cli import model_gate

    def _boom(*args, **kwargs):
        raise RuntimeError("catalog exploded")

    monkeypatch.setattr(model_gate, "model_known", _boom)
    assert kp.check_model(FakeTask(model_override="mimo-v2.6-flash"), None) is None


# --------------------------------------------------------------------------- #
# Provider credential
# --------------------------------------------------------------------------- #

def test_missing_credential_blocks(provider_stub):
    from hermes_cli.auth import AuthError

    provider_stub["error"] = AuthError("No credentials stored", provider="nvidia")
    kind, reason, _ = kp.check_provider(FakeTask(model_override="mimo-v2.6-flash"))
    assert kind == kp.KIND_ENVIRONMENT
    assert reason is not None and "credential missing" in reason


def test_rate_limit_is_transient_and_never_blocks(provider_stub):
    from hermes_cli.auth import CODEX_RATE_LIMITED_CODE, AuthError

    provider_stub["error"] = AuthError(
        "429 Too Many Requests", code=CODEX_RATE_LIMITED_CODE)
    kind, reason, _ = kp.check_provider(FakeTask(model_override="mimo-v2.6-flash"))
    assert (kind, reason) == (None, None), "a rate limit must never park a card"


def test_non_auth_provider_error_fails_open(provider_stub):
    provider_stub["error"] = RuntimeError("connection reset")
    kind, reason, _ = kp.check_provider(FakeTask(model_override="mimo-v2.6-flash"))
    assert (kind, reason) == (None, None)


def test_unpinned_card_skips_the_provider_ladder(provider_stub):
    assert kp.check_provider(FakeTask()) == (None, None, None)
    assert provider_stub["calls"] == 0, "an unpinned card must not probe credentials"


def test_resolved_provider_is_reported_for_the_model_check(provider_stub):
    provider_stub["runtime"] = {"provider": "opencode-go"}
    kind, reason, resolved = kp.check_provider(FakeTask(model_override="mimo-v2.6-flash"))
    assert (kind, reason) == (None, None)
    assert resolved == "opencode-go"


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #

def test_missing_skill_blocks(skill_stub):
    verdict = kp.check_skills(FakeTask(skills=["no-such-skill"]))
    assert verdict is not None
    kind, reason = verdict
    assert kind == kp.KIND_ENVIRONMENT
    assert "no-such-skill" in reason


def test_skill_setup_needed_blocks(skill_stub):
    skill_stub["payloads"]["forex-mt5-workflow"] = {
        "success": True, "setup_needed": True, "missing_prerequisites": ["mt5"],
    }
    verdict = kp.check_skills(FakeTask(skills=["forex-mt5-workflow"]))
    assert verdict is not None
    assert "mt5" in verdict[1]


def test_ready_skill_passes(skill_stub):
    skill_stub["payloads"]["obsidian"] = {"success": True}
    assert kp.check_skills(FakeTask(skills=["obsidian"])) is None


def test_skill_tool_error_fails_open(skill_stub, monkeypatch):
    import tools.skills_tool as st

    def _boom(*args, **kwargs):
        raise RuntimeError("scanner down")

    monkeypatch.setattr(st, "skill_view", _boom)
    assert kp.check_skills(FakeTask(skills=["obsidian"])) is None


def test_no_skills_is_a_pass():
    assert kp.check_skills(FakeTask()) is None
    assert kp.check_skills(FakeTask(skills=[])) is None


# --------------------------------------------------------------------------- #
# Script (opt-in `script:` directive — kanban tasks have no script column)
# --------------------------------------------------------------------------- #

def test_card_without_script_directive_passes():
    assert kp.check_script(FakeTask(body="ordinary card body")) is None


def test_missing_declared_script_blocks(tmp_path):
    task = FakeTask(body=f"do work\n\nscript: {tmp_path}/nope.py\n")
    verdict = kp.check_script(task, profile_home=str(tmp_path))
    assert verdict is not None
    kind, reason = verdict
    assert kind == kp.KIND_ENVIRONMENT
    assert "nope.py" in reason


def test_existing_absolute_script_passes(tmp_path):
    script = tmp_path / "run_me.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    task = FakeTask(body=f"script: {script}\n")
    assert kp.check_script(task, profile_home=str(tmp_path)) is None


def test_relative_script_resolves_under_profile_scripts(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "helper.py").write_text("x = 1\n", encoding="utf-8")
    task = FakeTask(body="script: helper.py\n")
    assert kp.check_script(task, profile_home=str(tmp_path)) is None


def test_relative_script_missing_lists_both_candidates(tmp_path):
    task = FakeTask(body="script: ghost.py\n")
    verdict = kp.check_script(task, profile_home=str(tmp_path))
    assert verdict is not None
    assert "ghost.py" in verdict[1]
    assert str(tmp_path / "scripts" / "ghost.py") in verdict[1]


def test_structured_script_attribute_wins(tmp_path):
    script = tmp_path / "attr.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    assert kp.check_script(FakeTask(script=str(script))) is None


# --------------------------------------------------------------------------- #
# Home
# --------------------------------------------------------------------------- #

def test_relative_home_blocks(tmp_path):
    verdict = kp.check_home("relative/dir")
    assert verdict is not None and verdict[0] == kp.KIND_ENVIRONMENT


def test_missing_home_blocks(tmp_path):
    verdict = kp.check_home(str(tmp_path / "gone"))
    assert verdict is not None and verdict[0] == kp.KIND_ENVIRONMENT


def test_absolute_existing_home_passes(tmp_path):
    assert kp.check_home(str(tmp_path)) is None


def test_unknown_home_passes():
    assert kp.check_home(None) is None


# --------------------------------------------------------------------------- #
# Entry point: fail-open + first-violation reporting
# --------------------------------------------------------------------------- #

def test_preflight_task_returns_none_pair_on_clean_card(model_catalog, provider_stub):
    assert kp.preflight_task(FakeTask()) == (None, None)


def test_preflight_task_reports_first_violation(model_catalog, provider_stub):
    kind, reason = kp.preflight_task(FakeTask(model_override="definitely-not-a-model-xyz"))
    assert kind == kp.KIND_CARD_VALUE and reason


def test_preflight_task_fails_open_when_a_checker_raises():
    class Exploding:
        @property
        def model_override(self):
            raise RuntimeError("boom")

        body = None
        skills = None
        script = None

    assert kp.preflight_task(Exploding()) == (None, None)


def test_preflight_task_survives_a_broken_scope_factory(model_catalog, provider_stub):
    def _broken_scope():
        raise RuntimeError("scope unavailable")

    kind, reason = kp.preflight_task(
        FakeTask(model_override="definitely-not-a-model-xyz"),
        scope_factory=_broken_scope,
    )
    assert kind == kp.KIND_CARD_VALUE, "a broken scope must not silence the checks"
    assert reason and "not a known model" in reason


# --------------------------------------------------------------------------- #
# Dispatcher integration: blocked, not gave_up, worker never spawned
# --------------------------------------------------------------------------- #

def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb

    kb.init_db()
    _clear_model_gate_caches()
    _write_model_catalog(home)
    return home


def _dispatch_env(monkeypatch, runtime):
    """Profile gate open + a controllable credential ladder."""
    from hermes_cli import profiles
    import hermes_cli.runtime_provider as rp

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    monkeypatch.setattr(rp, "resolve_runtime_provider", lambda **kwargs: dict(runtime))


def test_bad_model_pin_blocks_the_card_instead_of_spawning(tmp_path, monkeypatch, provider_stub):
    """Criterion: an unknown ``model_override`` → ``blocked`` + reason, never
    ``gave_up`` on a worker that never started, and the failure counter stays
    at zero (a config error must not eat the retry budget)."""
    _isolated_home(tmp_path, monkeypatch)
    _dispatch_env(monkeypatch, {"provider": "opencode-go"})

    spawned: list[str] = []

    def spawn(task, workspace, **kwargs):
        spawned.append(task.id)
        return 4242

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    try:
        with kbc.connect() as conn:
            tid = kb.create_task(
                conn, title="bad pin", assignee="default",
                model_override="gpt-5-nano",
            )
            res = kbd.dispatch_once(conn, dry_run=False, spawn_fn=spawn)
            task = kb.get_task(conn, tid)
            kinds = [e.kind for e in kb.list_events(conn, tid)]
            payloads = [e.payload for e in kb.list_events(conn, tid) if e.kind == "blocked"]
    finally:
        _clear_model_gate_caches()

    assert spawned == [], "a card that fails preflight must never spawn"
    assert task is not None and task.status == "blocked"
    assert task.consecutive_failures == 0, "a preflight block is not a failure"
    assert "gave_up" not in kinds
    assert payloads and "gpt-5-nano" in str(payloads[0])
    assert any(tid == got for got, _ in res.preflight_blocked)
    assert tid in res.auto_blocked, "the block must be visible to telemetry"


def test_healthy_card_still_spawns(tmp_path, monkeypatch, provider_stub):
    """The preflight must not become a blanket spawn-blocker (criterion:
    only a violated invariant fails a card)."""
    _isolated_home(tmp_path, monkeypatch)
    _dispatch_env(monkeypatch, {"provider": "opencode-go"})

    spawned: list[str] = []

    def spawn(task, workspace, **kwargs):
        spawned.append(task.id)
        return 4243

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    try:
        with kbc.connect() as conn:
            tid = kb.create_task(
                conn, title="clean card", assignee="default",
                model_override="mimo-v2.6-flash",
            )
            res = kbd.dispatch_once(conn, dry_run=False, spawn_fn=spawn)
            task = kb.get_task(conn, tid)
    finally:
        _clear_model_gate_caches()

    assert spawned == [tid], "a healthy pinned card must still reach its worker"
    assert res.preflight_blocked == []
    assert task is not None and task.status == "running"
