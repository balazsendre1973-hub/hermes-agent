"""Diagnostics must distinguish policy rejection from inspected job properties."""

import json
import plistlib

from tools.terminal_tool_guards import gateway_lifecycle_block


def test_bootstrap_rejection_does_not_invent_keepalive(tmp_path, monkeypatch):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_gateway_context_process", lambda: True)
    plist = tmp_path / "com.example.schedule.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "com.example.schedule",
        "ProgramArguments": ["/bin/true"],
        "RunAtLoad": False,
        "StartCalendarInterval": {"Hour": 9, "Minute": 0},
    }))
    blocked = gateway_lifecycle_block(
        command=f"launchctl bootstrap gui/501 {plist}",
        env=None, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="diagnostic-test",
    )
    assert blocked is not None
    result = json.loads(blocked)
    assert result["exit_code"] == 1
    assert result["error"]


def test_interpreter_kill_rejection_names_the_owned_process_route(tmp_path, monkeypatch):
    """Scheduled-Task topology (#113667): only ``HERMES_SUPERVISED_CHILD`` marks the launch, and an
    image-name kill of the interpreter is refused with the ``proc_*`` / explicit-PID route named.
    Other images and explicit PIDs pass."""
    import os

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    for marker in ("INVOCATION_ID", "XPC_SERVICE_NAME", "HERMES_S6_SUPERVISED_CHILD", "HERMES_GATEWAY_EXTERNAL_SUPERVISOR"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *a, **k: os.getpid())

    def run(command):
        return gateway_lifecycle_block(
            command=command, env=None, env_type="local", cwd=str(tmp_path), workdir=None,
            session_key="diagnostic-test",
        )

    blocked = json.loads(run("taskkill /F /IM python.exe 2>/dev/null | head -2"))
    assert blocked["exit_code"] == 1
    assert "proc_" in blocked["error"] and "explicit PID" in blocked["error"]
    assert "proc_" in json.loads(run("pkill -9 python3"))["error"]
    assert run("taskkill /F /IM agent-browser.exe /T") is None
    assert run("taskkill /F /PID 46544") is None
    # Absence on the wrong side: a plain foreground `hermes gateway run` carries no launch marker.
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD")
    assert run("pkill -9 python3") is None


def test_worker_inside_gateway_is_woken_up_by_the_guard(tmp_path, monkeypatch):
    """Criterion 3: a worker process (HERMES_SUPERVISED_CHILD=1, HERMES_HOME=profile home)
    is inside the supervised gateway even though it does not own the gateway PID file, so the
    terminal guard wakes up there too.

    The old pid-ownership gate (_is_supervised_gateway_process) is deliberately overridden to
    False here: the wake-up must come from the gateway-PID live-check, not from pid equality.
    """
    import os

    from tools import process_registry
    from cron.lifecycle_guard import USR1_GATEWAY_KILL_REJECTION

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    for marker in ("INVOCATION_ID", "XPC_SERVICE_NAME", "HERMES_S6_SUPERVISED_CHILD", "HERMES_GATEWAY_EXTERNAL_SUPERVISOR"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setattr("gateway.status.get_host_gateway_pid", lambda: 477345)
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: False)

    def run(command):
        return gateway_lifecycle_block(
            command=command, env=None, env_type="local", cwd=str(tmp_path), workdir=None,
            session_key="diagnostic-worker",
        )

    blocked = run("kill -USR1 477345")
    assert blocked is not None
    blocked = json.loads(blocked)
    assert blocked["exit_code"] == 1
    assert "gateway drain" in blocked["error"]
    assert "1800" in blocked["error"]
    assert "exit 130" in blocked["error"]
    assert "hermes gateway restart" in blocked["error"] and "separate shell outside" in blocked["error"]
    # Negatives: foreign pid and wrong signal stay allowed (Branch F is USR1 + gateway pid only).
    assert run("kill -USR1 12345") is None
    assert run("kill -9 477345") is None
    # The old pid-ownership gate really is off for this process: it is not the gateway itself.
    assert process_registry._is_supervised_gateway_process() is False
    assert process_registry._is_gateway_context_process() is True
