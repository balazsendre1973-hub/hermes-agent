"""Live-process guards for the kanban dispatcher — t_639b6d1d.

Measured event (Fin, 2026-11-06 16:00): while ``t_8e9d4fd0``'s ``mem_vec``
backfill was running, the retry worker started ``timeout 900 ... indexer.py
--index`` **twice** beside the live card; the Fin killed both and the card
lost two hours — the live case of the write-back conflict in the
``t_cd29edb4`` study.  A run whose worker died while a process it started is
still alive must not be requeued: the card parks ``blocked (transient)`` and no
second worker is spawned beside the first.

Two pid marks answer "is anything of this card's still running?":

* ``task_runs.worker_pid`` — the column is deliberately retained on the closed
  run row (``kanban_db._end_run``) so a worker that outlives its own terminal
  transition stays visible, and its ``worker_started_at`` fingerprint keeps a
  recycled PID from counting as ours;
* the process-registry checkpoint ``processes.json`` — the ``proc_*`` records
  the worker's ``terminal(background=true)`` left behind, matched by
  ``owner_task_id`` / ``task_id`` or by the card's own footprint (its id, and
  with it its workspace path, in ``command`` / ``cwd``), each re-validated
  against the spawn start time.

The two helpers that already expressed the same rule — "a process survived the
kill, so hold the claim instead of requeueing" — moved here with it: one
module owns *a live process blocks the requeue*.

Import discipline: nothing at module scope but the stdlib.  ``kanban_db``
imports the moved helpers from here and this module late-binds ``kanban_db``
(and ``kanban_db_dispatch._worker_alive``) inside its bodies, so every entry
order into the ``kanban_db`` <-> ``kanban_db_dispatch`` cycle resolves.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

# ``processes.json`` path -> ((mtime_ns, size), records).  The dispatcher reads
# this once per candidate card per tick; the stamp invalidates it on every
# registry write, which happens only on spawn/exit.
_CHECKPOINT_CACHE: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}


# --------------------------------------------------------------------------
# The process-registry checkpoint
# --------------------------------------------------------------------------

def _checkpoint_paths() -> list[Path]:
    """Every ``processes.json`` a worker of this dispatcher could have written.

    The gateway serves many profiles and each interpreter checkpoints its own
    registry beside its home, so the active home alone is not enough: when the
    active home is a profile (``.../profiles/<name>``) its parent's root is
    included too.  A test fixture's fake home never sits under ``profiles/``,
    so a suite resolves to its own temp tree and never reads the host's.
    """
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    roots = {home}
    if home.parent.name == "profiles":
        roots.add(home.parent.parent)
    out: list[Path] = []
    seen: set[Path] = set()
    for root in sorted(roots):
        for path in (root / "processes.json", *sorted(root.glob("profiles/*/processes.json"))):
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _checkpoint_records() -> list[dict[str, Any]]:
    """Running ``proc_*`` records from every checkpoint, cached per stamp."""
    records: list[dict[str, Any]] = []
    for path in _checkpoint_paths():
        try:
            stat = path.stat()
        except OSError:
            continue
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = _CHECKPOINT_CACHE.get(str(path))
        if cached is not None and cached[0] == stamp:
            records.extend(cached[1])
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        parsed = [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []
        _CHECKPOINT_CACHE[str(path)] = (stamp, parsed)
        records.extend(parsed)
    return records


def _mentions_task(text: str, task_id: str) -> bool:
    """``task_id`` as a whole token in ``text`` — its workspace path, a redirect.

    Bounded on both sides so a card id never matches inside a longer token.
    """
    if not text or not task_id:
        return False
    start = text.find(task_id)
    while start != -1:
        end = start + len(task_id)
        before, after = text[start - 1] if start else "", text[end:end + 1]
        if not _word_char(before) and not _word_char(after):
            return True
        start = text.find(task_id, end)
    return False


def _word_char(char: str) -> bool:
    return bool(char) and (char.isalnum() or char in "_-")


def _record_matches_task(record: dict[str, Any], task_id: str) -> bool:
    if str(record.get("owner_task_id") or "") == task_id:
        return True
    if str(record.get("task_id") or "") == task_id:
        return True
    return any(_mentions_task(str(record.get(key) or ""), task_id) for key in ("command", "cwd"))


def _alive(pid: Any, started_at: Any) -> bool:
    """True when ``pid`` is still the process the mark recorded.

    Late-bound: the identity rule (spawn fingerprint, reboot epoch, zombie
    state) lives in exactly one place, ``kanban_db_dispatch._worker_alive``.
    """
    from hermes_cli.kanban_db_dispatch import _worker_alive

    try:
        return _worker_alive(int(pid), started_at)
    except (TypeError, ValueError):
        return False


def _run_pid_marks(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """``task_runs.worker_pid`` marks of every attempt at ``task_id``.

    The row keeps its pid after the run closes — that retention is the whole
    point (see ``kanban_db._end_run``) — so a worker that outlived its own
    terminal transition is still answerable here.
    """
    try:
        rows = conn.execute(
            "SELECT id, worker_pid, worker_started_at FROM task_runs "
            "WHERE task_id = ? AND worker_pid IS NOT NULL",
            (task_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    marks: list[dict[str, Any]] = []
    for row in rows:
        try:
            pid = int(row["worker_pid"])
        except (TypeError, ValueError):
            continue
        marks.append({
            "source": "task_runs",
            "run_id": row["id"],
            "pid": pid,
            "started_at": row["worker_started_at"],
            "cmdline": "",
        })
    return marks


def live_background_processes(
    conn: sqlite3.Connection, task_id: str, *, exclude_pid: Any = None,
) -> list[dict[str, Any]]:
    """Live OS processes a previous run of ``task_id`` left behind.

    The dispatcher's own pid (and the pid the caller is about to reclaim, which
    it already proved dead) never counts — otherwise every dead-worker sweep
    would "find" the worker it just killed.
    """
    exclude = {os.getpid()}
    if exclude_pid:
        try:
            exclude.add(int(exclude_pid))
        except (TypeError, ValueError):
            pass
    found: list[dict[str, Any]] = []
    for mark in _run_pid_marks(conn, task_id):
        if mark["pid"] not in exclude and _alive(mark["pid"], mark["started_at"]):
            found.append(mark)
    for record in _checkpoint_records():
        raw_pid = record.get("pid")
        try:
            pid = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            pid = None
        if pid is None or pid in exclude or not _record_matches_task(record, task_id):
            continue
        if _alive(pid, record.get("host_start_time")):
            found.append({
                "source": "process_registry",
                "session_id": record.get("session_id"),
                "pid": pid,
                "started_at": record.get("host_start_time"),
                "cmdline": str(record.get("command") or ""),
            })
    return found


def park_for_live_background(
    conn: sqlite3.Connection, task_id: str, *, stage: str, exclude_pid: Any = None,
) -> bool:
    """Park ``blocked (transient)`` instead of the requeue the caller was writing.

    Returns True when a live process was found, so the caller must abandon its
    ``crashed``/``timed_out`` -> ``ready`` transition (or the spawn it was
    about to claim); False leaves the normal path untouched.  The detection is
    reported as its own ``background_proc_alive`` event and the block itself
    goes through ``block_task`` so the recurrence/loop-breaker bookkeeping
    stays the kernel's.
    """
    from hermes_cli import kanban_db as _kb

    live = live_background_processes(conn, task_id, exclude_pid=exclude_pid)
    if not live:
        return False
    marks = ", ".join(
        f"pid={mark['pid']}" + (f" {mark['cmdline'][:100]}" if mark.get("cmdline") else "")
        for mark in live
    )
    reason = (
        f"{stage}: a background process this card started is still alive ({marks}). "
        "Requeueing would run the same work beside it, so no new worker is spawned "
        "until it exits or an operator unblocks the card."
    )
    with _kb.write_txn(conn):
        _kb._append_event(
            conn, task_id, "background_proc_alive",
            {
                "stage": stage,
                "pids": [mark["pid"] for mark in live],
                "marks": [
                    {key: mark.get(key) for key in ("source", "pid", "started_at", "cmdline")}
                    for mark in live
                ],
            },
            run_id=_kb._current_run_id(conn, task_id),
        )
    _kb.block_task(conn, task_id, reason=reason, kind="transient")
    return True


# --------------------------------------------------------------------------
# Moved from hermes_cli/kanban_db_dispatch.py — same rule, one home
# --------------------------------------------------------------------------

def _worker_survived_termination(termination: dict) -> bool:
    """True when we tried to kill our own host-local worker and it is still alive.

    Reclaiming then would release the claim and spawn a second worker while the
    first still runs — the duplication loop. Only host-local workers we actually
    signalled count; a non-local lock or no-op attempt (no ``os.kill``) must fall
    through to the normal release path since we cannot manage that worker anyway.
    """
    return bool(
        termination.get("host_local")
        and (termination.get("termination_attempted") or termination.get("signal_refused"))
        and not termination.get("terminated")
    )


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records ``reclaim_deferred``.
    The next tick retries the kill; not spawning a duplicate is what lets the
    throttled worker finally die.
    """
    from hermes_cli import kanban_db as _kb

    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
        payload = {"reason": reason, "claim_lock": claim_lock, "claim_expires_now": grace}
        payload.update(termination)
        _kb._append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


__all__ = [
    "_defer_reclaim_for_live_worker",
    "_worker_survived_termination",
    "live_background_processes",
    "park_for_live_background",
]
