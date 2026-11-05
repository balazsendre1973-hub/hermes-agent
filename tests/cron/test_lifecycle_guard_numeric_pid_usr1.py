"""Branch F: numeric-PID ``kill -USR1 <gateway-pid>`` is blocked by the lifecycle guard.

The live gateway PID is resolved host-wide (host rendezvous record / launch-home
``gateway.pid`` via :func:`gateway.status.get_host_gateway_pid`), never via this process's
HERMES_HOME — a worker's HERMES_HOME is its profile home, which has no ``gateway.pid``.

Covers card t_50890a85 criteria 1-2: the positive case blocks, the negative cases pass.
"""

import os

import pytest

from cron import lifecycle_guard

GATEWAY_PID = 477345


def _patch_host_gateway_pid(monkeypatch, pid: int) -> None:
    monkeypatch.setattr("gateway.status.get_host_gateway_pid", lambda: pid)


@pytest.mark.parametrize(
    "command",
    [
        "kill -USR1 477345",
        "kill -s USR1 477345",
        "kill --signal USR1 477345",
        "kill -10 477345",
        "sudo kill -USR1 477345",          # transparent wrapper peeled
        "bash -c 'kill -USR1 477345'",     # kill as an operand of the executed command
    ],
)
def test_numeric_pid_usr1_against_live_gateway_is_blocked(monkeypatch, command):
    _patch_host_gateway_pid(monkeypatch, GATEWAY_PID)
    unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle(command)
    assert unsafe is True, command
    assert refusal is None
    assert lifecycle_guard.contains_gateway_lifecycle_command(command) is True, command


@pytest.mark.parametrize(
    "command",
    [
        "kill -USR1 12345",          # pid is not the gateway
        "kill -s USR1 12345",
        "kill -9 477345",            # wrong signal (SIGKILL): not in Branch F's scope
        "kill -9 12345",
        f"kill -USR1 {os.getpid()}",  # own process, not the gateway
        "kill -USR1",                 # no pid
        "kill -l USR1",               # lists signals, sends nothing
        "kill --list USR1",
        "pkill -USR1 477345",         # pkill matches names, not PIDs
    ],
)
def test_numeric_pid_usr1_negatives_pass(monkeypatch, command):
    _patch_host_gateway_pid(monkeypatch, GATEWAY_PID)
    unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle(command)
    assert unsafe is False, command


def test_unresolvable_gateway_pid_fails_open(monkeypatch):
    """Cannot prove which PID is the gateway -> numeric-PID kills are not newly blocked
    (Branch D still covers token-shaped kills)."""
    monkeypatch.setattr("gateway.status.get_host_gateway_pid", lambda: None)
    assert lifecycle_guard.scan_gateway_lifecycle("kill -USR1 477345") == (False, None)


def test_check_gateway_lifecycle_raises_on_usr1_gateway_pid(monkeypatch):
    _patch_host_gateway_pid(monkeypatch, GATEWAY_PID)
    with pytest.raises(lifecycle_guard.GatewayLifecycleBlocked):
        lifecycle_guard.check_gateway_lifecycle("kill -USR1 477345")


def test_branch_a_d_still_blocked(monkeypatch):
    _patch_host_gateway_pid(monkeypatch, GATEWAY_PID)
    for command in (
        "hermes gateway restart",
        "systemctl --user restart hermes-gateway",
        "pkill -f hermes gateway",
    ):
        unsafe, refusal = lifecycle_guard.scan_gateway_lifecycle(command)
        assert unsafe is True, command


def test_usr1_rejection_text_names_drain_and_outside_route_only():
    # Criterion 4: the block text names the drain (1800 s wait for in-flight turns) and that
    # workers die immediately (exit 130) - SIGUSR1 is not a plain TERM.
    text = lifecycle_guard.USR1_GATEWAY_KILL_REJECTION
    for phrase in ("gateway drain", "1800 s", "in-flight turns", "exit 130", "workers are interrupted"):
        assert phrase in text, text
    # Criterion 5: the only restart route recommended runs outside the gateway; no command that
    # could be executed from inside the gateway process is suggested.
    assert "Run `hermes gateway restart` from a separate shell outside the running gateway" in text
    assert "systemctl" not in text
    assert "pkill" not in text


def test_is_gateway_context_process_wakes_worker(monkeypatch):
    # Criterion 3 mechanism: a worker (markers inherited, HERMES_HOME = profile home) is inside
    # the supervised gateway although it does not own the gateway PID file. The old pid-ownership
    # gate (_is_supervised_gateway_process) stays strict.
    from tools import process_registry

    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD", raising=False)
    monkeypatch.setattr("gateway.status.get_host_gateway_pid", lambda: 477345)
    assert process_registry._is_gateway_context_process() is False

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    assert process_registry._is_gateway_context_process() is True

    monkeypatch.setattr("gateway.status.get_host_gateway_pid", lambda: None)
    assert process_registry._is_gateway_context_process() is False

    monkeypatch.setattr("gateway.status.get_running_pid", lambda *, cleanup_stale=False: os.getpid())
    monkeypatch.setenv("INVOCATION_ID", "x")
    monkeypatch.setattr("gateway.status.get_host_gateway_pid", lambda: 477345)
    assert process_registry._is_supervised_gateway_process() is True
    assert process_registry._is_gateway_context_process() is True
