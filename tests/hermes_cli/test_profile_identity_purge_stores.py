"""Delete-side ordering: the identity purge settles EVERY store that can hold the name (#112727).

`delete_profile` tombstones the name after its directory may already have been moved (a rename
writerchan -> logchan moves the data, and the dead name's rows travel with it), so the store that
holds `agent:writerchan:*` keys, heartbeats and LOCAL creation receipts is
`profiles/logchan/state.db` while `get_profile_dir('writerchan')` no longer exists. The old
two-store loop (root + the profile's own home) skipped that store: the rows survived the delete
and every inbound event on the dead name then logged ``Profile 'writerchan' does not exist`` for
the life of the store.

The durable branch (`live_mux=False`) is driven directly: the live multiplexer settles through the
``purge-profile-identity`` control verb, which is a different path with its own tests
(`tests/gateway/test_profile_identity_purge.py`).
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from hermes_state import SessionDB


def _open(path: Path) -> SessionDB:
    """A store at *path*, created with the full schema (parents included)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return SessionDB(path)


def _seed_name_identity(db: SessionDB, name: str) -> None:
    """One routing row and one heartbeat for *name* — what the purge settles by name."""
    key = f"agent:{name}:feishu:dm:chatA"
    db.save_gateway_routing_entry(key, json.dumps({"session_key": key}), scope="/root/sessions")
    db.register_backend_heartbeat(
        backend_id=f"be-{name}", pid=1, started_at=time.time(), profile=name, host="h")


def _seed_receipt(db: SessionDB, home: Path) -> str:
    """A LOCAL creation receipt whose ``profile_id`` names *home*; returns its session id.

    The stored shape is the one ``commit_local_session`` writes: ``profile_id`` at the top level
    of the value, under the ``gateway.local_policy.v1:`` key prefix.
    """
    from hermes_state_local import POLICY_PREFIX

    session_id = uuid.uuid4().hex
    db._write_sql(
        "INSERT INTO state_meta (key, value) VALUES (?, ?)",
        (POLICY_PREFIX + session_id, json.dumps({
            "session_id": session_id,
            "profile_id": str(home),
            "principal_id": "principal-1",
            "request_id": "request-1",
            "route": "agent:moved:feishu:dm:chatA",
            "policy": {},
            "entry": {},
        })),
    )
    return session_id


def _name_counts(path: Path, name: str) -> tuple[int, int]:
    """``(routing rows, heartbeats)`` for *name* in the store at *path*."""
    db = SessionDB(path)
    try:
        routing = db._read_one(
            "SELECT COUNT(*) AS n FROM gateway_routing WHERE session_key = ?",
            (f"agent:{name}:feishu:dm:chatA",))["n"]
        heartbeats = db._read_one(
            "SELECT COUNT(*) AS n FROM gateway_heartbeats WHERE profile = ?", (name,))["n"]
        return routing, heartbeats
    finally:
        db.close()


def _tombstone(root: Path, name: str) -> None:
    """Write the delete's tombstone marker for *name* (its directory may already be gone)."""
    marker = root / "profiles" / ".deleted" / name
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("deleted\n", encoding="utf-8")


def _sandbox_root() -> Path:
    """The test run's HERMES_HOME root — guarded so a broken sandbox can never reach live stores."""
    from hermes_constants import get_default_hermes_root

    root = get_default_hermes_root()
    # This scenario seeds rows a purge would really delete. If the test sandbox ever stops
    # isolating HERMES_HOME, fail before seeding instead of touching the live stores (the live
    # root is ~/.hermes, and every profile home — ~/.hermes/profiles/<name> — under it).
    assert not root.resolve().is_relative_to(Path.home()), \
        "test must never run against a home under the real user home"
    return root


def _moved_directory_scenario() -> tuple[Path, Path]:
    """Shared fixture body: writerchan's directory was renamed to logchan before the tombstone.

    Returns ``(root, moved_store_path)`` with the dead name's rows seeded in the root store AND in
    the moved directory's store, plus a live bystander (``keepme``) in every store. The profile's
    own directory ``profiles/writerchan`` does not exist — that is the bug being pinned.
    """
    root = _sandbox_root()
    moved = root / "profiles" / "logchan"
    bystander = root / "profiles" / "keepme"
    _tombstone(root, "writerchan")

    stores = [_open(root / "state.db"), _open(moved / "state.db"), _open(bystander / "state.db")]
    try:
        for db in stores:
            _seed_name_identity(db, "writerchan")  # the dead name, wherever its directory landed
            _seed_name_identity(db, "keepme")      # a live bystander in the same stores
    finally:
        for db in stores:
            db.close()

    assert not (root / "profiles" / "writerchan").exists()
    return root, moved / "state.db"


def test_purge_settles_the_store_of_a_moved_profile_directory():
    """The ordering fix: the moved directory's store is purged even though its name differs."""
    from hermes_cli.profile_identity import _purge_profile_identity

    root, moved_store = _moved_directory_scenario()
    bystander_store = root / "profiles" / "keepme" / "state.db"

    assert _purge_profile_identity("writerchan", live_mux=False) is True

    # Dead name gone from every store — including the moved directory's, which the old
    # root + get_profile_dir("writerchan") loop never opened (that directory does not exist).
    assert _name_counts(root / "state.db", "writerchan") == (0, 0)
    assert _name_counts(moved_store, "writerchan") == (0, 0)
    assert _name_counts(bystander_store, "writerchan") == (0, 0)
    # The live bystander is untouched everywhere.
    for store in (root / "state.db", moved_store, bystander_store):
        assert _name_counts(store, "keepme") == (1, 1)


def test_purge_is_idempotent_across_the_scanned_stores():
    """A retry of the settlement (the delete's ``purge-identity`` retry command) stays clean."""
    from hermes_cli.profile_identity import _purge_profile_identity

    _root, moved_store = _moved_directory_scenario()

    assert _purge_profile_identity("writerchan", live_mux=False) is True
    assert _purge_profile_identity("writerchan", live_mux=False) is True

    assert _name_counts(moved_store, "writerchan") == (0, 0)
    assert _name_counts(moved_store, "keepme") == (1, 1)


def test_purge_cleans_orphan_receipts_in_the_moved_directory_store():
    """Acceptance end to end: the moved store's LOCAL creation receipts for the tombstoned name
    are retired with it, while a receipt for another profile's home in the same shared store
    stays — receipt matching is by ``receipt.profile_id`` home path, not by directory name."""
    from hermes_cli.profile_identity import _purge_profile_identity
    from hermes_state_local import POLICY_PREFIX

    root = _sandbox_root()
    old_home = root / "profiles" / "writerchan"     # the home the receipts were created under
    keeper_home = root / "profiles" / "keepme"
    _tombstone(root, "writerchan")

    moved_store = root / "profiles" / "logchan" / "state.db"
    db = _open(moved_store)
    try:
        orphan_sid = _seed_receipt(db, old_home)
        keeper_sid = _seed_receipt(db, keeper_home)
    finally:
        db.close()

    assert _purge_profile_identity("writerchan", live_mux=False) is True

    db = SessionDB(moved_store)
    try:
        keys = {row["key"] for row in db._read_all(
            "SELECT key FROM state_meta WHERE key GLOB ?", (POLICY_PREFIX + "*",))}
    finally:
        db.close()
    assert POLICY_PREFIX + orphan_sid not in keys, "the tombstoned name's receipt survived"
    assert POLICY_PREFIX + keeper_sid in keys, "another profile's receipt was taken along"
