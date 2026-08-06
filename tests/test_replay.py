"""Tests for the pokerkit boundary.

These exist mainly to catch a pokerkit upgrade breaking us. `replay.py` relies on
three behaviors pokerkit does not document — `state_actions` yielding more states
than there are actions and in a different order, the `State` object being mutated
in place across yields, and `street_index` ordering — each of which fails by
producing plausible numbers from the wrong moment in the hand rather than by
raising. Assertions on concrete pot sizes and action kinds are what turn that
into a visible failure.

The heads-up tests are not redundant with the six-handed ones: pokerkit only
interleaves its extra states mid-hand when the table is heads-up, so a six-handed
fixture passes under a pairing that is wrong for every postflop action.
"""

from __future__ import annotations

import textwrap

import pytest

from poker_coach.models import ActionType, Position, Street
from poker_coach.replay import (
    ReplayError,
    big_blind,
    hero_index,
    iter_decisions,
    load,
    project_index,
)

# 6-max, 100bb, hero in the big blind. Chosen to exercise every ambiguous case in
# one hand: a call and a check both written `cc`, and a bet and a raise both
# written `cbr`.
HAND = textwrap.dedent("""
    variant = "NT"
    antes = [0, 0, 0, 0, 0, 0]
    blinds_or_straddles = [50, 100, 0, 0, 0, 0]
    min_bet = 100
    starting_stacks = [10000, 10000, 10000, 10000, 10000, 10000]
    actions = [
      "d dh p1 ????", "d dh p2 Kd9d", "d dh p3 ????",
      "d dh p4 ????", "d dh p5 ????", "d dh p6 AhKs",
      "p3 f", "p4 f", "p5 f", "p6 cbr 250", "p1 f", "p2 cc",
      "d db 7h2c3d", "p2 cc", "p6 cbr 300", "p2 cc",
      "d db Ts", "p2 cc", "p6 cc",
      "d db 4c", "p2 cbr 900", "p6 f",
    ]
    players = ["SB", "BB", "UTG", "HJ", "CO", "BTN"]
    finishing_stacks = [9950, 10530, 10000, 10000, 10000, 9400]
    currency = "USD"
    year = 2026
    month = 8
    day = 5
    _pc_site = "wpn"
    _pc_site_hand_id = "ACR-991"
    _pc_hero_index = 1
""").strip()


# Heads-up, and the shape that exposed the pairing bug. Six-handed hands happen
# to survive positional indexing into `hh.actions` -- pokerkit only appends its
# extra states at the end -- but heads-up it inserts one at every postflop street
# transition, so 16 actions come back as 21 pairs and the drift starts at the
# flop. Hero raises the turn and calls a re-raise: with the old pairing those
# read as a bet and a call of the wrong size, with every pot still adding up.
HU_HAND = textwrap.dedent("""
    variant = "NT"
    antes = [0, 0]
    blinds_or_straddles = [10, 5]
    min_bet = 10
    starting_stacks = [990, 1010]
    actions = [
      "d dh p1 QsTs", "d dh p2 KdAc",
      "p1 cbr 25", "p2 cbr 100", "p1 cc",
      "d db 9hQdQh", "p2 cbr 66", "p1 cc",
      "d db 5s", "p2 cbr 166", "p1 cbr 332", "p2 cbr 844", "p1 cc",
      "d db Td", "p1 sm QsTs", "p2 sm KdAc",
    ]
    finishing_stacks = [1930, 20]
    currency = "USD"
    _pc_site = "acr"
    _pc_site_hand_id = "HU-1"
    _pc_hero_index = 0
""").strip()


@pytest.fixture
def hh(tmp_path):
    path = tmp_path / "hand.phh"
    path.write_text(HAND)
    return load(path)


@pytest.fixture
def hu(tmp_path):
    path = tmp_path / "hu.phh"
    path.write_text(HU_HAND)
    return load(path)


@pytest.fixture
def hero_decisions(hh):
    return list(iter_decisions(hh, hand_id=1, actor=hero_index(hh)))


def test_hero_and_blind(hh):
    assert hero_index(hh) == 1
    assert big_blind(hh) == 100


def test_cc_resolves_to_check_or_call(hero_decisions):
    """PHH writes both as `cc`; only `to_call` separates them."""
    kinds = [d.action for d in hero_decisions]
    assert kinds == [
        ActionType.CALL,  # preflop, facing 250
        ActionType.CHECK,  # flop, unopened
        ActionType.CALL,  # flop, facing 300
        ActionType.CHECK,  # turn, unopened
        ActionType.BET,  # river, unopened -> bet, not raise
    ]


def test_state_actions_is_longer_than_the_action_list(hu):
    """The premise of the bug, asserted so a pokerkit upgrade cannot quietly
    remove it and leave the workaround looking unnecessary."""
    pairs = list(hu.state_actions)
    assert len(pairs) > len(hu.actions)
    interleaved = [i for i, (_, a) in enumerate(pairs) if a is None]
    assert any(i < len(hu.actions) for i in interleaved), (
        "the extra states must appear mid-hand, not only after the last action; "
        "if they moved to the end this test no longer covers the drift"
    )


def test_postflop_actions_are_paired_with_the_state_they_faced(hu):
    """The regression: positional indexing drifts one action per street.

    Every kind here was wrong before the fix -- the flop call read as a check
    and both turn raises as bets -- and none of it looked wrong, because the
    pot sizes it printed were real pot sizes from one action earlier.
    """
    acts = list(iter_decisions(hu, hand_id=1))
    got = [(d.street, d.position, d.action, d.to_call) for d in acts]
    assert got == [
        (Street.PREFLOP, Position.BTN, ActionType.RAISE, 5),
        (Street.PREFLOP, Position.BB, ActionType.RAISE, 15),
        (Street.PREFLOP, Position.BTN, ActionType.CALL, 75),
        (Street.FLOP, Position.BB, ActionType.BET, 0),
        (Street.FLOP, Position.BTN, ActionType.CALL, 66),
        (Street.TURN, Position.BB, ActionType.BET, 0),
        (Street.TURN, Position.BTN, ActionType.RAISE, 166),
        (Street.TURN, Position.BB, ActionType.RAISE, 166),
        (Street.TURN, Position.BTN, ActionType.CALL, 492),
    ]


def test_pot_advances_with_each_postflop_action(hu):
    """The drift kept the pot one action stale, which is why it stayed plausible."""
    turn = [d for d in iter_decisions(hu, hand_id=1) if d.street is Street.TURN]
    assert [d.pot_before for d in turn] == [332, 498, 830, 1508]


def test_cbr_unopened_is_a_bet_not_a_raise(hero_decisions):
    river = hero_decisions[-1]
    assert river.action is ActionType.BET
    assert river.to_call == 0


def test_amount_is_the_increment_and_to_amount_is_the_total(hero_decisions):
    """The distinction hand histories blur and EV math needs."""
    preflop = hero_decisions[0]
    assert preflop.amount == 150  # 250 to call, 100 already posted
    assert preflop.to_amount == 250


def test_pot_and_pot_odds(hero_decisions):
    preflop = hero_decisions[0]
    assert preflop.pot_before == 400  # 50 + 100 + 250
    assert preflop.pot_odds == pytest.approx(150 / 550)


def test_check_has_undefined_pot_odds(hero_decisions):
    assert hero_decisions[1].pot_odds is None


def test_board_grows_with_the_street(hero_decisions):
    assert [(d.street, d.board) for d in hero_decisions] == [
        (Street.PREFLOP, ""),
        (Street.FLOP, "7h2c3d"),
        (Street.FLOP, "7h2c3d"),
        (Street.TURN, "7h2c3dTs"),
        (Street.RIVER, "7h2c3dTs4c"),
    ]


def test_hole_cards_are_canonical(hero_decisions):
    assert all(d.hole_cards == "Kd9d" for d in hero_decisions)


def test_action_index_refers_to_the_phh_action_list(hh, hero_decisions):
    """The stable key `flagged_decisions` points at."""
    for d in hero_decisions:
        assert hh.actions[d.action_index].startswith("p2 ")


def test_folds_are_never_all_in(hh):
    folds = [d for d in iter_decisions(hh) if d.action is ActionType.FOLD]
    assert len(folds) == 5
    assert not any(d.is_all_in for d in folds)


def test_actor_filter(hh):
    assert len(list(iter_decisions(hh))) == 13
    assert len(list(iter_decisions(hh, actor=1))) == 5


def test_project_index(hh, tmp_path):
    idx = project_index(hh, phh_path="a.phh", phh_sha256="abc")
    assert (idx.site, idx.site_hand_id) == ("wpn", "ACR-991")
    assert idx.hero_position is Position.BB
    assert idx.street_reached is Street.RIVER
    assert idx.bb == 100
    # From starting stacks, not the post-blind state, so a 100bb hand reads 100.
    assert idx.eff_stack_bb == pytest.approx(100.0)
    assert idx.hero_net == 530  # finishing 10530 - starting 10000
    assert idx.rake == 120  # chips that left the table
    assert idx.played_at.date().isoformat() == "2026-08-05"


def test_positions_follow_phh_posting_order(hh):
    """Index 0 posts the small blind; the button is last."""
    seen = {d.position for d in iter_decisions(hh)}
    assert Position.BB in seen and Position.BTN in seen
    assert Position.UTG in seen


def test_illegal_action_sequence_is_rejected(tmp_path):
    """pokerkit validates betting legality, so the parser gets it for free."""
    bad = tmp_path / "bad.phh"
    bad.write_text(
        HAND.replace('"p3 f", "p4 f", "p5 f",', '"p3 f", "p3 f", "p4 f", "p5 f",')
    )
    with pytest.raises(ReplayError):
        load(bad)


def test_missing_hero_index_is_an_error(tmp_path):
    path = tmp_path / "nohero.phh"
    path.write_text(HAND.replace("_pc_hero_index = 1", ""))
    with pytest.raises(ReplayError, match="hero"):
        hero_index(load(path))
