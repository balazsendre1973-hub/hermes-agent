"""Mandatory flock gate for the ``mem.db`` writers — t_639b6d1d.

Contract under test (the Fin's two measured events, 2026-11-06 15:44 / 16:00):

* a ``mem.db`` writer that holds **no** lock-fd is refused (``no_write_lock``)
  — the signature of the concurrent ``indexer.py --index`` pair;
* ``timeout(1)`` wrapping a writer with no lock held anywhere in its ancestor
  chain is refused (``timeout_wrapper_no_lock``) — ``timeout`` does not
  inherit a lock, so the wrapper must take it or be dropped;
* the accepted shapes still pass: the writer itself holds the lock, or an
  ancestor wrapper (``flock(1)``) does;
* a busy lock is waited for, then refused (``lock_wait_timeout``) instead of
  writing alongside the holder.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import memdb_write_gate as g

REPO = Path(__file__).resolve().parents[2]
GATE_SCRIPT = REPO / "hermes_cli" / "memdb_write_gate.py"


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    return tmp_path / "mem.db.lock"


def _require_child(lock: Path, writer: str = "indexer.py") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE_SCRIPT), "--require",
         "--lock", str(lock), "--writer", writer],
        capture_output=True, text=True, check=False, timeout=60.0,
    )


def _rule_of(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    return payload["rule"]


def _harness_uses_timeout() -> bool:
    """True when this test runner itself sits under ``timeout(1)``.

    Then every chain contains ``timeout`` and the *specific* rule a lock-less
    writer gets would legitimately be the more specific one; the assertion is
    skipped rather than asserted against a harness artefact.
    """
    return g.chain_has_timeout(g.ancestor_chain(os.getpid()))


# ---------------------------------------------------------------------------
# Criterion 1 — a lock-less mem.db writer is refused
# ---------------------------------------------------------------------------

def test_lockless_writer_is_refused_in_process(lock: Path) -> None:
    """Direct call: no lock-fd, no holder in the chain -> refusal."""
    with pytest.raises(g.MemdbGateRefusal) as excinfo:
        g.require_write_lock(lock_path=lock, writer="indexer.py")
    assert excinfo.value.rule == (
        g.RULE_TIMEOUT_WRAPPER if _harness_uses_timeout() else g.RULE_NO_WRITE_LOCK
    )
    verdict = excinfo.value.verdict
    assert verdict is not None
    assert verdict.held is False
    # The measured signature: the writer has the lock file open nowhere.
    if g.proc_available():
        assert verdict.lock_fds == 0
    assert verdict.probe_held is not True


def test_lockless_writer_child_process_is_refused(lock: Path) -> None:
    """A whole writer process that never ``flock``-ed exits 2 with the rule."""
    if _harness_uses_timeout():
        pytest.skip("the test harness itself runs under timeout(1)")
    proc = _require_child(lock)
    assert _rule_of(proc) == g.RULE_NO_WRITE_LOCK
    payload = json.loads(proc.stdout)
    verdict = payload["verdict"]
    assert verdict["held"] is False
    assert verdict["lock_fds"] == 0
    assert verdict["writer"] == "indexer.py"


def test_refusal_names_the_writer_and_the_lock(lock: Path) -> None:
    with pytest.raises(g.MemdbGateRefusal) as excinfo:
        g.require_write_lock(lock_path=lock, writer="trigger.py")
    assert excinfo.value.verdict is not None
    assert excinfo.value.verdict.writer == "trigger.py"
    assert str(excinfo.value.verdict.lock_path) == str(lock)


# ---------------------------------------------------------------------------
# Criterion 2 — `timeout` + writer script on the cmdline is refused
# ---------------------------------------------------------------------------

def test_timeout_wrapped_writer_is_refused(lock: Path) -> None:
    """``timeout 900 indexer.py`` with no lock -> ``timeout_wrapper_no_lock``."""
    proc = subprocess.run(
        ["timeout", "30", sys.executable, str(GATE_SCRIPT), "--require",
         "--lock", str(lock), "--writer", "indexer.py"],
        capture_output=True, text=True, check=False, timeout=60.0, cwd=str(REPO),
    )
    assert _rule_of(proc) == g.RULE_TIMEOUT_WRAPPER
    payload = json.loads(proc.stdout)
    verdict = payload["verdict"]
    assert verdict["timeout_in_chain"] is True
    assert verdict["held"] is False
    # `timeout` really was an ancestor of the writer that got refused.  The pids
    # are read while the chain is alive — the refused writer has exited by now.
    assert verdict["timeout_pids"], verdict
    assert set(verdict["timeout_pids"]) <= set(verdict["chain"])


def test_hold_write_lock_never_self_rescues_under_timeout(lock: Path) -> None:
    """The acquire path must not silently take the lock under ``timeout``.

    That is the measured bug's shape: the wrapper is the party that has to own
    the lock, so a self-acquire here would mask the violation.
    """
    code = (
        "import sys\n"
        "from hermes_cli import memdb_write_gate as g\n"
        "try:\n"
        "    g.hold_write_lock(sys.argv[1], writer='indexer.py', wait_seconds=5)\n"
        "except g.MemdbGateRefusal as exc:\n"
        "    print('RULE=' + exc.rule)\n"
        "    raise SystemExit(2)\n"
        "raise SystemExit(0)\n"
    )
    proc = subprocess.run(
        ["timeout", "30", sys.executable, "-c", code, str(lock)],
        capture_output=True, text=True, check=False, timeout=60.0, cwd=str(REPO),
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "RULE=" + g.RULE_TIMEOUT_WRAPPER in proc.stdout
    assert not g.marker_path(lock).exists()


def test_timeout_wrapped_writer_with_the_wrapper_holding_the_lock_passes(lock: Path) -> None:
    """``flock <lock> timeout 900 <writer>`` — the wrapper owns the lock.

    ``timeout`` is in the chain, but so is a live holder, so the verdict is a
    pass: the rule refuses the *unlocked* timeout wrapper, never the shape.
    """
    import fcntl

    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        g.write_marker(lock, os.getpid())
        # The writer itself has no lock-fd — its ancestor chain does, which is
        # exactly what a `flock(1)` wrapper looks like from the inside.
        proc = subprocess.run(
            ["timeout", "30", sys.executable, str(GATE_SCRIPT), "--require",
             "--lock", str(lock), "--writer", "indexer.py"],
            capture_output=True, text=True, check=False, timeout=60.0, cwd=str(REPO),
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert payload["verdict"]["timeout_in_chain"] is True
        assert payload["verdict"]["held"] is True
        assert os.getpid() in payload["verdict"]["holder_pids"]
        assert payload["verdict"]["rule"] is None
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        g.clear_marker(lock, os.getpid())


# ---------------------------------------------------------------------------
# The gate's happy paths — the wiring must not break a correct writer
# ---------------------------------------------------------------------------

def test_hold_write_lock_acquires_verifies_and_releases(lock: Path) -> None:
    assert not g.marker_path(lock).exists()
    handle = g.hold_write_lock(lock, writer="indexer.py", wait_seconds=5)
    try:
        assert handle.acquired is True
        assert handle.verdict.held is True
        assert g.marker_path(lock).exists()
        verdict = g.require_write_lock(lock_path=lock, writer="indexer.py")
        assert verdict.held is True
        assert verdict.holder_pids == (os.getpid(),)
    finally:
        handle.release()
    # Release is idempotent and drops the marker with it.
    handle.release()
    assert not g.marker_path(lock).exists()
    with pytest.raises(g.MemdbGateRefusal) as excinfo:
        g.require_write_lock(lock_path=lock)
    assert excinfo.value.rule in (g.RULE_NO_WRITE_LOCK, g.RULE_TIMEOUT_WRAPPER)


def test_hold_write_lock_is_a_context_manager(lock: Path) -> None:
    with g.hold_write_lock(lock, writer="trigger.py", wait_seconds=5) as held:
        assert held.verdict.held is True
    assert not g.marker_path(lock).exists()


def test_busy_lock_is_waited_then_refused(lock: Path) -> None:
    """Another live holder -> bounded wait -> ``lock_wait_timeout``, no write."""
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "print('locked', flush=True)\n"
         "time.sleep(6)\n",
         str(lock)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        with pytest.raises(g.MemdbGateRefusal) as excinfo:
            g.hold_write_lock(lock, writer="indexer.py", wait_seconds=1)
        assert excinfo.value.rule == g.RULE_WAIT_TIMEOUT
        assert time.monotonic() - started >= 0.9  # it really waited
    finally:
        # The holder exits on its own: signalling across the test harness's
        # live-system guard is not this test's business.
        holder.wait(timeout=20)


def test_marker_is_the_ownership_evidence_without_proc(lock: Path) -> None:
    """No ``/proc``: the marker still proves ownership — but only if held."""
    import fcntl

    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        g.write_marker(lock, os.getpid())
        real_proc = g.proc_available
        g.proc_available = lambda: False  # type: ignore[assignment]
        try:
            verdict = g.evaluate(os.getpid(), lock, writer="indexer.py")
            assert verdict.held is True
            assert verdict.holder_pids == (os.getpid(),)
            assert verdict.rule is None
        finally:
            g.proc_available = real_proc  # type: ignore[assignment]
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        g.clear_marker(lock, os.getpid())

    # Marker present but the lock NOT held -> still refused: a stale marker is
    # never enough on its own.
    g.write_marker(lock, os.getpid())
    real_proc = g.proc_available
    g.proc_available = lambda: False  # type: ignore[assignment]
    try:
        verdict = g.evaluate(os.getpid(), lock, writer="indexer.py")
        assert verdict.held is False
        assert verdict.rule is not None
    finally:
        g.proc_available = real_proc  # type: ignore[assignment]
        g.clear_marker(lock, os.getpid())


def test_dead_marker_is_not_ownership(lock: Path) -> None:
    """A marker naming a reaped process cannot prove a live lock."""
    import fcntl

    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        g.write_marker(lock, os.getpid())
        marker = g.read_marker(lock)
        assert marker is not None
        assert g._marker_is_live(marker) is True
        # Point it at a pid that cannot be ours and cannot be alive: pid 2^22
        # is beyond /proc/sys/kernel/pid_max on this host.
        marker["pid"] = 4194303
        g.marker_path(lock).write_text(json.dumps(marker), encoding="utf-8")
        verdict = g.evaluate(os.getpid(), lock, writer="indexer.py")
        # The writer still holds its own fd, so ownership stands; the dead
        # marker contributed nothing.
        assert verdict.held is True
        assert 4194303 not in verdict.holder_pids
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        g.clear_marker(lock, os.getpid())


# ---------------------------------------------------------------------------
# CLI — the shape capture-cron.sh uses
# ---------------------------------------------------------------------------

def test_cli_require_on_the_callers_own_fd(lock: Path) -> None:
    """``--require --pid $$``: the shell's ``exec 9>`` + ``flock -n 9``."""
    import fcntl

    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        proc = subprocess.run(
            [sys.executable, str(GATE_SCRIPT), "--require",
             "--pid", str(os.getpid()), "--lock", str(lock),
             "--writer", "capture-cron.sh"],
            capture_output=True, text=True, check=False, timeout=60.0, cwd=str(REPO),
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert payload["verdict"]["lock_fds"] >= 1
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

    proc = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), "--require",
         "--pid", str(os.getpid()), "--lock", str(lock),
         "--writer", "capture-cron.sh"],
        capture_output=True, text=True, check=False, timeout=60.0, cwd=str(REPO),
    )
    assert _rule_of(proc) in (g.RULE_NO_WRITE_LOCK, g.RULE_TIMEOUT_WRAPPER)


def test_cli_hold_acquires_then_releases(lock: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), "--hold",
         "--lock", str(lock), "--writer", "indexer.py", "--wait", "5"],
        capture_output=True, text=True, check=False, timeout=60.0, cwd=str(REPO),
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    # The CLI has no lifetime to hold: the marker must not outlive it.
    assert not g.marker_path(lock).exists()


def test_cli_require_prints_the_rule_and_exits_two(lock: Path) -> None:
    if _harness_uses_timeout():
        pytest.skip("the test harness itself runs under timeout(1)")
    proc = _require_child(lock)
    assert _rule_of(proc) == g.RULE_NO_WRITE_LOCK
    assert "no lock-fd" in json.loads(proc.stdout)["message"]


def test_unknown_lock_file_is_created_by_the_probe_then_refused(lock: Path) -> None:
    assert not lock.exists()
    with pytest.raises(g.MemdbGateRefusal):
        g.require_write_lock(lock_path=lock, writer="indexer.py")
    assert lock.exists()  # the probe opened it; nothing was written to mem.db
