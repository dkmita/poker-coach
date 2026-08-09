#!/usr/bin/env python3
"""Estimate opponent ranges for the decisions in a set of hands.

    PYTHONPATH=src .venv/bin/python tools/estimate_ranges.py --filter interesting

Two model calls per distinct spot -- equilibrium, then this pool -- and the
answers are cached on disk by spot, so re-running costs nothing for spots
already done. Only decisions facing a bet are asked about: with no bet there is
no single opponent whose range the question is about.

Nothing here is required for the UI to work. A spot with no cached range renders
without one.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from poker_coach import handview
from poker_coach.agent.describe import describe
from poker_coach.agent.ranges import RangeEstimator
from poker_coach.agent.store import RangeStore, heuristics_digest
from poker_coach.heuristics import Heuristics
from poker_coach.llm import Budget, ProxyLLM
from poker_coach.replay import ReplayError, load
from poker_coach.solvers.ranges import ChartProvider


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--archive", type=Path, default=Path("archive/acr"))
    ap.add_argument("--charts", type=Path, default=Path("charts"))
    ap.add_argument("--heuristics", type=Path, default=Path("heuristics"))
    ap.add_argument("--out", type=Path, default=Path("ranges"))
    ap.add_argument(
        "--filter", default="interesting", choices=("interesting", "terminal", "all")
    )
    ap.add_argument("--max-requests", type=int, default=400)
    ap.add_argument("--dry-run", action="store_true", help="list the work, call nothing")
    args = ap.parse_args()

    provider = ChartProvider(args.charts)
    heuristics = Heuristics(args.heuristics)
    store = RangeStore(args.out)
    llm = ProxyLLM()
    estimator = RangeEstimator(
        llm=llm, heuristics=heuristics, budget=Budget(max_requests=args.max_requests)
    )
    digest = heuristics_digest(heuristics.prompt())

    # Distinct spots first, so a spot shared by several hands is asked once.
    wanted: dict[tuple[str, str, str], str] = {}
    for path in sorted(args.archive.glob("*.phh")):
        try:
            view = handview.build(load(path), provider=provider)
        except ReplayError:
            continue
        if args.filter == "interesting" and not view["interest"]["interesting"]:
            continue
        if args.filter == "terminal" and not view["terminal"]:
            continue
        for d in view["hero_decisions"]:
            board = "".join(d["board"])
            # Hero's own range is defined at every decision; the opponent's only
            # where there is one opponent, which means facing a bet.
            for kind in ("hero", "opponent"):
                text = describe(view, d["action_index"], kind)
                if text:
                    wanted.setdefault((d["spot_key"], board, kind), text)

    todo = [k for k in wanted if store.get(*k) is None]
    print(f"{len(wanted)} distinct spots, {len(todo)} not yet cached "
          f"({2 * len(todo)} calls)")
    if args.dry_run:
        for spot, board, kind in todo:
            print(f"  {kind:<8} {spot}  {board or '-'}")
        return 0

    done = failed = 0
    started = time.time()
    for spot, board, kind in todo:
        pair = estimator.estimate(spot, wanted[(spot, board, kind)], board, kind)
        if pair.gto is None:
            failed += 1
            print(f"  miss {kind} {spot} {board or '-'}: "
                  f"{getattr(llm, 'last_error', '') or 'unparseable twice'}")
            continue
        store.put(spot, board, {
            "spot_key": spot,
            "board": board,
            "kind": kind,
            "model": pair.gto.model,
            "heuristics_digest": digest,
            "gto": pair.gto.weights,
            "gto_raw": pair.gto.raw,
            "exploit": pair.exploit.weights if pair.exploit else None,
            "exploit_raw": pair.exploit.raw if pair.exploit else None,
            "drift_combos": round(pair.drift(), 1),
        })
        done += 1
        print(f"  {done + failed:>3}/{len(todo)}  {kind:<8} {spot} {board or '-'}  "
              f"drift {pair.drift():.0f}")
    print(f"\n{done} stored, {failed} missed, {time.time() - started:.0f}s, "
          f"{estimator.budget.requests} calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
