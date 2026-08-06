#!/usr/bin/env python3
"""Convert ACR session files into a .phh archive.

    .venv/bin/python tools/ingest_acr.py ~/Downloads/AmericasCardroom/handHistory \
        --out archive/acr

Idempotent: a hand is written to `<site>-<hand_id>.phh`, so re-running over the
same export overwrites rather than duplicating. Unconvertible hands are reported
and skipped -- one bad hand must not cost the session.
"""

from __future__ import annotations

import argparse
import collections
from pathlib import Path

from poker_coach.ingest.parsers.acr import SITE, ParseError, dumps_phh, parse


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path, help="a .txt file or a directory of them")
    ap.add_argument("--out", type=Path, default=Path("archive/acr"))
    args = ap.parse_args()

    files = (
        sorted(args.source.rglob("*.txt")) if args.source.is_dir() else [args.source]
    )
    if not files:
        print(f"no .txt files under {args.source}")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)

    written, skipped = 0, collections.Counter()
    for f in files:
        for hand_id, result in parse(f.read_text(errors="replace"), source_file=f.name):
            if isinstance(result, ParseError):
                reason = "dead blind" if "dead blind" in str(result) else "replay failed"
                skipped[reason] += 1
                print(f"  skip {hand_id}: {str(result).split(': ', 1)[-1]}")
                continue
            (args.out / f"{SITE}-{hand_id}.phh").write_text(dumps_phh(result))
            written += 1

    print(f"\n  {written} hands -> {args.out}")
    if skipped:
        total = sum(skipped.values())
        print(f"  {total} skipped: " + ", ".join(f"{v} {k}" for k, v in skipped.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
