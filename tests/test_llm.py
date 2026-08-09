"""Tests for the model boundary and the range estimator.

No network, ever. The whole point of `NullLLM` and `StubLLM` is that this layer
is exercised without credentials, and a test suite that needed a key would be a
test suite nobody runs.
"""

from __future__ import annotations

import json

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


def test_a_gateway_can_stand_in_for_anthropic(monkeypatch):
    """Any proxy speaking the Anthropic wire format is a base_url away.

    One that speaks a different shape wants its own class -- which is the whole
    reason `LLM` is a protocol rather than a subclass of this.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seen: dict[str, object] = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    llm = AnthropicLLM(
        base_url="https://gateway.example/v1",
        headers={"Authorization": "Bearer t"},
    )
    assert llm._connect() is not None
    assert seen["base_url"] == "https://gateway.example/v1"
    assert seen["default_headers"] == {"Authorization": "Bearer t"}
    # A gateway may authenticate by header, so no key is needed -- but the
    # client still wants the argument populated.
    assert seen["api_key"] == "unused"


def test_neither_key_nor_gateway_is_still_a_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert AnthropicLLM()._connect() is None


# ---- Indeed's gateway -------------------------------------------------------
# OpenAI-shaped, so a separate class rather than a base_url on the Anthropic
# one. Exercised against a fake opener: no network, and no key required.

def _fake_proxy(monkeypatch, payload, status=200, capture=None):
    import urllib.request

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(payload).encode()

    def fake_urlopen(request, timeout=None):
        if capture is not None:
            capture["url"] = request.full_url
            capture["headers"] = dict(request.headers)
            capture["body"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_the_proxy_sends_the_shape_the_gateway_expects(monkeypatch):
    from poker_coach.llm import ProxyLLM

    seen: dict = {}
    _fake_proxy(monkeypatch, {
        "model": "gpt-4.1-mini",
        "choices": [{"message": {"content": "AA, KK"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 5},
    }, capture=seen)

    r = ProxyLLM(api_key="secret").complete(system="sys", prompt="hand")
    assert r is not None and r.text == "AA, KK"
    assert r.input_tokens == 100 and r.output_tokens == 5

    assert seen["url"].endswith("/openai/v1/chat/completions")
    # Header names are normalised by urllib; compare case-insensitively.
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers["authorization"] == "Bearer secret"
    assert headers["x-indeed-cache"] == "false"
    # System first, per-hand second: a prefix cache needs that ordering.
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]
    assert seen["body"]["messages"][0]["content"] == "sys"


def test_the_proxy_key_never_appears_in_the_reply(monkeypatch):
    """A Reply gets logged and stored; a credential in it would follow."""
    from poker_coach.llm import ProxyLLM

    _fake_proxy(monkeypatch, {
        "choices": [{"message": {"content": "AA"}}], "usage": {},
    })
    r = ProxyLLM(api_key="secret").complete(system="s", prompt="p")
    assert "secret" not in repr(r)


def test_no_key_means_no_call(monkeypatch, tmp_path):
    from poker_coach import llm as llm_mod
    from poker_coach.llm import ProxyLLM

    monkeypatch.delenv("LLM_PROXY_API_KEY", raising=False)
    monkeypatch.delenv("VITE_INDEED_LLM_API_KEY", raising=False)
    monkeypatch.setattr(llm_mod, "PROXY_VAULT_PROPS", tmp_path / "absent.properties")
    called = []
    monkeypatch.setattr(
        __import__("urllib.request", fromlist=["x"]),
        "urlopen",
        lambda *a, **k: called.append(1),
    )
    assert ProxyLLM().complete(system="s", prompt="p") is None
    assert not called


def test_the_key_comes_from_the_vault_file_when_it_exists(monkeypatch, tmp_path):
    from poker_coach import llm as llm_mod
    from poker_coach.llm import PROXY_VAULT_KEY, ProxyLLM

    props = tmp_path / "p.properties"
    props.write_text(f"# comment\n\n{PROXY_VAULT_KEY}=from-vault\nother=x\n")
    monkeypatch.setattr(llm_mod, "PROXY_VAULT_PROPS", props)
    monkeypatch.delenv("LLM_PROXY_API_KEY", raising=False)
    assert ProxyLLM()._key() == "from-vault"


def test_a_gateway_failure_is_a_none(monkeypatch):
    """Including the body of the error, which can echo the prompt back."""
    import urllib.error
    import urllib.request

    from poker_coach.llm import ProxyLLM

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 500, "nope", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert ProxyLLM(api_key="k").complete(system="s", prompt="p") is None


def test_a_response_missing_the_answer_is_a_none(monkeypatch):
    from poker_coach.llm import ProxyLLM

    _fake_proxy(monkeypatch, {"error": "quota"})
    assert ProxyLLM(api_key="k").complete(system="s", prompt="p") is None


def test_the_estimator_works_through_the_proxy(monkeypatch):
    """End to end: gateway reply -> parsed range -> same shape a chart has."""
    from poker_coach.llm import ProxyLLM

    _fake_proxy(monkeypatch, {
        "choices": [{"message": {"content": "AA:1.0, KK:0.5, AKs"}}], "usage": {},
    })
    est = RangeEstimator(llm=ProxyLLM(api_key="k"))
    r = est.estimate("BTN_preflop_unopened_100bb", "BTN opens 2.5bb")
    assert r is not None and r.weights["KK"] == 0.5


def test_no_client_prints_its_key():
    """A dataclass repr prints every field, and these end up in tracebacks."""
    from poker_coach.llm import AnthropicLLM, ProxyLLM

    assert "hunter2" not in repr(ProxyLLM(api_key="hunter2"))
    assert "hunter2" not in repr(AnthropicLLM(api_key="hunter2"))
