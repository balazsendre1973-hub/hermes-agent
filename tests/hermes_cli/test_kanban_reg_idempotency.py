"""DB-level idempotency for registry cards: UNIQUE index, NOCASE lookup, key normalisation.

Regression for the 2026-11-06 duplicate cards: ``reg:default|t_7fbf209d|NEW`` and
``reg:default|t_7fbf209d|new`` minted two cards 59 s apart because the create-time lookup
matched exactly and no UNIQUE index existed over the key. The fix is three layers:

1. normalisation at entry — ``reg:<board>|<id>(|<kind>)`` → lowercase, kind-less
   ``reg:<board>|<id>`` (NEW/new/ERROR/error are one condition; the kind lives on the card);
2. a ``COLLATE NOCASE`` lookup (and read-back of the winner of an insert race);
3. a partial ``UNIQUE`` index over open ``reg:`` keys, created by a fail-safe migration
   that first normalises legacy keys and archives (never deletes) the duplicates already
   in the wild.

The metric for the invariant is ``kanban_db.count_duplicate_reg_keys() == 0``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }


def _legacy_board(db_path: Path) -> None:
    """A pre-index board: ``SCHEMA_SQL`` only (no migration), the 2026-11-06 dup shape.

    Three spellings of ONE condition (kind case variants plus a board-case
    variant), a lonely control key, an already-archived pair the partial index
    must ignore, and another producer's key scheme that must not be touched.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(kb.SCHEMA_SQL)
        rows = [
            ("t_dup_a", "reg:default|t_dup|NEW", 100, "todo"),
            ("t_dup_b", "reg:default|t_dup|new", 300, "todo"),  # newest → winner
            ("t_dup_c", "REG:Default|T_DUP|error", 200, "ready"),
            ("t_solo", "reg:default|t_solo|NEW", 400, "todo"),
            ("t_arch_a", "reg:default|t_arch|NEW", 50, "archived"),
            ("t_arch_b", "reg:default|t_arch|new", 60, "archived"),
            ("t_other", "user:alice|session-1", 70, "todo"),
        ]
        for tid, key, created_at, status in rows:
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (tid, f"title {tid}", status, created_at, key),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("reg:default|t_7fbf209d|NEW", "reg:default|t_7fbf209d"),
        ("reg:default|t_7fbf209d|new", "reg:default|t_7fbf209d"),
        ("reg:cron|7c79a0654217|ERROR", "reg:cron|7c79a0654217"),
        ("reg:cron|7c79a0654217|cron_error", "reg:cron|7c79a0654217"),
        ("reg:Default|T_ABC|New", "reg:default|t_abc"),
        ("reg:default|t_x", "reg:default|t_x"),
        # other producers' schemes are not ours to rewrite
        ("user:alice|session-1", "user:alice|session-1"),
        ("hermes:bot|42|retry", "hermes:bot|42|retry"),
        # malformed reg: keys pass through untouched — never silently mangled
        ("reg:onlyboard", "reg:onlyboard"),
        ("reg:board|", "reg:board|"),
        (None, None),
    ],
)
def test_normalize_reg_idempotency_key(raw, expected):
    assert kb.normalize_reg_idempotency_key(raw) == expected


def test_new_and_new_create_one_card(kanban_home):
    """NEW/new/bare spelling of one condition → one card, one canonical key."""
    conn = kbc.connect()
    try:
        first = kb.create_task(
            conn,
            title="dup probe",
            assignee="worker",
            idempotency_key="reg:default|t_probe|NEW",
        )
        second = kb.create_task(
            conn,
            title="dup probe again",
            assignee="worker",
            idempotency_key="reg:default|t_probe|new",
        )
        third = kb.create_task(
            conn,
            title="dup probe bare",
            assignee="worker",
            idempotency_key="reg:default|t_probe",
        )
        assert first == second == third
        rows = conn.execute(
            "SELECT id, idempotency_key FROM tasks WHERE idempotency_key LIKE 'reg:default|t_probe%'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == first
        assert rows[0]["idempotency_key"] == "reg:default|t_probe"  # stored canonical
        assert kb.count_duplicate_reg_keys(conn) == 0
    finally:
        conn.close()


def test_unique_index_rejects_case_variants_of_open_keys(kanban_home):
    """The DB itself refuses a duplicate open reg: key (second line of defence)."""
    conn = kbc.connect()
    try:
        kb.create_task(
            conn,
            title="seed",
            assignee="worker",
            idempotency_key="reg:default|t_index|NEW",
        )
        assert "idx_tasks_reg_idempotency" in _index_names(conn)
        # canonical and case-variant spellings of the stored key conflict
        for shadow_id, key in (
            ("t_shadow_same", "reg:default|t_index"),
            ("t_shadow_case", "REG:DEFAULT|T_INDEX"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tasks (id, title, status, created_at, idempotency_key) "
                    "VALUES (?, 'shadow', 'todo', 1, ?)",
                    (shadow_id, key),
                )
        # archived rows live outside the partial index: no conflict
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, idempotency_key) "
            "VALUES ('t_shadow_arch', 'shadow', 'archived', 1, 'reg:default|t_index')"
        )
        # A kind-ful LEGACY-shape key (what pre-fix writers emitted) is a
        # different string, so the index cannot see that collision — the write
        # path normalises instead, and count_duplicate_reg_keys() is the metric
        # that catches a raw writer sneaking one in.
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, idempotency_key) "
            "VALUES ('t_shadow_kind', 'shadow', 'todo', 1, 'reg:default|t_index|new')"
        )
        assert kb.count_duplicate_reg_keys(conn) == 1
        n = conn.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"]
        assert n == 3  # seed + the archived row + the kind-ful shadow
    finally:
        conn.close()


def test_migration_cleans_duplicates_and_creates_the_index(tmp_path):
    """Init on a legacy dup-laden board: cleanup runs, index lands, re-run is a no-op."""
    db = tmp_path / "legacy_kanban.db"
    _legacy_board(db)

    # Before init: the metric sees the duplicated condition, no unique index yet.
    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    try:
        assert kb.count_duplicate_reg_keys(raw) == 1
        assert "idx_tasks_reg_idempotency" not in _index_names(raw)
    finally:
        raw.close()

    conn = kbc.connect(db)  # the real init path — must not raise on dup data
    try:
        assert "idx_tasks_reg_idempotency" in _index_names(conn)
        assert kb.count_duplicate_reg_keys(conn) == 0

        rows = {
            r["id"]: r
            for r in conn.execute("SELECT id, status, idempotency_key FROM tasks")
        }
        # newest of the group survives, normalised
        assert rows["t_dup_b"]["status"] != "archived"
        assert rows["t_dup_b"]["idempotency_key"] == "reg:default|t_dup"
        # losers are archived (kept, not deleted) with canonical keys
        for loser in ("t_dup_a", "t_dup_c"):
            assert rows[loser]["status"] == "archived"
            assert rows[loser]["idempotency_key"] == "reg:default|t_dup"
        # controls: lonely open key normalised, still open (recompute_ready may
        # promote todo → ready; only the duplicate rule may archive);
        # archived pair and other schemes untouched (outside the rule)
        assert rows["t_solo"]["status"] != "archived"
        assert rows["t_solo"]["idempotency_key"] == "reg:default|t_solo"
        assert rows["t_arch_a"]["idempotency_key"] == "reg:default|t_arch|NEW"
        assert rows["t_arch_b"]["idempotency_key"] == "reg:default|t_arch|new"
        assert rows["t_other"]["idempotency_key"] == "user:alice|session-1"

        # audit: each loser carries an idempotency_dedup event naming the winner
        events = conn.execute(
            "SELECT task_id, payload FROM task_events WHERE kind = 'idempotency_dedup'"
        ).fetchall()
        assert {e["task_id"] for e in events} == {"t_dup_a", "t_dup_c"}
        assert all(json.loads(e["payload"])["winner"] == "t_dup_b" for e in events)

        # idempotent: a second migration pass changes nothing
        before = conn.execute("SELECT count(*) AS n FROM task_events").fetchone()["n"]
        kbc._migrate_add_optional_columns(conn)
        after = conn.execute("SELECT count(*) AS n FROM task_events").fetchone()["n"]
        assert after == before
        assert kb.count_duplicate_reg_keys(conn) == 0
    finally:
        conn.close()


def test_migration_survives_a_failing_cleanup(tmp_path, monkeypatch):
    """Fail-safe: if the cleanup explodes, init still opens the board (no index, logged)."""
    db = tmp_path / "stuck_kanban.db"
    _legacy_board(db)

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated cleanup failure")

    monkeypatch.setattr(kb, "archive_task", boom)
    conn = kbc.connect(db)  # must not raise
    try:
        assert conn.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 7
        assert "idx_tasks_reg_idempotency" not in _index_names(conn)
        # degraded, not broken: the metric still reports the uncleaned group
        assert kb.count_duplicate_reg_keys(conn) == 1
        # and the board still takes writes
        kb.create_task(conn, title="still works", assignee="worker")
    finally:
        conn.close()


def test_insert_race_reads_back_the_winner(kanban_home, monkeypatch):
    """Lookup→insert race: the UNIQUE index fires, create_task returns the winner's id."""
    conn = kbc.connect()
    other = kbc.connect()
    real_write_txn = kb.write_txn
    fired = {"done": False}

    def racing_write_txn(c, **kwargs):
        if not fired["done"]:
            fired["done"] = True
            # a concurrent creator commits between our lookup and our INSERT
            other.execute(
                "INSERT INTO tasks (id, title, status, created_at, idempotency_key) "
                "VALUES ('t_race_winner', 'winner', 'todo', 999, 'REG:DEFAULT|t_race')"
            )
        return real_write_txn(c, **kwargs)

    monkeypatch.setattr(kb, "write_txn", racing_write_txn)
    got = kb.create_task(
        conn,
        title="loser",
        assignee="worker",
        idempotency_key="reg:default|t_race|NEW",
    )
    assert got == "t_race_winner"
    open_rows = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key LIKE 'reg:default|t_race%' AND status != 'archived'"
    ).fetchall()
    assert [r["id"] for r in open_rows] == ["t_race_winner"]
    assert kb.count_duplicate_reg_keys(conn) == 0


def test_twenty_creates_yield_zero_duplicate_keys(kanban_home):
    """20 creates across 4 conditions × 5 spellings → 4 cards, metric 0."""
    conn = kbc.connect()
    try:
        variants = ["NEW", "new", "cron_error", "ERROR", ""]
        ids: dict[str, set[str]] = {}
        for i in range(20):
            condition = f"t_cond{i // 5}"
            kind = variants[i % 5]
            key = (
                f"reg:default|{condition}|{kind}"
                if kind
                else f"reg:default|{condition}"
            )
            ids.setdefault(condition, set()).add(
                kb.create_task(
                    conn, title=f"run {i}", assignee="worker", idempotency_key=key
                )
            )
        assert len(ids) == 4
        assert all(len(created) == 1 for created in ids.values()), ids
        total = conn.execute(
            "SELECT count(*) AS n FROM tasks WHERE idempotency_key LIKE 'reg:%'"
        ).fetchone()["n"]
        assert total == 4
        assert kb.count_duplicate_reg_keys(conn) == 0
    finally:
        conn.close()


def test_non_reg_key_schemes_are_not_constrained(kanban_home):
    """user:/hermes: schemes keep their own semantics — no normalisation, no index."""
    conn = kbc.connect()
    try:
        a = kb.create_task(
            conn, title="a", assignee="worker", idempotency_key="user:alice|session-1"
        )
        b = kb.create_task(
            conn,
            title="b",
            assignee="worker",
            idempotency_key="user:alice|session-1|retry",
        )
        c = kb.create_task(
            conn, title="c", assignee="worker", idempotency_key="user:alice|SESSION-1"
        )
        assert len({a, b, c}) == 3
        keys = {
            r["idempotency_key"]
            for r in conn.execute(
                "SELECT idempotency_key FROM tasks WHERE idempotency_key LIKE 'user:%'"
            )
        }
        assert keys == {
            "user:alice|session-1",
            "user:alice|session-1|retry",
            "user:alice|SESSION-1",
        }
        assert kb.count_duplicate_reg_keys(conn) == 0
    finally:
        conn.close()
