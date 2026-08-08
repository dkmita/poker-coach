"""Tests for the model boundary and the range estimator.

No network, ever. The whole point of `NullLLM` and `StubLLM` is that this layer
is exercised without credentials, and a test suite that needed a key would be a
test suite nobody runs.
"""

from __future__ import annotations

import pytest

from poker_coach.agent.ranges import RangeEstimator
from poker_coach.heuristics import Heuristics
from poker_coach.llm import AnthropicLLM, Budget, NullLLM, Reply, StubLLM


def test_the_default_model_answers_nothing_and_costs_nothing():
    """No credentials configured must be a degraded run, not a broken one."""
    assert NullLLM().complete(system="s", prompt="p") is None
    est = RangeEstimator()
    assert est.estimate("BTN_preflop_unopened_100bb", "anything") is None


def test_a_missing_key_is_a_none_not_an_exception(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert AnthropicLLM().complete(system="s", prompt="p") is None


def test_an_estimate_parses_into_the_same_shape_a_chart_has():
    est = RangeEstimator(llm=StubLLM(["AA:1.0, KK:1.0, AKs:0.75, 77+"]))
    r = est.estimate("BTN_preflop_unopened_100bb", "BTN opens 2.5bb")
    assert r is not None
    assert r.weights["AKs"] == 0.75
    assert r.weights["AA"] == 1.0
    assert "88" in r.weights           # 77+ expanded, exactly as a chart would


def test_a_fenced_or_chatty_answer_still_parses():
    """Models wrap the answer despite being told not to. Being lenient here is
    free; being lenient in `parse_range` would let a chart typo through."""
    est = RangeEstimator(llm=StubLLM(["Here is the range:\n```\nAA, KK\n```"]))
    r = est.estimate("s", "d")
    assert r is not None and set(r.weights) == {"AA", "KK"}


def test_an_unparseable_answer_is_a_miss_not_a_crash():
    est = RangeEstimator(llm=StubLLM(["I think they have a strong hand."]))
    assert est.estimate("s", "d") is None


def test_the_spot_is_asked_about_once():
    """Cached on the abstract spot, so a spot that recurs costs one call."""
    llm = StubLLM(["AA", "KK"])
    est = RangeEstimator(llm=llm)
    a = est.estimate("BB_preflop_vs_BTN_raise_100bb", "first")
    b = est.estimate("BB_preflop_vs_BTN_raise_100bb", "second")
    assert a is b
    assert len(llm.calls) == 1


def test_the_board_is_part_of_the_key():
    """Same spot key, different board, is a different question."""
    llm = StubLLM(["AA", "KK"])
    est = RangeEstimator(llm=llm)
    est.estimate("spot", "d", board="7s8s9s")
    est.estimate("spot", "d", board="2c2d2h")
    assert len(llm.calls) == 2


def test_a_failed_estimate_is_not_retried():
    """Otherwise a spot the model cannot answer is paid for on every hand."""
    llm = StubLLM([])
    est = RangeEstimator(llm=llm)
    assert est.estimate("spot", "d") is None
    assert est.estimate("spot", "d") is None
    assert len(llm.calls) == 1


def test_the_budget_stops_the_calls():
    llm = StubLLM(["AA", "KK", "QQ"])
    est = RangeEstimator(llm=llm, budget=Budget(max_requests=2))
    for i in range(5):
        est.estimate(f"spot-{i}", "d")
    assert len(llm.calls) == 2


def test_budget_accounting_uses_the_reported_usage():
    b = Budget(max_usd=1.0)
    b.record(Reply(text="", model="m", input_tokens=1_000_000, output_tokens=0), 5.0, 25.0)
    assert b.usd == pytest.approx(5.0)
    assert b.exhausted()


def test_the_system_prompt_is_stable_and_carries_the_heuristics(tmp_path):
    """It is the cache prefix. If it varies per hand nothing ever caches, and
    if the heuristics are missing from it the model is guessing unaided."""
    (tmp_path / "01-x.md").write_text("# X\n\nfold more rivers\n")
    est = RangeEstimator(llm=StubLLM([]), heuristics=Heuristics(tmp_path))
    first = est.system_prompt()
    assert first == est.system_prompt()
    assert "fold more rivers" in first
    assert "PioSolver" in first


def test_the_hand_never_leaks_into_the_cached_prefix():
    """Per-hand text belongs in the user turn; in the system prompt it would
    make the prefix unique and the cache useless."""
    llm = StubLLM(["AA"])
    est = RangeEstimator(llm=llm)
    est.estimate("spot", "hero holds QsTs on 9hQdQh")
    call = llm.calls[0]
    assert "QsTs" in call["prompt"]
    assert "QsTs" not in call["system"]
