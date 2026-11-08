"""The worker-receipt payload cap must agree with the worker's own outbox journal cap.

Regression for the 2026-11-07 22:44 save-crash (session local-5bbfe397, t_7c9736bb
run 1177 / worker pid 106092): a long-lived kanban session's proactive tool-result
prune committed ``compression.archive`` carrying the whole transcript as
``held_for_cas``. The worker's journal accepted it (its cap had been raised
4 MiB -> 32 MiB), but the owner refused the payload with ``invalid_params``
(``apply_worker_receipt``'s 4 MiB cap). The worker store then kept that failure
stickily, so every later save of the session — ``append_message``, the turn-final
``_persist_session``, ``flush_token_counts`` — failed and the worker exited 1 with
``managed_worker_failed``: hours of finished work lost to a capacity check.
"""
import inspect

import pytest

from agent.runtime_session_store import RuntimeSessionStore
from hermes_state import SessionDB
from hermes_state_runtime import (
    WORKER_PERSIST_MAX_PAYLOAD_BYTES,
    RuntimeStoreError,
    begin_runtime_epoch,
    mutate_worker_execution,
    register_worker_execution,
)

# The measured production size that outran the old cap: a 4.28 MB transcript archive
# (run 1089, session local-94ac465e) and a 5.78 MB whole-row encoding of the crashing
# session's 591 messages. This payload clears both.
MEASURED_ARCHIVE_HEADROOM = [{'role': 'assistant', 'content': 'x' * 70_000} for _ in range(70)]


def _owner(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('root', 'cli', system_prompt='PREFIX')
    epoch = begin_runtime_epoch(db, instance_id='owner')
    scope = dict(epoch=epoch, execution_id='worker', session_id='root', generation=0)
    register_worker_execution(db, **scope, kind='compute', adoption_secret='secret')
    return db, scope


def test_whole_transcript_archive_payload_commits(tmp_path):
    """The crashing payload: just over the old 4 MiB cap, far under the new one."""
    db, scope = _owner(tmp_path)
    try:
        result = mutate_worker_execution(db, **scope, sequence=1,
                                         operation='transcript.append',
                                         payload={'messages': MEASURED_ARCHIVE_HEADROOM})
        assert result['count'] == len(MEASURED_ARCHIVE_HEADROOM)
        with db._read_ctx() as conn:
            assert conn.execute('SELECT COUNT(*) FROM messages WHERE session_id=?',
                                ('root',)).fetchone()[0] == len(MEASURED_ARCHIVE_HEADROOM)
    finally:
        db.close()


def test_payload_beyond_the_cap_is_still_refused(tmp_path):
    """Raising the cap must not remove it: an absurd payload is still invalid_params."""
    db, scope = _owner(tmp_path)
    try:
        huge = [{'role': 'assistant', 'content': 'x' * (WORKER_PERSIST_MAX_PAYLOAD_BYTES // 3 + 1)}] * 3
        with pytest.raises(RuntimeStoreError) as err:
            mutate_worker_execution(db, **scope, sequence=1,
                                    operation='transcript.append', payload={'messages': huge})
        assert str(err.value) == 'invalid_params'
    finally:
        db.close()


def test_append_row_validation_outlives_the_cap(tmp_path):
    """The >1000-message fence is the append handler's own; the cap raise must not hide it."""
    db, scope = _owner(tmp_path)
    try:
        with pytest.raises(RuntimeStoreError) as err:
            mutate_worker_execution(db, **scope, sequence=1, operation='transcript.append',
                                    payload={'messages': [{'role': 'user', 'content': 'tiny'}] * 1001})
        assert str(err.value) == 'invalid_params'
    finally:
        db.close()


def test_the_owner_cap_equals_the_outbox_journal_cap():
    """Anti-skew guard: the two caps moving apart is exactly what killed the worker.

    ``RuntimeSessionStore._apply`` writes a journal entry whose payload is then sent
    verbatim as the receipt payload, so a journal cap ABOVE the owner cap produces a
    locally-accepted, remotely-refused commit — the 2026-11-07 crash.
    """
    default = inspect.signature(RuntimeSessionStore.__init__).parameters['max_bytes'].default
    assert default == WORKER_PERSIST_MAX_PAYLOAD_BYTES
