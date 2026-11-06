"""Kanban dispatcher preflight — the checks a card must pass *before* its worker spawns.

Why (measured, 2026-11-06 stability report): the dispatcher had no pre-flight
validation at all, so a card carrying a bad ``model_override`` (``gpt-5-nano``
on the wrong backend → ``HTTP 400 Model is unavailable``), a missing provider
credential, an unloadable ``skills`` entry or an unresolvable ``script`` path
was spawned anyway, crashed, and was parked ``gave_up`` by the failure breaker
— on a worker that never ran a single line of the card's work. The cron side
has had this since ``cron/scheduler_preflight.py``; this module is its kanban
twin, on the same contract:

* **READ-ONLY.** The preflight never writes — not the board, not the config.
* **FAIL-OPEN.** Every check swallows its own internal errors (unimportable
  module, missing catalog, unexpected exception) and reports *pass*. A broken
  checker must never take the board down; only an **affirmative** verdict
  blocks.
* **BLOCK, don't fail.** A card that fails preflight is parked ``blocked``
  with the reason and a :data:`VALID_BLOCK_KINDS` kind, so a human sees the
  config error at once instead of a ``gave_up`` two retries later. Blocking
  does not touch ``consecutive_failures`` (``kanban_db.block_task``), so a
  config error can never exhaust the failure limit.
* **One model-existence implementation.** The model verdict comes from
  ``hermes_cli.model_gate`` — the same module the write-gate uses. Do not
  re-implement catalog lookup here.

Two scopes, deliberately:

* **Profile scope** (``scope_factory()`` — the assignee's runtime scope, the
  environment the worker will actually see): provider credential, skills,
  script, home. Credentials and skill roots are per-profile facts.
* **Ambient scope** (the caller's — the dispatcher's — home): model
  existence. The model catalogs are installation knowledge read from
  *home-scoped* caches and memoized per process, so evaluating them under a
  per-dispatch profile override would mix one profile's cache into another's
  verdict.

Scope note on the provider check: it runs only for a card that **pins**
``model_override`` / ``provider_override``. An unpinned card runs on its
profile's default credentials through the worker's own path, and judging that
from the dispatcher's process would risk false positives across
single-profile vs multiplexed hosts (the child's secret scope differs). The
pinned case is the measured failure class (24 cards pinned a model in 7 days,
13 of them ended ``gave_up``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only; the dispatcher owns Task.
    from hermes_cli.kanban_db import Task

_log = logging.getLogger(__name__)

# Block kinds (kanban_db.VALID_BLOCK_KINDS) chosen per failure class:
# ``needs_input``  → the CARD carries the bad value; a human edits the card.
# ``capability``   → the environment lacks something; a human fixes the env.
KIND_CARD_VALUE = "needs_input"
KIND_ENVIRONMENT = "capability"

#: ``script: <path>`` — the only machine-readable way a card declares a script.
#: Kanban tasks have no ``script`` column (cron jobs do), so the directive is
#: the opt-in surface; 0 (zero) live cards use it, hence 0 false positives.
_SCRIPT_DIRECTIVE_RE = re.compile(r"^\s*script:[ \t]*(?P<path>\S+)[ \t]*$", re.MULTILINE)

#: Skill payloads that mean "this skill cannot run as-is" (mirrors the cron
#: preflight's verdict list).
_SKILL_MISSING_FIELDS = (
    ("missing_requirements", "missing requirement {0}"),
    ("missing_prerequisites", "missing prerequisite {0}"),
    ("missing_steps", "missing step {0}"),
)


def _attr(task: Any, name: str, default: Any = None) -> Any:
    """Read a column off a ``Task`` (or any row-like object) without raising."""
    try:
        value = getattr(task, name, default)
    except Exception:
        return default
    return default if value is None else value


def _declared_script(task: Any) -> Optional[str]:
    """Script this card runs, if it declares one (see module docstring)."""
    script = _attr(task, "script")
    if script:
        return str(script).strip() or None
    body = _attr(task, "body", "") or ""
    if not isinstance(body, str):
        return None
    match = _SCRIPT_DIRECTIVE_RE.search(body)
    return match.group("path") if match else None


def _hermes_root() -> Optional[str]:
    """Installation root (``~/.hermes``), independent of any profile scope."""
    try:
        from hermes_constants import get_default_hermes_root

        return str(get_default_hermes_root())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Individual checks — each returns ``(kind, reason)`` or ``None`` (pass).
# --------------------------------------------------------------------------- #

def check_home(profile_home: Optional[str]) -> Optional[tuple[str, str]]:
    """The worker's home must exist and be absolute.

    A relative/absent HOME is the ``task_register_sync.py`` class of failure:
    the script path resolves against whatever directory the process happened
    to start in, and the next start breaks silently.
    """
    if not profile_home:
        return None  # unknown home → the dispatcher's own profile-exists gate decides
    home = str(profile_home)
    if not os.path.isabs(home):
        return (
            KIND_ENVIRONMENT,
            f"preflight: profile home {home!r} is not an absolute path — script and "
            "config resolution would depend on the process cwd. Fix HERMES_HOME / the "
            "profile registration before dispatch.",
        )
    if not Path(home).exists():
        return (
            KIND_ENVIRONMENT,
            f"preflight: profile home {home!r} does not exist — the worker would start "
            "in a directory it cannot write. Fix the profile before dispatch.",
        )
    return None


def check_script(task: Any, profile_home: Optional[str] = None) -> Optional[tuple[str, str]]:
    """A declared script must resolve to a file that exists.

    Resolution order (mirrors how cron resolves a job's ``script`` field):

    1. ``~``/``$HOME`` expanded; an absolute path must simply exist.
    2. a relative path must exist under ``<profile_home>/scripts/`` or under
       the installation root ``scripts/`` — *both* are tried, so a card that
       names the right file with the wrong home still fails with both
       candidates in the message instead of a confusing cwd-relative miss.
    """
    script = _declared_script(task)
    if not script:
        return None
    expanded = os.path.expandvars(os.path.expanduser(script))
    candidates: list[Path] = []
    if os.path.isabs(expanded):
        candidates = [Path(expanded)]
    else:
        if profile_home:
            candidates.append(Path(profile_home) / "scripts" / expanded)
        root = _hermes_root()
        if root:
            candidates.append(Path(root) / "scripts" / expanded)
        candidates.append(Path(expanded))
    existing = [c for c in candidates if c.is_file()]
    if existing:
        target = existing[0]
        if not target.suffix and not os.access(target, os.X_OK):
            return (
                KIND_ENVIRONMENT,
                f"preflight: declared script {script!r} resolves to {target} but is not "
                "executable (no extension → it must carry the executable bit).",
            )
        return None
    tried = ", ".join(str(c) for c in candidates)
    return (
        KIND_ENVIRONMENT,
        f"preflight: declared script {script!r} does not exist (tried: {tried}). "
        "Fix the script path or the card's `script:` directive before dispatch.",
    )


def check_skills(task: Any) -> Optional[tuple[str, str]]:
    """Every skill a card pins must load and be ready to run.

    Two affirmative verdicts block, mirroring ``cron/scheduler_preflight.py``:

    * ``skill_view`` reports the skill **not found** — the card pins a name
      that does not exist in the assignee's skill roots (the worker would
      silently skip it and produce half a result);
    * the skill loads but reports ``setup_needed`` — missing prerequisites.

    Anything else (unreadable payload, unexpected shape, tool error) fails
    open: the worker's own skip handling covers it.

    ``not found`` only blocks when the skill registry can actually be
    enumerated (``find_all_skills``): an empty or unlistable root still
    answers "not found" for EVERY name — including real ones — and blocking on
    that would park cards in a fresh/isolated home for a reason that has
    nothing to do with the card. Same fail-open rule as the model catalog.
    """
    skills = _attr(task, "skills") or []
    if not isinstance(skills, (list, tuple)) or not skills:
        return None
    try:
        from tools.skills_tool import skill_view
    except Exception:
        return None  # fail-open: skills tool unavailable in this process
    registry: Optional[frozenset] = None
    try:
        from tools.skills_tool import find_all_skills

        names = {
            str(entry.get("name") or "").strip()
            for entry in find_all_skills(skip_disabled=False)
            if isinstance(entry, dict)
        }
        names.discard("")
        if names:
            registry = frozenset(names)
    except Exception:
        registry = None  # fail-open: cannot enumerate → not-found never blocks
    for name in skills:
        name = str(name).strip()
        if not name:
            continue
        try:
            payload = json.loads(skill_view(name))
        except Exception:
            continue  # unreadable → fail-open (same as the cron preflight)
        if not isinstance(payload, dict):
            continue
        if not payload.get("success"):
            error = str(payload.get("error") or "")
            if "not found" in error.lower() and registry is not None and name not in registry:
                return (
                    KIND_ENVIRONMENT,
                    f"preflight: skill {name!r} is not loaded ({error.strip()}). "
                    "Attach a skill that exists for this profile, or install it "
                    "before dispatch.",
                )
            continue  # other tool errors → fail-open (and an unverifiable not-found)
        if payload.get("setup_needed") or payload.get("readiness_status") == "setup_needed":
            missing = [
                fmt.format(skill)
                for key, fmt in _SKILL_MISSING_FIELDS
                for skill in payload.get(key) or []
            ]
            detail = ", ".join(missing) or "required setup incomplete"
            return (
                KIND_ENVIRONMENT,
                f"preflight: skill {name!r} is not ready: {detail}. Provide the missing "
                "prerequisites or detach the skill from the card.",
            )
    return None


def check_provider(task: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``(kind, reason, resolved_provider)`` — the pinned provider's credential.

    Read-only credential ladder (the same call the cron preflight makes): a
    missing key raises ``AuthError`` and blocks; a rate limit (the NIM 429
    class) is **transient** and passes; any other error fails open. The
    provider the ladder actually picked is returned so the model check can be
    provider-qualified even when the card pins no ``provider_override``.

    Runs only for a card that pins a model/provider (see module docstring).
    """
    model = str(_attr(task, "model_override", "") or "").strip()
    provider = str(_attr(task, "provider_override", "") or "").strip() or None
    if not model and not provider:
        return None, None, None
    try:
        from hermes_cli.auth import AuthError, is_rate_limited_auth_error
        from hermes_cli.runtime_provider import resolve_runtime_provider
    except Exception:
        return None, None, provider  # fail-open: credential stack unimportable here
    try:
        runtime = resolve_runtime_provider(requested=provider, target_model=model or None)
    except AuthError as exc:
        if is_rate_limited_auth_error(exc):
            return None, None, provider  # transient — never a preflight block
        return (
            KIND_ENVIRONMENT,
            f"preflight: provider credential missing: {exc}. Set the provider API key "
            "for this profile (.env / `hermes setup`) or clear the card's model pin "
            "before dispatch.",
            provider,
        )
    except Exception:
        return None, None, provider  # non-auth errors are not a missing-credential verdict
    resolved = provider
    if not resolved and isinstance(runtime, dict):
        resolved = str(runtime.get("provider") or "").strip() or None
    return None, None, resolved


def _catalog_cache_present() -> bool:
    """True when the active home carries a home-scoped model catalog.

    ``model_gate`` unions three sources: the packaged static list,
    ``<home>/cache/model_catalog.json`` and
    ``<home>/provider_models_cache.json``. Some live models (measured:
    ``minimax-m3.1-flash``, ``claude-haiku-5-5``) exist ONLY in the two
    home-scoped files, so without them "not found" means "no catalog", not
    "no model" — a false positive that would park healthy cards. Fail open in
    that environment (fresh profile, isolated test home): the worker's own
    HTTP 400 remains the backstop, and the operator sees the card start
    instead of a spurious block.
    """
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        return False
    return (
        (home / "provider_models_cache.json").is_file()
        or (home / "cache" / "model_catalog.json").is_file()
    )


def check_model(task: Any, provider: Optional[str] = None) -> Optional[tuple[str, str]]:
    """A pinned model must exist — provider-qualified when the provider is known.

    The provider qualification is what catches the measured incident:
    ``gpt-5-nano`` exists in the packaged catalog, but not on the backend the
    card pins it to, which reached the API as ``HTTP 400 Model is
    unavailable``. See :func:`_catalog_cache_present` for the fail-open rule.
    """
    model = str(_attr(task, "model_override", "") or "").strip()
    if not model:
        return None
    if not _catalog_cache_present():
        return None
    try:
        from hermes_cli import model_gate

        ok, why = model_gate.model_known(model, provider)
    except Exception:
        return None  # fail-open: catalog unavailable → the real path reports it
    if ok:
        return None
    return (
        KIND_CARD_VALUE,
        f"preflight: model_override {model!r} is not a known model for provider "
        f"{provider or 'the default provider'!r} ({why}). Fix or clear the "
        "card's model pin (`hermes kanban set-model <id> none`) before dispatch.",
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def warn_model_pin_drift(task: Any, profile_home: Optional[str] = None) -> None:
    """Layer (c) drift MONITORING — log when a card pin diverges from the pin.

    Never blocks: the write gate refuses new divergent pins and
    ``build_kanban_policy`` drops pre-existing ones, so this is the alarm that
    makes a drift visible in the dispatcher log instead of silent. Fail-open —
    an unreadable config simply produces no warning.
    """
    try:
        from hermes_cli import model_pin_drift

        drift = model_pin_drift.card_override_drift(
            getattr(task, "model_override", None),
            getattr(task, "provider_override", None),
            profile_home=profile_home,
        )
    except Exception:
        return
    if drift:
        _log.warning(
            "kanban preflight %s: %s",
            getattr(task, "id", None) or "?", drift,
        )


def preflight_task(
    task: Any,
    *,
    assignee: Optional[str] = None,
    profile_home: Optional[str] = None,
    scope_factory: Optional[Callable[[], Any]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Run every preflight check for *task*.

    Returns ``(kind, reason)`` — both ``None`` when the card may start. The
    caller (``kanban_db_dispatch._dispatch_lane_task``) parks the card
    ``blocked`` with this pair; it must never treat a preflight verdict as a
    spawn failure.

    *profile-scoped* checks (provider credential, skills, script, home) run
    inside ``scope_factory()`` — pass the assignee's
    ``_worker_profile_scope`` so they judge the environment the worker will
    actually see. The *model-existence* check runs in the caller's ambient
    scope (see module docstring). Every check fails open on its own errors.
    """

    def run(fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception:
            return None  # fail-open: a broken check never blocks a card

    holder: dict[str, Any] = {"provider": None}

    def profile_scoped_checks() -> Optional[tuple[Any, Any]]:
        verdict = run(check_home, profile_home)
        if verdict:
            return verdict
        provider_kind, provider_reason, resolved = run(check_provider, task) or (None, None, None)
        holder["provider"] = resolved
        if provider_reason:
            return provider_kind, provider_reason
        verdict = run(check_skills, task)
        if verdict:
            return verdict
        return run(check_script, task, profile_home)

    try:
        scope = scope_factory() if scope_factory is not None else contextlib.nullcontext()
        with scope:
            verdict = profile_scoped_checks()
    except Exception:
        # The profile scope itself could not be entered (unimportable scope
        # helper, secret-scope failure): retry the same read-only checks in the
        # ambient scope rather than skipping them — a degraded judgement still
        # beats no judgement, and every check fails open internally anyway.
        verdict = profile_scoped_checks()
    if verdict:
        return verdict
    verdict = run(check_model, task, holder["provider"])
    if verdict:
        return verdict
    # Layer (c): drift MONITORING on the spawn path — warn, never block.
    run(warn_model_pin_drift, task, profile_home)
    return None, None


__all__ = [
    "KIND_CARD_VALUE",
    "KIND_ENVIRONMENT",
    "check_home",
    "check_model",
    "check_provider",
    "check_script",
    "check_skills",
    "preflight_task",
    "warn_model_pin_drift",
]
