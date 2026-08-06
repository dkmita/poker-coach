"""How often one hand beats another, and what a call is therefore worth.

The second module allowed to import pokerkit, and the boundary is drawn the same
way `replay.py` draws its own: hand *evaluation* is a different pokerkit surface
(`pokerkit.hands`) from hand *replay* (`pokerkit.notation`), and folding it into
`replay.py` would turn that module into a grab bag. Nothing else imports either.

This exists to price **terminal** decisions -- the ones where calling ends the
hand, so the only continuations are call and fold and both have a closed-form
value. See `handview._terminal` for what qualifies.

    EV(call) = equity x (pot + call) - call
    EV(fold) = 0

Two arrangements of the same thing, and worth writing out because mixing them is
easy and was in fact done here:

    equity x (pot + call) - call      # your call is part of the pot you win
    equity x pot - (1 - equity) x call # ...or it is only ever at risk

They agree. What does not work is taking the first half of one and the second of
the other -- `equity x (pot + call) - (1 - equity) x call` counts your own call
as winnings and reports a 100%-equity call of 49bb into 151bb as +200bb rather
than +151bb.

`pot` throughout is the pot *before* the call, already including the bet being
called.

**This is hindsight.** Equity here is against the hand the opponent actually
showed, which is knowable only because the hand ended in a showdown. It answers
"what did this call gain or cost", not "was calling right" -- that needs the
range they would have bet, not the hand they turned out to have. Keeping those
apart is the whole reason the field is named `realised`.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass

from pokerkit.hands import StandardHighHand

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = tuple(r + s for r in RANKS for s in SUITS)

# Above this many runouts, sample instead of enumerating. pokerkit evaluates
# about 6,800 hands a second, so the flop's 990 runouts are instant and preflop's
# 1,712,304 would take eight minutes -- per hand, on a page load.
_EXACT_LIMIT = 50_000
# Enough to place a number to within roughly half a point. Deliberately not
# enough to resolve a genuinely marginal spot, which is why `exact` is reported
# alongside and the UI says "approx".
_SAMPLES = 20_000


def _cards(text: str) -> list[str]:
    return [text[i : i + 2] for i in range(0, len(text), 2)]


@dataclass(frozen=True, slots=True)
class Equity:
    """How often `hero` beats `villain`, ties counted as half."""

    equity: float
    runouts: int
    exact: bool


def hand_vs_hand(hero: str, villain: str, board: str = "", *, seed: int = 0) -> Equity:
    """Equity of `hero` against `villain` over every remaining board.

    Exact by enumeration when the runouts are few enough, otherwise sampled with
    a local RNG -- never the global one, because the synthetic corpus is
    reproducible from a seed and reaching into `random` would break that.

    A tie counts half, which is what makes a chopped pot come back 50% rather
    than 0%. `StandardHighHand` compares equal on a tie, including when the
    board plays.
    """
    known = set(_cards(hero)) | set(_cards(villain)) | set(_cards(board))
    if len(known) != len(_cards(hero)) + len(_cards(villain)) + len(_cards(board)):
        raise ValueError(f"duplicate card among {hero!r}, {villain!r}, {board!r}")
    rest = [c for c in DECK if c not in known]
    need = 5 - len(board) // 2
    if need < 0:
        raise ValueError(f"board is already longer than five cards: {board!r}")

    total = 1
    for i in range(need):
        total = total * (len(rest) - i) // (i + 1)
    exact = total <= _EXACT_LIMIT

    if exact:
        runouts: object = itertools.combinations(rest, need)
        count = total
    else:
        rng = random.Random(seed)
        runouts = (rng.sample(rest, need) for _ in range(_SAMPLES))
        count = _SAMPLES

    wins = ties = 0
    for extra in runouts:  # type: ignore[union-attr]
        full = board + "".join(extra)
        h = StandardHighHand.from_game(hero, full)
        v = StandardHighHand.from_game(villain, full)
        if h > v:
            wins += 1
        elif h == v:
            ties += 1
    return Equity(equity=(wins + ties / 2) / count, runouts=count, exact=exact)


def price_call(pot: float, call: float, equity: float) -> dict[str, float]:
    """What calling is worth, and what equity it needed to break even.

    Both in whatever unit `pot` and `call` are given in -- big blinds here, since
    this is display arithmetic and the result is never summed.
    """
    if call <= 0:
        raise ValueError("a call of nothing is a check, and free")
    needed = call / (pot + call)
    return {
        "needed": round(needed, 4),
        "ev": round(equity * (pot + call) - call, 2),
        # What folding gives up, which is the same number seen from the other
        # side: it is only ever zero, so the comparison is the EV itself.
        "edge": round(equity - needed, 4),
    }
