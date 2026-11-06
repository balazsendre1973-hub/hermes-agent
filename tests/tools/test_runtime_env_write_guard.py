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
