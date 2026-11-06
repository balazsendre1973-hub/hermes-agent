"""Read-only model pin: exactly one tier may decide the model (t_55e6fd81, layer (c)).

The measured gap (stabilitasi-architektura-jelentes §4): the card tier was a
third writable place for the model — 24 cards pinned ``model_override`` in 7
days, 13 of them ended ``gave_up``. The rule this module encodes:

    a card's ``model_override`` may only MIRROR the assignee profile's effective
    model pin; it can never CHANGE what runs.

Enforcement points (all call into this one module, so the verdicts cannot drift
apart):

* write time — ``hermes_cli.kanban_write_gate.require_model_pin`` refuses a
  divergent pin in ``create_task`` / ``set_model_override``;
* session build — ``gateway.session_kanban.build_kanban_policy`` drops a
  divergent pin (pre-existing cards keep running, on the profile pin);
* dispatch — ``hermes_cli.kanban_preflight`` logs the drift as a warning
  (monitoring only, never a block).

The pin itself is read from exactly one place: the profile's ``config.yaml``
(protected from agent writes by the (d) runtime-environment guard) with the
managed overlay applied on top (``hermes_cli.managed_scope`` — the cron-level
``cron.model`` pin lives there). Fail-open everywhere: an unreadable config, a
missing pin or an unresolvable profile means NO drift verdict, so a broken
config can never park healthy cards — the model-existence check
(``hermes_cli.model_gate``) still applies to every pin.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _config_path_for(profile_home: Optional[str], config_path: Optional[Path]) -> Optional[Path]:
    if config_path is not None:
        return Path(config_path)
    if not profile_home:
        return None
    return Path(profile_home) / "config.yaml"


def home_model_pin(
    profile_home: Optional[str] = None, *, config_path: Optional[Path] = None,
) -> tuple[Optional[str], Optional[str]]:
    """``(model.default, model.provider)`` of a profile's EFFECTIVE config.

    Reads the profile's ``config.yaml`` through
    ``hermes_cli.config_effective.load_user_config_effective`` so the managed
    overlay and ``${VAR}`` expansion are applied exactly like every runtime
    reader does. ``(None, None)`` on anything unreadable (fail-open).
    """
    path = _config_path_for(profile_home, config_path)
    if path is None:
        return None, None
    try:
        from hermes_cli.config_effective import load_user_config_effective

        effective = load_user_config_effective(Path(path))
        model = effective.get("model") if isinstance(effective, dict) else None
        if isinstance(model, dict):
            default = model.get("default")
            provider = model.get("provider")
            return (
                str(default) if default else None,
                str(provider) if provider else None,
            )
        if isinstance(model, str) and model:
            # Bare root ``model: <id>`` form: no provider implied (model ids may
            # themselves contain '/', e.g. openrouter's ``vendor/model``).
            return model, None
        return None, None
    except Exception as exc:
        logger.debug("model pin: cannot read home pin from %s (%s)", path, exc)
        return None, None


def managed_model_pins() -> dict[str, Any]:
    """Dotted leaf keys of the managed layer that pin a model.

    ``{'cron.model': '...', 'cron.model_provider': '...'}`` in this deployment;
    ``model.*`` would appear here too if an operator pinned it. Empty dict on
    any error (fail-open).
    """
    try:
        from hermes_cli.managed_scope import managed_config_keys, load_managed_config

        managed = load_managed_config()
        if not isinstance(managed, dict) or not managed:
            return {}
        pinned = managed_config_keys()
        flat: dict[str, Any] = {}

        def walk(node: Any, prefix: str) -> None:
            if isinstance(node, dict) and node:
                for key, value in node.items():
                    walk(value, f"{prefix}.{key}" if prefix else str(key))
            elif prefix:
                flat[prefix] = node

        walk(managed, "")
        return {key: value for key, value in flat.items() if key in pinned}
    except Exception as exc:
        logger.debug("model pin: cannot read managed pins (%s)", exc)
        return {}


def card_override_drift(
    model: Optional[str],
    provider: Optional[str],
    *,
    profile_home: Optional[str] = None,
    config_path: Optional[Path] = None,
) -> Optional[str]:
    """Why the card pin diverges from the profile pin, or ``None`` (no drift).

    ``None`` also when there is nothing to compare: no card pin, no home pin
    (profile without ``model.default``), or an unreadable config — the card
    tier stays writable exactly where there is no single-tier pin to protect.
    """
    if not model:
        return None
    home_model, home_provider = home_model_pin(profile_home, config_path=config_path)
    if not home_model:
        return None
    if str(model) != str(home_model):
        return (
            f"card model pin {model!r} diverges from the profile pin {home_model!r} — "
            "the model may only be set in the profile config (the model pin is "
            "read-only at card level); clear it with 'hermes kanban set-model <id> none' "
            "or align it with the profile pin"
        )
    if provider and home_provider and str(provider) != str(home_provider):
        return (
            f"card provider pin {provider!r} diverges from the profile provider "
            f"{home_provider!r} — the model pin is read-only at card level; clear it "
            "with 'hermes kanban set-model <id> none' or align it with the profile pin"
        )
    return None


def model_pin_drift_report(
    model: Optional[str],
    provider: Optional[str],
    *,
    profile_home: Optional[str] = None,
    config_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Structured drift verdict for the preflight warning / diagnostics."""
    drift = card_override_drift(
        model, provider, profile_home=profile_home, config_path=config_path,
    )
    home_model, home_provider = home_model_pin(profile_home, config_path=config_path)
    return {
        "card_model": model,
        "card_provider": provider,
        "home_model": home_model,
        "home_provider": home_provider,
        "drift": drift,
        "managed_pins": managed_model_pins(),
    }
