"""Layer (c) — the read-only model pin (t_55e6fd81): one tier decides the model.

Verdicts live in ``hermes_cli.model_pin_drift``; this file covers the module,
the write-gate refusal (``create_task`` / ``set_model_override``), the
preflight drift warning and the ``build_kanban_policy`` drop.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# model_pin_drift — the shared module
# ---------------------------------------------------------------------------


def _profile(tmp_path: Path, model_block: str) -> Path:
    home = tmp_path / "profiles" / "alpha"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(model_block, encoding="utf-8")
    return home


class TestHomeModelPin:
    def test_reads_model_default_and_provider(self, tmp_path):
        from hermes_cli.model_pin_drift import home_model_pin

        home = _profile(tmp_path, "model:\n  default: mimo-v2.6-flash\n  provider: opencode-go\n")
        assert home_model_pin(str(home)) == ("mimo-v2.6-flash", "opencode-go")

    def test_missing_config_is_fail_open(self, tmp_path):
        from hermes_cli.model_pin_drift import home_model_pin

        assert home_model_pin(str(tmp_path / "no-such-profile")) == (None, None)
        assert home_model_pin(None) == (None, None)

    def test_config_without_model_is_no_pin(self, tmp_path):
        from hermes_cli.model_pin_drift import home_model_pin

        home = _profile(tmp_path, "{}\n")
        assert home_model_pin(str(home)) == (None, None)

    def test_bare_root_model_string(self, tmp_path):
        from hermes_cli.model_pin_drift import home_model_pin

        home = _profile(tmp_path, "model: some-model-id\n")
        assert home_model_pin(str(home)) == ("some-model-id", None)

    def test_broken_yaml_fails_open(self, tmp_path):
        from hermes_cli.model_pin_drift import home_model_pin

        home = _profile(tmp_path, "model: [unclosed\n")
        assert home_model_pin(str(home)) == (None, None)


class TestCardOverrideDrift:
    def test_matching_pin_is_not_drift(self, tmp_path):
        from hermes_cli.model_pin_drift import card_override_drift

        home = _profile(tmp_path, "model:\n  default: mimo-v2.6-flash\n  provider: opencode-go\n")
        assert card_override_drift("mimo-v2.6-flash", "opencode-go", profile_home=str(home)) is None
        # Provider omitted = inherit the profile provider → also not drift.
        assert card_override_drift("mimo-v2.6-flash", None, profile_home=str(home)) is None

    def test_divergent_model_is_drift(self, tmp_path):
        from hermes_cli.model_pin_drift import card_override_drift

        home = _profile(tmp_path, "model:\n  default: mimo-v2.6-flash\n")
        reason = card_override_drift("glm-5.3-flash", None, profile_home=str(home))
        assert reason is not None and "diverges" in reason

    def test_divergent_provider_is_drift(self, tmp_path):
        from hermes_cli.model_pin_drift import card_override_drift

        home = _profile(tmp_path, "model:\n  default: mimo-v2.6-flash\n  provider: opencode-go\n")
        reason = card_override_drift("mimo-v2.6-flash", "openrouter", profile_home=str(home))
        assert reason is not None and "provider" in reason

    def test_no_card_pin_is_never_drift(self, tmp_path):
        from hermes_cli.model_pin_drift import card_override_drift

        home = _profile(tmp_path, "model:\n  default: x\n")
        assert card_override_drift(None, None, profile_home=str(home)) is None

    def test_no_home_pin_fails_open(self, tmp_path):
        from hermes_cli.model_pin_drift import card_override_drift

        home = _profile(tmp_path, "{}\n")
        assert card_override_drift("anything", None, profile_home=str(home)) is None

    def test_report_shape(self, tmp_path):
        from hermes_cli.model_pin_drift import model_pin_drift_report

        home = _profile(tmp_path, "model:\n  default: mimo-v2.6-flash\n  provider: opencode-go\n")
        report = model_pin_drift_report("glm-5.3-flash", None, profile_home=str(home))
        assert report["card_model"] == "glm-5.3-flash"
        assert report["home_model"] == "mimo-v2.6-flash"
        assert report["drift"] is not None
        assert isinstance(report["managed_pins"], dict)

    def test_managed_pins_shape(self):
        from hermes_cli.model_pin_drift import managed_model_pins

        pins = managed_model_pins()
        assert isinstance(pins, dict)
        # Whatever the host pins, only model/cron-model keys may appear.
        assert all(key.startswith(("model.", "cron.model")) for key in pins)


# ---------------------------------------------------------------------------
# Write gate — create / set-model refuse a divergent pin
# ---------------------------------------------------------------------------


@pytest.fixture()
def board(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    import hermes_cli.model_gate as mg

    mg.clear_cache()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(tmp_path / "profiles" / name),
    )
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    yield kb, kbc
    kb._INITIALIZED_PATHS.clear()


@pytest.fixture()
def pinned_profile(tmp_path):
    # Catalog-valid pair for the sandbox (static catalogs only): gpt-4o is in
    # openai's static list, so require_model passes and only the DRIFT rule can
    # fire in these tests.
    home = tmp_path / "profiles" / "alpha"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  default: gpt-4o\n  provider: openai\n", encoding="utf-8",
    )
    return home


class TestWriteGateModelPin:
    def test_create_refuses_divergent_pin(self, board, pinned_profile):
        kb, kbc = board
        with closing(kbc.connect()) as conn, pytest.raises(ValueError, match="diverges"):
            kb.create_task(
                conn, title="t", assignee="alpha",
                model_override="glm-5.3-flash", provider_override="opencode-go",
            )

    def test_create_accepts_matching_pin(self, board, pinned_profile):
        kb, kbc = board
        with closing(kbc.connect()) as conn:
            tid = kb.create_task(
                conn, title="t", assignee="alpha",
                model_override="gpt-4o", provider_override="openai",
            )
            assert kb.get_task(conn, tid).model_override == "gpt-4o"

    def test_create_without_home_pin_still_allowed(self, board):
        kb, kbc = board  # profiles/beta has no config → no pin → fail-open
        with closing(kbc.connect()) as conn:
            tid = kb.create_task(
                conn, title="t", assignee="beta",
                model_override="loop-model", provider_override="custom",
            )
            assert kb.get_task(conn, tid).model_override == "loop-model"

    def test_set_model_refuses_divergent_pin(self, board, pinned_profile):
        kb, kbc = board
        with closing(kbc.connect()) as conn:
            tid = kb.create_task(conn, title="t", assignee="alpha")
            with pytest.raises(ValueError, match="diverges"):
                kb.set_model_override(conn, tid, "glm-5.3-flash", provider="opencode-go")
            assert kb.get_task(conn, tid).model_override is None

    def test_set_model_accepts_matching_pin(self, board, pinned_profile):
        kb, kbc = board
        with closing(kbc.connect()) as conn:
            tid = kb.create_task(conn, title="t", assignee="alpha")
            assert kb.set_model_override(conn, tid, "gpt-4o", provider="openai")

    def test_clearing_always_allowed(self, board, pinned_profile):
        kb, kbc = board
        with closing(kbc.connect()) as conn:
            tid = kb.create_task(
                conn, title="t", assignee="alpha",
                model_override="gpt-4o", provider_override="openai",
            )
            assert kb.set_model_override(conn, tid, None)

    def test_gate_kill_switch_disables_the_pin_rule(self, board, pinned_profile, monkeypatch):
        kb, kbc = board
        monkeypatch.setenv("HERMES_KANBAN_WRITE_GATE", "off")
        with closing(kbc.connect()) as conn:
            tid = kb.create_task(
                conn, title="t", assignee="alpha", model_override="loop-model",
            )
            assert kb.get_task(conn, tid).model_override == "loop-model"


# ---------------------------------------------------------------------------
# Preflight — drift MONITORING (warn, never block)
# ---------------------------------------------------------------------------


class TestPreflightDriftWarning:
    def _task(self, **kw):
        from types import SimpleNamespace

        defaults = dict(
            id="t_drift", model_override=None, provider_override=None,
            skills=None, script=None, home=None, assignee="alpha",
        )
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    @pytest.fixture(autouse=True)
    def _stub_provider_ladder(self, monkeypatch):
        """The provider-credential ladder is not under test here (it needs a
        profile secret scope); pinning a model must not trip it into a
        'capability' block while we assert the DRIFT behaviour."""
        from hermes_cli import kanban_preflight as kp

        monkeypatch.setattr(kp, "check_provider", lambda task: (None, None, None))

    def test_divergent_pin_warns_but_never_blocks(self, tmp_path, caplog):
        import logging
        from hermes_cli import kanban_preflight as kp

        home = _profile(tmp_path, "model:\n  default: mimo-v2.6-flash\n")
        task = self._task(model_override="glm-5.3-flash")
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_preflight"):
            kind, reason = kp.preflight_task(task, assignee="alpha", profile_home=str(home))
        assert (kind, reason) == (None, None)  # monitoring only — no block
        assert any("diverges" in rec.message for rec in caplog.records)

    def test_matching_pin_is_silent(self, tmp_path, caplog):
        import logging
        from hermes_cli import kanban_preflight as kp

        home = _profile(tmp_path, "model:\n  default: mimo-v2.6-flash\n")
        task = self._task(model_override="mimo-v2.6-flash")
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_preflight"):
            kind, reason = kp.preflight_task(task, assignee="alpha", profile_home=str(home))
        assert (kind, reason) == (None, None)
        assert not [rec for rec in caplog.records if "diverges" in rec.message]

    def test_unpinned_card_is_silent(self, tmp_path, caplog):
        import logging
        from hermes_cli import kanban_preflight as kp

        home = _profile(tmp_path, "model:\n  default: mimo-v2.6-flash\n")
        task = self._task()
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_preflight"):
            kind, reason = kp.preflight_task(task, assignee="alpha", profile_home=str(home))
        assert (kind, reason) == (None, None)
        assert not [rec for rec in caplog.records if "diverges" in rec.message]


# ---------------------------------------------------------------------------
# build_kanban_policy — a divergent pin is DROPPED at session build
# ---------------------------------------------------------------------------


class TestSessionPolicyDrop:
    def _build(self, tmp_path, monkeypatch, *, card_model, card_provider, home_block):
        """Create+claim a card, then build its session policy.

        Order matters: the card is created BEFORE the profile config exists
        (a divergent pin would be refused at create time by the write gate),
        so this harness exercises the runtime drop for PRE-EXISTING pins.
        """
        from types import SimpleNamespace

        from gateway.session_contract import SessionActor
        from gateway.session_kanban import build_kanban_policy
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        import hermes_cli.model_gate as mg

        mg.clear_cache()
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        monkeypatch.setattr(
            "hermes_cli.profiles.resolve_profile_env",
            lambda name: str(tmp_path) if name == "default" else str(tmp_path / name),
        )
        kb._INITIALIZED_PATHS.clear()
        kb.init_db()
        workspace = tmp_path / "ws"
        workspace.mkdir(exist_ok=True)
        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "task"\n', encoding="utf-8",
        )
        overrides = {}
        if card_model:
            overrides = {"model_override": card_model, "provider_override": card_provider}
        with closing(kbc.connect()) as conn:
            tid = kb.create_task(
                conn, title="t", assignee="default",
                workspace_kind="dir", workspace_path=str(workspace), **overrides,
            )
            task = kb.claim_task(conn, tid)
        assert task is not None
        # Profile home for assignee 'default' is tmp_path (monkeypatch above),
        # so its config.yaml is the pin source the drop must read.
        (tmp_path / "config.yaml").write_text(home_block, encoding="utf-8")
        actor = SessionActor("owner", str(tmp_path), frozenset({"session:create"}), "transport")
        connection = SimpleNamespace(
            authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True,
        )
        params = dict(
            board="default", task_id=task.id,
            run_id=task.current_run_id, claim_lock=task.claim_lock,
        )
        config = {
            "model": {"default": "profile-model", "provider": "profile-provider"},
            "platform_toolsets": {"cli": ["terminal"]},
        }
        # A dropped pin leaves params_policy without a model, so
        # build_kanban_policy falls back to `from gateway.run import
        # _resolve_gateway_model`. Importing the REAL gateway.run pulls
        # hermes_bootstrap, which reads the install manifest under the REAL
        # home — the home_io_guard (tests/home_io_guard.py) refuses that in
        # tests, exactly as in the two pre-existing gateway.run import
        # failures. Stub the module; the fallback's job is only to resolve the
        # config default.
        import sys
        from types import ModuleType

        fake_run = ModuleType("gateway.run")
        fake_run._resolve_gateway_model = lambda cfg: "profile-model"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "gateway.run", fake_run)
        policy, _secrets = build_kanban_policy(connection, params, config)
        return policy

    def test_divergent_pin_is_dropped(self, tmp_path, monkeypatch, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="gateway.session_kanban"):
            policy = self._build(
                tmp_path, monkeypatch,
                card_model="glm-5.3-flash", card_provider="opencode-go",
                home_block="model:\n  default: profile-model\n  provider: profile-provider\n",
            )
        # The card pin is gone: the session resolves the profile pin instead.
        assert policy.model == "profile-model"
        assert policy.provider != "opencode-go"
        assert any("dropping divergent card model pin" in rec.message for rec in caplog.records)

    def test_matching_pin_is_kept(self, tmp_path, monkeypatch):
        policy = self._build(
            tmp_path, monkeypatch,
            card_model="profile-model", card_provider="profile-provider",
            home_block="model:\n  default: profile-model\n  provider: profile-provider\n",
        )
        assert policy.model == "profile-model"
        assert policy.provider == "profile-provider"

    def test_no_home_pin_keeps_the_card_pin(self, tmp_path, monkeypatch):
        policy = self._build(
            tmp_path, monkeypatch,
            card_model="loop-model", card_provider="custom",
            home_block="{}\n",
        )
        assert policy.model == "loop-model"
        assert policy.provider == "custom"
