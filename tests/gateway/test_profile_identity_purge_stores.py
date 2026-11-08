"""The live-gateway ``purge-profile-identity`` verb settles EVERY store that can hold the name.

``hermes profile delete`` settles identity through this verb while a multiplexer is live: the
gateway owns the routing index, so the durable path in ``hermes_cli/profile_identity`` hands the
name over the control socket instead of touching the stores itself (#111926). The verb used to
purge only the gateway's OWN root ``state.db`` plus the in-memory ``SessionStore._entries`` — a
directory moved before the tombstone (rename writerchan -> logchan) keeps the dead name's routing
and heartbeat rows in ``profiles/logchan/state.db``, under a directory name that no longer matches
the purged profile, and they survived the delete under a live gateway exactly as the durable
two-store loop's did (fixed there in d1d4793bc2).

The durable branch's mirror tests live in ``tests/hermes_cli/test_profile_identity_purge_stores.py``;
this file pins the verb handler itself. The home guard mirrors that file: a sandbox that silently
resolved to the operator's real home would seed rows the purge then deletes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace


def _sandbox_home() -> Path:
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    assert not home.resolve().is_relative_to(Path.home()), (
        f"HERMES_HOME resolved to the real user home: {home.resolve()}. Refusing to seed rows a "
        "purge would delete for real.")
    return home


def _make_store(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    store = SessionStore(
        sessions_dir,
        GatewayConfig(sessions_dir=sessions_dir, write_sessions_json=False,
                      multiplex_profiles=True),
    )
    store._ensure_loaded()
    return store


def _runner_stub(store):
    return SimpleNamespace(session_store=store)


def _seed_store(db_path: Path, *names: str) -> None:
    """Routing + heartbeat rows for every *name* in one store, full schema, closed again."""
    from hermes_state import SessionDB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path)
    try:
        for name in names:
            key = f"agent:{name}:feishu:dm:chatA"
            db.save_gateway_routing_entry(
                key, json.dumps({"session_key": key}), scope="/root/sessions")
            db.register_backend_heartbeat(
                backend_id=f"be-{name}", pid=1, started_at=time.time(),
                profile=name, host="h")
    finally:
        db.close()


def _name_counts(db_path: Path, name: str) -> tuple[int, int]:
    """(routing rows, heartbeat rows) for *name* in one store, read with a bare handle."""
    from hermes_state import SessionDB
    db = SessionDB(db_path)
    try:
        routing = db._read_one(
            "SELECT COUNT(*) AS n FROM gateway_routing WHERE session_key = ?",
            (f"agent:{name}:feishu:dm:chatA",))["n"]
        heartbeats = db._read_one(
            "SELECT COUNT(*) AS n FROM gateway_heartbeats WHERE profile = ?",
            (name,))["n"]
        return routing, heartbeats
    finally:
        db.close()


def test_purge_verb_settles_a_moved_profile_directory_store(tmp_path):
    """Dead-name rows in a sibling store survive the verb unless it scans ``profiles/*/state.db``.

    The store's directory name (``logchan``) differs from the purged name (``writerchan``) — the
    shape a rename-before-tombstone leaves behind — and it also holds a bystander profile's rows,
    which the purge must leave exactly as they were. Every handle the scan acquires must come back:
    a leaked shared handle keeps the store open in the gateway for the rest of its life.
    """
    from gateway.run_profile_reconcile import purge_profile_identity_verb
    from hermes_state_registry import stats

    home = _sandbox_home()
    moved = home / "profiles" / "logchan" / "state.db"       # dead name's rows live here
    bystander = home / "profiles" / "keepme" / "state.db"    # a live profile's rows
    _seed_store(moved, "writerchan", "keepme")
    _seed_store(bystander, "keepme")
    assert moved.parent.name != "writerchan"                 # pin the moved-directory scenario

    store = _make_store(tmp_path)
    assert store._routing_db is not None                     # warm: the verb reuses this handle
    before = stats()
    answer = purge_profile_identity_verb(_runner_stub(store))({"name": "writerchan"})

    assert answer["ok"] is True, answer
    # The scan keyed the moved store by its DIRECTORY name, not the purged one.
    assert "profile:logchan" in answer["db"], answer["db"]
    assert _name_counts(moved, "writerchan") == (0, 0), "dead-name rows survived the verb"
    assert _name_counts(moved, "keepme") == (1, 1), "bystander rows in the moved store were purged"
    assert _name_counts(bystander, "keepme") == (1, 1), "an unrelated profile store was purged"
    assert stats() == before, f"handle leaked by the store scan: {before} -> {stats()}"


def test_purge_verb_reports_failure_and_releases_handles(tmp_path, monkeypatch):
    """A store the scan cannot purge settles as ``ok: False`` — never as a raise into the socket.

    The failure semantics of the root purge carry over to the scan: warn and answer, keep purging
    the stores that do work, and release every handle already acquired so a retry starts clean.
    """
    import hermes_state
    from gateway.run_profile_reconcile import purge_profile_identity_verb
    from hermes_state_registry import stats

    home = _sandbox_home()
    broken = home / "profiles" / "logchan" / "state.db"
    _seed_store(broken, "writerchan")
    store = _make_store(tmp_path)
    with store._lock:
        from gateway.session import SessionEntry, SessionSource, Platform
        from gateway.session_lifecycle import _now
        now = _now()
        key = "agent:writerchan:feishu:dm:chatA"
        store._entries[key] = SessionEntry(
            session_key=key, session_id="sid-chatA", platform=Platform.FEISHU,
            chat_type="dm", created_at=now, updated_at=now,
            origin=SessionSource(platform=Platform.FEISHU, chat_id="chatA",
                                 profile="writerchan"))
    assert store._routing_db is not None
    before = stats()

    original = hermes_state.SessionDB.purge_profile_state

    def _boom(self, profile):
        if Path(self.db_path).resolve() == broken.resolve():
            raise RuntimeError("injected purge failure")
        return original(self, profile)

    monkeypatch.setattr(hermes_state.SessionDB, "purge_profile_state", _boom)
    answer = purge_profile_identity_verb(_runner_stub(store))({"name": "writerchan"})

    assert answer["ok"] is False, answer
    assert "injected purge failure" in answer["error"], answer
    assert answer["dropped"] == 1, "the in-memory index was not settled either"
    assert "agent:writerchan:feishu:dm:chatA" not in store._entries
    assert _name_counts(broken, "writerchan") == (1, 1), "a failed purge must not claim success"
    assert stats() == before, f"handle leaked on the failure path: {before} -> {stats()}"
