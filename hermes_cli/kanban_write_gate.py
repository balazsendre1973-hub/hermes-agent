"""Write-gate for kanban card mutations: machine checklist + automatic restore.

Adapted from ``~/.hermes/scripts/memory_consolidation.py`` (the MEMORY.md
consolidation pattern). The pattern is copied, not reinvented; only the target
differs (a SQLite board instead of one markdown file):

    snapshot (sha256) → mutation → post-run diff → machine checklist →
    on a violated invariant: automatic restore + non-zero exit

What maps where:

* ``memory_consolidation``'s ``tar.gz`` backup of MEMORY.md → a canonical-JSON
  **row snapshot** (``tasks`` row + the ``task_events``/``task_runs`` watermark
  for this card) held in the closure and recorded in the ledger with
  ``before_sha256``/``after_sha256``. A per-mutation tar of a hot WAL database
  would be a torn copy, so the backup granularity is the row, not the file.
* the ledger → append-only JSONL next to the board DB
  (``.write_gate_ledger.jsonl``); every gated mutation writes one entry with
  the unified diff, and a restore writes a SECOND entry — the backup (the
  before-state + its hash) therefore survives the restore, so a restore is
  itself reversible.
* the must-keep checklist → :func:`check_task_row` + :func:`violations`
  (machine-checkable: returns a set of rule ids, no prose).
* the restore → :func:`restore` re-applies the snapshot and drops the rows the
  mutation appended (events/runs beyond the watermark), then raises
  :class:`GateViolation` (non-zero exit for the CLI, a failed task for the
  dispatcher).

Delta rule (workflow safety): only violations present in the AFTER state and
absent in the BEFORE state count. A pre-existing violation is reported by the
ledger but never blocks an unrelated mutation, so the gate cannot wedge a card
that was already sick — only a mutation that *introduces* a violation is
rolled back. This is what keeps the gate from breaking legitimate work done by
weak models.

Layers (why both exist):

* **pre-check** (:func:`require_assignee` / :func:`require_model` /
  :func:`require_idempotency_key`) — called from the mutation functions before
  any write: a bad input never reaches the database (``ValueError``).
* **post-check** (the :func:`gated` decorator) — re-reads the row after the
  write and restores it if an invariant slipped through any other path.
* **SQL trigger** (``TRIGGER_SQL`` in ``kanban_db``) — ``RAISE(ABORT)`` on an
  illegal ``status``/``block_kind`` so a *direct* ``UPDATE``/``INSERT``
  (dispatcher internals, transfer, an external tool) fails too — the
  pragma_gate principle: enforcement lives in the write layer, not in the
  individual caller.

Environment kill switch: ``HERMES_KANBAN_WRITE_GATE=off`` disables the Python
pre/post checks (the SQL triggers stay; drop them manually if ever needed).
"""

from __future__ import annotations

import contextlib
import difflib
import functools
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

_log = logging.getLogger(__name__)

LEDGER_NAME = ".write_gate_ledger.jsonl"
GATE_VERSION = "kanban-write-gate/1"
# Event-scan window for the chain check: bounded so a 10k-event card does not
# cost a JSON parse storm per mutation. Symmetric before/after (both sides scan
# the newest N), so a violation older than the window is never "new".
_EVENT_SCAN_LIMIT = 500
_DIFF_LIMIT = 4000


class GateViolation(RuntimeError):
    """A gated mutation introduced an invariant violation and was restored.

    Carries the machine-checkable rule ids (``violations``) so callers/tests
    can assert without string-matching the message. ``restored`` says whether
    the automatic restore succeeded (``False`` = the row is still in the
    violated state; the ledger entry has the before-snapshot hash either way).
    """

    def __init__(self, task_id: Optional[str], kind: str, violations: list[str], restored: bool = True):
        self.task_id = task_id
        self.kind = kind
        self.violations = list(violations)
        self.restored = restored
        state = "change restored" if restored else "RESTORE FAILED - row still violated"
        super().__init__(
            f"kanban write-gate [{kind}] blocked mutation of {task_id}: "
            + "; ".join(self.violations)
            + f" ({state})"
        )


def enabled() -> bool:
    """False only when the operator explicitly kills the Python-side gate."""
    return os.environ.get("HERMES_KANBAN_WRITE_GATE", "").strip().lower() not in {"off", "0", "false", "no"}


# --------------------------------------------------------------------------- #
# Registry checks (the "known values" side of the checklist)
# --------------------------------------------------------------------------- #

def assignee_registry() -> tuple[frozenset[str], bool]:
    """``(known profile names, registry available)``.

    ``available=False`` when the profiles registry does not exist on disk (a
    fresh install or an isolated test home): with no registry to validate
    against a strict check would reject every name, so the assignee rule is
    skipped — the same fail-open rule as the model catalog. Production homes
    always have ``<root>/profiles``.
    """
    try:
        from hermes_constants import get_default_hermes_root

        profiles_dir = get_default_hermes_root() / "profiles"
    except Exception:
        return frozenset(), False
    if not profiles_dir.is_dir():
        return frozenset(), False
    try:
        from hermes_cli import kanban_db as _kb

        return frozenset(_kb.list_profiles_on_disk()), True
    except Exception:
        return frozenset(), False


_REG_KEY_SPLIT = re.compile(r"\s")


def idempotency_format_error(key: Any) -> Optional[str]:
    """Rule id when a ``reg:<board>|<id>(|<kind>)`` key is malformed, else None.

    Only ``reg:``-prefixed keys are format-constrained (the registry's shape —
    ``task_register_sync._norm_reg_key``); free-form keys from other producers
    pass, but an empty/whitespace/multi-blank ``reg:`` key — the kind that
    silently duplicated registry cards — is rejected.
    """
    if key is None:
        return None
    key = str(key)
    if not key.startswith("reg:"):
        return None
    parts = key.split("|")
    if len(parts) < 2:
        return f"idempotency_malformed:{key!r}"
    if any(not p.strip() for p in parts) or any(_REG_KEY_SPLIT.search(p) for p in parts):
        return f"idempotency_malformed:{key!r}"
    if not parts[0][len("reg:"):].strip():
        return f"idempotency_malformed:{key!r}"
    return None


def _valid_statuses() -> frozenset[str]:
    from hermes_cli.kanban_workflow import DEFAULT_STATUSES

    return DEFAULT_STATUSES


def _valid_block_kinds() -> frozenset[str]:
    from hermes_cli import kanban_db as _kb

    return frozenset(_kb.VALID_BLOCK_KINDS)


def _valid_workspace_kinds() -> frozenset[str]:
    from hermes_cli import kanban_db as _kb

    return frozenset(_kb.VALID_WORKSPACE_KINDS)


def check_task_row(row: Mapping[str, Any]) -> set[str]:
    """Machine-checkable must-keep checklist for a ``tasks`` row.

    Returns the set of violated rule ids (empty = clean). Pure function of the
    row (plus the profile registry / model catalogs); no DB access — the
    event/run-chain rules live in :func:`violations`.
    """
    violated: set[str] = set()

    status = row.get("status")
    if status is None or status not in _valid_statuses():
        violated.add(f"status_illegal:{status!r}")

    block_kind = row.get("block_kind")
    if block_kind is not None and block_kind not in _valid_block_kinds():
        violated.add(f"block_kind_illegal:{block_kind!r}")

    workspace_kind = row.get("workspace_kind")
    if workspace_kind is not None and workspace_kind not in _valid_workspace_kinds():
        violated.add(f"workspace_kind_illegal:{workspace_kind!r}")

    assignee = row.get("assignee")
    if assignee is not None:
        known, available = assignee_registry()
        if available and assignee not in known:
            violated.add(f"assignee_unknown:{assignee!r}")

    model = row.get("model_override")
    if model:
        from hermes_cli import model_gate

        ok, reason = model_gate.model_known(str(model), row.get("provider_override"))
        if not ok:
            violated.add(f"model_unknown:{model!r}:{reason}")

    for rule in (idempotency_format_error(row.get("idempotency_key")),):
        if rule:
            violated.add(rule)

    for col in ("consecutive_failures", "block_recurrences"):
        val = row.get(col)
        if isinstance(val, int) and not isinstance(val, bool) and val < 0:
            violated.add(f"{col}_negative:{val}")

    return violated


def check_run_refs(conn: sqlite3.Connection, task_id: str, row: Mapping[str, Any]) -> set[str]:
    """``current_run_id`` must point at a run of THIS card (dangling refs are
    how a half-applied write shows up)."""
    current = row.get("current_run_id")
    if current is None:
        return set()
    try:
        hit = conn.execute(
            "SELECT 1 FROM task_runs WHERE id = ? AND task_id = ?", (int(current), task_id)
        ).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return {f"run_ref_broken:{current!r}"}
    return set() if hit else {f"run_ref_broken:{current!r}"}


def check_event_chain(conn: sqlite3.Connection, task_id: str, prev_max_event_id: int) -> set[str]:
    """The ``task_events`` log for this card must stay append-only and
    well-formed for everything written since *prev_max_event_id*.

    Note: ``task_events`` carries NO ``prev_hash`` chain (the schema is
    id/task_id/run_id/kind/payload/created_at) — the mem.db ``prev_hash`` chain
    belongs to ``memoria-v2/trigger.py`` and is verified with
    ``trigger.py --verify-chain``; here the chain invariant is append-only +
    parseable payloads.
    """
    violated: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT id, kind, payload FROM task_events WHERE task_id = ? AND id > ? "
            "ORDER BY id DESC LIMIT ?",
            (task_id, prev_max_event_id, _EVENT_SCAN_LIMIT),
        ).fetchall()
    except sqlite3.Error as exc:
        return {f"event_chain_unreadable:{exc}"}
    for row in rows:
        rid = _row_field(row, 0)
        kind = _row_field(row, 1)
        payload = _row_field(row, 2)
        if not kind or not str(kind).strip():
            violated.add(f"event_kind_empty:{rid}")
        if payload is not None:
            try:
                json.loads(payload)
            except (TypeError, ValueError):
                violated.add(f"event_payload_unparseable:{rid}")
    return violated


def _row_field(row: Any, index: int) -> Any:
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Snapshot / diff / ledger (the backup side of the pattern)
# --------------------------------------------------------------------------- #

def _row_dict(conn: sqlite3.Connection, table: str, row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    cols = [info[1] for info in conn.execute(f"PRAGMA table_info({table})")]
    return dict(zip(cols, row))


def snapshot(conn: sqlite3.Connection, task_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Before/after-state of one card: the ``tasks`` row plus the watermarks
    needed to restore (event/run ids) and the runs that were still open.

    ``None`` = no such row (used as the "before" of a create).
    """
    if not task_id:
        return None
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    max_event_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()
    max_run_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()
    active_rows = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND ended_at IS NULL", (task_id,)
    ).fetchall()
    return {
        "row": _row_dict(conn, "tasks", row),
        "max_event_id": int(_row_field(max_event_id, 0) or 0),
        "max_run_id": int(_row_field(max_run_id, 0) or 0),
        "active_runs": [r for r in (_row_dict(conn, "task_runs", ar) for ar in active_rows) if r],
    }


def sha256_of(obj: Any) -> str:
    """sha256 of the canonical JSON encoding (keys sorted, stable)."""
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pretty(obj: Any) -> list[str]:
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, default=str)
    return text.splitlines()


def row_diff(before: Any, after: Any) -> str:
    """Unified diff of the canonical before/after state (truncated), recorded
    in the ledger so every gated write is auditable line by line."""
    diff = "\n".join(
        difflib.unified_diff(
            _pretty(before), _pretty(after), fromfile="before", tofile="after", lineterm="", n=2
        )
    )
    if len(diff) > _DIFF_LIMIT:
        diff = diff[:_DIFF_LIMIT] + "\n...[diff truncated]"
    return diff


def _db_file(conn: sqlite3.Connection) -> Optional[Path]:
    """File path of the database behind *conn* (ledger goes next to it)."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    try:
        path = row[2]
    except (IndexError, KeyError, TypeError):
        return None
    return Path(path) if path else None


def ledger_path(conn: sqlite3.Connection) -> Optional[Path]:
    db_file = _db_file(conn)
    if db_file is None:
        return None
    return db_file.parent / LEDGER_NAME


def ledger_append(conn: sqlite3.Connection, entry: Mapping[str, Any]) -> Optional[Path]:
    """Append one JSON line to the board's write-gate ledger.

    Audit-only: a ledger IO failure is logged, never raised — the checklist is
    what blocks a bad write, and wedging the board because a log file could not
    be appended would be a worse failure than a missing audit line.
    """
    path = ledger_path(conn)
    if path is None:
        return None
    payload = dict(entry)
    payload.setdefault("at", int(time.time()))
    payload.setdefault("gate", GATE_VERSION)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        _log.warning("kanban write-gate: ledger append failed (%s): %s", path, exc)
        return None
    return path


def ledger_read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Parse the ledger (tests/diagnostics); missing file → []."""
    path = ledger_path(conn)
    if path is None or not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                entries.append({"raw": line, "gate": "unparseable"})
    return entries


# --------------------------------------------------------------------------- #
# Restore (the automatic-restore side of the pattern)
# --------------------------------------------------------------------------- #

_TASK_CHILD_TABLES = (
    "task_links",
    "task_comments",
    "task_events",
    "task_runs",
    "task_attachments",
    "kanban_notify_subs",
)


def _delete_task_everywhere(conn: sqlite3.Connection, task_id: str) -> None:
    """Roll a create back to "never happened": drop the row and everything the
    mutation attached to it (links/comments/events/runs/attachments/subs)."""
    for table in _TASK_CHILD_TABLES:
        try:
            cols = {info[1] for info in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if "task_id" in cols:
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def restore(conn: sqlite3.Connection, task_id: Optional[str], snap: Optional[Mapping[str, Any]]) -> bool:
    """Re-apply the snapshot. Returns True on success.

    The before-state itself lives on in the ledger (``before_sha256`` + the
    diff), so a restore never destroys the backup: the restore can be undone by
    replaying the ledger entry. Runs created by the mutation are deleted, runs
    that were open before are put back exactly as they were, and events beyond
    the watermark are dropped so the event log returns to its pre-mutation state.
    """
    if not task_id:
        return False
    from hermes_cli.kanban_db_connect import write_txn

    try:
        with write_txn(conn, allow_nested=True):
            if snap is None:
                _delete_task_everywhere(conn, task_id)
            else:
                row = dict(snap.get("row") or {})
                row.pop("id", None)
                if row:
                    sets = ", ".join(f"{col} = ?" for col in row)
                    conn.execute(
                        f"UPDATE tasks SET {sets} WHERE id = ?", [*row.values(), task_id]
                    )
                conn.execute(
                    "DELETE FROM task_events WHERE task_id = ? AND id > ?",
                    (task_id, int(snap.get("max_event_id") or 0)),
                )
                conn.execute(
                    "DELETE FROM task_runs WHERE task_id = ? AND id > ?",
                    (task_id, int(snap.get("max_run_id") or 0)),
                )
                for run in snap.get("active_runs") or ():
                    run = dict(run)
                    run_id = run.pop("id", None)
                    if run_id is None or not run:
                        continue
                    sets = ", ".join(f"{col} = ?" for col in run)
                    conn.execute(
                        f"UPDATE task_runs SET {sets} WHERE id = ?", [*run.values(), run_id]
                    )
    except sqlite3.Error as exc:
        _log.error("kanban write-gate: restore of %s failed: %s", task_id, exc)
        return False
    return True


# --------------------------------------------------------------------------- #
# Checklist driver (the "checklist → restore" step)
# --------------------------------------------------------------------------- #

def violations(
    conn: sqlite3.Connection,
    task_id: Optional[str],
    snap: Optional[Mapping[str, Any]],
    prev_snap: Optional[Mapping[str, Any]] = None,
) -> set[str]:
    """Violated rule ids for *snap* (empty = clean).

    *prev_snap* is the pre-mutation snapshot: the event-chain watermark is
    compared against it, and callers compute the delta (``after - before``) so
    only NEW violations block. ``prev_snap=None`` scans the card's newest
    events (the symmetric "before" pass).
    """
    if snap is None or not task_id:
        return set()
    row = snap.get("row") or {}
    violated = check_task_row(row)
    violated |= check_run_refs(conn, task_id, row)
    watermark = int((prev_snap or {}).get("max_event_id") or 0) if prev_snap is not None else -1
    violated |= check_event_chain(conn, task_id, watermark)
    if prev_snap is not None and int(snap.get("max_event_id") or 0) < int(prev_snap.get("max_event_id") or 0):
        violated.add("event_chain_regression")
    return violated


def _record(
    conn: sqlite3.Connection,
    task_id: Optional[str],
    kind: str,
    action: str,
    violated: Any,
    before: Any,
    after: Any,
    error: Optional[str] = None,
) -> None:
    entry: dict[str, Any] = {
        "kind": kind,
        "task_id": task_id,
        "action": action,
        "violations": sorted(violated) if violated else [],
        "before_sha256": sha256_of(before) if before is not None else None,
        "after_sha256": sha256_of(after) if after is not None else None,
        "diff": row_diff(before, after),
    }
    if error:
        entry["error"] = error
    ledger_append(conn, entry)


# --------------------------------------------------------------------------- #
# Pre-checks (refuse the bad input BEFORE anything is written)
# --------------------------------------------------------------------------- #

def require_assignee(conn: sqlite3.Connection, profile: Optional[str]) -> None:
    """``ValueError`` when *profile* is not an on-disk profile.

    Registry-based only (no DB-history leniency): a name that merely exists on
    another card is exactly the stale-assignee bug this rule catches. Skipped
    entirely when the registry is unavailable (see :func:`assignee_registry`).
    """
    if profile is None or not enabled():
        return
    known, available = assignee_registry()
    if not available or profile in known:
        return
    raise ValueError(
        f"unknown assignee profile {profile!r}; on-disk profiles: {sorted(known)}"
    )


def require_model(model: Optional[str], provider: Optional[str] = None) -> None:
    """``ValueError`` when the model/provider override does not exist locally.

    Shares :func:`hermes_cli.model_gate.model_known` with the dispatcher
    preflight — write-time and dispatch-time verdicts cannot drift apart.
    """
    if not model or not enabled():
        return
    from hermes_cli import model_gate

    ok, reason = model_gate.model_known(str(model), provider)
    if not ok:
        raise ValueError(f"unknown model {model!r} (provider={provider or 'profile default'}): {reason}")


def require_idempotency_key(key: Optional[str]) -> None:
    """``ValueError`` for a malformed ``reg:`` registry key (create path)."""
    if not key or not enabled():
        return
    rule = idempotency_format_error(key)
    if rule:
        raise ValueError(f"malformed idempotency_key {key!r}: expected reg:<board>|<id>(|<kind>)")


def require_model_pin(
    assignee: Optional[str], model: Optional[str], provider: Optional[str] = None,
) -> None:
    """``ValueError`` when the card pin diverges from the profile's model pin.

    Layer (c) — the read-only pin: a card may only mirror the assignee
    profile's effective ``model.default``/``model.provider`` (the one tier that
    decides the model; see ``hermes_cli.model_pin_drift``). Clearing a pin
    (``model=None``) and any pin on a profile without a pin of its own are
    always allowed; every error path is fail-open, so this can never park a
    card on a config we cannot read.
    """
    if not model or not enabled() or not assignee:
        return
    try:
        from hermes_cli import model_pin_drift
        from hermes_cli.profiles import resolve_profile_env

        try:
            profile_home = str(resolve_profile_env(str(assignee)))
        except Exception:
            return
        drift = model_pin_drift.card_override_drift(
            model, provider, profile_home=profile_home,
        )
    except Exception as exc:
        _log.debug("write-gate: model-pin check skipped (%s)", exc)
        return
    if drift:
        raise ValueError(drift)


# --------------------------------------------------------------------------- #
# The gate decorator (snapshot → mutate → diff → checklist → restore)
# --------------------------------------------------------------------------- #

def gated(
    kind: str,
    *,
    task_id_arg: str = "task_id",
    returns_task_id: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap a ``kanban_db`` mutation function with the full gate.

    ``returns_task_id=True`` is for ``create_task``: there is no before-row, so
    a violated create is restored by deleting the row it just made.

    The wrapper verifies even when the wrapped function raised: a mutation can
    commit and then fail in a post-commit side effect (observer/notify), and
    "the row changed but the caller saw an exception" is precisely the state a
    silent corruption hides in.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not enabled():
                return fn(*args, **kwargs)
            conn = args[0] if args else kwargs.get("conn")
            if conn is None:
                return fn(*args, **kwargs)
            task_id: Optional[str] = None
            if not returns_task_id:
                task_id = args[1] if len(args) > 1 else kwargs.get(task_id_arg)
            before = None if returns_task_id else snapshot(conn, task_id)
            before_violations: set[str] = set()
            if before is not None:
                before_violations = violations(conn, task_id, before, None)
            # ``create_task`` can also RETURN a pre-existing row (idempotency
            # hit): only a call that actually inserted rows may be rolled back.
            # Comparing total_changes avoids ever "restoring" a row this call
            # never touched — that would delete someone else's card.
            changes_before = conn.total_changes

            exc: Optional[BaseException] = None
            result: Any = None
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc_:  # verified below, then re-raised
                exc = exc_

            created = bool(returns_task_id) and conn.total_changes > changes_before
            target = result if (returns_task_id and isinstance(result, str)) else task_id
            # Verify only against a state we can compare: an existing row
            # (``before``) or a row this very call inserted (``created``).
            if target and (before is not None or created):
                after = snapshot(conn, target)
                if after is not None:
                    after_violations = violations(conn, target, after, before)
                    new_violations = sorted(after_violations - before_violations)
                    if new_violations:
                        _record(conn, target, kind, "violation", new_violations, before, after,
                                error=type(exc).__name__ if exc else None)
                        restored = restore(conn, target, before)
                        # Two ledger lines: the violation (pre → violated) and the
                        # reversal (violated → pre). The before-state stays in the
                        # first entry, so the restore itself is reversible.
                        _record(conn, target, kind, "restored" if restored else "restore_failed",
                                new_violations, after, before if restored else None)
                        raise GateViolation(target, kind, new_violations, restored=restored) from exc
                    if exc is None:
                        _record(conn, target, kind, "verified", [], before, after)
            if exc is not None:
                raise exc
            return result

        return wrapper

    return decorate
