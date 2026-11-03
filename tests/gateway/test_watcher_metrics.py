"""Unit tests for the structured metrics emitted by gateway/run_watchers.py.

Covers one pass of session housekeeping (eviction/prune counts, incl. the zero
pass), the stall watchdog (candidates/latches_set/latches_cleared/notified for
a stale pending session and a fresh one), catalog refresh (duration_ms/ok),
stable event names, and that instrumentation never changes watcher control
flow (a failing watcher pass is still swallowed).

stdlib unittest only -- no pytest/pytest-asyncio required. Gateway internals
are mocked exactly as run_watchers.py imports them lazily:
patch("gateway.run.X") / patch("hermes_cli.model_catalog.X").

Run:
    cd /home/endre/.hermes/hermes-agent
    python3 -m unittest tests.gateway.test_watcher_metrics -v
"""

import sys
from pathlib import Path

# Ensure the canonical repo (parent of tests/) is importable when this file is
# executed directly as a script, e.g. ``python3 tests/gateway/test_watcher_metrics.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncio
import logging
import os
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from gateway.run import GatewayRunner
from gateway.run_watchers import GatewaySessionWatchersMixin

HOUSEKEEPING = "watcher.housekeeping.pass"
STALL = "watcher.stall.pass"
CATALOG = "watcher.catalog.refresh"

METRICS_LOGGER = logging.getLogger("gateway.run")


class _RecordHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _metric_fields(message):
    fields = {}
    for token in message.split():
        if "=" in token:
            key, _, value = token.partition("=")
            fields[key] = value
    return fields


class _FakeAgent:
    def __init__(self, idle_seconds):
        self._idle = float(idle_seconds)

    def get_activity_summary(self):
        return {"seconds_since_activity": self._idle}


class _FakeSource:
    def __init__(self, chat_id="1", platform="tg"):
        self.chat_id = chat_id
        self.platform = platform


class _FakeEvent:
    def __init__(self, source):
        self.source = source


class _FakeAdapter:
    def __init__(self, pending=None):
        self._pending_messages = dict(pending or {})


class _FakeSessionStore:
    def __init__(self, pruned=0):
        self.pruned = pruned
        self.calls = 0

    async def prune_old_entries(self, max_age_days):
        self.calls += 1
        return self.pruned


class TestWatcherMetrics(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._logger = METRICS_LOGGER
        self._saved_level = self._logger.level
        self._saved_propagate = self._logger.propagate
        self._handler = _RecordHandler()
        self._records = self._handler.records  # the handler appends here; alias for assertions
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        self._logger.propagate = False  # do not leak test records to root/production handlers
        self.addCleanup(self._restore_logging)

    def _restore_logging(self):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._saved_level)
        self._logger.propagate = self._saved_propagate

    def _records_for(self, event_name):
        return [r for r in self._records if r.getMessage().startswith(event_name + " ")]

    def _assert_metric(self, event_name, **expected):
        records = self._records_for(event_name)
        self.assertEqual(
            len(records),
            1,
            f"expected exactly one {event_name} record, got {len(records)}: {[r.getMessage() for r in records]}",
        )
        fields = _metric_fields(records[0].getMessage())
        for key, value in expected.items():
            self.assertEqual(
                fields.get(key),
                str(value),
                f"{event_name}: field {key!r} = {fields.get(key)!r} (want {value!r})",
            )

    def _make_runner(self, **overrides):
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = False
        runner._sweep_idle_cached_agents = lambda: 0
        runner._sweep_agent_cache_under_pressure = lambda: 0
        runner.__dict__.update(overrides)
        return runner

    # ── session housekeeping ──────────────────────────────────────────

    async def test_housekeeping_emits_evicted_and_prune_counts(self):
        runner = self._make_runner()
        runner._sweep_idle_cached_agents = lambda: 3
        runner._sweep_agent_cache_under_pressure = lambda: 2
        runner._last_session_store_prune_ts = 0.0  # hourly prune window due
        runner.config = types.SimpleNamespace(session_store_max_age_days=7)
        fake_store = _FakeSessionStore(pruned=5)
        with patch.object(
            GatewayRunner,
            "async_session_store",
            property(lambda self, _store=fake_store: _store),
        ):
            await runner._session_housekeeping()

        self._assert_metric(HOUSEKEEPING, evicted_idle=3, evicted_pressure=2, pruned=5)
        self.assertEqual(fake_store.calls, 1)

    async def test_housekeeping_zero_case(self):
        runner = self._make_runner()
        runner._last_session_store_prune_ts = time.time()  # prune not due this pass

        await runner._session_housekeeping()

        self._assert_metric(HOUSEKEEPING, evicted_idle=0, evicted_pressure=0, pruned=0)

    async def test_housekeeping_tolerates_omitted_runner_attributes(self):
        # Known risk: watcher loops are exercised with runners that omit
        # config / async_session_store / _last_session_store_prune_ts; every
        # access must be getattr-guarded so nothing crashes and the metric
        # line is still emitted (all zeros).
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._sweep_idle_cached_agents = lambda: 0
        runner._sweep_agent_cache_under_pressure = lambda: 0
        # no config, no async_session_store, no _last_session_store_prune_ts

        await runner._session_housekeeping()

        self._assert_metric(HOUSEKEEPING, evicted_idle=0, evicted_pressure=0, pruned=0)
        # prune window was due (ts defaults to 0.0); the missing config raised
        # inside the try and was swallowed, but the timestamp was still recorded.
        self.assertIsInstance(getattr(runner, "_last_session_store_prune_ts", None), float)

    async def test_housekeeping_swallows_sweep_exception_and_still_logs(self):
        runner = self._make_runner()
        runner._sweep_idle_cached_agents = lambda: 1

        def failing_pressure():
            raise RuntimeError("pressure sweep died")

        runner._sweep_agent_cache_under_pressure = failing_pressure
        runner._last_session_store_prune_ts = time.time()

        await runner._session_housekeeping()  # must not propagate

        self._assert_metric(HOUSEKEEPING, evicted_idle=1, evicted_pressure=0, pruned=0)

    async def test_housekeeping_watcher_swallows_failing_pass(self):
        # Control-flow check: a failing watcher pass is swallowed by the watch
        # loop exactly as before instrumentation; the loop continues to the next
        # pass (which logs its metric record) and exits cleanly when _running
        # flips. A pass that dies before the structured log line emits nothing.
        runner = self._make_runner()
        runner._running = True
        real_housekeeping = GatewaySessionWatchersMixin._session_housekeeping
        state = {"calls": 0}

        async def flaky_housekeeping():
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("housekeeping died")
            runner._running = False  # stop after the recovering pass
            await real_housekeeping(runner)  # recovering pass logs its metric record

        runner._session_housekeeping = flaky_housekeeping

        calls = {"n": 0}

        async def fake_sleep(seconds):
            calls["n"] += 1  # never flips; let interruptible sleep run its course

        with patch("gateway.run_watchers.asyncio.sleep", fake_sleep):
            await runner._session_housekeeping_watcher(interval=300)

        self.assertEqual(state["calls"], 2, "watcher must continue past a failing pass")
        self.assertGreaterEqual(calls["n"], 3)  # initial sleep(60) + interruptible sleeps
        self.assertTrue(
            any("Session housekeeping error" in r.getMessage() for r in self._records),
            "expected the swallowed-exception debug record",
        )
        self._assert_metric(HOUSEKEEPING, evicted_idle=0, evicted_pressure=0, pruned=0)

    # ── stall watchdog ────────────────────────────────────────────────

    async def test_stall_stale_pending_session_emits_fields(self):
        runner = self._make_runner()
        source = _FakeSource(chat_id="1")
        runner.adapters = {"fake": _FakeAdapter({"sess:1": _FakeEvent(source)})}
        runner._running_agents = {"sess:1": _FakeAgent(idle_seconds=120)}  # stale vs timeout 60
        runner._session_stall_notified = {}
        notified = []

        async def notify(session_key, adapter, pending_event, idle_seconds, activity,
                         timeout_seconds, notified_map):
            notified.append(session_key)
            notified_map[session_key] = True
            return True

        runner._notify_session_stall = notify

        sent = await runner._check_session_stalls(timeout_seconds=60.0)

        self.assertEqual(sent, 1)
        self.assertEqual(notified, ["sess:1"])
        self._assert_metric(STALL, candidates=1, latches_set=1, latches_cleared=0, notified=1)

    async def test_stall_fresh_session_is_candidate_but_not_notified(self):
        runner = self._make_runner()
        source = _FakeSource(chat_id="1")
        runner.adapters = {"fake": _FakeAdapter({"sess:1": _FakeEvent(source)})}
        runner._running_agents = {"sess:1": _FakeAgent(idle_seconds=5)}  # fresh vs timeout 60
        runner._session_stall_notified = {}
        notified = []

        async def notify(session_key, adapter, pending_event, idle_seconds, activity,
                         timeout_seconds, notified_map):
            notified.append(session_key)
            notified_map[session_key] = True
            return True

        runner._notify_session_stall = notify

        sent = await runner._check_session_stalls(timeout_seconds=60.0)

        self.assertEqual(sent, 0)
        self.assertEqual(notified, [])
        self._assert_metric(STALL, candidates=1, latches_set=0, latches_cleared=0, notified=0)

    async def test_stall_clears_latch_when_session_drops_out(self):
        runner = self._make_runner()  # no adapters -> no candidates
        runner._session_stall_notified = {"orphan:9": True}

        async def notify(session_key, adapter, pending_event, idle_seconds, activity,
                         timeout_seconds, notified_map):
            notified_map[session_key] = True
            return True

        runner._notify_session_stall = notify

        await runner._check_session_stalls(timeout_seconds=60.0)

        self._assert_metric(STALL, candidates=0, latches_set=0, latches_cleared=1, notified=0)
        self.assertNotIn("orphan:9", runner._session_stall_notified)

    async def test_stall_tolerates_omitted_runner_attributes(self):
        # Bare runner: no adapters / _profile_adapters / _queued_events /
        # _running_agents / _notify_session_stall; _check_session_stalls must
        # still emit an all-zero metric line without crashing.
        runner = GatewayRunner.__new__(GatewayRunner)

        await runner._check_session_stalls(timeout_seconds=60.0)

        self._assert_metric(STALL, candidates=0, latches_set=0, latches_cleared=0, notified=0)
        self.assertEqual(runner._session_stall_notified, {})

    # ── catalog refresh ───────────────────────────────────────────────

    async def test_catalog_refresh_emits_duration_and_ok(self):
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = True
        refresh_mock = MagicMock(return_value=True)

        calls = {"n": 0}

        async def fake_sleep(seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                return  # initial asyncio.sleep(30)
            runner._running = False  # stop after one refresh pass

        with patch("hermes_cli.model_catalog.refresh_catalogs", refresh_mock), \
             patch("hermes_cli.model_catalog.refresh_interval_seconds", return_value=1200.0), \
             patch("gateway.run_watchers.asyncio.sleep", fake_sleep):
            await runner._model_catalog_refresh_watcher()

        self.assertEqual(refresh_mock.call_count, 1)
        record = self._records_for(CATALOG)
        self.assertEqual(len(record), 1)
        fields = _metric_fields(record[0].getMessage())
        self.assertEqual(fields["ok"], "1")
        duration_ms = float(fields["duration_ms"])
        self.assertGreaterEqual(duration_ms, 0.0)

    async def test_catalog_refresh_ok_zero_when_refresh_fails(self):
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = True
        refresh_mock = MagicMock(side_effect=RuntimeError("catalog down"))

        calls = {"n": 0}

        async def fake_sleep(seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                return
            runner._running = False

        with patch("hermes_cli.model_catalog.refresh_catalogs", refresh_mock), \
             patch("hermes_cli.model_catalog.refresh_interval_seconds", return_value=1200.0), \
             patch("gateway.run_watchers.asyncio.sleep", fake_sleep):
            await runner._model_catalog_refresh_watcher()  # must not propagate

        record = self._records_for(CATALOG)
        self.assertEqual(len(record), 1)
        fields = _metric_fields(record[0].getMessage())
        self.assertEqual(fields["ok"], "0")
        self.assertGreaterEqual(float(fields["duration_ms"]), 0.0)

    # ── lazy gateway.run imports (mocked as the module does) ──────────

    def test_session_stall_timeout_seconds_respects_env(self):
        runner = GatewayRunner.__new__(GatewayRunner)
        os.environ["HERMES_SESSION_STALL_TIMEOUT"] = "99"
        try:
            self.assertEqual(runner._session_stall_timeout_seconds(), 99.0)
        finally:
            del os.environ["HERMES_SESSION_STALL_TIMEOUT"]

    def test_session_stall_timeout_seconds_can_be_mocked_via_gateway_run(self):
        runner = GatewayRunner.__new__(GatewayRunner)
        with patch("gateway.run._float_env", return_value=42.0):
            self.assertEqual(runner._session_stall_timeout_seconds(), 42.0)

    # ── stable event names / field sets ───────────────────────────────

    async def test_event_names_and_field_sets_are_stable(self):
        runner = self._make_runner()
        runner._sweep_idle_cached_agents = lambda: 0
        runner._sweep_agent_cache_under_pressure = lambda: 0
        runner._last_session_store_prune_ts = 0.0
        runner.config = types.SimpleNamespace(session_store_max_age_days=0)
        await runner._session_housekeeping()

        source = _FakeSource(chat_id="1")
        runner.adapters = {"fake": _FakeAdapter({"sess:1": _FakeEvent(source)})}
        runner._running_agents = {"sess:1": _FakeAgent(idle_seconds=120)}
        runner._session_stall_notified = {}

        async def notify(session_key, adapter, pending_event, idle_seconds, activity,
                         timeout_seconds, notified_map):
            notified_map[session_key] = True
            return True

        runner._notify_session_stall = notify
        await runner._check_session_stalls(timeout_seconds=60.0)

        runner._running = True
        calls = {"n": 0}

        async def fake_sleep(seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                return
            runner._running = False

        with patch("hermes_cli.model_catalog.refresh_catalogs", MagicMock(return_value=True)), \
             patch("hermes_cli.model_catalog.refresh_interval_seconds", return_value=1200.0), \
             patch("gateway.run_watchers.asyncio.sleep", fake_sleep):
            await runner._model_catalog_refresh_watcher()

        expected_fields = {
            HOUSEKEEPING: {"evicted_idle", "evicted_pressure", "pruned"},
            STALL: {"candidates", "latches_set", "latches_cleared", "notified"},
            CATALOG: {"duration_ms", "ok"},
        }
        seen = set()
        for record in self._records:
            message = record.getMessage()
            if not message.startswith("watcher."):
                continue
            event_name = message.split(" ", 1)[0]
            self.assertIn(event_name, expected_fields, f"unexpected watcher event: {message!r}")
            self.assertEqual(
                set(_metric_fields(message)),
                expected_fields[event_name],
                f"metric field set changed for {message!r}",
            )
            seen.add(event_name)
        self.assertEqual(seen, {HOUSEKEEPING, STALL, CATALOG})


if __name__ == "__main__":
    unittest.main(verbosity=2)
