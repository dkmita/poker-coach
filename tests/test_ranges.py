"""Tests for reading exported solver ranges."""

from __future__ import annotations

import pytest

from poker_coach.solvers.ranges import ChartProvider, canonical_class, parse_range


def test_canonical_class_collapses_suits():
    assert canonical_class("AdJs") == "AJo"
    assert canonical_class("KdQd") == "KQs"
    assert canonical_class("7h7s") == "77"
    assert canonical_class("JsAd") == "AJo"  # order independent


def test_parse_weights_and_bare_hands():
    r = parse_range("AA:1.0, AKs:0.75, AJo")
    assert r == {"AA": 1.0, "AKs": 0.75, "AJo": 1.0}


def test_plus_expansion():
    assert set(parse_range("TT+")) == {"TT", "JJ", "QQ", "KK", "AA"}
    assert set(parse_range("A2s+")) == {f"A{r}s" for r in "23456789TJQK"}


def test_typos_raise_rather_than_silently_dropping():
    """A dropped entry becomes a confidently wrong frequency later."""
    with pytest.raises(ValueError):
        parse_range("AA:1.0, XX:0.5")


@pytest.fixture
def charts(tmp_path):
    spot = tmp_path / "BB_preflop_vs_UTG_raise_2.5bb_100bb"
    spot.mkdir()
    (spot / "call.txt").write_text("AJo:0.8, KQs:1.0, 77+")
    (spot / "raise.txt").write_text("AJo:0.2, AA:1.0")
    return ChartProvider(tmp_path, source="GTO Wizard export")


def test_lookup_returns_normalized_frequencies(charts):
    s = charts.lookup("BB_preflop_vs_UTG_raise_2.5bb_100bb", "AdJs")
    assert s is not None
    assert s.hand == "AJo"
    assert s.frequency_of("call") == pytest.approx(0.8)
    assert s.frequency_of("raise") == pytest.approx(0.2)
    assert sum(a.frequency for a in s.actions) == pytest.approx(1.0)
    assert s.is_mixed()


def test_missing_fold_is_inferred_as_the_remainder(charts):
    """Exports usually cover only continuing actions."""
    s = charts.lookup("BB_preflop_vs_UTG_raise_2.5bb_100bb", "KdQd")
    assert s.frequency_of("call") == pytest.approx(1.0)
    s2 = charts.lookup("BB_preflop_vs_UTG_raise_2.5bb_100bb", "9h9s")
    assert s2.frequency_of("call") == pytest.approx(1.0)


def test_hand_absent_from_every_action_returns_none(charts):
    """Charted spot, but this hand never reaches it — not a strategy."""
    assert charts.lookup("BB_preflop_vs_UTG_raise_2.5bb_100bb", "7h2c") is None


def test_uncharted_spot_returns_none(charts):
    assert charts.lookup("CO_flop_unopened_100bb", "AdJs") is None


def test_unknown_action_filename_raises(tmp_path):
    spot = tmp_path / "some_spot"
    spot.mkdir()
    (spot / "shove.txt").write_text("AA")
    with pytest.raises(ValueError, match="unknown action"):
        ChartProvider(tmp_path)


def test_missing_root_is_not_an_error(tmp_path):
    """A provider with no charts yet answers nothing, rather than failing."""
    p = ChartProvider(tmp_path / "nope")
    assert p.spot_keys == ()
    assert p.lookup("anything", "AdJs") is None


def test_comments_are_stripped_per_line():
    """A `#` comments out the rest of its line.

    Regression: stripping `#`-prefixed *tokens* after splitting on whitespace
    left the comment's remaining words as entries, and since unparseable entries
    raise, one note at the top of an exported chart took the provider down.
    """
    r = parse_range("# GTO Wizard export -- not a real solve\nAA:1.0, KK  # trailing note\n")
    assert r == {"AA": 1.0, "KK": 1.0}


def test_underscore_files_are_ignored(tmp_path):
    spot = tmp_path / "s"
    spot.mkdir()
    (spot / "call.txt").write_text("AA")
    (spot / "_notes.txt").write_text("free text, not a range at all")
    assert ChartProvider(tmp_path).spot_keys == ("s",)


def test_grid_uses_canonical_class_names(charts):
    """Regression: building names off the ascending RANKS string produced '27s'
    where the canonical class is '72s', so every off-diagonal cell missed."""
    g = charts.grid("BB_preflop_vs_UTG_raise_2.5bb_100bb")
    assert len(g) == 169
    for name in ("AA", "72o", "72s", "AKo", "AKs", "22"):
        assert name in g, name
    assert "27s" not in g and "KAo" not in g


def test_grid_fills_the_fold_remainder(charts):
    g = charts.grid("BB_preflop_vs_UTG_raise_2.5bb_100bb")
    assert g["72o"] == {"fold": 1.0}
    assert abs(sum(g["AJo"].values()) - 1.0) < 1e-9
