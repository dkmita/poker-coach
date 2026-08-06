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
from poker_coach.models import (
    PHH_COLLECTED, PHH_HERO_INDEX, PHH_SITE_HAND_ID, Position, position_of,
)
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

# A villain buying in mid-orbit: Villain3 posts a big blind out of position from
# under the gun, then checks their option.
LIVE_POST = textwrap.dedent("""
    Hand #1004 - Holdem (No Limit) - $0.05/$0.10 - 2026/08/06 15:25:09 UTC
    Table1 6-max Seat #1 is the button
    Seat 1: HeroName ($10.00)
    Seat 2: Villain1 ($10.00)
    Seat 3: Villain2 ($10.00)
    Seat 4: Villain3 ($10.00)
    Villain1 posts the small blind $0.05
    Villain2 posts the big blind $0.10
    Villain3 posts $0.10
    *** HOLE CARDS ***
    Dealt to HeroName [Ac Kd]
    Villain3 checks
    HeroName raises $0.30 to $0.30
    Villain1 folds
    Villain2 folds
    Villain3 folds
    Uncalled bet ($0.20) returned to HeroName
    *** SUMMARY ***
    Total pot $0.25
""").strip()

# The same shape with hero doing the posting -- which is refused, because the
# conversion would turn hero's check into a voluntary call.
HERO_LIVE_POST = textwrap.dedent("""
    Hand #1005 - Holdem (No Limit) - $0.05/$0.10 - 2026/08/06 15:26:09 UTC
    Table1 6-max Seat #1 is the button
    Seat 1: Villain1 ($10.00)
    Seat 2: Villain2 ($10.00)
    Seat 3: Villain3 ($10.00)
    Seat 4: HeroName ($10.00)
    Villain2 posts the small blind $0.05
    Villain3 posts the big blind $0.10
    HeroName posts $0.10
    *** HOLE CARDS ***
    Dealt to HeroName [7s Jd]
    HeroName checks
    Villain1 raises $0.30 to $0.30
    Villain2 folds
    Villain3 folds
    HeroName folds
    Uncalled bet ($0.20) returned to Villain1
    *** SUMMARY ***
    Total pot $0.25
""").strip()


# A chopped pot, which is the case that breaks every "one winner" assumption.
# Both players make the same straight and are paid 9c out of a 22c pot; after 4c
# of rake they each finish 1c *down* despite winning.
CHOPPED = textwrap.dedent("""
    Hand #1006 - Holdem (No Limit) - $0.02/$0.04 - 2026/08/06 15:26:29 UTC
    Table1 6-max Seat #3 is the button
    Seat 1: Villain2 ($2.00)
    Seat 2: HeroName ($2.00)
    Seat 3: Villain1 ($3.87)
    Villain2 posts the small blind $0.02
    HeroName posts the big blind $0.04
    *** HOLE CARDS ***
    Dealt to HeroName [6s Kh]
    Villain1 raises $0.06 to $0.10
    Villain2 folds
    HeroName calls $0.06
    *** FLOP *** [Jh Qc 4h]
    HeroName checks
    Villain1 checks
    *** TURN *** [Jh Qc 4h] [Td]
    HeroName checks
    Villain1 checks
    *** RIVER *** [Jh Qc 4h Td] [As]
    HeroName checks
    Villain1 checks
    *** SHOW DOWN ***
    Villain1 shows [6d Kd] (a straight, Ace high [As Kd Qc Jh Td])
    HeroName shows [6s Kh] (a straight, Ace high [As Kh Qc Jh Td])
    Villain1 collected $0.09 from main pot
    HeroName collected $0.09 from main pot
    *** SUMMARY ***
    Total pot $0.22 | Rake $0.04
""").strip()


def test_cents_never_goes_through_float():
    assert cents("$0.05") == 5
    assert cents("$19.30") == 1930
    assert cents("$0.29") == 29  # 0.29 * 100 is 28.999... in binary


def test_split_hands():
    assert len(list(split_hands(f"{HEADS_UP}\n\n{SITTING_OUT}"))) == 2


def test_heads_up_puts_the_big_blind_at_index_zero():
    """pokerkit opens postflop betting at index 0 whatever the blinds say.

    Heads up that has to be the big blind, so the order is [BB, button] and the
    amounts are written reversed to match -- pokerkit posts entry 1 to index 0.
    Put the button at index 0 instead and pokerkit has it act first on the flop,
    then covers the disagreement by inventing a check for it.
    """
    hh = to_phh(HEADS_UP)
    assert hh.blinds_or_straddles == [5, 10]
    st = hh.create_state()
    assert list(st.bets) == [10, 5]  # index 0 posted the big blind
    assert position_of(0, 2) is Position.BB
    assert position_of(1, 2) is Position.BTN


def test_hero_and_ids_recorded(): 
    hh = to_phh(HEADS_UP)
    assert hh.user_defined_fields[PHH_SITE_HAND_ID] == "1001"
    assert hh.players[hh.user_defined_fields[PHH_HERO_INDEX]] == "HeroName"


def test_starting_stack_is_before_blinds():
    """The Seat line shows the stack before posting, not after."""
    assert to_phh(HEADS_UP).starting_stacks == [1000, 1000]


def test_players_not_dealt_in_are_excluded():
    """A seat line is not proof of being in the hand.

    Two of the four seats are out, which leaves this hand heads up -- so the
    order is [BB, button], not the small-blind-first order used at every other
    table size.
    """
    hh = to_phh(SITTING_OUT)
    assert hh.players == ["Villain1", "HeroName"]


def test_chopped_pot_charges_rake_to_both_winners():
    """Rake comes out of the pot, so it is charged to whoever was paid from it.

    Charging it all to the biggest *gainer* was wrong twice over here: after rake
    both winners are down a cent, so the biggest gainer is whichever of them the
    rounding favoured -- and that one would have paid the other's rake too.
    """
    hh = to_phh(CHOPPED)
    i = {n: k for k, n in enumerate(hh.players)}
    assert hh.user_defined_fields["_pc_rake_cents"] == 4
    collected = dict(
        p.split(":") for p in hh.user_defined_fields[PHH_COLLECTED].split(",")
    )
    assert {int(k): int(v) for k, v in collected.items()} == {
        i["Villain1"]: 9, i["HeroName"]: 9
    }
    # 2c each, not 4c off one of them. These are the site's own numbers.
    assert hh.finishing_stacks[i["HeroName"]] == 199
    assert hh.finishing_stacks[i["Villain1"]] == 386
    assert hh.finishing_stacks[i["Villain2"]] == 198  # posted the sb and folded
    # Everything that left the table is the rake.
    assert sum(hh.starting_stacks) - sum(hh.finishing_stacks) == 4


def test_rake_shares_sum_to_the_rake_exactly():
    """Largest-remainder, so a chop cannot lose or invent a cent to rounding."""
    hh = to_phh(CHOPPED.replace("Rake $0.04", "Rake $0.03"))
    assert sum(hh.starting_stacks) - sum(hh.finishing_stacks) == 3


def test_rake_includes_the_jackpot_fee():
    assert to_phh(SITTING_OUT).user_defined_fields["_pc_rake_cents"] == 2


def test_finishing_stacks_are_set():
    """Needed for hero_net and for reconciling against the site."""
    hh = to_phh(HEADS_UP)
    assert hh.finishing_stacks is not None
    hero = hh.user_defined_fields[PHH_HERO_INDEX]
    assert hh.finishing_stacks[hero] - hh.starting_stacks[hero] == -10  # lost the bb


def test_live_post_is_carried_on_the_posters_own_action():
    """An out-of-turn post has no PHH slot, so it is not posted at all.

    In `blinds_or_straddles` pokerkit reads it as a straddle and moves where the
    action starts; as an ante it is dead money and the poster ends up paying
    twice. Left out, their recorded check resolves to a call of the same amount
    at their own turn -- same money, same finishing stack, and an action order
    pokerkit agrees with.
    """
    hh = to_phh(LIVE_POST)
    # Order from the small blind: Villain1, Villain2, Villain3, HeroName.
    assert hh.players == ["Villain1", "Villain2", "Villain3", "HeroName"]
    assert hh.blinds_or_straddles == [5, 10, 0, 0]  # the post is not here
    assert hh.user_defined_fields["_pc_live_post"] == "Villain3:10"
    # Villain3 still contributes exactly the 10 they posted.
    v3 = hh.players.index("Villain3")
    assert hh.starting_stacks[v3] - hh.finishing_stacks[v3] == 10


def test_live_post_by_hero_is_refused():
    """The conversion renames a check to a call. Tolerable for a villain in a
    hand hero is being studied in; for hero it invents a decision, and the
    preflop chart layer would go on to judge it."""
    with pytest.raises(ParseError, match="hero posted out of turn"):
        to_phh(HERO_LIVE_POST)


def test_partial_out_of_turn_post_is_refused():
    """Only a full big blind rides in on the poster's own action. Anything else
    is genuinely dead money with nowhere to go."""
    with pytest.raises(ParseError, match="not the big blind"):
        to_phh(LIVE_POST.replace("Villain3 posts $0.10", "Villain3 posts $0.05"))


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
