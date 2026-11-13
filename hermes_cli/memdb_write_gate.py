"""Mandatory flock gate for the ``mem.db`` (memoria-v2) writers — t_639b6d1d.

Measured event (Fin, 2026-11-06 15:44 / 16:00): two ``indexer.py --index`` runs
were writing one ``mem.db`` at the same time.  PID 1289472 held the ``flock``
(1 lock-fd); the retry worker's ``timeout 900 ... indexer.py --index`` (PID
1316580) held **0** lock-fds and wrote into ``mem_vec`` uncontrolled — load
21.6.  A ``flock`` convention is not enough, so this module makes it a gate.

Two rules, both fail-closed:

``no_write_lock``
    The writer process must actually HOLD the ``flock`` on ``<db>.lock``.
    Ownership is proven by ``/proc/<pid>/fd`` — the lock file open in the
    writer itself or in an ancestor wrapper such as ``flock(1)`` — *and* by a
    non-blocking flock probe that still reports the lock held.  The
    ``<lock>.holder`` marker (written under the lock by :func:`hold_write_lock`)
    is the fallback when ``/proc`` is unavailable and the evidence that
    separates a holder from a waiter that merely has the file open.

``timeout_wrapper_no_lock``
    ``timeout(1)`` does not inherit a lock, so ``timeout 900 <writer>`` is the
    exact shape of the measured bug.  Either the wrapper takes the lock
    (``flock mem.db.lock timeout 900 <writer>``) or there is no wrapper at all:
    with one cmdline carrying **both** ``timeout`` and the writer script and no
    lock held, the gate refuses before a single row is written.  Requiring both
    tokens in the same cmdline is what keeps an unrelated test runner's own
    ``timeout`` from wrapping every writer it happens to launch.

The module is deliberately **stdlib-only** and runnable as a plain script.  The
live writers live outside this repository (``~/.hermes/memoria-v2/*.py``,
``~/.hermes/memoria-v2/capture-cron.sh``, ``~/.hermes/scripts/*.py``) and load
it by file path, so no ``hermes_cli`` package import — and no dependency
cascade — is needed.

CLI (for the shell writer)::

    python3 hermes_cli/memdb_write_gate.py --require --pid $$ \
        --lock /home/endre/.hermes/memoria-v2/mem.db.lock --writer capture-cron.sh

exit 0 = the lock is held, exit 2 = refused (rule printed on stdout as JSON).
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

GATE_NAME = "memdb-write-gate"
GATE_VERSION = 1

# Rules (stable identifiers — tests and the card's acceptance criteria key on them).
RULE_NO_WRITE_LOCK = "no_write_lock"
RULE_TIMEOUT_WRAPPER = "timeout_wrapper_no_lock"
RULE_WAIT_TIMEOUT = "lock_wait_timeout"
RULE_INTERNAL = "gate_internal_error"

# ``timeout(1)`` argv[0] spellings that make a writer a "timeout-wrapped writer".
_TIMEOUT_BASENAMES = frozenset({"timeout", "timeout.exe", "timeout.real"})

# How long a writer waits for another writer to release the lock before it
# refuses.  Matches the ``flock -w 3600`` the project already uses.
DEFAULT_WAIT_SECONDS = 3600.0

# Ancestor walk bound: deep enough for bash -> timeout -> flock -> python.
_MAX_CHAIN_HOPS = 32


class MemdbGateRefusal(RuntimeError):
    """The gate refused the write.  ``rule`` is one of the ``RULE_*`` ids."""

    def __init__(self, rule: str, message: str, *, verdict: Optional[Verdict] = None):
        super().__init__(message)
        self.rule = rule
        self.verdict = verdict

    def as_dict(self) -> dict:
        payload: dict = {"rule": self.rule, "message": str(self)}
        if self.verdict is not None:
            payload["verdict"] = self.verdict.as_dict()
        return payload


@dataclass(frozen=True)
class Verdict:
    """Everything the gate looked at, so a refusal is auditable."""

    pid: int
    lock_path: str
    held: bool
    holder_pids: tuple[int, ...]
    lock_fds: int
    probe_held: Optional[bool]
    marker_pid: Optional[int]
    timeout_in_chain: bool
    timeout_pids: tuple[int, ...]
    chain: tuple[int, ...]
    writer: Optional[str]
    rule: Optional[str]
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# /proc helpers
# --------------------------------------------------------------------------

def proc_available() -> bool:
    """True when this host exposes the ``/proc`` fd tables the gate reads."""
    return os.path.isdir("/proc/self/fd")


def _read_proc_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def process_cmdline(pid: int) -> list[str]:
    """Argv of ``pid`` (NUL-split), or ``[]`` when unreadable."""
    raw = _read_proc_text(f"/proc/{int(pid)}/cmdline")
    if not raw:
        return []
    return [tok for tok in raw.split("\0") if tok]


def _process_ppid(pid: int) -> Optional[int]:
    """Parent pid from ``/proc/<pid>/stat`` field 4 (``comm`` may hold spaces)."""
    raw = _read_proc_text(f"/proc/{int(pid)}/stat")
    if not raw:
        return None
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 1:].split()
    # after ")": state, ppid, pgrp, ...
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def ancestor_chain(pid: int, max_hops: int = _MAX_CHAIN_HOPS) -> tuple[int, ...]:
    """``pid`` followed by its live ancestors, self first, cycle-safe."""
    chain: list[int] = []
    seen: set[int] = set()
    current = int(pid)
    for _ in range(max_hops):
        if current <= 0 or current in seen:
            break
        seen.add(current)
        chain.append(current)
        parent = _process_ppid(current)
        if parent is None or parent == current:
            break
        current = parent
    return tuple(chain)


def _is_timeout_argv(token: str) -> bool:
    base = os.path.basename(token.rstrip("/"))
    return base in _TIMEOUT_BASENAMES


def _cmdline_runs_writer(cmd: list[str], writer: str) -> bool:
    """``writer`` appears as its own argv token (basename) in ``cmd``."""
    wanted = os.path.basename(str(writer))
    return any(os.path.basename(token.rstrip("/")) == wanted for token in cmd)


def _cmdline_text_matches_writer(cmd: list[str], writer: str) -> bool:
    """``writer``'s basename anywhere in the cmdline text.

    Falls back to a substring match for shapes where the writer name is not an
    argv token of its own — e.g. ``timeout 30 python -c "g.hold_write_lock(lock,
    writer='indexer.py')"``, where ``indexer.py`` lives only inside the
    ``-c`` code string.  The design rule still holds: both tokens (``timeout``
    and the writer) must co-occur in the *same* cmdline, so an unrelated test
    runner's own ``timeout`` never counts as this writer's wrapper.
    """
    wanted = os.path.basename(str(writer))
    return wanted in " ".join(cmd)


def find_timeout_pids(chain, writer: Optional[str] = None) -> tuple[int, ...]:
    """Pids whose cmdline runs *this* writer under a ``timeout(1)`` wrapper.

    The Fin's rule is literal: a ``cmdline`` carrying **both** ``timeout`` and
    the writer script — ``timeout 900 .../indexer.py --index`` — with no lock
    held.  Both tokens must sit in the same cmdline, so a test runner's own
    ``timeout`` around pytest never counts as a wrapper for an unrelated
    writer.  ``writer=None`` (the caller declared no script) degrades to "any
    ``timeout`` ancestor": fail closed rather than silently unscoped.

    Writer detection is token-based (``_cmdline_runs_writer``) plus a
    substring fallback (``_cmdline_text_matches_writer``) so the
    ``python -c`` shape is caught too: under it the writer name is passed as a
    keyword argument inside the code string, never as its own argv token.

    The pids are captured while the chain is still readable — a refused writer
    has usually exited by the time its caller inspects the verdict.
    """
    hits: list[int] = []
    for pid in chain:
        cmd = process_cmdline(pid)
        if not cmd:
            continue
        if not any(_is_timeout_argv(token) or token == "timeout" for token in cmd):
            continue
        if writer is None or _cmdline_runs_writer(cmd, writer) or _cmdline_text_matches_writer(cmd, writer):
            hits.append(pid)
    return tuple(hits)


def chain_has_timeout(chain, writer: Optional[str] = None) -> bool:
    """True when a ``timeout(1)`` wrapper of ``writer`` is in ``chain``."""
    return bool(find_timeout_pids(chain, writer))


def lock_open_fds(pid: int, lock_path) -> Optional[int]:
    """How many fds of ``pid`` point at ``lock_path``.

    ``None`` = ``/proc`` unavailable (fall back to the marker); ``0`` = the
    process provably has the lock file open nowhere, so it cannot hold its
    ``flock`` (the measured "0 flock-fd" signature).

    Identity is the (device, inode) pair rather than a resolved path: the
    descriptor's target can live anywhere on disk — including a tree this
    process must not resolve during tests — and the inode is what ``flock``
    actually keys on.
    """
    if not proc_available():
        return None
    key = _lock_inode(lock_path)
    if key is None:
        return 0  # no lock file yet: nothing can hold a flock on it
    fd_dir = f"/proc/{int(pid)}/fd"
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return 0  # process gone: it holds nothing
    hits = 0
    for name in names:
        try:
            st = os.stat(os.path.join(fd_dir, name))
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == key:
            hits += 1
    return hits


def _lock_inode(lock_path) -> Optional[tuple[int, int]]:
    try:
        st = os.stat(str(lock_path))
    except OSError:
        return None
    return int(st.st_dev), int(st.st_ino)


def probe_lock_held(lock_path) -> Optional[bool]:
    """Is *anybody* holding the ``flock`` on ``lock_path`` right now?

    A fresh descriptor is a different open-file-description, so a successful
    ``LOCK_EX|LOCK_NB`` proves nobody holds it (and we release it again);
    ``EWOULDBLOCK``/``EACCES`` proves somebody does.  ``None`` = the probe
    itself could not run.
    """
    import fcntl  # POSIX-only: imported lazily (footgun: module-level import)

    path = str(lock_path)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EACCES, errno.ENOLCK, errno.EAGAIN):
                return True
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Lock-holder marker (the /proc-free fallback + waiter/holder separator)
# --------------------------------------------------------------------------

def marker_path(lock_path) -> Path:
    return Path(str(lock_path) + ".holder")


def _marker_int_pid(marker: Optional[dict]) -> Optional[int]:
    raw = marker.get("pid") if marker else None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def read_marker(lock_path) -> Optional[dict]:
    try:
        raw = marker_path(lock_path).read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _marker_matches_lock(marker: Optional[dict], lock_path) -> bool:
    """Marker was written against the lock file that exists *now*.

    A ``mem.db.lock`` that was deleted and recreated (a restore, a cleanup)
    invalidates every marker naming the old inode — ownership of the new file
    has to be proven again.
    """
    expected = (marker or {}).get("lock_inode")
    if expected is None:
        return True  # marker predates the field: pid + start time still apply
    try:
        wanted = (int(expected[0]), int(expected[1]))
    except (TypeError, ValueError, IndexError):
        return False
    current = _lock_inode(lock_path)
    return current is not None and current == wanted


def _marker_is_live(marker: Optional[dict]) -> bool:
    """Marker names a process that is still alive AND still the one recorded."""
    pid = _marker_int_pid(marker)
    if pid is None or pid <= 0:
        return False
    if not _pid_exists(pid):
        return False
    expected = (marker or {}).get("start_time")
    if expected is None:
        return True
    current = _start_time(pid)
    return current is None or current == expected


def write_marker(lock_path, pid: int) -> bool:
    payload = {
        "pid": int(pid),
        "start_time": _start_time(int(pid)),
        "lock_path": os.path.abspath(str(lock_path)),
        "lock_inode": _lock_inode(lock_path),
        "acquired_at": int(time.time()),
        "gate": f"{GATE_NAME}/{GATE_VERSION}",
    }
    path = marker_path(lock_path)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def clear_marker(lock_path, pid: int) -> None:
    marker = read_marker(lock_path)
    if marker is not None and _marker_int_pid(marker) == int(pid):
        try:
            marker_path(lock_path).unlink()
        except OSError:
            pass


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)  # windows-footgun: ok — POSIX-only gate (flock + /proc); never reached on Windows
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if sys.platform == "linux":
        state = (_read_proc_text(f"/proc/{int(pid)}/status") or "")
        for line in state.splitlines():
            if line.startswith("State:"):
                return not line.split()[1:2] == ["Z"]
    return True


def _start_time(pid: int) -> Optional[int]:
    """Kernel start ticks for ``pid`` (``/proc/<pid>/stat`` field 22)."""
    raw = _read_proc_text(f"/proc/{int(pid)}/stat")
    if not raw:
        return None
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 1:].split()
    # after ")": state(0) ppid(1) ... starttime is field 20 there (stat field 22).
    if len(fields) < 21:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------

def evaluate(pid: Optional[int] = None, lock_path=None, *, writer: Optional[str] = None) -> Verdict:
    """Decide whether ``pid`` may write ``mem.db``.  Never raises for a miss."""
    who = int(pid) if pid is not None else os.getpid()
    lock = Path(lock_path) if lock_path is not None else default_lock_path()
    chain = ancestor_chain(who)

    fds = lock_open_fds(who, lock)
    probe = probe_lock_held(lock)
    marker = read_marker(lock)
    marker_pid = _marker_int_pid(marker)

    holders: list[int] = []
    if probe is True:
        # Only a live lock can be held, so the probe gates every other source.
        if fds:
            holders.append(who)
        for ancestor in chain[1:]:
            if lock_open_fds(ancestor, lock):
                holders.append(ancestor)
        if (marker_pid is not None and marker_pid in chain
                and _marker_is_live(marker) and _marker_matches_lock(marker, lock)):
            holders.append(marker_pid)
    elif probe is None and marker_pid is not None and _marker_is_live(marker) \
            and _marker_matches_lock(marker, lock):
        # No /proc, no probe: the marker is the only ownership evidence left.
        if marker_pid in chain:
            holders.append(marker_pid)

    # De-duplicate, keep chain order.
    ordered = [p for p in chain if p in set(holders)]
    held = bool(ordered)
    timeout_pids = find_timeout_pids(chain, writer)
    timeout_in_chain = bool(timeout_pids)

    rule: Optional[str] = None
    if not held and timeout_in_chain:
        rule = RULE_TIMEOUT_WRAPPER
        detail = (
            f"timeout(1) wraps this writer ({who}) and no process in its chain "
            f"holds the flock on {lock}; timeout does not inherit a lock, so the "
            "wrapper must take it (flock <lock> timeout 900 <writer>) or be dropped"
        )
    elif not held:
        rule = RULE_NO_WRITE_LOCK
        detail = (
            f"writer {who} holds no lock-fd on {lock} "
            f"(lock_fds={fds}, probe_held={probe}, marker_pid={marker_pid})"
        )
    else:
        detail = f"lock held by pid(s) {ordered} on {lock}"

    return Verdict(
        pid=who,
        lock_path=str(lock),
        held=held,
        holder_pids=tuple(ordered),
        lock_fds=-1 if fds is None else int(fds),
        probe_held=probe,
        marker_pid=marker_pid,
        timeout_in_chain=timeout_in_chain,
        timeout_pids=timeout_pids,
        chain=chain,
        writer=writer,
        rule=rule,
        detail=detail,
    )


def require_write_lock(
    pid: Optional[int] = None, lock_path=None, *, writer: Optional[str] = None,
) -> Verdict:
    """The gate: raise :class:`MemdbGateRefusal` unless the lock is held.

    Verify-only — it never takes the lock, so a caller that has not
    ``flock``-ed is refused.  This is the check the acceptance criteria call
    "a lock-less ``mem.db`` writer is rejected".
    """
    verdict = evaluate(pid, lock_path, writer=writer)
    if verdict.rule is not None:
        raise MemdbGateRefusal(verdict.rule, verdict.detail, verdict=verdict)
    return verdict


def hold_write_lock(
    lock_path=None,
    *,
    writer: Optional[str] = None,
    pid: Optional[int] = None,
    wait_seconds: Optional[float] = None,
) -> HeldLock:
    """Acquire the ``flock`` if nobody else has it, then verify ownership.

    The order matters:

    1. already held by this process or an ancestor wrapper -> nothing to do;
    2. ``timeout`` in the chain and no lock -> **refuse**, never self-rescue:
       the wrapper is the party that must own the lock (rule
       ``timeout_wrapper_no_lock``);
    3. otherwise take it (bounded wait, rule ``lock_wait_timeout``) and record
       the marker, then re-run the verification.

    Returns a handle whose :meth:`HeldLock.release` is idempotent; use it as a
    context manager.
    """
    who = int(pid) if pid is not None else os.getpid()
    lock = Path(lock_path) if lock_path is not None else default_lock_path()
    verdict = evaluate(who, lock, writer=writer)
    if verdict.held:
        return HeldLock(lock, who, acquired=False, verdict=verdict)
    if verdict.timeout_in_chain:
        raise MemdbGateRefusal(RULE_TIMEOUT_WRAPPER, verdict.detail, verdict=verdict)

    budget = (
        DEFAULT_WAIT_SECONDS if wait_seconds is None
        else max(0.0, float(wait_seconds))
    )
    deadline = time.monotonic() + budget
    import fcntl  # POSIX-only: imported lazily (footgun: module-level import)

    try:
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        raise MemdbGateRefusal(
            RULE_INTERNAL, f"cannot open lock file {lock}: {exc}", verdict=verdict,
        ) from exc
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EACCES, errno.ENOLCK, errno.EAGAIN):
                    raise MemdbGateRefusal(
                        RULE_INTERNAL, f"flock({lock}) failed: {exc}", verdict=verdict,
                    ) from exc
                if time.monotonic() >= deadline:
                    raise MemdbGateRefusal(
                        RULE_WAIT_TIMEOUT,
                        f"another mem.db writer still holds {lock} after {budget:.0f}s; refusing to write",
                        verdict=verdict,
                    )
                time.sleep(0.25)
        write_marker(lock, who)
        verdict = evaluate(who, lock, writer=writer)
        if not verdict.held or verdict.rule is not None:
            raise MemdbGateRefusal(
                verdict.rule or RULE_NO_WRITE_LOCK, verdict.detail, verdict=verdict,
            )
        handle = HeldLock(lock, who, acquired=True, verdict=verdict, _fd=fd)
        fd = -1  # ownership transferred to the handle
        return handle
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


class HeldLock:
    """An acquired (or already-held-by-wrapper) mem.db lock."""

    def __init__(self, lock_path, pid: int, *, acquired: bool, verdict: Verdict, _fd: int = -1):
        self.lock_path = Path(lock_path)
        self.pid = int(pid)
        self.acquired = bool(acquired)
        self.verdict = verdict
        self._fd = int(_fd)
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._fd >= 0:
            import fcntl  # POSIX-only: imported lazily (footgun: module-level import)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
        if self.acquired:
            clear_marker(self.lock_path, self.pid)

    def __enter__(self) -> HeldLock:
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def lock_path_for(db_path=None) -> Path:
    """Lock for ``db_path``: ``$MEMORIA_CAPTURE_LOCK`` (the capture-cron
    convention, which the shell takes with ``exec 9>`` + ``flock -n 9``) when
    the operator declared it, else ``<db>.lock``."""
    env = os.environ.get("MEMORIA_CAPTURE_LOCK")
    if env:
        return Path(env)
    if db_path is None:
        return default_lock_path()
    return Path(str(db_path) + ".lock")


def default_db_path() -> Path:
    """``mem.db`` the writers share: ``$MEMORIA_MEM_DB`` / ``$MEM_DB`` / default."""
    # health: allow HX001 -- the shared memoria-v2 mem.db deliberately lives under the
    # user's home root, not the profile: every profile's writers share ONE database.
    for key in ("MEMORIA_MEM_DB", "MEM_DB"):
        value = os.environ.get(key)
        if value:
            return Path(value)
    return Path(os.path.expanduser("~/.hermes/memoria-v2/mem.db"))


def default_lock_path() -> Path:
    value = os.environ.get("MEMORIA_CAPTURE_LOCK")
    if value:
        return Path(value)
    return Path(str(default_db_path()) + ".lock")


# --------------------------------------------------------------------------
# CLI (capture-cron.sh and any other shell writer)
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memdb_write_gate.py",
        description="Mandatory flock gate for the mem.db writers (t_639b6d1d).",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--require", action="store_true",
                      help="verify the lock is held; never acquires it")
    mode.add_argument("--hold", action="store_true",
                      help="acquire the lock if free, then verify")
    parser.add_argument("--lock", default=None, help="lock file path")
    parser.add_argument("--pid", type=int, default=None,
                        help="writer pid to inspect (default: this process)")
    parser.add_argument("--writer", default=None,
                        help="writer script name, recorded in the verdict")
    parser.add_argument("--wait", type=float, default=None,
                        help="--hold: seconds to wait for a busy lock")
    args = parser.parse_args(argv)

    lock = Path(args.lock) if args.lock else default_lock_path()
    try:
        if args.require:
            verdict = require_write_lock(args.pid, lock, writer=args.writer)
            print(json.dumps({"ok": True, "gate": GATE_NAME,
                              "verdict": verdict.as_dict()}, sort_keys=True))
            return 0
        handle = hold_write_lock(
            lock, writer=args.writer, pid=args.pid, wait_seconds=args.wait,
        )
        try:
            print(json.dumps({"ok": True, "gate": GATE_NAME,
                              "verdict": handle.verdict.as_dict()}, sort_keys=True))
        finally:
            # A CLI invocation has no lifetime to hold: --hold only proves the
            # acquisition path; the shell writer uses --require on its own fd.
            handle.release()
        return 0
    except MemdbGateRefusal as exc:
        print(json.dumps({"ok": False, "gate": GATE_NAME, **exc.as_dict()}, sort_keys=True))
        return 2
    except (OSError, ValueError, TypeError) as exc:  # boundary: always answer as JSON
        print(json.dumps({"ok": False, "gate": GATE_NAME, "rule": RULE_INTERNAL,
                          "message": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 3


if __name__ == "__main__":
    sys.exit(main())
