"""A standalone-only platform must never be reported as an unavailable adapter.

The ``log`` platform is a send-only cron delivery lane: its ``adapter_factory`` returns ``None``
on purpose and cron delivery rides its ``standalone_sender_fn`` (the Telegram bot behind
``LOG_TELEGRAM_TOKEN``). Startup used to treat every enabled platform without an adapter as
``adapter_unavailable``/fatal, so each profile logged ``No adapter available for enabled log``
ten times over and ``gateway_state.json`` carried a fatal ``log`` / ``coder:log`` verdict for a
platform that was in fact served. The flag lives on ``PlatformEntry.standalone_only``; without it
the field defaults to ``False`` and every ordinary platform keeps the old fatal behaviour.
"""

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run import GatewayRunner
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.status import flush_runtime_status_async, read_runtime_status


def _entry(name: str, *, standalone_only: bool) -> PlatformEntry:
    return PlatformEntry(
        name=name,
        label=name,
        adapter_factory=lambda pconfig: None,
        check_fn=lambda: True,
        standalone_only=standalone_only,
    )


class _Recorder(GatewayAdapterLifecycleMixin):
    """Lifecycle mixin with only the runtime-status writer replaced."""

    def __init__(self) -> None:
        self.status_calls: list[tuple[str, dict]] = []

    def _update_platform_runtime_status(self, status_key, **kwargs) -> None:
        self.status_calls.append((status_key, kwargs))


def test_platform_entry_standalone_only_defaults_to_false():
    """Platforms that never opt in keep the historical fatal-on-missing-adapter behaviour."""
    assert _entry("telegram", standalone_only=False).standalone_only is False
    assert "standalone_only" in PlatformEntry.__dataclass_fields__


def test_is_standalone_only_platform_reads_the_registered_flag():
    """The verdict comes from the registry entry, and it is gone again once unregistered."""
    previous = platform_registry.get("log")
    assert GatewayAdapterLifecycleMixin._is_standalone_only_platform(Platform("log")) is False
    platform_registry.register(_entry("log", standalone_only=True))
    try:
        assert GatewayAdapterLifecycleMixin._is_standalone_only_platform(Platform("log")) is True
    finally:
        if previous is None:
            platform_registry.unregister("log")
        else:
            platform_registry.register(previous)
    assert GatewayAdapterLifecycleMixin._is_standalone_only_platform(Platform("log")) is False


def test_mark_standalone_only_platform_records_a_non_fatal_verdict():
    """The status it writes is served-but-adapter-less: disabled, no attention, no error code."""
    recorder = _Recorder()
    recorder._mark_standalone_only_platform("coder:log", Platform("log"))

    assert len(recorder.status_calls) == 1
    status_key, payload = recorder.status_calls[0]
    assert status_key == "coder:log"
    assert payload["platform_state"] == "disabled"
    assert payload["needs_attention"] is False
    assert payload["error_code"] is None
    assert "standalone-only" in payload["error_message"]


@pytest.mark.asyncio
async def test_startup_prefilter_never_flags_a_standalone_only_platform_fatal(
    monkeypatch, tmp_path
):
    """The primary startup pass records the standalone verdict instead of ``adapter_unavailable``:
    the platform is enabled and counted, gets no adapter, and still ends up non-fatal."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    previous = platform_registry.get("log")
    platform_registry.register(_entry("log", standalone_only=True))
    try:
        runner = GatewayRunner(
            GatewayConfig(
                platforms={Platform("log"): PlatformConfig(enabled=True)},
                sessions_dir=tmp_path / "sessions",
            )
        )
        monkeypatch.setattr(runner, "_create_adapter", lambda platform, platform_config: None)

        aborted, enabled_count, _skipped, pending = await runner._start_prefilter_platforms()

        assert aborted is False
        assert enabled_count == 1
        assert pending == []
        assert not getattr(runner, "_failed_platforms", {})
        assert await flush_runtime_status_async()
        state = read_runtime_status()
        log_status = state["platforms"]["log"]
        assert log_status["state"] != "fatal"
        assert log_status["state"] == "disabled"
        assert log_status.get("needs_attention") is False
        assert log_status["error_code"] is None
    finally:
        if previous is None:
            platform_registry.unregister("log")
        else:
            platform_registry.register(previous)
