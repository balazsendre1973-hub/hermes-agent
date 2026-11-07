"""The long-run outbox death: an oversized commit must not kill the interpreter nor block later writes.

Regression for the 2026-11-06 kanban crashes (runs 989/1008/1088/1089, board default): a
multi-hour kanban session's pruned-transcript commit — ``compression.archive`` with the
unpruned edges as ``held_for_cas`` — serialized to more than the 4 MB retry-journal cap, so
``_save`` set a sticky ``outbox_full`` failure. Every later persist (the turn-final
``_persist_session``, then ``store.flush_token_counts()`` at ``agent/managed_worker.py``)
raised unless someone called ``retry_pending()`` — nobody does for an empty journal — so the
child exited 1 with ``managed_worker_failed``, the owner settled the admission ``unknown``,
and the dispatcher's ``prompt.receipt`` answered ``unknown`` → ``Kanban execution unknown;
retained by owner`` (``hermes_cli/kanban_worker_client.py``). The run's whole work was lost.
"""
import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError


def _owner_of_calls(calls):
    def owner(method, **params):
        calls.append((method, params.get('sequence')))
        payload = params.get('payload') or {}
        rows = payload.get('messages') or [1]
        return {'value': 'stored', 'annotations': [{} for _ in rows]}
    return owner


def test_oversize_candidate_that_queued_nothing_does_not_poison_later_writes(tmp_path):
    calls = []
    store = RuntimeSessionStore(_owner_of_calls(calls), {'session_id': 's'}, tmp_path / 'private',
                               max_bytes=600)
    try:
        with pytest.raises(WorkerPersistenceError, match='outbox_full'):
            store.archive_and_compact('s', [{'role': 'user', 'content': 'y' * 1200}],
                                      held_for_cas=[{'role': 'user', 'content': 'z' * 1200}],
                                      watermark=3)
        assert store.path.stat().st_size <= 600   # the journal never carried the candidate
        assert not store.journal['pending']
        # The turn-final flush (run 1089's fatalizer) must pass and a later write must commit.
        store.queue_token_counts('s', input_tokens=5)
        store.flush_token_counts()
        assert store.failure is None
        assert calls[-1] == ('worker.persist', 2)
    finally:
        store.close()


def test_default_outbox_cap_holds_a_long_transcript_commit(tmp_path):
    """A whole-transcript compaction commit (handoff rows + held CAS edges) fits: the measured
    4.28 MB transcript (run 1089, session local-94ac465e) that outran the old 4 MB cap must not
    be permanently undeliverable."""
    calls = []
    store = RuntimeSessionStore(_owner_of_calls(calls), {'session_id': 's'}, tmp_path / 'private')
    try:
        armada = [{'role': 'user', 'content': 'x' * 40_000} for _ in range(100)]
        store.archive_and_compact('s', armada, held_for_cas=armada, watermark=9)
        assert store.failure is None
        store.flush_token_counts()
        positions = [p for (m, p) in calls if m == 'worker.persist']
        assert max(positions) >= len(armada)
    finally:
        store.close()


def test_oversize_queued_entry_blocks_new_writes_until_reconciled(tmp_path):
    """The retained safety property: an entry that DID queue (fits under the cap) but failed
    delivery keeps the exactly-once fence — later writes refuse (``pending_receipt``) rather
    than silently skip the undelivered row; only a verified retry clears it."""
    calls = []
    store = RuntimeSessionStore(_owner_of_calls(calls), {'session_id': 's'}, tmp_path / 'private',
                               max_bytes=1024 * 1024)
    def busy(method, **params):
        raise TimeoutError('worker_receipt_timeout')
    store.rpc = busy
    try:
        with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
            store.queue_token_counts('s', input_tokens=1)
        assert store.journal['pending'] and store.failure
        with pytest.raises(WorkerPersistenceError, match='pending_receipt'):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'small'}])
        assert store.path.stat().st_size <= store.max_bytes
    finally:
        store.close()
