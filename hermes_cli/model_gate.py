"""Shared model-existence check — ONE implementation, two call sites.

* the kanban **write-gate** (``hermes_cli.kanban_write_gate``) — at WRITE time,
  refusing a card mutation that carries a model the target provider does not
  offer (the ``gpt-5-nano`` → HTTP 400 class);
* the kanban **dispatcher preflight** (``kanban_db_dispatch`` / ``kanban_preflight``)
  — at DISPATCH time, before a worker is spawned for a card.

Both layers must stay consistent (one shared checking module), so neither layer
re-implements the rules: import :func:`model_known` from here.

Offline-first: a write path and a dispatch tick must never block on the network,
so only LOCAL catalogs are consulted:

* the curated static per-provider tables (``hermes_cli.models.static_catalog``),
* the live per-provider disk cache (``$HERMES_HOME/provider_models_cache.json``),
* the model-catalog manifest disk cache (``$HERMES_HOME/cache/model_catalog.json``).

Fail-open rule (the gate must not break meaningful work): a model is rejected
only on **positive evidence of absence** —

* provider given: only when that provider HAS a local catalog (any of the three
  sources) and the model is in none of them;
* provider unknown: only when some local catalog is non-empty and the model is
  in none of them.

An empty/missing catalog source (fresh install, offline box, custom base_url
provider) passes with a reason: blocking a real model would break work, while
the gate's job is to catch a proven-bad override before it reaches the API.

Results are memoized in-process for :data:`CACHE_TTL_SECONDS` seconds so a
mutation burst does not re-read the catalogs; tests reset it with
:func:`clear_cache`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

CACHE_TTL_SECONDS = 60.0

# (kind, provider) -> (ids, loaded_at); kind in {"static", "manifest", "live"}.
_catalog_cache: dict[tuple[str, str], tuple[frozenset[str], float]] = {}
# (model, provider) -> (ok, reason, checked_at)
_result_cache: dict[tuple[str, str], tuple[bool, str, float]] = {}


def clear_cache() -> None:
    """Drop every memoized catalog/result (tests, post-refresh)."""
    _catalog_cache.clear()
    _result_cache.clear()


def _hermes_home() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:
        return None


def _fresh(entry: tuple[Any, float], now: float) -> bool:
    return (now - entry[1]) < CACHE_TTL_SECONDS


def _load_json(path: Path) -> Any:
    """Read a JSON file; missing/corrupt → ``None`` (never raises: the gate must
    degrade to fail-open, not to an exception in a write path)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _static_ids(provider: Optional[str], now: float) -> frozenset[str]:
    key = ("static", provider or "*")
    hit = _catalog_cache.get(key)
    if hit is not None and _fresh(hit, now):
        return hit[0]
    ids: set[str] = set()
    try:
        from hermes_cli.models import static_catalog, static_catalog_providers

        if provider:
            ids.update(static_catalog(provider))
        else:
            for name in static_catalog_providers():
                ids.update(static_catalog(name))
    except Exception:
        ids = set()
    _catalog_cache[key] = (frozenset(ids), now)
    return _catalog_cache[key][0]


def _manifest_ids(provider: Optional[str], now: float) -> frozenset[str]:
    key = ("manifest", provider or "*")
    hit = _catalog_cache.get(key)
    if hit is not None and _fresh(hit, now):
        return hit[0]
    ids: set[str] = set()
    home = _hermes_home()
    data = _load_json(home / "cache" / "model_catalog.json") if home else None
    if isinstance(data, dict):
        blocks = data.get("providers")
        if isinstance(blocks, dict):
            wanted = [provider] if provider else list(blocks)
            for name in wanted:
                block = blocks.get(name)
                if not isinstance(block, dict):
                    continue
                for entry in block.get("models") or ():
                    if isinstance(entry, dict):
                        mid = str(entry.get("id") or "").strip()
                        if mid:
                            ids.add(mid)
    _catalog_cache[key] = (frozenset(ids), now)
    return _catalog_cache[key][0]


def _live_ids(provider: Optional[str], now: float) -> frozenset[str]:
    key = ("live", provider or "*")
    hit = _catalog_cache.get(key)
    if hit is not None and _fresh(hit, now):
        return hit[0]
    ids: set[str] = set()
    home = _hermes_home()
    data = _load_json(home / "provider_models_cache.json") if home else None
    if isinstance(data, dict):
        wanted = [provider] if provider else list(data)
        for name in wanted:
            entry = data.get(name)
            if isinstance(entry, dict):
                for mid in entry.get("models") or ():
                    mid = str(mid).strip()
                    if mid:
                        ids.add(mid)
    _catalog_cache[key] = (frozenset(ids), now)
    return _catalog_cache[key][0]


def _normalize_provider(provider: Optional[str]) -> Optional[str]:
    provider = (provider or "").strip()
    if not provider:
        return None
    try:
        from hermes_cli.models import normalize_provider

        provider = normalize_provider(provider) or provider
    except Exception:
        pass
    return provider.strip().lower() or None


def known_ids(provider: Optional[str] = None) -> frozenset[str]:
    """Union of every local catalog for *provider* (all providers when None).
    Exposed for diagnostics/tests; cheap thanks to the in-process cache."""
    now = time.time()
    return _static_ids(provider, now) | _manifest_ids(provider, now) | _live_ids(provider, now)


def model_known(model: Optional[str], provider: Optional[str] = None) -> tuple[bool, str]:
    """``(ok, reason)`` — does *model* exist for *provider* in the local catalogs?

    Shared by the write-gate (write time) and the dispatcher preflight (dispatch
    time). Empty/absent model (a cleared override) is always OK. See the module
    docstring for the fail-open rule.
    """
    model = (model or "").strip()
    if not model:
        return True, "no model override to check"
    provider = _normalize_provider(provider)

    now = time.time()
    cached = _result_cache.get((model, provider or "*"))
    if cached is not None and (now - cached[2]) < CACHE_TTL_SECONDS:
        return cached[0], cached[1]

    ok, reason = _evaluate(model, provider, now)
    _result_cache[(model, provider or "*")] = (ok, reason, now)
    return ok, reason


def _evaluate(model: str, provider: Optional[str], now: float) -> tuple[bool, str]:
    if provider:
        scoped = _static_ids(provider, now) | _manifest_ids(provider, now) | _live_ids(provider, now)
        if model in scoped:
            return True, f"local catalog for provider {provider!r} lists it"
        if not scoped:
            # No catalog for this provider at all (custom base_url provider,
            # never probed): nothing to check against → fail-open.
            return True, f"no local catalog for provider {provider!r} (fail-open)"
        everywhere = known_ids(None)
        if model in everywhere:
            # Positive evidence of absence: the model exists, but not for this
            # provider — exactly the gpt-5-nano-on-the-wrong-backend signature.
            return False, (
                f"model exists locally but provider {provider!r} does not offer it "
                "(would reach the API as HTTP 400)"
            )
        if not everywhere:
            return True, "no local catalog at all (fail-open)"
        return False, f"model not found in any local catalog (provider {provider!r})"

    everywhere = known_ids(None)
    if model in everywhere:
        return True, "listed in a local catalog"
    if not everywhere:
        return True, "no local catalog at all (fail-open)"
    return False, "model not found in any local catalog"


def diagnose(model: Optional[str], provider: Optional[str] = None) -> dict[str, Any]:
    """Structured breakdown of what each source says (for preflight/gate logs)."""
    provider = _normalize_provider(provider)
    now = time.time()
    return {
        "model": model,
        "provider": provider,
        "known": model_known(model, provider)[0],
        "reason": model_known(model, provider)[1],
        "static": sorted(_static_ids(provider, now)),
        "manifest": sorted(_manifest_ids(provider, now)),
        "live": sorted(_live_ids(provider, now)),
    }
