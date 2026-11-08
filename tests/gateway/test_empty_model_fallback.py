"""Tests for empty model fallback — when provider is configured but model is missing."""

from unittest.mock import patch

import pytest








class TestGatewayEmptyModelFallback:
    """Test that _resolve_session_agent_runtime fills in empty model from provider catalog."""

    def test_empty_model_filled_from_provider(self):
        """When config has no model but provider is openai-codex, use first codex model."""
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}

        # Mock _resolve_gateway_model to return empty string
        # Mock _resolve_runtime_agent_kwargs to return openai-codex provider
        with patch("gateway.run._resolve_gateway_model", return_value=""), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "openai-codex",
                 "api_key": "test-key",
                 "base_url": "https://chatgpt.com/backend-api/codex",
                 "api_mode": "codex_responses",
             }):
            model, kwargs = runner._resolve_session_agent_runtime()

        # Model should have been filled in from provider catalog
        assert model, "Model should not be empty when provider is known"
        assert isinstance(model, str)
        assert kwargs["provider"] == "openai-codex"

    def test_nonempty_model_not_overridden(self):
        """When config has a model set, don't override it."""
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}

        with patch("gateway.run._resolve_gateway_model", return_value="gpt-5.4"), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "openai-codex",
                 "api_key": "test-key",
                 "base_url": "https://chatgpt.com/backend-api/codex",
                 "api_mode": "codex_responses",
             }):
            model, _kwargs = runner._resolve_session_agent_runtime()

        assert model == "gpt-5.4", "Explicit model should not be overridden"

    def test_empty_model_no_provider_stays_empty(self):
        """When both model and provider are empty, model stays empty."""
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}

        with patch("gateway.run._resolve_gateway_model", return_value=""), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "",
                 "api_key": "test-key",
                 "base_url": "https://example.com",
                 "api_mode": "chat_completions",
             }):
            model, _kwargs = runner._resolve_session_agent_runtime()

        # Can't fill in a default without knowing the provider
        assert model == ""


class TestResolveGatewayModel:
    """Test _resolve_gateway_model reads model from config correctly.

    Fail-closed: there is no longer a ``''`` return path. If the profile names no model and no
    welcome trial is active, :exc:`ModelResolutionError` is raised (``t_605d71ef``, ``t_f59435a5``,
    ``t_2f00675a``).
    """

    def test_returns_default_key(self):
        from gateway.run import _resolve_gateway_model
        assert _resolve_gateway_model({"model": {"default": "gpt-5.4"}}) == "gpt-5.4"

    def test_returns_model_key_fallback(self):
        from gateway.run import _resolve_gateway_model
        assert _resolve_gateway_model({"model": {"model": "gpt-5.4"}}) == "gpt-5.4"

    def test_raises_when_model_dict_missing_default(self):
        from gateway.run import ModelResolutionError
        from gateway.run import _resolve_gateway_model
        with pytest.raises(ModelResolutionError) as exc:
            _resolve_gateway_model({"model": {}})
        assert "no configured model and no welcome trial active" in str(exc.value)

    def test_raises_when_no_model_section(self):
        from gateway.run import ModelResolutionError
        from gateway.run import _resolve_gateway_model
        with pytest.raises(ModelResolutionError) as exc:
            _resolve_gateway_model({})
        assert "no configured model and no welcome trial active" in str(exc.value)

    def test_string_model_config(self):
        from gateway.run import _resolve_gateway_model
        assert _resolve_gateway_model({"model": "my-model"}) == "my-model"


class TestTrialModelFailClosed:
    """The fail-closed contract of :func:`hermes_cli.anon_auth.trial_model` (``t_2f00675a``)."""

    def test_trial_model_raises_when_no_trial(self):
        import hermes_cli.anon_auth as anon_auth
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(anon_auth, "free_inference_active", lambda: False)
            with pytest.raises(anon_auth.TrialModelUnavailable) as exc:
                anon_auth.trial_model()
            assert "no configured model and no welcome trial active" in str(exc.value)

    def test_trial_model_returns_guest_model_when_trial_active(self):
        import hermes_cli.anon_auth as anon_auth
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(anon_auth, "free_inference_active", lambda: True)
            assert anon_auth.trial_model() == anon_auth.GUEST_MODEL
            assert anon_auth.trial_model() == "nous/welcome"
