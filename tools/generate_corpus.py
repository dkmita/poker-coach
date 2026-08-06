#!/usr/bin/env python3
"""Generate a synthetic .phh corpus for developing the pipeline.

    .venv/bin/python tools/generate_corpus.py --count 200 --out archive/synthetic

Hands are *played* with pokerkit rather than templated, so every one is legal by
construction — pokerkit rejects an illegal betting sequence, which means a hand
that generates is a hand that replays.

The point isn't realistic poker. It's a corpus where **we know the right answer**.
Hero plays a simple positional strategy into which specific mistakes are
deliberately planted, and every planted mistake is recorded in `manifest.json`
against the ordinal of hero's decision within that hand:

    {"file": "hand_00007.phh", "hero_index": 3, "planted": [
        {"hero_decision": 0, "leak": "bb_overfold", "detail": "folded Kd9d vs BTN 2.5x"}]}

That gives triage a **recall** measure, not just the precision that
`v_detector_precision` reports from live data. Precision alone can't see the
mistakes a detector never flagged; planted ground truth can. A detector that
scores 95% precision while missing four of five planted `bb_overfold`s is not
working, and only this corpus can tell you that.

What this does NOT do is help write the ACR parser. These are already PHH files;
the parser's job is converting ACR's undocumented text format *into* PHH, and
that needs real samples. Nothing here substitutes for them.
"""

from __future__ import annotations

import argparse
import json
import random
from functools import partial
from pathlib import Path

from pokerkit import Automation, Mode, NoLimitTexasHoldem
from pokerkit.notation import HandHistory, rake

# Everything except the betting decisions is automated, so the policy below only
# ever has to answer "what does this player do now".
AUTOMATIONS = (
    Automation.ANTE_POSTING,
    Automation.BET_COLLECTION,
    Automation.BLIND_OR_STRADDLE_POSTING,
    Automation.CARD_BURNING,
    Automation.HOLE_DEALING,
    Automation.BOARD_DEALING,
    Automation.RUNOUT_COUNT_SELECTION,
    Automation.HOLE_CARDS_SHOWING_OR_MUCKING,
    Automation.HAND_KILLING,
    Automation.CHIPS_PUSHING,
    Automation.CHIPS_PULLING,
)

SB, BB = 50, 100  # cents; $0.50/$1.00
PLAYERS = 6

# Roughly ACR-shaped: 5% capped at 3bb, nothing raked in an unopened pot. Exact
# numbers don't matter yet -- what matters is that rake is non-zero and
# reconstructable from finishing stacks, so the ingest path gets exercised.
RAKE = partial(rake, percentage=0.05, cap=3 * BB, no_flop_no_drop=True)

RANK_ORDER = "23456789TJQKA"


def hand_strength(cards: list) -> float:
    """Crude 0..1 preflop score. Not an equity model — just monotonic enough to
    order hands sensibly, which is all the thresholds below need."""
    ranks = sorted((RANK_ORDER.index(c.rank.value) for c in cards), reverse=True)
    hi, lo = ranks
    suited = cards[0].suit == cards[1].suit
    if hi == lo:  # pair
        return 0.70 + 0.30 * (hi / 12)
    gap = hi - lo
    score = 0.30 * (hi / 12) + 0.20 * (lo / 12)
    score += 0.10 if suited else 0.0
    score += max(0.0, 0.12 - 0.03 * gap)
    return min(score, 0.95)


def _score_distribution() -> list[float]:
    """Every 169 starting hands, weighted by combination count."""
    scores = []
    for i, hi in enumerate(RANK_ORDER):
        for j, lo in enumerate(RANK_ORDER[: i + 1]):
            if hi == lo:
                combos, variants = 6, [(True, False)]
            else:
                combos, variants = 4, [(False, True), (False, False)]
            for _, suited in variants:
                weight = 4 if suited else (12 if hi != lo else 6)
                s = _score_from_ranks(i, j, suited)
                scores.extend([s] * weight)
    return sorted(scores)


def _score_from_ranks(hi: int, lo: int, suited: bool) -> float:
    if hi == lo:
        return 0.70 + 0.30 * (hi / 12)
    gap = hi - lo
    score = 0.30 * (hi / 12) + 0.20 * (lo / 12) + (0.10 if suited else 0.0)
    return min(score + max(0.0, 0.12 - 0.03 * gap), 0.95)


_SORTED_SCORES = _score_distribution()


def _threshold(top_fraction: float) -> float:
    """Score cutoff that admits roughly the top `top_fraction` of hands.

    Thresholds are expressed as frequencies rather than magic numbers because
    hand-picked ones silently stop meaning anything the moment `hand_strength`
    changes — which is exactly what happened on the first pass: every cutoff sat
    above the best unpaired hand, so only pocket pairs ever opened and 92% of
    hands ended preflop.
    """
    idx = int((1.0 - top_fraction) * (len(_SORTED_SCORES) - 1))
    return _SORTED_SCORES[idx]


# Open-raise frequencies in the rough shape of a 6-max cash baseline.
RFI_THRESHOLD = {
    "UTG": _threshold(0.18),
    "HJ": _threshold(0.23),
    "CO": _threshold(0.30),
    "BTN": _threshold(0.45),
    "SB": _threshold(0.38),
    "BB": _threshold(0.35),
}
THREE_BET = _threshold(0.07)
CALL_RAISE = _threshold(0.28)
# The big blind is already half-invested and closing the action, so it continues
# far wider than any other seat. This is the floor for "should have defended",
# and it has to be a real floor: label a fold of the bottom of the deck as a
# mistake and the manifest stops being ground truth.
BB_DEFEND = _threshold(0.45)


def made_strength(hole: list, board: list) -> float:
    """0..1 postflop score from actual board interaction.

    Preflop score alone would leave a hand that completely missed still 'strong',
    which produces nonsense postflop lines. Cheap category detection is enough
    here — the corpus needs plausible action, not correct strategy.
    """
    if len(board) < 3:
        return hand_strength(hole)
    hole_ranks = [RANK_ORDER.index(c.rank.value) for c in hole]
    board_ranks = [RANK_ORDER.index(c.rank.value) for c in board]
    suits = [c.suit for c in hole] + [c.suit for c in board]

    counts = {r: (hole_ranks + board_ranks).count(r) for r in set(hole_ranks + board_ranks)}
    paired_with_board = [r for r in hole_ranks if r in board_ranks]
    best = max(counts.values())
    flush = max(suits.count(s) for s in set(suits)) >= 5
    flush_draw = max(suits.count(s) for s in set(suits)) == 4

    if flush or best >= 4:
        return 0.97
    if best == 3:
        return 0.90
    if len(paired_with_board) >= 2:
        return 0.82  # two pair
    if paired_with_board:
        top_board = max(board_ranks)
        pair_rank = max(paired_with_board)
        return 0.74 if pair_rank >= top_board else 0.58  # top pair vs weaker pair
    if hole_ranks[0] == hole_ranks[1]:  # pocket pair, unimproved
        return 0.66 if hole_ranks[0] > max(board_ranks) else 0.48
    if flush_draw:
        return 0.52
    return 0.20 + 0.15 * (max(hole_ranks) / 12)  # air, with high-card kicker value


def position_names(hero: int) -> str:
    layout = ("SB", "BB", "UTG", "HJ", "CO", "BTN")
    return layout[hero]


class Policy:
    """Villain baseline. Tight-ish, position-aware, no attempt at balance."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    # Cap on voluntary re-raises per street. Without it two players who both
    # clear the 3-bet threshold min-raise at each other indefinitely
    # (250 -> 450 -> 650 -> ...), producing 26-decision hands that are legal,
    # replayable, and nothing like poker.
    MAX_RAISES_PER_STREET = 3

    def act(self, state, seat: int, strength: float, position: str) -> None:
        to_call = state.checking_or_calling_amount or 0
        can_raise = (
            state.can_complete_bet_or_raise_to()
            and state.completion_betting_or_raising_count < self.MAX_RAISES_PER_STREET
        )

        if state.street_index == 0:
            self._preflop(state, strength, position, to_call, can_raise)
        else:
            self._postflop(state, strength, to_call, can_raise)

    def _preflop(self, state, strength, position, to_call, can_raise) -> None:
        opened = to_call > BB
        if not opened:
            if strength >= RFI_THRESHOLD[position] and can_raise:
                self._raise_to(state, int(2.5 * BB))
            else:
                state.check_or_call() if to_call == 0 else state.fold()
            return
        # Facing a raise.
        if strength >= THREE_BET and can_raise:
            self._raise_to(state, int(to_call * 3))
        elif strength >= CALL_RAISE:
            state.check_or_call()
        else:
            state.fold()

    def _postflop(self, state, strength, to_call, can_raise) -> None:
        r = self.rng.random()
        if to_call == 0:
            if strength >= 0.70 and r < 0.65 and can_raise:
                self._raise_to(state, max(BB, state.total_pot_amount // 2))
            else:
                state.check_or_call()
            return
        if strength >= 0.85 and can_raise and r < 0.35:
            self._raise_to(state, to_call * 3)
        elif strength >= 0.55:
            state.check_or_call()
        else:
            state.fold()

    @staticmethod
    def _raise_to(state, amount: int) -> None:
        lo = state.min_completion_betting_or_raising_to_amount
        hi = state.max_completion_betting_or_raising_to_amount
        state.complete_bet_or_raise_to(max(lo, min(amount, hi)))


class HeroPolicy(Policy):
    """Hero, with specific mistakes planted on purpose.

    Each leak is one a detector should be able to catch from charts and pot odds
    alone, so triage can be developed and measured against them before any solver
    or agent exists.
    """

    def __init__(self, rng: random.Random):
        super().__init__(rng)
        self.planted: list[dict] = []
        self.decision_ordinal = -1

    def act(self, state, seat: int, strength: float, position: str) -> None:
        self.decision_ordinal += 1
        to_call = state.checking_or_calling_amount or 0

        # Leak 1: over-folding the big blind to a single late-position open. The
        # canonical low-stakes leak, and cheap to detect from a chart.
        if (
            state.street_index == 0
            and position == "BB"
            and 0 < to_call <= int(2.5 * BB)
            and BB_DEFEND <= strength < THREE_BET
            and self.rng.random() < 0.85
        ):
            self._plant("bb_overfold", f"folded {strength:.2f}-strength vs {to_call}c open")
            state.fold()
            return

        # Leak 2: calling a river bet laying worse odds than the hand can be
        # good. Detectable from pot odds without any range assumption.
        river_odds = (
            to_call / (state.total_pot_amount + to_call) if to_call > 0 else 0.0
        )
        if (
            state.street_index == 3
            and to_call > 0
            and strength < 0.45
            and river_odds > 0.28  # laying worse than ~2.5:1 with a weak holding
            and self.rng.random() < 0.85
        ):
            odds = river_odds
            self._plant("river_overcall", f"called {odds:.0%} pot odds with weak holding")
            state.check_or_call()
            return

        # Leak 3: peeling the flop with nothing. Planted partly for its own sake
        # -- it is a real and common leak -- and partly because without it hero
        # never reaches the river holding air, so the river_overcall branch above
        # could never fire. A planted leak with no reachable path is ground truth
        # that silently measures nothing.
        if (
            state.street_index == 1
            and to_call > 0
            and strength < 0.35
            and to_call <= state.total_pot_amount
            and self.rng.random() < 0.55
        ):
            self._plant("flop_float_with_air", f"called {to_call}c on the flop with air")
            state.check_or_call()
            return

        # Leak 4: opening far too wide from under the gun.
        if (
            state.street_index == 0
            and position == "UTG"
            and to_call == BB
            and strength < RFI_THRESHOLD['UTG']
            and state.can_complete_bet_or_raise_to()
            and self.rng.random() < 0.35
        ):
            self._plant("utg_open_too_wide", f"opened {strength:.2f}-strength from UTG")
            self._raise_to(state, int(2.5 * BB))
            return

        super().act(state, seat, strength, position)

    def _plant(self, leak: str, detail: str) -> None:
        self.planted.append(
            {"hero_decision": self.decision_ordinal, "leak": leak, "detail": detail}
        )


def generate_hand(rng: random.Random, hero: int, hand_no: int) -> tuple[str, list[dict]]:
    depth_bb = rng.choice((80, 100, 100, 100, 125, 150))  # 100bb weighted
    stacks = tuple(int(depth_bb * BB) for _ in range(PLAYERS))

    game = NoLimitTexasHoldem(
        AUTOMATIONS,
        False,
        0,
        (SB, BB) + (0,) * (PLAYERS - 2),
        BB,
        mode=Mode.CASH_GAME,
        rake=RAKE,
    )
    state = game(stacks, PLAYERS)

    villain = Policy(rng)
    hero_policy = HeroPolicy(rng)

    while state.status:
        seat = state.actor_index
        if seat is None:
            break
        board = [c for grp in state.board_cards for c in grp]
        strength = made_strength(list(state.hole_cards[seat]), board)
        policy = hero_policy if seat == hero else villain
        policy.act(state, seat, strength, position_names(seat))

    hh = HandHistory.from_game_state(
        game,
        state,
        finishing_stacks=[int(s) for s in state.stacks],
        currency="USD",
        year=2026,
        month=8,
        day=5,
        _pc_site="synthetic",
        _pc_site_hand_id=f"SYN-{hand_no:06d}",
        _pc_hero_index=hero,
    )
    return hh.dumps(), hero_policy.planted


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--out", type=Path, default=Path("archive/synthetic"))
    ap.add_argument("--seed", type=int, default=1, help="fixed so runs are reproducible")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    # pokerkit shuffles from the *global* random module, so seeding only our own
    # Random() leaves the dealt cards different on every run. Both have to be
    # seeded or the corpus isn't reproducible -- and the manifest's planted-leak
    # ordinals are worthless if the hands they refer to change.
    random.seed(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)

    manifest, planted_total = [], 0
    for n in range(1, args.count + 1):
        # Rotate hero through every seat so the corpus covers all positions
        # rather than over-representing one.
        hero = (n - 1) % PLAYERS
        text, planted = generate_hand(rng, hero, n)
        name = f"hand_{n:05d}.phh"
        (args.out / name).write_text(text)
        manifest.append({"file": name, "hero_index": hero, "planted": planted})
        planted_total += len(planted)

    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    by_leak: dict[str, int] = {}
    for entry in manifest:
        for p in entry["planted"]:
            by_leak[p["leak"]] = by_leak.get(p["leak"], 0) + 1
    print(f"wrote {args.count} hands to {args.out}")
    print(f"planted {planted_total} mistakes: {by_leak}")


if __name__ == "__main__":
    main()
