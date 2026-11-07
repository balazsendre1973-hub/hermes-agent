"""``repair-profiles`` and the LOCAL creation receipts in ``state_meta`` (``orphan_receipt``).

A creation receipt records the profile that created its local session (``receipt.profile_id``),
so a renamed or moved profile leaves receipts this store's boot refuses with ``profile_mismatch``
— while the session row under it only reports ``unclaimed_namespace`` with no action. The command
now scans ``state_meta`` for those receipts and settles each one: ``retire`` (default) drops the
receipt and keeps the transcript as history, ``rekey`` moves the receipt onto this store's profile
keeping its session id, ``report`` leaves them alone. Part of #88715.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX
from hermes_state_local_migration import LEGACY_PREFIX

GONE_HOME = "/nowhere/profiles/gone"          # the profile that created the orphan receipt
ORPHAN_SID = "local-" + hashlib.sha256(b"orphan").hexdigest()
OWNED_SID = "local-" + hashlib.sha256(b"owned").hexdigest()
PRINCIPAL = "uid:1"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    from hermes_cli.profiles import create_profile
    create_profile("acme", no_alias=True, no_skills=True)
    return {"default": root, "acme": root / "profiles" / "acme"}


def _origin(profile: str, sid: str) -> dict:
    return {"chat_id": sid, "chat_name": None, "chat_topic": None, "chat_type": "dm",
            "platform": "local", "profile": profile, "thread_id": None,
            "user_id": PRINCIPAL, "user_name": None}


def _seed(db_path: Path, *, sid: str, profile_id: str, profile: str) -> str:
    """A local session row plus its creation receipt; returns the route both are keyed on."""
    db = SessionDB(db_path)
    route = f"agent:{profile}:local:dm:{sid}"
    db.create_session(sid, "local", session_key=route, chat_id=sid, chat_type="dm",
                      profile_name=profile, user_id=PRINCIPAL, system_prompt="sys")
    for i in range(2):
        db.append_message(sid, "user" if i % 2 == 0 else "assistant", f"{sid} m{i}")
    receipt = {
        "entry": {"session_id": sid, "session_key": route,
                  "created_at": "2026-11-06T12:00:00.000000",
                  "updated_at": "2026-11-06T12:00:00.000000", "chat_type": "dm",
                  "platform": "local", "origin": _origin(profile, sid)},
        "policy": {"client_env_json": None, "client_version": None,
                   "source": {"platform": "local", "profile": profile},
                   "request_json": {"input": "seed"}},
        "principal_id": PRINCIPAL, "request_id": f"req-{sid}", "session_id": sid,
        "profile_id": profile_id, "route": route,
    }
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO state_meta(key, value) VALUES (?, ?)",
        (POLICY_PREFIX + sid, json.dumps(receipt, sort_keys=True))))
    db.save_gateway_routing_entry(route, json.dumps({"session_key": route, "session_id": sid}),
                                  scope=str(db_path.parent))
    db.close()
    return route


@pytest.fixture
def seeded(homes):
    """acme's store holds one receipt of its own and one whose profile_id names a dead home."""
    _seed(homes["acme"] / "state.db", sid=OWNED_SID, profile_id=str(homes["acme"]), profile="acme")
    _seed(homes["acme"] / "state.db", sid=ORPHAN_SID, profile_id=GONE_HOME, profile="ghost")
    return homes


def _run(**kw) -> int:
    from hermes_cli.sessions_cmd import cmd_sessions
    args = Namespace(sessions_action="repair-profiles", apply=False, json=False, yes=True,
                     legacy_main="report")
    for key, value in kw.items():
        setattr(args, key, value)
    return cmd_sessions(args) or 0


def _report(**kw) -> dict:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert _run(json=True, **kw) == 0
    return json.loads(buffer.getvalue())


def _receipt(db_path: Path, sid: str) -> dict | None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?",
                           (POLICY_PREFIX + sid,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def _dump(path: Path) -> dict:
    """Every user table as sorted row tuples — the whole-file mutation oracle for the dry run."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE '%_fts%' ORDER BY name")]
        return {t: sorted(map(repr, conn.execute(f"SELECT * FROM {t}").fetchall())) for t in tables}
    finally:
        conn.close()


def _orphan_findings(report: dict) -> list[dict]:
    return [f for f in report["findings"] if f["kind"] == "orphan_receipt"]


def test_dry_run_names_the_orphan_with_a_concrete_action_and_the_owned_one_not_at_all(seeded):
    report = _report()
    orphans = _orphan_findings(report)

    assert [f["subject"] for f in orphans] == [ORPHAN_SID]
    finding = orphans[0]
    assert finding["store"] == "acme"
    assert finding["action"], "the receipt must offer a settlement, not action: null"
    assert finding["reason"] is None
    assert GONE_HOME in finding["detail"] and str(seeded["acme"]) in finding["detail"]
    # its session row still reports the dead namespace it also sits in
    unclaimed = [f for f in report["findings"]
                 if f["kind"] == "unclaimed_namespace" and f["subject"] == ORPHAN_SID]
    assert unclaimed and unclaimed[0]["action"] is None
    # the healthy receipt's own row is never a finding
    assert not [f for f in report["findings"] if f["subject"] == OWNED_SID]


def test_dry_run_changes_nothing(seeded):
    path = seeded["acme"] / "state.db"
    before = _dump(path)
    _report()
    assert _dump(path) == before


def test_report_policy_keeps_the_action_null(seeded):
    finding = _orphan_findings(_report(orphan_receipts="report"))[0]
    assert finding["action"] is None and finding["reason"]
    assert "retire" in finding["reason"]


def test_apply_retires_the_orphan_and_keeps_the_transcript(seeded):
    path = seeded["acme"] / "state.db"
    before = _dump(path)
    assert _run(apply=True, yes=True) == 0

    assert _receipt(path, ORPHAN_SID) is None
    assert _receipt(path, OWNED_SID) is not None        # the store's own receipt is untouched
    after = _dump(path)
    assert after["sessions"] == before["sessions"]      # rows and transcript stay as history
    assert after["messages"] == before["messages"]
    assert not _orphan_findings(_report())


def test_apply_rekeys_the_orphan_onto_the_store_profile(seeded):
    path = seeded["acme"] / "state.db"
    home = str(seeded["acme"])
    before = _dump(path)
    assert _run(apply=True, yes=True, orphan_receipts="rekey") == 0

    receipt = _receipt(path, ORPHAN_SID)
    assert receipt is not None
    assert receipt["profile_id"] == home
    assert receipt["session_id"] == ORPHAN_SID          # the id the rows and messages key on
    assert receipt["route"].startswith("agent:acme:")
    assert receipt["entry"]["origin"]["profile"] == "acme"
    # the store records why that id is no longer derived from the receipt's own profile
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        fence = conn.execute("SELECT value FROM state_meta WHERE key = ?",
                             (LEGACY_PREFIX + ORPHAN_SID,)).fetchone()
        row = conn.execute("SELECT session_key, profile_name FROM sessions WHERE id = ?",
                           (ORPHAN_SID,)).fetchone()
    assert fence is not None
    assert json.loads(fence[0]) == {"session_id": ORPHAN_SID, "profile_id": home,
                                    "principal_id": PRINCIPAL, "legacy_session_id": ORPHAN_SID}
    assert row == (receipt["route"], "acme")
    after = _dump(path)
    assert after["sessions"] == before["sessions"] and after["messages"] == before["messages"]
    assert not _orphan_findings(_report(orphan_receipts="rekey"))


def test_a_rekeyed_receipt_restores_through_the_production_checks(seeded):
    path = seeded["acme"] / "state.db"
    assert _run(apply=True, yes=True, orphan_receipts="rekey") == 0

    from gateway.session_local_recovery import owned_receipt
    from hermes_state_local_lineage import validate_local_lineage

    receipt = _receipt(path, ORPHAN_SID)
    assert receipt is not None
    db = SessionDB(path)
    with db._read_ctx() as conn:
        assert validate_local_lineage(conn, receipt) == [ORPHAN_SID]
    authority = SimpleNamespace(
        profile_id=str(seeded["acme"]), db=db,
        runner=SimpleNamespace(
            session_authorities=SimpleNamespace(profile_name=lambda authority: "acme"),
            session_store=SimpleNamespace(
                _generate_session_key=lambda source: f"agent:acme:local:dm:{source.chat_id}")))
    _, source, route, entry = owned_receipt(authority, ORPHAN_SID)
    assert route == receipt["route"] == entry.session_key
    assert source.chat_id == ORPHAN_SID and source.user_id == PRINCIPAL
    db.close()


def test_the_routing_index_is_not_resurrected_by_a_stale_move(seeded):
    """A rekey renames the row first, so the same apply's routing move must find nothing to copy."""
    root, acme = seeded["default"], seeded["acme"]
    assert _run(apply=True, yes=True, orphan_receipts="rekey") == 0
    with sqlite3.connect(f"file:{root / 'state.db'}?mode=ro", uri=True) as conn:
        dead = conn.execute(
            "SELECT count(*) FROM gateway_routing WHERE session_key LIKE 'agent:ghost:%'").fetchone()
    with sqlite3.connect(f"file:{acme / 'state.db'}?mode=ro", uri=True) as conn:
        moved = conn.execute("SELECT count(*) FROM gateway_routing WHERE session_key = ?",
                             (f"agent:acme:local:dm:{ORPHAN_SID}",)).fetchone()
    assert dead == (0,) and moved == (1,)
