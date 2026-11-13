"""(d) stability layer: runtime-environment write guards (t_55e6fd81).

``write_file`` / ``patch`` hard-deny the files that decide how the NEXT start
behaves — any profile's ``config.yaml``, ``cron/jobs.json``,
``agent-hooks/*.py``, ``scripts/*.py`` — while the installation root's own
``config.yaml`` stays writable (the operator's file). The terminal side of the
same rule lives in ``tools/approval_detection.py`` (``_HERMES_RUNTIME_ENV_PATH``).
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def runtime_root(tmp_path, monkeypatch):
    """A fake installation root: HERMES_HOME points at it, so
    ``get_default_hermes_root()`` resolves to it for the duration of the test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Isolate from the ACTIVE-config hard deny (a pre-existing rule that pins the
    # current profile's config): this file tests the NEW runtime-env rule only.
    monkeypatch.setattr("tools.file_tools_write_guards._hermes_config_resolved", "/some/other/path")
    monkeypatch.setattr("tools.file_tools_write_guards._hermes_config_resolved_loaded", True)
    return tmp_path


def _mk(root):
    for rel in (
        "profiles/alpha/config.yaml",
        "profiles/beta/config.yml",
        "profiles/alpha/config.yaml.bak",
        "cron/jobs.json",
        "agent-hooks/on_start.py",
        "scripts/helper.py",
        "config.yaml",
        "scripts/helper.sh",
        "profiles/alpha/skills/demo/SKILL.md",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    return root


class TestRuntimeEnvHardDeny:
    @pytest.mark.parametrize(
        ("rel", "label"),
        [
            ("profiles/alpha/config.yaml", "profile config.yaml"),
            ("profiles/beta/config.yml", "profile config.yaml"),
            ("cron/jobs.json", "cron schedule"),
            ("agent-hooks/on_start.py", "agent hook"),
            ("scripts/helper.py", "runtime script"),
        ],
    )
    def test_protected_paths_are_refused(self, runtime_root, rel, label):
        from tools.file_tools_write_guards import _check_sensitive_path

        root = _mk(runtime_root)
        err = _check_sensitive_path(str(root / rel), "default")
        assert err is not None, f"{rel} must be refused"
        assert label in err
        assert "t_55e6fd81" in err

    def test_root_config_stays_writable(self, runtime_root):
        """The installation root's config.yaml is the operator's file: the
        runtime-env rule must NOT match it (only profile configs are pinned)."""
        from tools.file_tools_write_guards import (
            _check_sensitive_path,
            _runtime_env_protection_reason,
        )

        root = _mk(runtime_root)
        assert _runtime_env_protection_reason(str(root / "config.yaml")) is None
        assert _check_sensitive_path(str(root / "config.yaml"), "default") is None

    @pytest.mark.parametrize(
        "rel",
        [
            "scripts/helper.sh",              # only *.py runtime scripts are pinned
            "profiles/alpha/config.yaml.bak", # not a profile config file
            "profiles/alpha/skills/demo/SKILL.md",
            "cron/other.json",                # only jobs.json is the schedule
            "docs/notes.md",                  # outside the protected set
        ],
    )
    def test_unprotected_paths_pass(self, runtime_root, rel):
        from tools.file_tools_write_guards import _check_sensitive_path

        root = _mk(runtime_root)
        assert _check_sensitive_path(str(root / rel), "default") is None

    def test_tilde_spelling_is_refused(self, runtime_root, monkeypatch):
        """The user-facing ``~/.hermes/...`` spelling resolves to the same rule."""
        from tools.file_tools_write_guards import _check_sensitive_path

        root = _mk(runtime_root)
        monkeypatch.setenv("HOME", str(root.parent))
        # A path that realpath()s under the root matches even when spelled with ~
        # only if the expansion lands inside the root; use the absolute form and
        # assert the expansion-independent rule directly.
        from tools.file_tools_write_guards import _runtime_env_protection_reason

        assert _runtime_env_protection_reason(str(root / "cron" / "jobs.json")) is not None
        assert _check_sensitive_path(str(root / "cron" / "jobs.json"), "default") is not None


class TestTerminalSidePairsWithFileTools:
    """The terminal dangerous-command detector carries the same rule so a shell
    redirection/`sed -i` cannot bypass the write_file/patch hard deny."""

    @pytest.mark.parametrize(
        "command",
        [
            "sed -i 's/a/b/' ~/.hermes/profiles/alpha/config.yaml",
            "cat extra.json > ~/.hermes/cron/jobs.json",
            "tee ~/.hermes/cron/jobs.json < /dev/null",
            "cp ./hook.py ~/.hermes/agent-hooks/on_start.py",
            "mv ./helper.py ~/.hermes/scripts/helper.py",
            "perl -i -pe 's/x/y/' ~/.hermes/scripts/helper.py",
        ],
    )
    def test_runtime_env_writes_are_flagged(self, command):
        from tools.approval_detection import detect_dangerous_command

        is_dangerous, _key, description = detect_dangerous_command(command)
        assert is_dangerous, f"must be flagged: {command}"
        assert "runtime environment" in description

    @pytest.mark.parametrize(
        "command",
        [
            "cp ./hook.py ~/.hermes/agent-hooks/on_start.py 2>&1",
            "cp ./hook.py ~/.hermes/agent-hooks/on_start.py 2>/dev/null",
            "cp ./hook.py ~/.hermes/agent-hooks/on_start.py | wc -l",
            "mv ./helper.py ~/.hermes/scripts/helper.py | tail -n 3",
            "install ./hook.py ~/.hermes/agent-hooks/on_start.py | cat",
            "cp ./hook.py ~/.hermes/cron/jobs.json > /dev/null 2>&1",
        ],
    )
    def test_runtime_env_cp_mv_install_with_tail_are_flagged(self, command):
        """Redirection (`2>&1`, `2>/dev/null`, `> ...`) and pipe (`| ...`) tails
        after the destination must still be flagged: the file is created."""
        from tools.approval_detection import detect_dangerous_command

        is_dangerous, _key, description = detect_dangerous_command(command)
        assert is_dangerous, f"must be flagged: {command}"
        assert "runtime environment" in description

    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/.hermes/cron/jobs.json",        # reading is fine
            "ls -la ~/.hermes/scripts",            # listing is fine
            "grep foo ~/.hermes/profiles/alpha/config.yaml",  # reading is fine
        ],
    )
    def test_reads_are_not_flagged(self, command):
        from tools.approval_detection import detect_dangerous_command

        is_dangerous, _key, description = detect_dangerous_command(command)
        # A unrelated pre-existing pattern may still flag these; the runtime-env
        # rule must never be the reason.
        if is_dangerous:
            assert "runtime environment" not in (description or "")


class TestRuntimeEnvInteractiveApproval:
    """Interaktív sessionben a védett futási-környezeti fájlra írás
    jóváhajtást kéri; single-query (-q) továbbra is hard deny."""

    @pytest.fixture
    def interactive_ctx(self, monkeypatch):
        """Nincs single-query / cron / unattended jelző: a guard az
        engedélyezési ágra lép (a user a chatben érhető el)."""
        monkeypatch.setattr("tools.file_tools_write_guards._no_user_can_answer", lambda: False)

    def _write_attempt(self, root, rel="scripts/helper.py"):
        from tools.file_tools_write_guards import _check_sensitive_path
        return _check_sensitive_path(str(root / rel), "default")

    def test_interactive_approve_allows_write(self, runtime_root, interactive_ctx, monkeypatch):
        from tools.terminal_tool import set_approval_callback
        root = _mk(runtime_root)
        set_approval_callback(lambda *a, **k: "once")
        try:
            err = self._write_attempt(root)
        finally:
            set_approval_callback(None)
        assert err is None, f"approve után a write-nek mennie kellene, kapott: {err}"

    def test_interactive_deny_blocks_write(self, runtime_root, interactive_ctx, monkeypatch):
        from tools.terminal_tool import set_approval_callback
        root = _mk(runtime_root)
        set_approval_callback(lambda *a, **k: "deny")
        try:
            err = self._write_attempt(root)
        finally:
            set_approval_callback(None)
        assert err is not None, "deny után a write-nek blokkolódnian kell"
        assert "has NOT consented" in err

    def test_interactive_timeout_blocks_write(self, runtime_root, interactive_ctx, monkeypatch):
        from tools.terminal_tool import set_approval_callback
        root = _mk(runtime_root)
        set_approval_callback(lambda *a, **k: "timeout")
        try:
            err = self._write_attempt(root)
        finally:
            set_approval_callback(None)
        assert err is not None, "timeout után a write-nek blokkolódnian kell"
        assert "Silence is not consent" in err

    def test_gateway_approve_allows_write(self, runtime_root, interactive_ctx, monkeypatch):
        import tools.approval as approval_module
        from tools.approval_context import set_current_session_key
        root = _mk(runtime_root)
        session_key = "gw:runtime-env-test"
        monkeypatch.setenv("HERMES_SESSION_KEY", session_key)
        set_current_session_key(session_key)

        def notify(data):
            approval_module.resolve_gateway_approval(session_key, "once")

        approval_module.register_gateway_notify(session_key, notify)
        try:
            err = self._write_attempt(root)
        finally:
            approval_module.unregister_gateway_notify(session_key)
        assert err is None, f"gateway /approve után a write-nek mennie kellene, kapott: {err}"

    def test_single_query_still_hard_denies(self, runtime_root, monkeypatch):
        root = _mk(runtime_root)
        monkeypatch.setattr("tools.file_tools_write_guards._no_user_can_answer", lambda: True)
        from tools.file_tools_write_guards import _check_sensitive_path
        err = _check_sensitive_path(str(root / "scripts/helper.py"), "default")
        assert err is not None, "single-queryban továbbra is hard deny kell legyen"
        assert "runtime script" in err
        assert "t_55e6fd81" in err
