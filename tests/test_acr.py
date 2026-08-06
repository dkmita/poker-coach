"""Tests for the ACR/WPN parser.

The fixture is anonymized: real exports contain other players' screen names and
live under a gitignored `samples/`. These blocks reproduce the format's awkward
parts rather than a typical hand.
"""

from __future__ import annotations

import textwrap

import pytest

from poker_coach.ingest.parsers.acr import (
    ParseError, cents, dumps_phh, parse, split_hands, to_phh,
)
from poker_coach.models import PHH_HERO_INDEX, PHH_SITE_HAND_ID
from poker_coach.replay import hero_index, iter_decisions
from pokerkit.notation import HandHistory

HEADS_UP = textwrap.dedent("""
    Hand #1001 - Holdem (No Limit) - $0.05/$0.10 - 2026/08/06 02:14:08 UTC
    Table1 6-max Seat #4 is the button
    Seat 3: HeroName ($10.00)
    Seat 4: Villain1 ($10.00)
    Villain1 posts the small blind $0.05
    HeroName posts the big blind $0.10
    *** HOLE CARDS ***
    Dealt to HeroName [9c 5h]
    Villain1 raises $0.25 to $0.30
    HeroName folds
    Uncalled bet ($0.20) returned to Villain1
    Villain1 does not show
    *** SUMMARY ***
    Total pot $0.20
""").strip()

# Six seats listed, but two are not dealt in: one is waiting for the big blind,
# one has no stack at all.
SITTING_OUT = textwrap.dedent("""
    Hand #1002 - Holdem (No Limit) - $0.05/$0.10 - 2026/08/06 02:16:35 UTC
    Table1 6-max Seat #3 is the button
    Seat 1: Villain2 will be allowed to play after the button
    Seat 3: HeroName ($18.95)
    Seat 4: Villain1 ($5.00)
    Seat 6: Villain3 ($10.00)
    HeroName posts the small blind $0.05
    Villain1 posts the big blind $0.10
    Villain3 waits for big blind
    *** HOLE CARDS ***
    Dealt to HeroName [Ks 2d]
    HeroName raises $0.20 to $0.25
    Villain1 folds
    Uncalled bet ($0.15) returned to HeroName
    *** SUMMARY ***
    Total pot $0.20 | Rake $0.01 | JP Fee $0.01
""").strip()

# Distinct hand id: parse() is keyed by it, so reusing 1001 would make the two
# fixtures collide in the result dict.
DEAD_BLIND = HEADS_UP.replace("Hand #1001", "Hand #1003").replace(
    "*** HOLE CARDS ***", "Villain2 posts $0.10\n*** HOLE CARDS ***"
)


def test_cents_never_goes_through_float():
    assert cents("$0.05") == 5
    assert cents("$19.30") == 1930
    assert cents("$0.29") == 29  # 0.29 * 100 is 28.999... in binary


def test_split_hands():
    assert len(list(split_hands(f"{HEADS_UP}\n\n{SITTING_OUT}"))) == 2


def test_heads_up_blinds_are_reversed_for_pokerkit():
    """pokerkit reads index 0 as the big blind when two players are dealt."""
    hh = to_phh(HEADS_UP)
    assert hh.blinds_or_straddles == [10, 5]
    st = hh.create_state()
    assert list(st.bets) == [5, 10]  # small blind first, as intended


def test_hero_and_ids_recorded(): 
    hh = to_phh(HEADS_UP)
    assert hh.user_defined_fields[PHH_SITE_HAND_ID] == "1001"
    assert hh.players[hh.user_defined_fields[PHH_HERO_INDEX]] == "HeroName"


def test_starting_stack_is_before_blinds():
    """The Seat line shows the stack before posting, not after."""
    assert to_phh(HEADS_UP).starting_stacks == [1000, 1000]


def test_players_not_dealt_in_are_excluded():
    """A seat line is not proof of being in the hand."""
    hh = to_phh(SITTING_OUT)
    assert hh.players == ["HeroName", "Villain1"]


def test_rake_includes_the_jackpot_fee():
    assert to_phh(SITTING_OUT).user_defined_fields["_pc_rake_cents"] == 2


def test_finishing_stacks_are_set():
    """Needed for hero_net and for reconciling against the site."""
    hh = to_phh(HEADS_UP)
    assert hh.finishing_stacks is not None
    hero = hh.user_defined_fields[PHH_HERO_INDEX]
    assert hh.finishing_stacks[hero] - hh.starting_stacks[hero] == -10  # lost the bb


def test_dead_blind_refused_with_a_reason():
    """No PHH representation; pokerkit would read it as a straddle and shift
    where the action starts. Refused rather than silently mis-converted."""
    with pytest.raises(ParseError, match="dead blind"):
        to_phh(DEAD_BLIND)


def test_source_text_survives_the_round_trip(tmp_path):
    """pokerkit's dumps() would write it single-quoted on one line: invalid TOML,
    and the failure only shows up when reading the file back."""
    hh = to_phh(HEADS_UP, source_file="s.txt")
    path = tmp_path / "h.phh"
    path.write_text(dumps_phh(hh))
    back = HandHistory.loads(path.read_text())
    assert back.user_defined_fields["_pc_source_text"].startswith("Hand #1001")
    assert back.user_defined_fields["_pc_source_file"] == "s.txt"


def test_decisions_are_replayable(tmp_path):
    hh = to_phh(SITTING_OUT)
    d = list(iter_decisions(hh, actor=hero_index(hh)))
    assert [x.action.value for x in d] == ["raise"]
    assert d[0].to_amount == 25


def test_parse_yields_failures_instead_of_raising():
    results = dict(parse(f"{HEADS_UP}\n\n{DEAD_BLIND}"))
    assert isinstance(results["1001"], HandHistory)
    assert isinstance(results["1003"], ParseError)  # one bad hand, session survives


def test_heads_up_big_blind_is_not_halved():
    """Regression: pokerkit's heads-up swap makes the array [big, small], so
    reading index 1 halved the big blind and doubled every bb figure --
    a $10 stack at $0.05/$0.10 rendered as '198bb effective'."""
    from poker_coach import handview
    from poker_coach.replay import big_blind

    hh = to_phh(HEADS_UP)
    assert big_blind(hh) == 10
    v = handview.build(hh)
    assert v["hand"]["stakes"]["label"] == "$0.05/$0.10"
    assert v["hand"]["hero"]["eff_stack_bb"] == pytest.approx(100.0)
