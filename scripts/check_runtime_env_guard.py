#!/usr/bin/env python3
"""Guard-presence monitor for the (d) stability layer (t_55e6fd81).

Olvasó-only ellenőrzés a FUTÓ checkoutban: a
``_check_runtime_env_path`` / ``_runtime_env_protection_reason``
(tools/file_tools_write_guards.py) és a ``_HERMES_RUNTIME_ENV_PATH``
(tools/approval_detection.py) jelenlétét ellenőrzi.

Kilési kód:
  0 = ZÖLD (mindkét réteg szimbóluma létezik a futó checkoutban)
  1 = S1  (bármelyik szimbólom hiányzik -> a (d) írásvédelem nem él)

Iratkoztatva a kártya t_43361aa1 megelőzési kapuja ("monitor" réteg).
Nem ír semmit, csak olvashat.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (fájl, kötelező szimbólum) párok a (d) rétegből
CHECKS = (
    (ROOT / "tools" / "file_tools_write_guards.py", "_check_runtime_env_path"),
    (ROOT / "tools" / "file_tools_write_guards.py", "_runtime_env_protection_reason"),
    (ROOT / "tools" / "approval_detection.py", "_HERMES_RUNTIME_ENV_PATH"),
)


def main() -> int:
    missing = []
    for path, symbol in CHECKS:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            missing.append(f"{path}: olvasási hiba ({exc})")
            continue
        if symbol not in text:
            missing.append(f"{path}: {symbol}")

    if missing:
        print(
            "S1: (d) stabilitási réteg guard hiányzik a futó checkoutbol "
            "(irasvedelmi regreszió):",
            file=sys.stderr,
        )
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        return 1

    print("ZOLD: (d) stabilitasi reteg guardjai jelen a futo checkoutban")
    for path, symbol in CHECKS:
        print(f"  {path}: {symbol} OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
