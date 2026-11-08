"""The long-run outbox contract: an oversized commit must stay fenced, and a
whole-transcript commit must deliver.

Regression for two measured crashes of unattended kanban workers:

* 2026-11-05/06 (runs 989/1008/1088/1089, session local-94ac465e): a multi-hour
  session's pruned-transcript commit — ``compression.archive`` with the unpruned
  edges as ``held_for_cas`` — serialized past the 4 MB retry-journal cap, so
  ``_save`` set a sticky ``outbox_full``. Every later persist (the turn-final
  ``_persist_session``, then ``store.flush_token_counts()`` in
  ``agent/managed_worker.py``) raised, the child exited 1 with
  ``managed_worker_failed``, and the run's whole work was lost.
* 2026-11-07 22:44 (session local-5bbfe397, run 1177 / pid 106092): the journal
  cap had been raised to 32 MiB, so the SAME commit then reached the owner and was
  refused by the receipt cap (``invalid_params``) — the sticky failure killed the
  session's saves exactly the same way. ``tests/hermes_state/
  test_runtime_worker_payload_capacity.py`` guards the owner side; the end-to-end
  case below guards that the two caps cannot drift apart in practice.

A candidate that already QUEUED but failed delivery keeps the exactly-once fence:
later writes refuse (``pending_receipt``) until a verified retry clears the entry.
That sticky-failure behaviour is deliberate and out of scope here — only the CAP
SKEW is being fixed.
"""
import contextlib

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution


def _echo_owner(calls):
    def owner(method, **params):
        calls.append((method, params.get('sequence')))
        rows = (params.get('payload') or {}).get('messages') or [1]
        return {'value': 'stored', 'count': len(rows), 'annotations': [{} for _ in rows]}
    return owner


def _real_owner(tmp_path):
    """A live owner DB: the exact ``worker.persist`` path the gateway serves."""
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('root', 'cli', system_prompt='PREFIX')
    epoch = begin_runtime_epoch(db, instance_id='owner')
    scope = dict(epoch=epoch, execution_id='worker', session_id='root', generation=0)
    register_worker_execution(db, **scope, kind='compute', adoption_secret='secret')
    return db, scope, (lambda method, **params: mutate_worker_execution(db, **params))


def test_default_outbox_cap_holds_a_long_transcript_commit(tmp_path):
    """A whole-transcript compaction commit (handoff rows + held CAS edges) fits: the
    measured 4.28 MB transcript (run 1089, session local-94ac465e) that outran the old
    4 MB cap must not be permanently undeliverable."""
    calls = []
    store = RuntimeSessionStore(_echo_owner(calls), {'session_id': 's'}, tmp_path / 'private')
    try:
        armada = [{'role': 'user', 'content': 'x' * 40_000} for _ in range(100)]
        store.archive_and_compact('s', armada, held_for_cas=armada, watermark=9)
        assert store.failure is None
        assert [p for (m, p) in calls if m == 'worker.persist'] == [1]
        assert store.journal['pending'] == []
        store.flush_token_counts()
    finally:
        store.close()


def test_a_whole_transcript_archive_round_trips_through_the_real_owner(tmp_path):
    """The 2026-11-07 22:44 crash, end to end: the commit that fit the journal must also
    be ACCEPTED by the owner — one receipt, no ``invalid_params``, rows durable."""
    db, scope, owner = _real_owner(tmp_path)
    store = RuntimeSessionStore(owner, scope, tmp_path / 'outbox')
    try:
        armada = [{'role': 'user', 'content': 'x' * 40_000} for _ in range(100)]
        seed = [{'role': 'user', 'content': 'seed'}]
        store.append_messages_batch('root', seed)
        store.archive_and_compact('root', armada, watermark=1)
        assert store.failure is None
        assert store.journal['pending'] == []
        store.flush_token_counts()
        with db._read_ctx() as conn:
            assert conn.execute('SELECT COUNT(*) FROM messages WHERE session_id=?',
                                ('root',)).fetchone()[0] >= len(armada)
    finally:
        store.failure = None
        store.journal['pending'] = []
        store.close()
        db.close()


def test_oversize_candidate_refuses_and_keeps_the_journal_untouched(tmp_path):
    """Under an explicit tiny cap an oversized candidate raises ``outbox_full`` and never
    reaches the journal: the store stays fenced (sticky failure is the exactly-once
    contract), the entry is not queued, and the file keeps the pre-candidate bytes."""
    calls = []
    store = RuntimeSessionStore(_echo_owner(calls), {'session_id': 's'}, tmp_path / 'private',
                               max_bytes=600)
    try:
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.archive_and_compact('s', [{'role': 'user', 'content': 'y' * 1200}],
                                      held_for_cas=[{'role': 'user', 'content': 'z' * 1200}],
                                      watermark=3)
        assert store.path.stat().st_size <= 600
        assert not store.journal['pending']
        assert store.failure == 'outbox_full'
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.queue_token_counts('s', input_tokens=5)
    finally:
        store.failure = None
        store.journal['pending'] = []
        store.close()


def test_oversize_queued_entry_blocks_new_writes_until_reconciled(tmp_path):
    """The retained safety property: an entry that DID queue (fits under the cap) but
    failed delivery keeps the exactly-once fence — later writes refuse
    (``pending_receipt``) rather than silently skip the undelivered row; only a
    verified retry clears it."""
    calls = []
    store = RuntimeSessionStore(_echo_owner(calls), {'session_id': 's'}, tmp_path / 'private',
                               max_bytes=1024 * 1024)

    def busy(method, **params):
        raise TimeoutError('worker_receipt_timeout')

    store.rpc = busy
    try:
        with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
            store.queue_token_counts('s', input_tokens=1)
        assert store.journal['pending'] and store.failure == 'worker_receipt_timeout'
        # The in-memory fence: the sticky failure refuses the next write, so the
        # undelivered row is never skipped over.
        with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'small'}])
        assert store.path.stat().st_size <= store.max_bytes
    finally:
        with contextlib.suppress(WorkerPersistenceError):
            store.close()
    # The durable fence survives a restart: a fresh store over the same journal
    # REPLAYS the retained entry on construction (``retry_pending`` in __init__) —
    # delivered exactly once, never dropped — and the store is usable again.
    delivered = []
    replay = RuntimeSessionStore(_echo_owner(delivered), {'session_id': 's'}, tmp_path / 'private')
    assert replay.journal['pending'] == [] and replay.failure is None
    assert delivered == [('worker.persist', 1)]
    replay.append_messages_batch('s', [{'role': 'user', 'content': 'small'}])
    assert delivered[-1] == ('worker.persist', 2)
    replay.close()
