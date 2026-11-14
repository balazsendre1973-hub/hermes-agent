"""GIT-GUARD: a local fix commit must SURVIVE `hermes update` (t_6d485906).

MEASURED LOSS CLASS (2026-11-12/13, the live tree):
``hermes update`` has exactly ONE shape — it makes the served checkout equal
``origin/main``. Anything not reachable from ``origin/main`` leaves the served
tree:

  * a commit made on a DETACHED HEAD -> the update parks it behind
    ``refs/hermes-update-backups/detached-<branch>-<ts>-<sha12>`` and moves on
    (``hermes_cli/update_cmd.py:1059`` and ``:1416`` -> ``hermes_cli/update_cmd_git.py:112``);
  * a commit on a NAMED branch -> ``checkout -B <branch> origin/<branch>`` RESETS
    that branch's ref to the remote tip (``hermes_cli/update_cmd.py:1215``),
    or the parked-branch guard switches the served tree back to the update target
    and leaves the branch behind
    (``hermes_cli/update_cmd_git.py:178`` -> ``hermes_cli/update_cmd.py:1335``).

Two real fixes were lost this way (``3af93426fd`` review-gate, ``65f8e5cc0e``
reviewer-default): a card's green test run did NOT survive the next update.

THE USER MODEL (operator, 2026-11-13 02:33 CET):
    "Külön tartjuk nyilván a saját fejlesztéseinket, és ahhoz húzzuk hozzá a
     hermes hivatalos fejlesztéseit."
Our developments live on their own tracked branch; the official Hermes updates are
merged INTO that branch. So the served checkout must be:
    A. our dev branch, carrying our local fix commits as ANCESTORS of HEAD, and
    B. containing origin/main as an ancestor (upstream merged INTO ours).

The config knob that implements it is upstream-supported:
``updates.parked_branch_strategy: update_in_place`` ("merge origin/<target> INTO
it after leaving a pre-update-<stamp> tag; a conflict stops the update cleanly").

These tests exercise the update's OWN decision functions
(``_assess_parked_branch_switch`` -> ``_apply_parked_branch_guard``) against REAL
git repositories — init, commit, branch, clone, fetch, merge — not mocked
subprocess.run, so the verdict is the shipped code's verdict.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Imported at module level on purpose: ``hermes_cli.main`` pulls in
# ``hermes_bootstrap``, which activates the dependency generation. Importing it
# lazily (inside a test) would run that activation AFTER the home_io_guard is
# installed, and the activation reads the install's manifest.json — a path the
# guard refuses as "file I/O against the REAL hermes home".
from hermes_cli import main as hermes_main
from hermes_cli import update_cmd


GIT = ["git"]


def _git(cwd, *args, check=True):
    p = subprocess.run(GIT + list(args), check=False, cwd=cwd,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed rc={p.returncode}\n{p.stderr}")
    return p


def _out(cwd, *args):
    return (_git(cwd, *args, check=False).stdout or "").strip()


def _commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", msg)
    return _out(repo, "rev-parse", "HEAD")


@pytest.fixture()
def dev_checkout(tmp_path, monkeypatch):
    """A clone parked on OUR dev branch (``local/dev``) that carries a local fix
    commit, with ``origin/main`` one commit ahead (the update has work to do).

    Returns (checkout, origin, fix_sha).
    """
    origin = tmp_path / "origin"
    checkout = tmp_path / "checkout"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "T")
    _commit(origin, "shared.txt", "base\n", "c1")

    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    _git(checkout, "config", "user.email", "t@example.invalid")
    _git(checkout, "config", "user.name", "T")

    # OUR branch — the fix is committed here, never on a detached HEAD.
    _git(checkout, "checkout", "-qb", "local/dev")
    fix_sha = _commit(checkout, "fix_local.py", "# our fix\n", "fix: local fix (t_6d485906)")

    # Upstream moves on.
    _commit(origin, "upstream_new.txt", "new upstream\n", "upstream c2")
    _git(checkout, "fetch", "-q", "origin", "main")
    return checkout, origin, fix_sha


def _strategy(monkeypatch, value: str):
    """Point the update's config reader at a specific parked_branch_strategy.

    The reader takes the EXPLICITLY SET value from the RAW config (the file as
    written); ``load_config()`` merges ``DEFAULT_CONFIG``, which always carries
    the default ``"switch"`` and would make the root-home fallback
    unreachable. So both reads are pinned here.
    """
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"updates": {"parked_branch_strategy": value,
                              "auto_switch_parked_branch": True}},
    )
    monkeypatch.setattr(
        hermes_config,
        "read_raw_config",
        lambda: {"updates": {"parked_branch_strategy": value}},
    )


def _is_ancestor(checkout: Path, sha: str, ref: str = "HEAD") -> bool:
    return subprocess.run(GIT + ["merge-base", "--is-ancestor", sha, ref], check=False,
                          cwd=checkout, capture_output=True).returncode == 0


def _run_update_path(checkout: Path, monkeypatch, strategy: str) -> dict:
    """Run the update's real branch-switch decision, then apply its verdict.

    ``update_in_place`` -> the update merges ``origin/main`` INTO the checked-out
    branch (the checkout never moves).  ``switch`` -> the update moves the served
    tree to the update target (the sweep this card closes).
    """
    _strategy(monkeypatch, strategy)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", checkout)

    current = _out(checkout, "branch", "--show-current")
    safe, reason = update_cmd._assess_parked_branch_switch(GIT, checkout, current, "main")
    switched, in_place, block_reason = update_cmd._apply_parked_branch_guard(
        GIT, "main", current, switch_branch=False, _windows_gateway_resume=None)

    if in_place:
        _git(checkout, "merge", "--no-edit", "origin/main")
    elif switched:
        _git(checkout, "checkout", "main")
        _git(checkout, "reset", "--hard", "origin/main")
    return {"safe": safe, "reason": reason, "switched": switched,
            "in_place": in_place, "block_reason": block_reason}


def test_local_fix_survives_the_update_in_place(dev_checkout, monkeypatch):
    """The structural fix: upstream is merged INTO our branch, the fix stays served."""
    checkout, _origin, fix_sha = dev_checkout

    assert _out(checkout, "branch", "--show-current") == "local/dev"
    assert _is_ancestor(checkout, fix_sha), "precondition: the fix is served"

    plan = _run_update_path(checkout, monkeypatch, "update_in_place")

    assert plan["in_place"] is True, "the update must merge in place, not switch"
    assert plan["switched"] is False, "the served checkout must not move"
    assert _out(checkout, "branch", "--show-current") == "local/dev"
    assert _is_ancestor(checkout, fix_sha), "the fix MUST still be an ancestor of HEAD"
    assert _is_ancestor(checkout, "origin/main"), "upstream MUST be merged INTO our branch"


def test_default_switch_strategy_sweeps_the_local_fix_out(dev_checkout, monkeypatch):
    """NEGATIVE CONTROL: the default ``switch`` strategy is what lost the fixes.

    This pins the LOSS, so a future change to the default that keeps the fix
    served cannot pass silently — it would have to update this expectation.
    """
    checkout, _origin, fix_sha = dev_checkout

    plan = _run_update_path(checkout, monkeypatch, "switch")

    assert plan["in_place"] is False
    assert plan["switched"] is True, "the default strategy switches the served tree"
    assert not _is_ancestor(checkout, fix_sha), (
        "the default strategy is expected to drop the fix from the served tree "
        "(this is the loss class t_6d485906 closes)")


def test_fix_commit_must_never_be_made_on_a_detached_head(dev_checkout, monkeypatch):
    """A commit made on a detached HEAD is what the update parks and abandons.

    The served checkout must be on a NAMED branch, and the fix must be reachable
    from that branch — the rule the instruction layer names.
    """
    checkout, _origin, fix_sha = dev_checkout

    branch = _out(checkout, "branch", "--show-current")
    assert branch and branch != "HEAD", "the served checkout must be on a named branch"

    containing = _out(checkout, "for-each-ref", "--contains", fix_sha,
                      "--format=%(refname)")
    assert containing, "the fix commit must be reachable from a ref (never dangling)"


def test_update_in_place_reports_the_branch_it_updates(dev_checkout, monkeypatch, capsys):
    """The update must SAY which branch it updates in place (no silent move)."""
    checkout, _origin, _fix_sha = dev_checkout

    _run_update_path(checkout, monkeypatch, "update_in_place")

    out = capsys.readouterr().out
    assert "local/dev" in out, f"the update must name the branch it updates: {out!r}"
    assert "updating it in place" in out, f"the in-place notice is missing: {out!r}"


def test_dirty_tree_does_not_block_the_in_place_update(dev_checkout, monkeypatch, capsys):
    """A dirty tree must not skip the update when the checkout never moves.

    The "dirty" refusal exists so uncommitted work cannot ride an autostash across a
    BRANCH boundary. An in-place update merges origin/main INTO the same branch and the
    checkout never moves, so nothing crosses that boundary — refusing there skipped the
    whole update over one dirty file (t_6d485906).
    """
    checkout, _origin, _fix_sha = dev_checkout
    (checkout / "dirty_local.py").write_text("# uncommitted work\n", encoding="utf-8")

    safe, reason = update_cmd._assess_parked_branch_switch(GIT, checkout, "local/dev", "main")
    assert (safe, reason) == (False, "dirty"), "the assessment itself still reports dirty"

    _strategy(monkeypatch, "update_in_place")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", checkout)
    current = _out(checkout, "branch", "--show-current")
    switched, in_place, _block = update_cmd._apply_parked_branch_guard(
        GIT, "main", current, switch_branch=False, _windows_gateway_resume=None)

    assert in_place is True, "the in-place update must proceed on a dirty tree"
    assert switched is False, "the served checkout must not move"

    out = capsys.readouterr().out
    assert "CODE UPDATE SKIPPED" not in out, f"the update must not be skipped: {out!r}"
    assert "uncommitted changes" in out, f"the dirty tree must be named: {out!r}"


def test_dirty_tree_still_blocks_the_switch_strategy(dev_checkout, monkeypatch, capsys):
    """NEGATIVE CONTROL: with the default strategy a dirty tree is still refused.

    The default strategy DOES move the checkout, so the cross-branch autostash hazard is
    real there and the refusal must stay.
    """
    checkout, _origin, _fix_sha = dev_checkout
    (checkout / "dirty_local.py").write_text("# uncommitted work\n", encoding="utf-8")

    _strategy(monkeypatch, "switch")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", checkout)
    current = _out(checkout, "branch", "--show-current")

    with pytest.raises(SystemExit) as exc:
        update_cmd._apply_parked_branch_guard(
            GIT, "main", current, switch_branch=False, _windows_gateway_resume=None)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "CODE UPDATE SKIPPED" in out, f"the dirty tree must still block the switch: {out!r}"


def test_profile_scoped_update_reads_the_root_home_strategy(monkeypatch, tmp_path):
    """A PROFILE-scoped update must see the ROOT home's branch strategy.

    The shared checkout serves every profile, so the branch policy is a ROOT-level
    decision. Measured 2026-11-13 04:5x CET: a profile-scoped update read only its
    OWN ``config.yaml`` — where the key is absent — and fell back to the default
    ``parked_branch_strategy: "switch"``, the strategy that moves the served tree
    off the local dev branch and drops the local fix commits (t_6d485906).

    The EXPLICITLY SET value decides, so the RAW file is what the reader consults:
    ``load_config()`` merges ``DEFAULT_CONFIG``, which always carries the default
    ``"switch"`` and would make the root fallback unreachable.
    """
    import hermes_cli.config as hermes_config

    # The profile's own config says NOTHING about the strategy (the common case).
    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {"updates": {"parked_branch_strategy": "switch",
                              "auto_switch_parked_branch": True}},
    )
    monkeypatch.setattr(hermes_config, "read_raw_config", lambda: {"updates": {}})

    root_config = _root_config(monkeypatch, tmp_path)
    assert root_config["updates"]["parked_branch_strategy"] == "update_in_place"

    assert update_cmd._updates_config()["parked_branch_strategy"] == "update_in_place", (
        "a profile-scoped update must take the root home's strategy; the default "
        "'switch' sweeps the local fix commits out of the served tree"
    )


def test_profile_scoped_update_keeps_its_own_explicit_strategy(monkeypatch, tmp_path):
    """The profile's OWN explicit value still wins over the root's."""
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(
        hermes_config, "load_config",
        lambda: {"updates": {"parked_branch_strategy": "switch"}},
    )
    monkeypatch.setattr(
        hermes_config, "read_raw_config",
        lambda: {"updates": {"parked_branch_strategy": "switch"}},
    )
    _root_config(monkeypatch, tmp_path)

    assert update_cmd._updates_config()["parked_branch_strategy"] == "switch"


def _root_config(monkeypatch, tmp_path):
    """Point the reader at a root home whose config sets update_in_place."""
    import hermes_cli.config as hermes_config

    root_home = Path(tmp_path) / "root-home"
    root_home.mkdir(parents=True, exist_ok=True)
    doc = {"updates": {"parked_branch_strategy": "update_in_place"}}
    monkeypatch.setattr(
        hermes_config, "read_config_doc", lambda _path: doc)
    monkeypatch.setattr(
        "hermes_constants.get_default_hermes_root", lambda *a, **k: root_home)
    return doc
