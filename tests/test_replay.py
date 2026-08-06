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


# Heads up, correctly encoded: index 0 is the big blind and index 1 the button,
# and the blind amounts are written reversed because pokerkit posts entry 1 to
# index 0. Postflop bet, raise, re-raise, call -- the sequence whose kinds and
# sizes depend entirely on pairing each action with the state it faced.
HU_HAND = textwrap.dedent("""
    variant = "NT"
    antes = [0, 0]
    blinds_or_straddles = [5, 10]
    min_bet = 10
    starting_stacks = [1010, 990]
    actions = [
      "d dh p1 KdAc", "d dh p2 QsTs",
      "p2 cbr 25", "p1 cbr 100", "p2 cc",
      "d db 9hQdQh", "p1 cbr 66", "p2 cc",
      "d db 5s", "p1 cbr 166", "p2 cbr 332", "p1 cbr 844", "p2 cc",
      "d db Td", "p2 sm QsTs", "p1 sm KdAc",
    ]
    finishing_stacks = [20, 1930]
    currency = "USD"
    _pc_site = "acr"
    _pc_site_hand_id = "HU-1"
    _pc_hero_index = 1
""").strip()

# The same hand with the button at index 0 -- the encoding this project used
# until it was found to be wrong. pokerkit disagrees about who opens the flop
# and repairs the difference by inserting a check for the button, so this
# replays without complaint and reports a hand nobody played.
HU_BUTTON_FIRST = textwrap.dedent("""
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
    _pc_site_hand_id = "HU-2"
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


def test_a_repaired_hand_is_rejected_not_reported(tmp_path):
    """pokerkit does not reject a sequence it disagrees with, it rewrites one.

    With the button at index 0 it thinks the button opens the flop, and inserts
    a check nobody made. That costs no chips, so the hand replays, the finishing
    stacks still reconcile against the site, and the output is a hand that was
    never played. It has to fail loudly instead.
    """
    path = tmp_path / "bad.phh"
    path.write_text(HU_BUTTON_FIRST)
    hh = load(path)  # pokerkit itself is perfectly happy with it
    with pytest.raises(ReplayError, match="repaired"):
        list(iter_decisions(hh))


def test_postflop_actions_are_paired_with_the_state_they_faced(hu):
    """Each action judged against the state it actually faced.

    `to_call` is the only thing separating a check from a call and a bet from a
    raise, so pairing an action with the wrong state does not produce nonsense
    -- it produces the wrong verb next to a real pot size from one action
    earlier, which reads as perfectly ordinary poker.
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
