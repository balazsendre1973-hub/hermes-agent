"""An invalidated execution must recover or report its real verdict, never a bare exit-1 refusal.

The owner-fence wedge (t_8b2ecc3c): a managed run dies with
``Kanban owner refused: Kanban execution unknown; retained by owner`` -> exit 1 -> the dispatcher
counts a card failure and burns the retry budget. Two shapes produce ``status='unknown'``:

  * a gateway restart invalidates the execution record (``recover_session_inputs`` sets BOTH the
    admission and its worker-executions row ``unknown``) — the card is not at fault, so the exact
    replay must resurrect it to ``queued`` and the attempt runs again;
  * a lost managed worker (``lose_admission_worker`` sets the worker row ``terminal``) — the
    execution is provably over, so the fence must stand and the submitter must report the verdict
    the child already booked (75 quota / 78 re-login / 1) instead of masking it.
"""
from contextlib import closing
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
import hermes_state_runtime as runtime
# Module level (collection time), like the other worker-client tests: importing
# ``kanban_worker_client`` pulls in ``hermes_bootstrap``, which reads the home manifest — the
# real-home I/O guard is an autouse fixture armed only at test runtime, so an in-function import
# would trip it.
from hermes_cli import kanban_worker_client
_exit_code = kanban_worker_client._exit_code
_await_receipt = kanban_worker_client._await_receipt
_Claim = kanban_worker_client._Claim


def hello(process):
    from hermes_cli.process_identity import worker_birth
    import psutil
    return {'type': 'hello', 'pid': process.pid, 'birth': worker_birth(psutil.Process(process.pid)),
            'ancestors': [__import__('os').getpid()]}


def _spawn():
    return subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- Fix 1: admit_session_input resurrects only a restart-fenced unknown ----------------------

def test_restart_fenced_unknown_is_resurrected_by_exact_replay(tmp_path):
    """Restart shape: ``recover_session_inputs`` fenced admission AND worker row to ``unknown``.
    The exact replay must resurrect to ``queued`` so the submitter's retry runs."""
    from gateway.session_worker_reservation import reserve_admission_worker
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session('owned', 'cli')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        authority = SimpleNamespace(db=db, epoch=epoch, profile_id=str(tmp_path),
                                    _require_admission_open=lambda: None)
        admit_session_input(db, epoch=epoch, principal_id='human', session_id='owned',
                            request_id='kanban-attempt', payload={'text': 'x'})
        row = claim_session_input(db, epoch=epoch, session_id='owned')
        child = _spawn()
        try:
            reserve_admission_worker(authority, admission_id=row['admission_id'], process=child,
                                     principal_id='human', hello=hello(child))
            restarted = begin_runtime_epoch(db, instance_id='restarted')
            runtime.recover_session_inputs(db, epoch=restarted)
            # The restart fence: BOTH rows are 'unknown', the worker row is NOT terminal.
            assert db._read_one('SELECT status FROM session_admissions')[0] == 'unknown'
            assert db._read_one('SELECT status FROM worker_executions')[0] == 'unknown'
            replay = admit_session_input(db, epoch=restarted, principal_id='human',
                                         session_id='owned', request_id='kanban-attempt',
                                         payload={'text': 'x'})
            assert replay['status'] == 'queued', replay
        finally:
            child.stdin.close()
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_lost_worker_unknown_is_not_resurrected_by_exact_replay(tmp_path):
    """Loss shape: ``lose_admission_worker`` left the worker row ``terminal`` — that proves the
    execution is over, so the exact replay must return ``unknown`` unchanged (the submitter then
    reports the booked verdict; resurrecting would undo the loss fence and wedge the card)."""
    from gateway.session_worker_reservation import reserve_admission_worker, lose_admission_worker
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session('owned', 'cli')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        authority = SimpleNamespace(db=db, epoch=epoch, profile_id=str(tmp_path),
                                    _require_admission_open=lambda: None)
        admit_session_input(db, epoch=epoch, principal_id='human', session_id='owned',
                            request_id='input', payload={})
        row = claim_session_input(db, epoch=epoch, session_id='owned')
        child = _spawn()
        try:
            scope = reserve_admission_worker(authority, admission_id=row['admission_id'],
                                             process=child, principal_id='human', hello=hello(child))
            child.kill()
            child.wait(timeout=5)
            lose_admission_worker(authority, row, scope)
            assert db._read_one('SELECT status FROM session_admissions')[0] == 'unknown'
            assert db._read_one('SELECT status FROM worker_executions')[0] == 'terminal'
            replay = admit_session_input(db, epoch=epoch, principal_id='human',
                                         session_id='owned', request_id='input', payload={})
            assert replay['status'] == 'unknown', replay
        finally:
            child.stdin.close()
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


# --- Fix 2: _exit_code reports the booked verdict instead of a bare refusal -------------------

def _board_with_verdict(tmp_path, monkeypatch, verdict):
    """A claimed attempt on a private board; ``verdict`` is the booked worker_result payload (or
    None for no verdict at all). Returns the ``(context, board_db)`` pair _exit_code needs."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    kb.init_db()
    path = kb.kanban_db_path()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title='owner fence', assignee='default')
        task = kb.claim_task(conn, task_id, claimer='private-host:owner')
        context = {'db': str(path), 'task_id': task_id, 'run_id': task.current_run_id,
                   'claim_lock': task.claim_lock}
        if verdict is not None:
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, 'worker_result',
                                 {'pid': 4242, 'claim_lock': task.claim_lock, **verdict},
                                 run_id=task.current_run_id)
    return context, path


def test_unknown_receipt_reports_the_booked_verdict(tmp_path, monkeypatch):
    """The child booked 75 (quota wall) before the owner fenced the admission ``unknown``: the
    submitter must return 75 — the dispatcher requeues it without counting a failure — instead of
    masking the quota wall behind a bare exit 1."""
    context, path = _board_with_verdict(tmp_path, monkeypatch,
                                        {'exit_code': 75, 'last_output': ''})
    assert _exit_code({'status': 'unknown', 'admission_id': 'a'}, context, path) == 75


def test_unknown_receipt_without_a_verdict_is_still_refused(tmp_path, monkeypatch):
    """No verdict at all — an invalidation the owner never resolved — keeps the honest refusal."""
    from hermes_cli.gateway_client import GatewayClientError
    context, path = _board_with_verdict(tmp_path, monkeypatch, None)
    with pytest.raises(GatewayClientError, match='Kanban execution unknown'):
        _exit_code({'status': 'unknown', 'admission_id': 'a'}, context, path)


def test_terminal_receipt_still_returns_the_booked_verdict(tmp_path, monkeypatch):
    """Regression: the settled path is unchanged — a terminal receipt reports its booked code."""
    context, path = _board_with_verdict(tmp_path, monkeypatch,
                                        {'exit_code': 0, 'last_output': 'done'})
    assert _exit_code({'status': 'terminal', 'admission_id': 'a'}, context, path) == 0


# --- _await_receipt: a restart invalidation re-submits instead of exiting ---------------------

class _FakeClient:
    """Scripted owner: each ``kanban.run`` answer then the ``prompt.receipt`` polls after it."""

    def __init__(self, run_answers, receipt_answers):
        self.run_answers = list(run_answers)
        self.receipt_answers = list(receipt_answers)
        self.methods = []

    async def rpc(self, method, **kwargs):
        self.methods.append(method)
        if method == 'kanban.run':
            return self.run_answers.pop(0)
        if method == 'prompt.receipt':
            return self.receipt_answers.pop(0)
        raise AssertionError(f'unexpected rpc {method}')


@pytest.mark.asyncio
async def test_restart_fenced_unknown_resubmits_until_it_runs(tmp_path, monkeypatch):
    """No booked verdict + ``unknown`` clears the admission and re-submits (bounded): the restart
    invalidation recovers to a runnable admission instead of exiting 1."""
    context, path = _board_with_verdict(tmp_path, monkeypatch, None)
    client = _FakeClient(
        run_answers=[
            {'session_id': 's1', 'receipt': {'status': 'unknown', 'admission_id': 'a1'}},
            {'session_id': 's2', 'receipt': {'status': 'queued', 'admission_id': 'a2'}},
        ],
        receipt_answers=[{'status': 'terminal', 'admission_id': 'a2'}],
    )
    claim = _Claim(answered=0.0)
    receipt = await _await_receipt(client, context, path, claim)
    assert receipt['status'] == 'terminal'
    assert client.methods.count('kanban.run') == 2, client.methods  # re-submitted once
    assert claim.unknown_recoveries == 1


@pytest.mark.asyncio
async def test_booked_verdict_short_circuits_the_resubmit(tmp_path, monkeypatch):
    """A booked verdict means the execution is over: return immediately, never re-submit (which
    would otherwise loop against a fence that must stand)."""
    context, path = _board_with_verdict(tmp_path, monkeypatch, {'exit_code': 1, 'last_output': ''})
    client = _FakeClient(
        run_answers=[{'session_id': 's1', 'receipt': {'status': 'unknown', 'admission_id': 'a1'}}],
        receipt_answers=[],
    )
    claim = _Claim(answered=0.0)
    receipt = await _await_receipt(client, context, path, claim)
    assert receipt['status'] == 'unknown'
    assert client.methods == ['kanban.run'], client.methods  # no resubmit
    assert claim.unknown_recoveries == 0
