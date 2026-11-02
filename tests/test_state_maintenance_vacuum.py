"""Regression tests for ``SessionMaintenanceMixin.maybe_auto_prune_and_vacuum`` VACUUM gating.

Covers the VACUUM admission logic around ``_freelist_ratio()`` (PRAGMA
``freelist_count / page_count``) without touching any real database: every
method the mixin reaches for is mocked, ``_freelist_ratio()`` and the
``VACUUM`` execution path are stubbed, and no production state.db is opened.

These tests encode the INTENDED post-fix behavior and are expected to FAIL
against the current buggy code until the fix lands:

* test_freelist_ratio_none_skips_vacuum        -- ratio None must not VACUUM (bug: it runs)
* test_close_only_run_with_grown_freelist_vacuums -- pruned == 0 must still VACUUM (bug: skipped)

Run from the repo root:

    python -m pytest tests/test_state_maintenance_vacuum.py -v

(Equivalent: ``python -m pytest tests/ -v -k vacuum`` from the repo root.)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from hermes_state_maintenance import SessionMaintenanceMixin

# AUTO_VACUUM_MIN_FREELIST_RATIO == 0.25 (hermes_state_common); tests pass the
# threshold explicitly so they do not depend on the constant's value.
THRESHOLD = 0.25


def _build_fake(*, pruned=3, closed=2, ratio=0.5, last_auto_prune=None, last_vacuum=None):
    """Fake SessionDB stand-in whose maintenance-touching methods are recorded mocks."""
    call_order = []

    def sweep(*args, **kwargs):
        call_order.append("sweep")
        return [f"sid-{i}" for i in range(closed)]

    def freelist_ratio(*args, **kwargs):
        call_order.append("freelist_ratio")
        return ratio

    meta = {"last_auto_prune": last_auto_prune, "last_vacuum": last_vacuum}
    fake = SimpleNamespace(
        db_path="/tmp/scratch/fake-state.db",
        _lock=None,
        _AUTO_PRUNE_STALE_OPEN_SOURCES=[],
        get_meta=mock.Mock(side_effect=lambda key: meta.get(key)),
        set_meta=mock.Mock(return_value=None),
        prune_sessions=mock.Mock(return_value=pruned),
        sweep_orphaned_sessions=mock.Mock(side_effect=sweep),
        _freelist_ratio=mock.Mock(side_effect=freelist_ratio),
        vacuum=mock.Mock(return_value=None),
    )
    return fake, call_order


def _run_maintenance(fake, *, ratio_threshold=THRESHOLD, **kwargs):
    with (
        mock.patch(
            "hermes_state_repair._try_acquire_auto_maintenance_lock", return_value=object()
        ),
        mock.patch("hermes_state_repair._release_auto_maintenance_lock"),
        mock.patch("hermes_state_maintenance.report_startup_progress"),
        mock.patch("hermes_state_holders.foreign_state_db_holders", return_value=[]),
        mock.patch("hermes_state_holders.in_process_state_db_holders", return_value=[]),
    ):
        return SessionMaintenanceMixin.maybe_auto_prune_and_vacuum(
            fake,
            retention_days=90,
            min_interval_hours=24,
            vacuum=True,
            min_vacuum_interval_days=30,
            min_vacuum_freelist_ratio=ratio_threshold,
            **kwargs,
        )


def test_freelist_ratio_none_skips_vacuum():
    """1. _freelist_ratio() returns None (PRAGMA read fails) -> VACUUM must NOT run.

    Current code runs VACUUM here (``ratio is None or ...`` admits it); this is
    the bug under test.
    """
    fake, _call_order = _build_fake(pruned=3, closed=0, ratio=None)
    result = _run_maintenance(fake)
    fake.vacuum.assert_not_called()
    assert result["vacuumed"] is False


def test_freelist_ratio_at_or_below_threshold_skips_vacuum():
    """2. Ratio <= min_vacuum_freelist_ratio -> VACUUM must NOT run."""
    fake, _call_order = _build_fake(pruned=3, closed=0, ratio=0.25)
    result = _run_maintenance(fake, ratio_threshold=0.25)
    fake.vacuum.assert_not_called()
    assert result["vacuumed"] is False


def test_freelist_ratio_above_threshold_runs_vacuum():
    """3. Ratio > min_vacuum_freelist_ratio and vacuum_due -> VACUUM runs."""
    fake, _call_order = _build_fake(pruned=3, closed=0, ratio=0.5)
    result = _run_maintenance(fake, ratio_threshold=0.25)
    fake.vacuum.assert_called_once()
    assert result["vacuumed"] is True


def test_freelist_ratio_sampled_after_stale_open_sweep():
    """4. The ratio is sampled AFTER the stale-open connection sweep."""
    fake, call_order = _build_fake(pruned=3, closed=2, ratio=0.5)
    _run_maintenance(fake, ratio_threshold=0.25)
    assert call_order == ["sweep", "freelist_ratio"], call_order
    fake.sweep_orphaned_sessions.assert_called_once()
    fake._freelist_ratio.assert_called_once()


def test_close_only_run_with_grown_freelist_vacuums():
    """5. Close-only run (pruned == 0) with a grown freelist and vacuum_due -> VACUUM runs.

    Secondary finding: VACUUM admission must not require this run to have pruned
    rows; freelist grown by earlier deletes still justifies a rewrite.
    """
    fake, _call_order = _build_fake(pruned=0, closed=1, ratio=0.5)
    result = _run_maintenance(fake, ratio_threshold=0.25)
    fake.vacuum.assert_called_once()
    assert result["vacuumed"] is True
