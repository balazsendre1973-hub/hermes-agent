"""T-23 / incident b022e5_e9fd0b71bd28: a cron script job must never inherit the
delegated-child Kanban write fence.

A ``no_agent``/monitor script runs as an OPERATOR action, but its spawn chain can
carry ``HERMES_DELEGATED_CHILD_CONTEXT`` (e.g. a gateway or session started inside a
fenced delegate context).  The marker then reaches ``ticket_intake.py``'s
``hermes kanban create/edit`` subprocesses and the run dies with::

    kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI

— measured 2026-11-12 10:04 CET, the T-10 ticket went uncreated
(incident b022e5_e9fd0b71bd28, job b022e5974240).  ``_run_job_script`` strips the
marker at spawn (``env.pop(DELEGATED_CHILD_ENV_MARKER, None)``); these tests pin
that strip: a plain spawn still carries the marker (the probe is sensitive), the
scheduler's spawn does not.

Scope note: the full ``hermes kanban create`` outcome is NOT asserted here — cron
tests run with ``HERMES_RUNTIME_DIR`` pointed at an empty store on purpose
(tests/cron/conftest.py::_no_managed_store), so the CLI's dependency venv must not
be selected inside the suite.  The strip is the mechanism this file pins; the
end-to-end create is verified live (fenced session -> ticket_intake.create_ticket
-> read-back, evidence on the T-23 card).
"""

from __future__ import annotations

import pytest

# The spawn path consults the installation manifest (read-only metadata of the real
# install root); the test-only home I/O guard (tests/conftest.py::_forbid_real_hermes_home_io)
# refuses that read while HERMES_HOME is isolated — same call as
# tests/hermes_cli/test_kanban_write_guard.py. The board's own isolation stays intact:
# the script itself only runs against the tmp HERMES_HOME's board.
pytestmark = pytest.mark.allow_real_home_io


@pytest.fixture()
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts don't leak (same pattern as
    tests/cron/test_cron_no_agent.py)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


_MARKER_PROBE = (
    "#!/usr/bin/env bash\n"
    'echo "marker=${HERMES_DELEGATED_CHILD_CONTEXT:-MISSING}"\n'
)


@pytest.fixture()
def fenced_spawn_chain(monkeypatch):
    """Put the delegated-child marker into this process env — the condition under
    which the 2026-11-12 intake run failed."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, "/home/endre/.hermes")
    return DELEGATED_CHILD_ENV_MARKER


def test_control_plain_spawn_carries_the_marker(fenced_spawn_chain, hermes_env):
    """Control: without the scheduler's strip a child WOULD see the marker — so the
    probe below is a real discriminator, not a test that always passes."""
    import subprocess

    out = subprocess.run(
        ["bash", "-c", 'echo "marker=${HERMES_DELEGATED_CHILD_CONTEXT:-MISSING}"'],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert out.stdout.strip() == "marker=/home/endre/.hermes"


def test_no_agent_script_never_sees_the_delegated_child_kanban_fence(
    hermes_env, fenced_spawn_chain,
):
    """Regression: ``_run_job_script`` strips ``HERMES_DELEGATED_CHILD_CONTEXT`` so a
    fenced spawn chain cannot break ticket_intake's ``hermes kanban create`` (T-10)."""
    from cron.scheduler_script import _run_job_script

    (hermes_env / "scripts" / "fence_probe.sh").write_text(_MARKER_PROBE, encoding="utf-8")

    ok, output = _run_job_script("fence_probe.sh")
    assert ok is True, output
    assert output.strip() == "marker=MISSING"
