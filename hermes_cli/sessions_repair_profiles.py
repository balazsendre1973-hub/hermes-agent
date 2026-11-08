"""``hermes sessions repair-profiles`` — settle crossed-profile durable state across every store.

The per-profile store model (#88734) and the identity fences that followed are forward-only: they
put NEW rows in the right place and refuse to widen existing damage, but nothing walks the stores
and settles what an earlier release left crossed. This does, for six kinds of crossing:

1. ``sessions.profile_name`` disagreeing with the profile in the row's own ``session_key``;
2. rows physically in the wrong store (a named profile's keys in the root ``state.db``, another
   profile's keys in ``profiles/<p>/state.db``) — moved to the owning profile's store;
3. ``parent_session_id`` crossing namespaces — severed (the child's own identity is untouched);
4. ``gateway_routing`` rows in a named profile's store (the routing index lives in the gateway's
   home; #66887 copied every profile's rows into whichever store was scoped) — moved to the routing
   store when absent there, dropped otherwise — and root rows for a namespace no profile claims;
5. profile-less Telegram topic bindings and ``gateway_voice_mode.json`` entries owned by a
   non-default bot — relabelled from the evidence of the rows themselves;
6. ``sessions.json`` mirror entries whose key namespace no served profile claims — dropped, since
   the legacy import re-injects any key the routing DB lacks on every boot.

Plus the LOCAL creation receipts in ``state_meta`` (``gateway.local_policy.v1:*``) whose
``profile_id`` names a profile other than their own store's home: the boot restore refuses every
one of them with ``profile_mismatch``, and ``--orphan-receipts`` settles each instead of leaving
it to the ``unclaimed_namespace`` dead end its session row reports — ``retire`` (default) drops
the receipt and leaves the guarded row's transcript as ordinary history, ``rekey`` moves the
receipt onto this store's profile keeping its session id, ``report`` leaves them alone.

Legacy ``agent:main:`` rows inside a named profile's store are reported but NOT repaired unless
``--legacy-main`` says how: they are either a standalone (pre-multiplex) gateway's own history that
must be rekeyed to ``agent:<p>:`` (the #113884 incident), or a default-profile chat that leaked in
under a scoped write (#102157) and must move to the root store. The row carries no evidence that
tells the two apart, and guessing wrong hands a conversation to the other bot.

Report-only by default. ``--apply`` refuses while a gateway owning any touched store is live (it
holds the routing index in memory and writes it back), takes a quick snapshot of every store it
will mutate, and is idempotent: a second run finds nothing.
"""
from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_LEGACY_MAIN_CHOICES = ("report", "rekey", "move")
_ORPHAN_RECEIPT_CHOICES = ("retire", "rekey", "report")
_VOICE_MODE_FILE = "gateway_voice_mode.json"


@dataclass(frozen=True)
class Store:
    """One profile's ``state.db``; ``routing`` marks the store that owns the multiplexer's index."""
    profile: str
    home: Path
    routing: bool = False

    @property
    def db_path(self) -> Path:
        return self.home / "state.db"


@dataclass
class Finding:
    kind: str
    store: str
    subject: str
    detail: str
    action: Optional[str] = None      # what --apply does; None = report only
    reason: Optional[str] = None      # why it is report only
    _fix: Optional[Callable[[_Session], dict[str, int]]] = field(default=None, repr=False, compare=False)

    @property
    def repairable(self) -> bool:
        return self._fix is not None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "store": self.store, "subject": self.subject, "detail": self.detail,
                "action": self.action, "reason": self.reason}


class _Session:
    """Open store handles for one run; every store opened at most once, all released at the end."""

    def __init__(self, *, read_only: bool) -> None:
        self.read_only = read_only
        self._dbs: dict[Path, Any] = {}

    def db(self, store: Store):
        """The store's SessionDB. Read-only runs never create a file: a live profile that has no
        ``state.db`` yet reads as empty; an apply run opens it for real when a move lands there."""
        path = store.db_path
        if path not in self._dbs:
            if self.read_only:
                from hermes_state import SessionDB
                self._dbs[path] = SessionDB(path, read_only=True) if path.exists() else _EMPTY_STORE
            else:
                from hermes_state_registry import acquire
                self._dbs[path] = acquire(path)
        return self._dbs[path]

    def close(self) -> None:
        from hermes_state_registry import release_or_close
        for db in self._dbs.values():
            if db is _EMPTY_STORE:
                continue
            with contextlib.suppress(Exception):
                release_or_close(db) if not self.read_only else db.close()
        self._dbs.clear()


class _EmptyStore:
    """Stand-in for a live profile whose ``state.db`` does not exist yet: nothing to find."""
    def find_crossed_profile_sessions(self, owner):
        return {"mislabelled": [], "foreign": [], "crossed_parents": []}

    def find_profile_less_telegram_topic_rows(self):
        return []

    def list_gateway_routing_rows(self):
        return []

    def key_profiles_for_chat(self, platform, chat_id):
        return set()

    def _read_all(self, sql, params=()):
        return []  # no state.db: no receipts to find


_EMPTY_STORE = _EmptyStore()


# ── enumeration ───────────────────────────────────────────────────────────────

def enumerate_stores() -> list[Store]:
    """Default root plus every live named profile (a live profile claims its namespace whether or not
    it has written a ``state.db`` yet). The root store owns the routing index: the multiplexer's
    ``_routing_home`` is its launch home, the root."""
    from hermes_cli.profiles import _get_default_hermes_home, _iter_named_profile_dirs
    root = _get_default_hermes_home()
    stores = [Store("default", root, routing=True)]
    stores.extend(Store(entry.name, entry) for entry in _iter_named_profile_dirs())
    return stores


def _gateway_multiplexes(root: Path) -> bool:
    """Does the default gateway serve every profile? Same reader as every other CLI surface: the live
    record, else the explicit flag, else False — an unset flag is a verdict only the gateway reaches,
    and guessing "yes" would uproot a standalone gateway's own routing index."""
    from hermes_cli.gateway_multiplex_mode import default_gateway_multiplexes
    try:
        return default_gateway_multiplexes(root)
    except Exception:
        return False


def live_gateway_homes(stores: Iterable[Store]) -> list[tuple[str, int]]:
    from gateway.status import live_gateway_pid_for_home
    live = []
    for store in stores:
        pid = live_gateway_pid_for_home(store.home)
        if pid is not None:
            live.append((store.profile, pid))
    return live


# ── scan ──────────────────────────────────────────────────────────────────────

class RepairPlan:
    def __init__(self, stores: list[Store], *, legacy_main: str = "report",
                 orphan_receipts: str = "retire") -> None:
        if legacy_main not in _LEGACY_MAIN_CHOICES:
            raise ValueError(f"legacy_main must be one of {_LEGACY_MAIN_CHOICES}")
        if orphan_receipts not in _ORPHAN_RECEIPT_CHOICES:
            raise ValueError(f"orphan_receipts must be one of {_ORPHAN_RECEIPT_CHOICES}")
        self.stores = stores
        self.by_profile = {s.profile: s for s in stores}
        self.claimed: set[str] = set(self.by_profile)
        self.routing_store = next((s for s in stores if s.routing), None)
        self.legacy_main = legacy_main
        self.orphan_receipts = orphan_receipts
        self.multiplexes = _gateway_multiplexes(self.routing_store.home) if self.routing_store else False
        self.findings: list[Finding] = []
        self._moves: dict[tuple[Path, Path], _MoveBatch] = {}

    # -- helpers --
    def _add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def repairable(self) -> list[Finding]:
        return [f for f in self.findings if f.repairable]

    # -- scan --
    def scan(self, session: _Session) -> RepairPlan:
        for store in self.stores:
            self._scan_sessions(session, store)
            self._scan_topics(session, store)
            self._scan_state_meta_receipts(session, store)
        for store in self.stores:
            self._scan_routing(session, store)
        if self.routing_store is not None:
            self._scan_voice_modes(session, self.routing_store)
            self._scan_sessions_json(self.routing_store)
        return self

    def _scan_sessions(self, session: _Session, store: Store) -> None:
        db = session.db(store)
        crossed = db.find_crossed_profile_sessions(store.profile)
        moving: set[str] = set()
        held = _ledgered_lineages([r for r in crossed["foreign"] if self._would_move(store, r)])
        for row in crossed["foreign"]:
            if row["id"] in held:
                moving.add(row["id"])  # neither relabelled nor severed in place: it is a stray
                self._add(Finding(
                    "wrong_store", store.profile, row["id"],
                    f"key {row['session_key']!r} ({row['message_count']} messages) sits in the "
                    f"{store.profile} store", reason=held[row["id"]]))
                continue
            self._plan_foreign_row(store, row, moving)
        for row in crossed["mislabelled"]:
            if row["id"] in moving:
                continue  # the move stamps the right label
            self._add(Finding(
                "mislabelled", store.profile, row["id"],
                f"profile_name={row['profile_name']!r} but key {row['session_key']!r} belongs to "
                f"{row['key_profile']!r}", action=f"relabel to {row['key_profile']!r}",
                _fix=lambda s, st=store, sid=row["id"]: {
                    "relabelled": s.db(st).relabel_sessions_to_key_profile([sid])}))
        for row in crossed["crossed_parents"]:
            if row["id"] in moving:
                continue  # the import drops a parent that is not in the target store
            self._add(Finding(
                "crossed_parent", store.profile, row["id"],
                f"{row['key_profile']!r} row inherits from parent {row['parent_session_id']} keyed under "
                f"{row['parent_profile']!r}", action="sever parent_session_id",
                _fix=lambda s, st=store, sid=row["id"]: {"severed": s.db(st).sever_crossed_parents([sid])}))
        for row in db._read_all(
                "SELECT id, session_key, transport_profile FROM sessions "
                "WHERE transport_profile IS NOT NULL AND transport_profile <> ?",
                (store.profile,)):
            dead = row["transport_profile"]
            if dead in self.claimed:
                continue  # names a live profile (incl. default): cross-profile transport, not residue
            self._add(Finding(
                "transport_profile_dead_name", store.profile, row["id"],
                f"transport_profile={dead!r} on session {row['session_key']!r} names a profile "
                f"that no longer exists", action=f"relabel transport_profile to {store.profile!r}",
                _fix=lambda s, st=store, sid=row["id"], dead=dead: {
                    "transport_profile_relabelled": _relabel_transport_profile(s, st, sid, dead)}))

    def _would_move(self, store: Store, row: dict[str, Any]) -> bool:
        """Mirror of :meth:`_plan_foreign_row`: does the plan copy this row to another store?"""
        if row["key_profile"] == "default" and store.profile != "default":
            return self.legacy_main == "move" and self.routing_store is not None
        return row["key_profile"] in self.claimed

    def _plan_foreign_row(self, store: Store, row: dict[str, Any], moving: set[str]) -> None:
        key_profile, sid = row["key_profile"], row["id"]
        detail = f"key {row['session_key']!r} ({row['message_count']} messages) sits in the {store.profile} store"
        if key_profile == "default" and store.profile != "default":
            self._plan_legacy_main_row(store, row, moving)
            return
        if key_profile not in self.claimed:
            self._add(Finding(
                "unclaimed_namespace", store.profile, sid, detail,
                reason=f"no live profile named {key_profile!r}; create it, then rerun, or "
                       f"`hermes profile migrate-identity {key_profile} <target>`"))
            return
        moving.add(sid)
        self._add(Finding(
            "wrong_store", store.profile, sid, detail, action=f"move to the {key_profile} store",
            _fix=self._move_batch(store, self.by_profile[key_profile]).fix_for(sid)))

    def _plan_legacy_main_row(self, store: Store, row: dict[str, Any], moving: set[str]) -> None:
        sid = row["id"]
        detail = (f"legacy default-namespace key {row['session_key']!r} ({row['message_count']} messages) "
                  f"in the {store.profile} store")
        if self.legacy_main == "rekey":
            self._add(Finding(
                "legacy_main", store.profile, sid, detail, action=f"rekey to agent:{store.profile}:",
                _fix=lambda s, st=store, i=sid: {"rekeyed": s.db(st).rekey_legacy_main_sessions([i], st.profile)}))
        elif self.legacy_main == "move" and self.routing_store is not None:
            moving.add(sid)
            self._add(Finding(
                "legacy_main", store.profile, sid, detail, action="move to the default store",
                _fix=self._move_batch(store, self.routing_store).fix_for(sid)))
        else:
            self._add(Finding(
                "legacy_main", store.profile, sid, detail,
                reason="either a standalone gateway's own history (--legacy-main rekey) or a default "
                       "chat that leaked in (--legacy-main move); the row cannot say which"))

    def _move_batch(self, src: Store, dst: Store) -> _MoveBatch:
        return self._moves.setdefault((src.db_path, dst.db_path), _MoveBatch(src, dst))

    def _scan_topics(self, session: _Session, store: Store) -> None:
        rows = session.db(store).find_profile_less_telegram_topic_rows()
        for row in rows:
            self._add(Finding(
                "topic_profile_less", store.profile, f"chat {row['chat_id']} thread {row['thread_id']}",
                f"telegram topic binding labelled 'default' but keyed {row['session_key']!r}",
                action=f"relabel to {row['key_profile']!r}",
                _fix=lambda s, st=store, r=row: s.db(st).relabel_telegram_topic_rows([r])))

    def _scan_state_meta_receipts(self, session: _Session, store: Store) -> None:
        """``state_meta`` LOCAL creation receipts this store cannot serve: ``receipt.profile_id``
        names a profile other than *store*'s own home, so the boot restore refuses each one with
        ``profile_mismatch`` (``owned_receipt``). The transcript underneath is intact, so every
        receipt gets a concrete settlement here instead of only its session row's
        ``unclaimed_namespace`` dead end: ``retire`` (default) drops the receipt and leaves the row
        as ordinary history, ``rekey`` moves the receipt onto this profile."""
        from hermes_state_local import POLICY_PREFIX

        home = str(store.home)
        for row in session.db(store)._read_all(
                "SELECT key, value FROM state_meta WHERE key GLOB ?", (POLICY_PREFIX + "*",)):
            try:
                receipt = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
            profile_id = receipt.get("profile_id") if isinstance(receipt, dict) else None
            if not isinstance(profile_id, str) or profile_id == home:
                continue
            sid = row["key"][len(POLICY_PREFIX):]
            route = receipt.get("route")
            detail = (f"creation receipt for session {sid} names profile {profile_id!r} while this "
                      f"store belongs to {home!r}"
                      + (f"; route {route!r}" if isinstance(route, str) else ""))
            if self.orphan_receipts == "retire":
                self._add(Finding(
                    "orphan_receipt", store.profile, sid, detail,
                    action=f"retire receipt ({row['key']}); the transcript stays as history",
                    _fix=lambda s, st=store, i=sid: {"receipts_retired": _retire_orphan_receipt(s, st, i)}))
            elif self.orphan_receipts == "rekey":
                self._add(Finding(
                    "orphan_receipt", store.profile, sid, detail,
                    action=f"rekey receipt onto {home} (profile, route and the row's key namespace)",
                    _fix=lambda s, st=store, i=sid: {"receipts_rekeyed": _rekey_orphan_receipt(s, st, i)}))
            else:
                self._add(Finding(
                    "orphan_receipt", store.profile, sid, detail,
                    reason="its profile no longer exists; settle it with --orphan-receipts retire "
                           "or rekey"))

    def _scan_routing(self, session: _Session, store: Store) -> None:
        from hermes_state_profile_repair import session_key_profile
        rows = session.db(store).list_gateway_routing_rows()
        for row in rows:
            key_profile = session_key_profile(row["session_key"])
            if key_profile is None:
                continue
            subject = f"{row['session_key']} (scope {row['scope']!r})"
            if store.routing:
                if key_profile not in self.claimed:
                    self._add(Finding(
                        "routing_unclaimed", store.profile, subject,
                        f"routing row for a namespace no live profile claims ({key_profile!r}); every "
                        "inbound event on it logs a missing-profile error", action="delete",
                        _fix=lambda s, st=store, r=row: {
                            "routing_deleted": s.db(st).delete_gateway_routing_rows([(r["scope"], r["session_key"])])}))
                continue
            if key_profile == store.profile and not self.multiplexes:
                continue  # a standalone gateway's own index lives in its own store
            routing_store = self.routing_store
            if routing_store is None:
                continue
            self._add(Finding(
                "routing_stray", store.profile, subject,
                f"routing row in the {store.profile} store; the index lives in the default store",
                action="move to the default store (existing row there wins)",
                _fix=lambda s, st=store, r=row, rs=routing_store: self._move_routing_row(s, st, rs, r)))

    def _move_routing_row(self, session: _Session, store: Store, routing_store: Store,
                          row: dict[str, Any]) -> dict[str, int]:
        # Re-read inside the transaction: an earlier fix in this same apply may already have
        # settled (or rekeyed) the row, and copying the stale report back would resurrect it.
        if not session.db(store)._read_all(
                "SELECT 1 FROM gateway_routing WHERE scope = ? AND session_key = ?",
                (row["scope"], row["session_key"])):
            return {}
        adopted = session.db(routing_store).insert_gateway_routing_rows_if_absent(
            [(row["scope"], row["session_key"], row["entry_json"], row["updated_at"])])
        deleted = session.db(store).delete_gateway_routing_rows([(row["scope"], row["session_key"])])
        return {"routing_adopted": adopted, "routing_deleted": deleted}

    def _scan_voice_modes(self, session: _Session, store: Store) -> None:
        path = store.home / _VOICE_MODE_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        for key in sorted(k for k in data if isinstance(k, str) and k.count(":") == 1):
            platform, chat_id = key.split(":", 1)
            owners: set[str] = set()
            for st in self.stores:
                owners |= session.db(st).key_profiles_for_chat(platform, chat_id)
            if not owners or "default" in owners:
                continue  # the default bot speaks there: the unprefixed key is its own
            if len(owners) > 1:
                self._add(Finding(
                    "voice_profile_less", store.profile, key,
                    f"voice mode entry without a profile; chat is held by {sorted(owners)}",
                    reason="more than one non-default bot speaks in this chat"))
                continue
            owner = next(iter(owners))
            self._add(Finding(
                "voice_profile_less", store.profile, key,
                f"voice mode {data[key]!r} without a profile; chat is held only by {owner!r}",
                action=f"rekey to {owner}:{key}",
                _fix=lambda s, p=path, k=key, o=owner: _rekey_voice_mode_entry(p, k, o)))

    def _scan_sessions_json(self, store: Store) -> None:
        from hermes_state_profile_repair import session_key_profile
        path = store.home / "sessions" / "sessions.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        for key in sorted(k for k in data if isinstance(k, str) and not k.startswith("_")):
            key_profile = session_key_profile(key)
            if key_profile is None or key_profile in self.claimed:
                continue
            self._add(Finding(
                "sessions_json_unclaimed", store.profile, key,
                f"sessions.json mirror entry for a namespace no live profile claims ({key_profile!r}); "
                "the legacy import re-injects it into routing on every boot", action="delete entry",
                _fix=lambda s, p=path, k=key: _drop_sessions_json_entry(p, k)))

    # -- apply --
    def apply(self, session: _Session, *, snapshot: Callable[[Store], Optional[str]]) -> dict[str, Any]:
        """Run every repairable fix; snapshots every store first. Fixes are independent and each
        re-checks its precondition, so a partial run leaves a state the next run completes."""
        snapshots = {s.profile: snapshot(s) for s in self.stores if s.db_path.exists()}
        totals: dict[str, int] = {}
        failures: list[dict[str, str]] = []
        for finding in self.repairable:
            fix = finding._fix
            assert fix is not None
            try:
                for key, count in (fix(session) or {}).items():
                    totals[key] = totals.get(key, 0) + int(count)
            except Exception as exc:  # one bad row must not abandon the rest of the plan
                logger.warning("repair-profiles: %s on %s failed", finding.kind, finding.subject, exc_info=True)
                failures.append({"kind": finding.kind, "subject": finding.subject,
                                 "error": f"{type(exc).__name__}: {exc}"})
        return {"snapshots": snapshots, "totals": totals, "failures": failures}


class _MoveBatch:
    """Every session moving from one store to another, moved together: export all, import all
    (parents before children, so a moved child keeps its moved parent), then delete from the source.
    Row by row, deleting a parent would detach the children still waiting in the source. Copy
    precedes delete, so a crash between the two leaves a duplicate the next run settles
    (``present`` on import, then the delete). A row that fails is that row's failure alone: the
    rest of the batch still moves, and only its lineage (descendants, and the parent it still
    points at in the source) waits with it for the next run."""

    def __init__(self, src: Store, dst: Store) -> None:
        self.src, self.dst = src, dst
        self.ids: list[str] = []
        self._result: Optional[dict[str, dict[str, int]]] = None
        self._errors: dict[str, Exception] = {}

    def fix_for(self, sid: str) -> Callable[[_Session], dict[str, int]]:
        self.ids.append(sid)
        return lambda session: self._outcome(session, sid)

    def _outcome(self, session: _Session, sid: str) -> dict[str, int]:
        result = self.run(session)
        if sid in self._errors:
            raise self._errors[sid]
        return result.get(sid, {"missing": 1})

    def run(self, session: _Session) -> dict[str, dict[str, int]]:
        if self._result is not None:
            return self._result
        src_db, dst_db = session.db(self.src), session.db(self.dst)
        from hermes_state_profile_repair import SessionLedgeredError
        payloads: dict[str, Optional[dict[str, Any]]] = {}
        for sid in self.ids:
            try:
                payloads[sid] = src_db.export_session_for_move(sid)
            except SessionLedgeredError as exc:  # gained ledger rows since the scan: never copied
                self._errors[sid] = exc
        ordered = _parents_first([p for p in payloads.values() if p is not None])
        result: dict[str, dict[str, int]] = {sid: {"missing": 1} for sid, p in payloads.items()
                                             if p is None and sid not in self._errors}
        for payload in ordered:
            sid, parent = payload["session"]["id"], payload["session"].get("parent_session_id")
            if parent in self._errors:
                # Imported now, it would land without the parent and never regain the link.
                self._errors[sid] = RuntimeError(
                    f"its parent {parent} did not move; kept in the {self.src.profile} store with it")
                continue
            try:
                outcome = dst_db.import_moved_session(payload, profile_name=self.dst.profile)
            except Exception as exc:
                self._errors[sid] = exc
                continue
            if dst_db.count_messages_all(sid) < len(payload["messages"]):
                logger.warning("repair-profiles: %s copied into %s with fewer messages than the source; "
                               "source row kept", sid, self.dst.profile)
                result[sid] = {"copied_incomplete": 1}
                continue
            result[sid] = {outcome: 1}
        # Deleting a row detaches its children still in the source; a child that did not move
        # must find its parent there on the next run.
        awaited = {p["session"].get("parent_session_id") for p in ordered if p["session"]["id"] in self._errors}
        for payload in ordered:
            sid = payload["session"]["id"]
            if sid not in result or sid in awaited or "copied_incomplete" in result[sid]:
                continue
            try:
                if src_db.delete_moved_session(sid):
                    result[sid]["moved"] = 1
            except Exception as exc:
                self._errors[sid] = exc
        self._result = result
        return result


def _ledgered_lineages(foreign: list[dict[str, Any]]) -> dict[str, str]:
    """Stray rows that must stay put, each with the reason. A row the gateway ledger references
    (admissions/workers, ``ON DELETE RESTRICT``) cannot be deleted from its store, so copying it
    would leave the session in both; the stray rows linked to it by ``parent_session_id`` stay
    with it, since moving a parent detaches the child left behind and a moved child loses its
    parent. Whether a move should carry the ledger is a separate policy decision."""
    by_id = {row["id"]: row for row in foreign}
    groups: dict[str, set[str]] = {sid: {sid} for sid in by_id}
    for row in foreign:
        parent = row.get("parent_session_id")
        if parent in by_id and groups[parent] is not groups[row["id"]]:
            merged = groups[parent] | groups[row["id"]]
            for sid in merged:
                groups[sid] = merged
    held: dict[str, str] = {}
    for sid, row in by_id.items():
        anchors = sorted(m for m in groups[sid] if by_id[m].get("ledgered"))
        if row.get("ledgered"):
            held[sid] = "has gateway admission/worker history, which a move cannot carry"
        elif anchors:
            held[sid] = f"its lineage holds {anchors[0]}, which has gateway admission/worker history"
    return held


def _parents_first(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order moved sessions so every parent in the batch precedes its children (a cycle, impossible
    for a well-formed store, falls back to insertion order)."""
    pending = {p["session"]["id"]: p for p in payloads}
    ordered: list[dict[str, Any]] = []
    while pending:
        ready = [sid for sid, p in pending.items() if p["session"].get("parent_session_id") not in pending]
        if not ready:
            ready = list(pending)
        for sid in ready:
            ordered.append(pending.pop(sid))
    return ordered


def _rekey_voice_mode_entry(path: Path, key: str, owner: str) -> dict[str, int]:
    from utils import atomic_json_write
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if key not in data:
        return {}
    data.setdefault(f"{owner}:{key}", data.pop(key))
    data.pop(key, None)
    atomic_json_write(path, data)
    return {"voice_rekeyed": 1}


def _drop_sessions_json_entry(path: Path, key: str) -> dict[str, int]:
    from utils import atomic_json_write
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if key not in data:
        return {}
    del data[key]
    atomic_json_write(path, data, mode=0o600)
    return {"sessions_json_dropped": 1}


def _retire_orphan_receipt(session: _Session, store: Store, sid: str) -> int:
    """Drop *sid*'s LOCAL creation receipt, so the boot restore stops refusing the row with
    ``profile_mismatch``. The ``sessions``/``messages`` rows underneath are untouched: a retired
    receipt leaves the conversation as ordinary history. Re-derives the target inside the
    transaction — never trusts a stale report."""
    from hermes_state_local import POLICY_PREFIX

    home = str(store.home)

    def _do(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (POLICY_PREFIX + sid,)).fetchone()
        if row is None:
            return 0
        try:
            receipt = json.loads(row[0])
        except (TypeError, ValueError):
            return 0
        if not isinstance(receipt, dict) or receipt.get("profile_id") == home:
            return 0  # already this store's own receipt: the report was stale
        return conn.execute("DELETE FROM state_meta WHERE key = ?", (POLICY_PREFIX + sid,)).rowcount

    return session.db(store)._execute_write(_do)


def _rekey_orphan_receipt(session: _Session, store: Store, sid: str) -> int:
    """Move *sid*'s LOCAL receipt onto *store*'s profile, keeping the session id.

    A receipt's id is ``local_identity(profile_id, principal_id, request_id)``: rewriting only
    ``profile_id`` would strand the receipt (its id no longer derives from it) and re-deriving the
    id would strand the row it guards — messages, admissions and worker history all key on it. So
    the id stays and is recorded as the row's legacy identity (``gateway.local_legacy.v1:<sid>``,
    the store's own mechanism for a session id that is not derived from its receipt), and the
    row's key namespace moves to this profile exactly as ``rekey_profile_state`` does for a rename.
    """
    from gateway.session import _session_key_namespace
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_local_migration import LEGACY_PREFIX
    from hermes_state_runtime import _json

    home = str(store.home)

    def _do(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (POLICY_PREFIX + sid,)).fetchone()
        if row is None:
            return 0
        receipt = json.loads(row[0])
        if receipt.get("profile_id") == home:
            return 0  # already rekeyed
        if receipt.get("session_id") != sid:
            raise ValueError(f"its session id {receipt.get('session_id')!r} is not its key")
        route = receipt.get("route")
        if not isinstance(route, str) or not route.startswith("agent:") or route.count(":") < 2:
            raise ValueError(f"route {route!r} is not an agent: key")
        new_route = _session_key_namespace(store.profile) + ":" + route.split(":", 2)[2]
        if new_route != route and conn.execute(
                "SELECT 1 FROM sessions WHERE session_key = ? AND id <> ?",
                (new_route, sid)).fetchone() is not None:
            raise ValueError(f"{new_route} is already owned by another row")
        receipt["profile_id"] = home
        receipt["legacy_session_id"] = sid
        receipt["route"] = new_route
        entry = receipt.get("entry")
        if isinstance(entry, dict):
            entry["session_key"] = new_route
            entry["session_id"] = sid
        _rekey_profile_refs(receipt, store.profile)
        conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (_json(receipt), POLICY_PREFIX + sid))
        conn.execute("INSERT OR REPLACE INTO state_meta(key, value) VALUES (?, ?)",
                     (LEGACY_PREFIX + sid, _json({"session_id": sid, "profile_id": home,
                                                  "principal_id": receipt.get("principal_id"),
                                                  "legacy_session_id": sid})))
        conn.execute("UPDATE sessions SET session_key = ?, profile_name = ? WHERE id = ?",
                     (new_route, store.profile, sid))
        stored = conn.execute("SELECT origin_json FROM sessions WHERE id = ?", (sid,)).fetchone()
        if stored is not None and stored[0]:
            try:
                origin = json.loads(stored[0])
            except (TypeError, ValueError):
                origin = None
            if isinstance(origin, dict):
                _rekey_profile_refs(origin, store.profile)
                conn.execute("UPDATE sessions SET origin_json = ? WHERE id = ?",
                             (json.dumps(origin, ensure_ascii=False), sid))
        for routing in conn.execute("SELECT scope, entry_json FROM gateway_routing WHERE session_key = ?",
                                    (route,)).fetchall():
            body = None
            if routing["entry_json"]:
                try:
                    body = json.loads(routing["entry_json"])
                except (TypeError, ValueError):
                    body = None
            if isinstance(body, dict):
                if isinstance(body.get("session_key"), str):
                    body["session_key"] = new_route
                _rekey_profile_refs(body, store.profile)
                body = json.dumps(body, ensure_ascii=False)
            conn.execute("UPDATE gateway_routing SET session_key = ?, entry_json = ? "
                         "WHERE scope = ? AND session_key = ?",
                         (new_route, body, routing["scope"], route))
        return 1

    return session.db(store)._execute_write(_do)


def _relabel_transport_profile(session: _Session, store: Store, sid: str, dead: str) -> int:
    """Relabel one dead ``sessions.transport_profile`` onto this store's own profile.

    Re-checks the stale value inside the transaction so a concurrent fix or an earlier run
    cannot clobber a fresher name. Idempotent: a row already relabelled matches nothing.
    """
    def _do(conn):
        return conn.execute(
            "UPDATE sessions SET transport_profile = ? WHERE id = ? AND transport_profile = ?",
            (store.profile, sid, dead)).rowcount

    return session.db(store)._execute_write(_do)


def _rekey_profile_refs(payload: Any, profile: str) -> None:
    """Rewrite every ``profile`` field inside a receipt / origin / routing-entry JSON body to
    *profile*: the old home's name is stamped in ``origin.profile`` and ``policy.source.profile``,
    and leaving either would read the row back as another profile's."""
    if isinstance(payload, dict):
        if payload.get("profile"):
            payload["profile"] = profile
        for value in payload.values():
            _rekey_profile_refs(value, profile)
    elif isinstance(payload, list):
        for value in payload:
            _rekey_profile_refs(value, profile)


# ── entry points ──────────────────────────────────────────────────────────────

def scan_stores(stores: Optional[list[Store]] = None, *, legacy_main: str = "report",
                orphan_receipts: str = "retire", read_only: bool = True) -> tuple[RepairPlan, _Session]:
    """Open every store and build the plan. The caller owns the returned session (close it)."""
    stores = enumerate_stores() if stores is None else stores
    session = _Session(read_only=read_only)
    try:
        plan = RepairPlan(stores, legacy_main=legacy_main, orphan_receipts=orphan_receipts).scan(session)
    except Exception:
        session.close()
        raise
    return plan, session


def default_snapshot(store: Store) -> Optional[str]:
    from hermes_cli.backup import create_quick_snapshot
    try:
        return create_quick_snapshot(label="repair-profiles", hermes_home=store.home)
    except Exception as exc:
        logger.warning("repair-profiles: snapshot of %s failed: %s", store.home, exc)
        return None
