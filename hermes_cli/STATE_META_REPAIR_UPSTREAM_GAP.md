# Upstream gap: orphan LOCAL state_meta receipt repair (commit 123a778fe5)

## What the local commit adds

Commit `123a778fe5` — `fix(sessions): repair-profiles settles orphan LOCAL state_meta receipts (t_74706f50)` — is present on the local branch `feature/standalone-only-platform` and is an ancestor of `HEAD`.

It teaches `hermes sessions repair-profiles` to scan `state_meta` for LOCAL creation receipts (`gateway.local_policy.v1:<sid>`) whose `receipt.profile_id` names a profile other than the store's own home. Those receipts are refused by every boot with `profile_mismatch`, while the guarded session row only reports `unclaimed_namespace` with `action: null`. The commit adds a `--orphan-receipts` option (`retire` = default drops the receipt and keeps the transcript as history, `rekey` moves the receipt onto this store's profile keeping the session id, `report` leaves them alone) plus `_retire_orphan_receipt`, `_rekey_orphan_receipt`, `_rekey_profile_refs`, and `_scan_state_meta_receipts`, re-reading `_move_routing_row` inside the transaction so a same-apply fix can't resurrect a stale subject.

Files touched: `hermes_cli/sessions_repair_profiles.py`, `hermes_cli/sessions_cmd_repair_profiles.py`, `hermes_cli/subcommands/sessions.py`, `tests/hermes_cli/test_sessions_repair_profiles_state_meta.py`.

## origin/main does NOT have this

- `git merge-base --is-ancestor 123a778fe5 origin/main` → NOT an ancestor.
- `git branch -r --contains 123a778fe5` → no remote branch contains it.
- `origin/main` (currently `a9ff9799e2`) has state_meta rekeying for profile-move session ids, but **no orphan-LOCAL-receipt repair and no `--orphan-receipts` in `repair-profiles`**.

## WARNING — `hermes update` must not remove this commit

A future `hermes update` that merges/checks out/resets to `origin/main` will **drop** commit `123a778fe5` because upstream does not contain it. If the local branch is fast-forwarded or hard-reset to `origin/main`, the orphan-receipt repair is lost and `repair-profiles` reverts to the `unclaimed_namespace` dead end (`action: null`), so every boot refuses those receipts with `profile_mismatch`.

**Do not let an update discard `123a778fe5`.** If an update removes it, re-apply it:

```
git cherry-pick 123a778fe5   # from a local ref / backup that still holds it
```

or re-introduce the `--orphan-receipts` scan/fix/`_move_routing_row` re-read into the three `hermes_cli` files above.

## Verification (run after every `hermes update`)

1. Commit still in local history:
   ```
   git merge-base --is-ancestor 123a778fe5 HEAD && echo PRESERVED || echo LOST
   ```
2. Repair code present in the working tree:
   ```
   grep -R "_scan_state_meta_receipts\|orphan_receipts\|_retire_orphan_receipt\|_rekey_orphan_receipt" \
     hermes_cli/sessions_repair_profiles.py hermes_cli/sessions_cmd_repair_profiles.py \
     hermes_cli/subcommands/sessions.py
   ```
3. Tests still pass:
   ```
   python -m pytest tests/hermes_cli/test_sessions_repair_profiles_state_meta.py -q
   ```

If any check fails, the update removed the local repair — restore commit `123a778fe5` before resuming.
