"""Tests for the hand view — the contract a UI renders.

Two things are load-bearing and easy to break: the facts must be present without
any provider (so a client renders for free), and numbers that are arithmetically
defined but conventionally meaningless must stay absent.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from poker_coach import handview
from poker_coach.replay import load
from poker_coach.solvers.base import ActionFrequency, NullProvider, Solution

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
    finishing_stacks = [9950, 10530, 10000, 10000, 10000, 9400]
    currency = "USD"
    year = 2026
    month = 8
    day = 5
    _pc_site = "wpn"
    _pc_site_hand_id = "ACR-991"
    _pc_hero_index = 1
""").strip()


@pytest.fixture
def view(tmp_path):
    p = tmp_path / "h.phh"
    p.write_text(HAND)
    return handview.build(load(p), hand_id=1)


def test_json_serializable(view):
    json.dumps(view)  # the whole point: it is a UI contract


def test_facts_present_without_a_provider(view):
    assert view["hand"]["stakes"]["label"] == "$0.50/$1.00"
    assert view["hand"]["hero"]["position"] == "BB"
    assert view["hand"]["hero"]["hole_cards"] == ["Kd", "9d"]
    assert [s["street"] for s in view["streets"]] == ["preflop", "flop", "turn", "river"]
    assert len(view["hero_decisions"]) == 5


def test_money_appears_as_both_cents_and_bb(view):
    d = view["hero_decisions"][0]
    assert d["to_call_bb"] == 1.5
    assert view["result"]["hero_net_cents"] == 530


def test_pot_odds_null_facing_a_check(view):
    check = next(d for d in view["hero_decisions"] if d["hero_action"] == "check")
    assert check["pot_odds"] is None


def test_spr_and_pct_pot_suppressed_preflop(view):
    """Both are arithmetically defined preflop and meaningless there."""
    pre = view["hero_decisions"][0]
    assert pre["street"] == "preflop"
    assert pre["spr"] is None
    preflop_actions = view["streets"][0]["actions"]
    assert all(a["pct_pot"] is None for a in preflop_actions)


def test_spr_present_postflop(view):
    flop = next(d for d in view["hero_decisions"] if d["street"] == "flop")
    assert flop["spr"] is not None


def test_facing_describes_the_aggressor(view):
    pre = view["hero_decisions"][0]
    assert pre["facing"]["position"] == "BTN"
    assert pre["facing"]["to_bb"] == 2.5


def test_villain_cards_are_none_when_unknown(view):
    unknown = [s for s in view["seats"] if s["hole_cards"] is None]
    assert unknown, "unknown villain cards must be null, not a '??' sentinel"


def test_gto_and_analysis_slots_exist_and_are_null(view):
    for d in view["hero_decisions"]:
        assert d["gto"] is None
        assert d["analysis"] is None
    assert view["analysis"] is None


def test_null_provider_changes_nothing(tmp_path):
    p = tmp_path / "h.phh"
    p.write_text(HAND)
    hh = load(p)
    assert handview.build(hh) == handview.build(hh, provider=NullProvider())


def test_spot_key_is_hand_independent(view):
    """The solver cache key must not mention hero's cards, or nothing ever hits."""
    for d in view["hero_decisions"]:
        assert "Kd" not in d["spot_key"] and "9d" not in d["spot_key"]
        assert d["spot_key"].startswith("BB_")


def test_provider_fills_gto_facts(tmp_path):
    class Fake:
        name = "fake"
        def lookup(self, spot_key, hand):
            return Solution(spot_key=spot_key, hand=hand, provider="fake",
                            actions=(ActionFrequency("call", 0.8),
                                     ActionFrequency("fold", 0.2)))

    p = tmp_path / "h.phh"
    p.write_text(HAND)
    v = handview.build(load(p), provider=Fake())
    gto = v["hero_decisions"][0]["gto"]
    assert gto["actions"][0] == {"action": "call", "frequency": 0.8,
                                 "ev_cents": None, "to_bb": None}
    assert gto["mixed"] is True


def test_broken_provider_does_not_lose_the_hand(tmp_path):
    class Broken:
        name = "broken"
        def lookup(self, spot_key, hand):
            raise RuntimeError("scraper died")

    p = tmp_path / "h.phh"
    p.write_text(HAND)
    v = handview.build(load(p), provider=Broken())
    assert len(v["hero_decisions"]) == 5
    assert all(d["gto"] is None for d in v["hero_decisions"])


HERO_FOLDS_PREFLOP = textwrap.dedent("""
    variant = "NT"
    antes = [0, 0, 0, 0, 0, 0]
    blinds_or_straddles = [50, 100, 0, 0, 0, 0]
    min_bet = 100
    starting_stacks = [10000, 10000, 10000, 10000, 10000, 10000]
    actions = [
      "d dh p1 Ad6s", "d dh p2 4d4s", "d dh p3 ????",
      "d dh p4 ????", "d dh p5 ????", "d dh p6 JhAs",
      "p3 f", "p4 f", "p5 f", "p6 cbr 250", "p1 f", "p2 cbr 450", "p6 cc",
      "d db 5s7sKh", "p2 cc", "p6 cc",
      "d db 7d", "p2 cc", "p6 cc",
      "d db Js", "p2 cc", "p6 cc",
      "p2 sm 4d4s", "p6 sm JhAs",
    ]
    finishing_stacks = [9950, 9550, 10000, 10000, 10000, 10452]
    _pc_site = "wpn"
    _pc_site_hand_id = "ACR-2"
    _pc_hero_index = 0
""").strip()


@pytest.fixture
def folded_view(tmp_path):
    p = tmp_path / "f.phh"
    p.write_text(HERO_FOLDS_PREFLOP)
    return handview.build(load(p))


def test_hero_street_is_not_the_hands_street(folded_view):
    """Hero folds preflop; the other two run it to the river.

    Regression: the result block reported the hand's street and "anyone showed"
    as if they were hero's, so a preflop fold rendered as
    "reached river, showdown" -- which inflates how often you think you saw a flop.
    """
    r = folded_view["result"]
    assert r["hero_street_reached"] == "preflop"
    assert r["street_reached"] == "river"


def test_hero_showdown_is_not_anyone_showdown(folded_view):
    r = folded_view["result"]
    assert r["hero_went_to_showdown"] is False
    assert r["showdown"] is True


def test_hero_reaching_showdown_is_reported(view):
    """The other direction: when hero does show, say so."""
    assert view["result"]["hero_street_reached"] == "river"


def test_board_comes_from_the_deal_actions(tmp_path):
    """Regression: reading the board from the state at the first player action
    showed the turn with only three cards -- the state lags a deal."""
    p = tmp_path / "h.phh"
    p.write_text(HAND)
    v = handview.build(load(p))
    by = {s["street"]: s["board"] for s in v["streets"]}
    assert by["flop"] == ["7h", "2c", "3d"]
    assert by["turn"] == ["7h", "2c", "3d", "Ts"]
    assert by["river"] == ["7h", "2c", "3d", "Ts", "4c"]


def test_showdown_reports_cards_and_result(view):
    """Hero's cards come from the deal, so they are known even when the hand
    ends with a fold and nobody shows."""
    sd = view["showdown"]
    assert sd["board"] == ["7h", "2c", "3d", "Ts", "4c"]
    assert sd["went_to_showdown"] is False  # villain folded the river
    hero = next(p for p in sd["players"] if p["is_hero"])
    assert hero["cards"] == ["Kd", "9d"]
    assert hero["showed"] is False
    assert hero["net_cents"] == 530 and hero["won"] is True


def test_actions_carry_the_actors_cards_when_known(view):
    """Each action row renders on its own, so it carries whose cards they are.

    The fixture's p6 never shows, so their rows stay null: the client draws a
    card back either way, but only a non-null value is clickable, and offering a
    reveal that cannot resolve is worse than not offering one.
    """
    acts = [a for s in view["streets"] for a in s["actions"]]
    hero = [a for a in acts if a["is_hero"]]
    assert hero and all(a["cards"] == ["Kd", "9d"] for a in hero)
    assert any(a["cards"] is None for a in acts if not a["is_hero"])


def test_unrevealed_hands_are_null_not_invented(view):
    """A villain who folded keeps their cards; inferring them would be fiction."""
    unknown = [p for p in view["showdown"]["players"] if p["cards"] is None]
    assert unknown


def test_verdict_carries_the_hand_class_for_linking(tmp_path):
    """The link points at one square, so the verdict has to say which."""
    from poker_coach.solvers.base import ActionFrequency, Solution

    class Fake:
        name = "fake"
        def lookup(self, spot_key, hand):
            from poker_coach.solvers.ranges import canonical_class
            return Solution(spot_key=spot_key, hand=canonical_class(hand),
                            provider="fake",
                            actions=(ActionFrequency("call", 1.0),))

    p = tmp_path / "h.phh"
    p.write_text(HAND)
    v = handview.build(load(p), provider=Fake())
    pre = v["hero_decisions"][0]
    assert pre["verdict"]["hand"] == "K9s"        # Kd9d
    assert pre["verdict"]["chart"] == pre["spot_key"]
    assert pre["verdict"]["tone"] == "good"       # hero called; chart calls 100%
