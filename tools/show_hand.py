#!/usr/bin/env python3
"""Render one hand as text or JSON.

    .venv/bin/python tools/show_hand.py archive/synthetic/hand_00002.phh
    .venv/bin/python tools/show_hand.py archive/synthetic/hand_00002.phh --json

The JSON form is the contract a UI consumes: facts always present, `gto` and
`analysis` null until a provider or the analyze stage fills them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from poker_coach import handview
from poker_coach.replay import ReplayError, load
from poker_coach.solvers.base import NullProvider


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path)
    ap.add_argument("--json", action="store_true", help="emit the UI contract")
    args = ap.parse_args()

    try:
        hh = load(args.path)
    except ReplayError as exc:
        print(f"cannot replay: {exc}")
        return 1

    view = handview.build(hh, provider=NullProvider())
    print(json.dumps(view, indent=2) if args.json else handview.render_text(view))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
