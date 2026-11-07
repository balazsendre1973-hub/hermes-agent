"""Regression tests for profile-name-keyed state migration on `hermes profile rename`.

When a profile is renamed, the directory move carries the row data, but the profile name is also
baked into session keys (``agent:<name>:*``), ``sessions.profile_name``, ``gateway_heartbeats.profile``,
``delivery_obligations`` and the ``gateway_routing`` index. Left stale, an inbound event on a chat keyed
to the old name resolves to a profile that no longer exists and floods errors.log. See the profile-rename
identity-migration fix.
"""
import json
import sqlite3
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def _create_legacy_v2_topic_tables(db):
    """Create the supported pre-profile-name topic schema without migrating it."""
    db._write_sql("""
        CREATE TABLE telegram_dm_topic_mode (
            chat_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            activated_at REAL NOT NULL, updated_at REAL NOT NULL,
            has_topics_enabled INTEGER, allows_users_to_create_topics INTEGER,
            capability_checked_at REAL, intro_message_id TEXT, pinned_message_id TEXT
        )
    """)
    db._write_sql("""
        CREATE TABLE telegram_dm_topic_bindings (
            chat_id TEXT NOT NULL, thread_id TEXT NOT NULL, user_id TEXT NOT NULL,
            session_key TEXT NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            managed_mode TEXT NOT NULL DEFAULT 'auto',
            linked_at REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY (chat_id, thread_id)
        )
    """)


def _create_legacy_ledger_without_adapter_profile(db):
    """The delivery_obligations shape before ``adapter_profile`` was added (ledger never reopened)."""
    db._write_sql("""
        CREATE TABLE delivery_obligations (
            obligation_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
            platform TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT, content TEXT NOT NULL,
            state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            owner_pid INTEGER, owner_started_at INTEGER, last_error TEXT
        )
    """)
    for oid, key in (("ob_gone", "agent:gone:feishu:dm:chatA"), ("ob_keep", "agent:keepme:feishu:dm:chatB")):
        db._write_sql(
            "INSERT INTO delivery_obligations (obligation_id, session_key, platform, chat_id, "
            "content, state, created_at, updated_at) VALUES (?, ?, 'feishu', 'c', 'hi', 'pending', 1, 1)",
            (oid, key))


class TestRekeyProfileState:
    def test_rekeys_session_key_namespace_and_profile_columns(self, db):
        # A session owned by the old profile, keyed in its namespace.
        db.create_session(
            "sess_old", "feishu", session_key="agent:oldname:feishu:dm:chatA",
            profile_name="oldname", chat_id="chatA", chat_type="dm",
        )
        # An unrelated profile's session must be left untouched.
        db.create_session(
            "sess_other", "feishu", session_key="agent:keepme:feishu:dm:chatB",
            profile_name="keepme", chat_id="chatB", chat_type="dm",
        )

        counts = db.rekey_profile_state("oldname", "newname")

        assert counts["sessions_session_key"] == 1
        assert counts["sessions_profile_name"] == 1
        # Renamed row now lives under the new namespace + owner.
        row = db._read_one(
            "SELECT session_key, profile_name FROM sessions WHERE id = ?", ("sess_old",))
        assert row["session_key"] == "agent:newname:feishu:dm:chatA"
        assert row["profile_name"] == "newname"
        # Bystander untouched.
        other = db._read_one(
            "SELECT session_key, profile_name FROM sessions WHERE id = ?", ("sess_other",))
        assert other["session_key"] == "agent:keepme:feishu:dm:chatB"
        assert other["profile_name"] == "keepme"

    def test_rekeys_routing_index_key_and_embedded_profile(self, db):
        entry = {
            "session_key": "agent:oldname:feishu:dm:chatA",
            "session_id": "sess_old",
            "origin": {"platform": "feishu", "chat_id": "chatA", "profile": "oldname"},
        }
        db.save_gateway_routing_entry(
            "agent:oldname:feishu:dm:chatA", json.dumps(entry), scope="/root/sessions")

        counts = db.rekey_profile_state("oldname", "newname")
        assert counts["gateway_routing"] == 1

        rows = db.load_gateway_routing_entries(scope="/root/sessions")
        assert "agent:oldname:feishu:dm:chatA" not in rows
        assert "agent:newname:feishu:dm:chatA" in rows
        payload = json.loads(rows["agent:newname:feishu:dm:chatA"])
        assert payload["session_key"] == "agent:newname:feishu:dm:chatA"
        assert payload["origin"]["profile"] == "newname"

    def test_rekeys_heartbeats_and_delivery_obligations(self, db, tmp_path, monkeypatch):
        db.register_backend_heartbeat(
            backend_id="be1", pid=123, started_at=time.time(),
            profile="oldname", host="h")
        # delivery_obligations is created lazily by the delivery ledger against the same state.db.
        monkeypatch.setenv("HERMES_HOME", str(db.db_path.parent))
        from gateway import delivery_ledger
        monkeypatch.setattr(delivery_ledger, "_db_path", lambda: db.db_path)
        with delivery_ledger._connect() as conn:
            now = time.time()
            conn.execute(
                "INSERT INTO delivery_obligations (obligation_id, session_key, platform, chat_id, "
                "content, state, created_at, updated_at, adapter_profile) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("ob1", "agent:oldname:feishu:dm:chatA", "feishu", "chatA",
                 "hi", "pending", now, now, "oldname"),
            )
            conn.commit()

        counts = db.rekey_profile_state("oldname", "newname")

        assert counts["gateway_heartbeats_profile"] == 1
        assert counts["delivery_obligations_adapter_profile"] == 1
        assert counts["delivery_obligations_session_key"] == 1
        hb = db._read_one("SELECT profile FROM gateway_heartbeats WHERE backend_id = ?", ("be1",))
        assert hb["profile"] == "newname"
        ob = db._read_one(
            "SELECT session_key, adapter_profile FROM delivery_obligations WHERE obligation_id = ?",
            ("ob1",))
        assert ob["session_key"] == "agent:newname:feishu:dm:chatA"
        assert ob["adapter_profile"] == "newname"

    def test_underscore_in_profile_name_is_not_a_like_wildcard(self, db):
        db.create_session(
            "literal", "telegram", session_key="agent:foo_bar:telegram:dm:a",
            profile_name="foo_bar", chat_id="a", chat_type="dm")
        db.create_session(
            "bystander", "telegram", session_key="agent:fooXbar:telegram:dm:b",
            profile_name="fooXbar", chat_id="b", chat_type="dm")
        db.rekey_profile_state("foo_bar", "renamed")
        assert db._read_one(
            "SELECT session_key FROM sessions WHERE id = ?", ("literal",)
        )["session_key"] == "agent:renamed:telegram:dm:a"
        assert db._read_one(
            "SELECT session_key FROM sessions WHERE id = ?", ("bystander",)
        )["session_key"] == "agent:fooXbar:telegram:dm:b"

    def test_rekeys_origin_json_and_telegram_topic_state(self, db):
        db.create_session(
            "sess_old", "telegram", session_key="agent:oldname:telegram:dm:chatA",
            profile_name="oldname", chat_id="chatA", chat_type="dm")
        db._write_sql(
            "UPDATE sessions SET origin_json = ? WHERE id = ?",
            (json.dumps({"platform": "telegram", "profile": "oldname"}), "sess_old"))
        db.enable_telegram_topic_mode(
            chat_id="chatA", user_id="userA", profile_name="oldname")
        db.bind_telegram_topic(
            chat_id="chatA", thread_id="threadA", user_id="userA",
            session_key="agent:oldname:telegram:dm:chatA", session_id="sess_old",
            profile_name="oldname")
        counts = db.rekey_profile_state("oldname", "newname")
        row = db._read_one("SELECT origin_json FROM sessions WHERE id = ?", ("sess_old",))
        assert json.loads(row["origin_json"])["profile"] == "newname"
        assert counts["sessions_origin_json"] == 1
        binding = db._read_one(
            "SELECT profile_name, session_key FROM telegram_dm_topic_bindings "
            "WHERE chat_id = ? AND thread_id = ?", ("chatA", "threadA"))
        assert binding["profile_name"] == "newname"
        assert binding["session_key"] == "agent:newname:telegram:dm:chatA"

    def test_rekeys_legacy_v2_topic_binding_by_session_key(self, db):
        """Legacy v2 tables have no profile_name, but bindings retain profile namespaces."""
        db.create_session(
            "sess_old", "telegram", session_key="agent:oldname:telegram:dm:chatA",
            profile_name="oldname", chat_id="chatA", chat_type="dm")
        db.create_session(
            "sess_keep", "telegram", session_key="agent:keepme:telegram:dm:chatB",
            profile_name="keepme", chat_id="chatB", chat_type="dm")
        _create_legacy_v2_topic_tables(db)
        db._write_sql(
            "INSERT INTO telegram_dm_topic_mode "
            "(chat_id, user_id, enabled, activated_at, updated_at) VALUES (?, ?, 1, 1, 1)",
            ("chatA", "userA"))
        db._write_sql(
            "INSERT INTO telegram_dm_topic_bindings "
            "(chat_id, thread_id, user_id, session_key, session_id, linked_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 1)",
            ("chatA", "threadA", "userA", "agent:oldname:telegram:dm:chatA", "sess_old"))
        db._write_sql(
            "INSERT INTO telegram_dm_topic_bindings "
            "(chat_id, thread_id, user_id, session_key, session_id, linked_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 1)",
            ("chatB", "threadB", "userB", "agent:keepme:telegram:dm:chatB", "sess_keep"))

        counts = db.rekey_profile_state("oldname", "newname")

        assert counts["telegram_dm_topic_bindings_session_key"] == 1
        assert db._read_one(
            "SELECT session_key FROM telegram_dm_topic_bindings WHERE chat_id = ?", ("chatA",)
        )["session_key"] == "agent:newname:telegram:dm:chatA"
        assert db._read_one(
            "SELECT session_key FROM telegram_dm_topic_bindings WHERE chat_id = ?", ("chatB",)
        )["session_key"] == "agent:keepme:telegram:dm:chatB"

    def test_rekeys_legacy_ledger_without_adapter_profile_by_session_key(self, db):
        """A ledger created before adapter_profile existed is rekeyed on its namespace alone."""
        _create_legacy_ledger_without_adapter_profile(db)

        counts = db.rekey_profile_state("gone", "newname")

        assert "delivery_obligations_adapter_profile" not in counts
        assert counts["delivery_obligations_session_key"] == 1
        keys = {row["obligation_id"]: row["session_key"] for row in db._read_all(
            "SELECT obligation_id, session_key FROM delivery_obligations")}
        assert keys == {"ob_gone": "agent:newname:feishu:dm:chatA",
                        "ob_keep": "agent:keepme:feishu:dm:chatB"}


def _seed_local_session(db, *, home, name, sid, principal, request, task="t_rekey"):
    """One committed-shape LOCAL session: creation receipt, session row, transcript, admission,
    usage row, routing entry and a declared conversation key — what a real store carries for one
    local chat. Returns ``(receipt, the exact bytes its frozen request was stored with)``."""
    from hermes_state_local import API_DECLARED_PREFIX, POLICY_PREFIX

    route = f"agent:{name}:local:dm:{sid}"
    origin = {
        "chat_id": sid, "chat_name": None, "chat_topic": None, "chat_type": "dm",
        "platform": "local", "profile": name, "thread_id": None, "user_id": principal,
        "user_name": None,
    }
    entry = {
        "origin": origin, "session_key": route, "session_id": sid, "chat_type": "dm",
        "platform": "local", "metadata": {}, "display_name": None, "suspended": False,
    }
    request_json = json.dumps({"cwd": "/work", "source": "kanban", "task_id": task}, sort_keys=True)
    kanban_json = json.dumps(
        {"board": "default", "claim_lock": "u:1", "db": "/x/kanban.db", "profile": name,
         "run_id": 7, "task_id": task, "workspaces_root": "/x/workspaces"}, sort_keys=True)
    receipt = {
        "entry": entry,
        "policy": {
            "client_env_json": None, "client_env_remote": False, "client_env_rules_json": None,
            "coding_posture": False,
            "config_json": json.dumps({"model": {"default": "nvidia/x"}}, sort_keys=True),
            "config_secret_ref": None, "credential_ref": None, "cwd": "/work",
            "edit_approval_policy": "ask", "editor_mcp_json": None, "ignore_user_config": False,
            "kanban_json": kanban_json, "model": "nvidia/x", "platform": "cli",
            "request_json": request_json, "safe_mode": False, "skills_prompt": None,
            "source": "kanban", "terminal_json": None, "toolsets": ["terminal"],
        },
        "principal_id": principal,
        "profile_id": home,
        "request_id": request,
        "route": route,
        "session_id": sid,
    }
    now = time.time()

    def write(conn):
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                     (POLICY_PREFIX + sid, json.dumps(receipt, ensure_ascii=False)))
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                     (API_DECLARED_PREFIX + sid, sid))
        conn.execute(
            """INSERT INTO sessions (id, source, started_at, session_key, chat_id, chat_type,
                                     user_id, profile_name, origin_json, message_count, cwd)
               VALUES (?, 'cli', ?, ?, ?, 'dm', ?, ?, ?, 1, '/work')""",
            (sid, now, route, sid, principal, name, json.dumps(origin, ensure_ascii=False)))
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'hi', ?)",
            (sid, now))
        conn.execute(
            """INSERT INTO session_admissions (admission_id, request_id, principal_id,
 target_session_id, lineage_json, payload_json, payload_digest, intent, status)
               VALUES (?, ?, ?, ?, ?, ?, 'digest', 'queue', 'queued')""",
            (f"adm-{name}", request, principal, sid, json.dumps([sid]),
             json.dumps({"text": "hi"})))
        conn.execute("INSERT INTO session_model_usage (session_id, model) VALUES (?, 'nvidia/x')",
                     (sid,))
        conn.execute(
            "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at) "
            "VALUES ('', ?, ?, ?)",
            (route, json.dumps(entry, ensure_ascii=False), now))
    db._execute_write(write)
    return receipt, request_json


def _rows_mentioning(conn, needle):
    """``table.column=rowcount`` for every cell of every table that still carries *needle*."""
    hits = []
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        for row in conn.execute(f"PRAGMA table_info('{table}')"):
            column = row[1]
            try:
                count = conn.execute(
                    f"SELECT count(*) FROM {table} WHERE CAST({column} AS TEXT) LIKE ?",
                    (f"%{needle}%",)).fetchone()[0]
            except sqlite3.Error:
                continue
            if count:
                hits.append(f"{table}.{column}={count}")
    return hits


def test_local_receipt_rekeys_onto_the_new_profile_home(db, tmp_path, monkeypatch):
    """A LOCAL receipt under the old profile_id is rebased onto the new home, its session id
    re-derived from it, and every row that carried the old id follows — so the conversation the
    receipt guards stays restorable instead of pointing at a session id nothing owns."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state_keys import local_identity
    from hermes_state_local import API_DECLARED_PREFIX, POLICY_PREFIX
    from hermes_state_local_lineage import validate_local_lineage

    old_home, new_home = tmp_path / "profiles" / "oldname", tmp_path / "profiles" / "newname"
    principal, request = "uid:1000", 'kanban:["default","t_rekey",7]'
    old_sid = local_identity(str(old_home), principal, request)
    new_sid = local_identity(str(new_home), principal, request)
    _seeded, request_json = _seed_local_session(
        db, home=str(old_home), name="oldname", sid=old_sid, principal=principal, request=request)
    # Another profile's receipt in the same shared store: not identity of this rename.
    other_home = tmp_path / "profiles" / "othername"
    other_sid = local_identity(str(other_home), principal, request)
    other, _ = _seed_local_session(
        db, home=str(other_home), name="othername", sid=other_sid, principal=principal,
        request=request)

    counts = db.rekey_profile_state("oldname", "newname")

    assert counts["state_meta_receipts"] == 1
    assert counts["sessions_id"] == 1 and counts["messages_session_id"] == 1
    route = f"agent:newname:local:dm:{new_sid}"
    with db._read_ctx() as conn:
        keys = {row[0] for row in conn.execute(
            "SELECT key FROM state_meta WHERE key GLOB ?", (POLICY_PREFIX + "*",))}
        assert POLICY_PREFIX + old_sid not in keys
        assert POLICY_PREFIX + new_sid in keys
        rekeyed = json.loads(conn.execute(
            "SELECT value FROM state_meta WHERE key = ?",
            (POLICY_PREFIX + new_sid,)).fetchone()[0])

        # The acceptance: a receipt under the old profile_id rekeyed to the new one, with its
        # session id hash re-derived from exactly that new profile_id.
        assert rekeyed["profile_id"] == str(new_home)
        assert rekeyed["session_id"] == local_identity(
            rekeyed["profile_id"], principal, request) == new_sid

        # Its nested profile references moved with it: route, transcript key, local chat id,
        # the profile name in the origin, and the profile inside the kanban policy blob.
        assert rekeyed["route"] == route
        assert rekeyed["entry"]["session_key"] == route
        assert rekeyed["entry"]["session_id"] == new_sid
        assert rekeyed["entry"]["origin"]["chat_id"] == new_sid
        assert rekeyed["entry"]["origin"]["profile"] == "newname"
        assert json.loads(rekeyed["policy"]["kanban_json"])["profile"] == "newname"
        # ...while the frozen create request keeps its exact bytes: nothing in it is identity.
        assert rekeyed["policy"]["request_json"] == request_json

        # The conversation followed the id instead of being orphaned by it.
        session = conn.execute(
            "SELECT session_key, chat_id, profile_name FROM sessions WHERE id = ?",
            (new_sid,)).fetchone()
        assert session is not None
        assert (session["session_key"], session["chat_id"], session["profile_name"]) == (
            route, new_sid, "newname")
        assert conn.execute(
            "SELECT count(*) FROM sessions WHERE id = ?", (old_sid,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE session_id = ?", (new_sid,)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT target_session_id FROM session_admissions WHERE admission_id = ?",
            ("adm-oldname",)).fetchone()[0] == new_sid
        assert conn.execute(
            "SELECT lineage_json FROM session_admissions WHERE admission_id = ?",
            ("adm-oldname",)).fetchone()[0] == json.dumps([new_sid])
        routing = conn.execute(
            "SELECT session_key, json_extract(entry_json, '$.session_id'),"
            " json_extract(entry_json, '$.origin.profile') FROM gateway_routing WHERE session_key = ?",
            (route,)).fetchone()
        assert routing is not None
        assert (routing["session_key"], routing[1], routing[2]) == (route, new_sid, "newname")
        assert conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (API_DECLARED_PREFIX + new_sid,),
        ).fetchone()[0] == new_sid
        assert API_DECLARED_PREFIX + old_sid not in {row[0] for row in conn.execute(
            "SELECT key FROM state_meta")}

        # The receipt still validates as this store's own, and nothing dangles.
        validate_local_lineage(conn, rekeyed)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _rows_mentioning(conn, old_sid) == []

        # The other profile's receipt and rows are byte-for-byte untouched.
        assert conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (POLICY_PREFIX + other_sid,),
        ).fetchone()[0] == json.dumps(other, ensure_ascii=False)
        bystander = conn.execute(
            "SELECT session_key, chat_id, profile_name FROM sessions WHERE id = ?",
            (other_sid,)).fetchone()
        assert (bystander["session_key"], bystander["chat_id"], bystander["profile_name"]) == (
            f"agent:othername:local:dm:{other_sid}", other_sid, "othername")


def test_legacy_receipt_rebases_its_home_without_moving_its_id(db, tmp_path, monkeypatch):
    """A legacy receipt's id is its pre-unified id, not a hash of the home: only the profile_id
    moves — in the receipt *and* in the binding row ``legacy_lineage_root`` compares it against,
    or the re-based receipt would refuse to restore."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_local_lineage import validate_local_lineage
    from hermes_state_local_migration import LEGACY_PREFIX

    old_home, new_home = tmp_path / "profiles" / "oldname", tmp_path / "profiles" / "newname"
    legacy_sid = "cron_oldjob_20261106_120000"
    principal, request = "uid:1000", "cron:oldjob:execution"
    route = f"agent:oldname:local:dm:{legacy_sid}"
    origin = {"chat_id": legacy_sid, "chat_type": "dm", "platform": "local", "profile": "oldname",
              "user_id": principal}
    receipt = {
        "entry": {"origin": origin, "session_key": route, "session_id": legacy_sid},
        "legacy_session_id": legacy_sid,
        "policy": {"request_json": json.dumps({"source": "cron"}, sort_keys=True)},
        "principal_id": principal,
        "profile_id": str(old_home),
        "request_id": request,
        "route": route,
        "session_id": legacy_sid,
    }

    def seed(conn):
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                     (POLICY_PREFIX + legacy_sid, json.dumps(receipt, ensure_ascii=False)))
        conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                     (LEGACY_PREFIX + legacy_sid, json.dumps(
                         {"session_id": legacy_sid, "profile_id": str(old_home),
                          "principal_id": principal, "legacy_session_id": legacy_sid},
                         ensure_ascii=False)))
        conn.execute(
            """INSERT INTO sessions (id, source, started_at, session_key, chat_id, chat_type,
                                     user_id, profile_name, origin_json, message_count)
               VALUES (?, 'cli', ?, ?, ?, 'dm', ?, 'oldname', ?, 0)""",
            (legacy_sid, time.time(), route, legacy_sid, principal,
             json.dumps(origin, ensure_ascii=False)))
    db._execute_write(seed)

    counts = db.rekey_profile_state("oldname", "newname")

    assert counts["state_meta_receipts"] == 1
    assert counts.get("sessions_id") is None  # the legacy id is never re-derived
    with db._read_ctx() as conn:
        rebased = json.loads(conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (POLICY_PREFIX + legacy_sid,),
        ).fetchone()[0])
        binding = json.loads(conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (LEGACY_PREFIX + legacy_sid,),
        ).fetchone()[0])
        assert rebased["profile_id"] == binding["profile_id"] == str(new_home)
        assert rebased["session_id"] == rebased["legacy_session_id"] == legacy_sid
        assert rebased["route"] == f"agent:newname:local:dm:{legacy_sid}"
        assert rebased["entry"]["origin"]["profile"] == "newname"
        session = conn.execute(
            "SELECT session_key, profile_name FROM sessions WHERE id = ?", (legacy_sid,)).fetchone()
        assert (session["session_key"], session["profile_name"]) == (
            f"agent:newname:local:dm:{legacy_sid}", "newname")
        validate_local_lineage(conn, rebased)
