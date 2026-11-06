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


# ---------------------------------------------------------------------------
# security fixes from review t_cce44a6d (MERGE AFTER FIX):
# 2a redakció a közvetlen send-útvonalon, 2d UTF-16 limit, 1/5 standalone_only assert
# ---------------------------------------------------------------------------

def _dummy_sender(*_args, **_kwargs):
    """Standalone sender stand-in: the registration guards only need a truthy sender."""
    return {"success": True, "message_id": "1"}


def _plugin_ctx(source: str, name: str = "seastandalone"):
    """A PluginContext as a plugin gets it — ``source`` decides bundled vs user."""
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    return PluginContext(
        PluginManifest(name=name, key=name, source=source),
        PluginManager(scope_key=platform_registry.current_scope_key()),
    )


@pytest.fixture
def sent_chunks(monkeypatch):
    """The text log_standalone_send would put on the wire (no HTTP): one entry per chunk."""
    import plugins.platforms.log as log_plugin
    sent: list[str] = []

    def _fake_send(token, chat_id, text):
        del token, chat_id
        sent.append(text)
        return {"ok": True, "message_id": str(len(sent))}, None

    monkeypatch.setattr(log_plugin, "_send_one", _fake_send)
    monkeypatch.setenv("LOG_TELEGRAM_TOKEN", "123456789:TESTTOKEN-not-a-real-one")
    return sent


@pytest.mark.asyncio
async def test_log_standalone_send_redacts_secrets_before_sending(sent_chunks):
    """2a: the direct send path (`hermes send --target log`, send_message_tool) reached the Log
    channel unredacted — a secret in the payload must never leave, whatever the caller scrubbed."""
    import plugins.platforms.log as log_plugin
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # prefix + 36 chars
    result = await log_plugin.log_standalone_send(None, "", f"elotte {secret} utana")

    assert result.get("success") is True, result
    assert sent_chunks, "the message was delivered at all"
    wire = "".join(sent_chunks)
    assert secret not in wire
    assert "elotte" in wire and "utana" in wire  # the message itself still goes out


@pytest.mark.asyncio
async def test_log_standalone_send_chunks_stay_within_the_utf16_limit(sent_chunks):
    """2d: the Telegram limit counts UTF-16 units, not code points — 4000 astral emoji are
    8000 units, so code-point chunking produced a 400 and the whole delivery failed."""
    import plugins.platforms.log as log_plugin
    from gateway.platforms.base import utf16_len

    message = "\U0001F600" * 4000
    result = await log_plugin.log_standalone_send(None, "", message)

    assert result.get("success") is True, result
    assert len(sent_chunks) > 1, "the message was split"
    assert "".join(sent_chunks) == message, "no character lost or split in half"
    for chunk in sent_chunks:
        assert utf16_len(chunk) <= 4096, "inside the Telegram limit"
        assert utf16_len(chunk) <= log_plugin.MAX_CHUNK, "inside the chunk budget"


def test_utf16_helper_matches_the_gateway_measure():
    """The plugin carries its own measure (it must not import gateway code) — it has to agree
    with gateway.platforms.base.utf16_len, the limit the adapters are sized against."""
    import plugins.platforms.log as log_plugin
    from gateway.platforms.base import utf16_len

    for sample in ("", "ascii only", "\U0001F600" * 7, "日本語テスト", "a\U0001F600b",
                   "\U0001F600" * 4000, "CJK Ext B \U00020000"):
        assert log_plugin._utf16_len(sample) == utf16_len(sample)


def test_standalone_only_without_a_sender_is_refused():
    """1/5: no sender means the cron delivery this platform exists for fails silently."""
    ctx = _plugin_ctx("bundled")
    with pytest.raises(ValueError, match="standalone_only"):
        ctx.register_platform("seasolo", "Solo", adapter_factory=lambda cfg: None,
                              check_fn=lambda: True, standalone_only=True)
    assert platform_registry.get("seasolo") is None


def test_standalone_only_on_a_core_platform_is_refused():
    """1/5: even a bundled plugin may not claim the flag for a core adapter — startup would
    record telegram as disabled / needs_attention=False and drop its reconnect retry."""
    ctx = _plugin_ctx("bundled")
    with pytest.raises(ValueError, match="standalone_only"):
        ctx.register_platform("telegram", "Telegram", adapter_factory=lambda cfg: None,
                              check_fn=lambda: True, standalone_sender_fn=_dummy_sender,
                              standalone_only=True)


def test_standalone_only_is_refused_for_a_non_bundled_plugin():
    """1/5: a user plugin must not claim the flag at all (it could re-point any platform)."""
    ctx = _plugin_ctx("user", name="seaevil")
    with pytest.raises(ValueError, match="standalone_only"):
        ctx.register_platform("seaevilplatform", "Evil", adapter_factory=lambda cfg: None,
                              check_fn=lambda: True, standalone_sender_fn=_dummy_sender,
                              standalone_only=True)


def test_a_bundled_plugin_still_registers_its_own_standalone_only_platform():
    """Control: the guards must not block the legitimate case — the log plugin itself."""
    ctx = _plugin_ctx("bundled")
    ctx.register_platform("sealoglike", "Loglike", adapter_factory=lambda cfg: None,
                          check_fn=lambda: True, standalone_sender_fn=_dummy_sender,
                          standalone_only=True)
    try:
        entry = platform_registry.get("sealoglike")
        assert entry is not None and entry.standalone_only is True
        assert entry.standalone_sender_fn is not None
    finally:
        platform_registry.unregister("sealoglike", scope=platform_registry.current_scope_key())


def test_mark_standalone_only_platform_logs_a_warning(caplog):
    """4: the status is disabled / needs_attention=False and leaves the reconnect queue, so the
    drop has to show up in the log instead of happening silently."""
    import logging

    recorder = _Recorder()
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        recorder._mark_standalone_only_platform("coder:log", Platform("log"))
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("standalone-only platform 'log'" in m for m in messages), messages
