"""Estimating an opponent's range with a model.

The layer that exists because charts run out. Charts cover preflop spots that
somebody published; this covers the rest -- every postflop decision, and every
preflop spot the pack does not have.

The model is asked for a **range**, never for a number. Everything downstream --
equity, pot odds, EV -- is computed from that range deterministically, so the
one thing the model can get wrong is the one thing a human can check by looking
at a 13x13 grid. Asking it for an EV figure instead would put arithmetic in the
hands of the component least able to do it and least able to be audited.

Output goes through the same `parse_range` the charts use, so a model range and
a chart range are the same object downstream and render in the same grid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..heuristics import EXPLOIT_GROUPS, GTO_GROUPS, Heuristics
from ..llm import DEFAULT_MAX_TOKENS, LLM, Budget, NullLLM
from ..solvers.ranges import parse_range

_PROMPTS = Path(__file__).with_name("prompts")
_ESTIMATE = _PROMPTS / "estimate_range.md"
_ADJUST = _PROMPTS / "adjust_range.md"


@dataclass(frozen=True, slots=True)
class EstimatedRange:
    weights: dict[str, float]
    spot_key: str
    model: str
    raw: str
    basis: str = "gto"


@dataclass(frozen=True, slots=True)
class RangePair:
    """What equilibrium does here, and what this pool does instead.

    Two estimates rather than one blended answer. Mixed into a single prompt you
    cannot tell afterwards which part came from theory and which from a read;
    kept apart, the difference between them *is* the exploit and can be looked
    at.

    Either side may be None -- the pool pass is skipped when the equilibrium one
    failed, since it has nothing to adjust.
    """

    gto: EstimatedRange | None = None
    exploit: EstimatedRange | None = None

    @property
    def best(self) -> EstimatedRange | None:
        """The one to price against: the read if there is one, else theory."""
        return self.exploit or self.gto

    def drift(self) -> float:
        """Total absolute weight moved between the two, in combos.

        Zero means the pool pass returned the equilibrium range unchanged, which
        is a real answer and not a failure.
        """
        if not (self.gto and self.exploit):
            return 0.0
        from ..equity import combos

        keys = set(self.gto.weights) | set(self.exploit.weights)
        return sum(
            abs(self.exploit.weights.get(k, 0.0) - self.gto.weights.get(k, 0.0))
            * len(combos(k, set()))
            for k in keys
        )


@dataclass
class RangeEstimator:
    """Model-estimated ranges, cached on the abstract spot.

    Keyed on `spot_key` plus the board, never on the asking player's cards --
    the same rule the solver cache follows, and for the same reason: a key that
    mentions hero's hand never hits twice.
    """

    llm: LLM = field(default_factory=NullLLM)
    heuristics: Heuristics | None = None
    budget: Budget = field(default_factory=Budget)
    _cache: dict[str, RangePair] = field(default_factory=dict, repr=False)

    def system_prompt(self, basis: str = "gto") -> str:
        """Task description then heuristics, in that order, and nothing else.

        Byte-stable across hands so the provider can cache it. Nothing about a
        specific hand may appear here -- that is what the user turn is for.

        The two passes get different prefixes, so they cache separately: the
        equilibrium pass never sees the population notes, which is what makes
        its answer a statement about theory rather than a blend.
        """
        if basis == "exploit":
            parts = [_ADJUST.read_text().rstrip()]
            groups = EXPLOIT_GROUPS
        else:
            parts = [_ESTIMATE.read_text().rstrip()]
            groups = GTO_GROUPS
        if self.heuristics is not None:
            body = self.heuristics.prompt(*groups).strip()
            if body:
                parts.append("# Standing guidance\n\n" + body)
        return "\n\n".join(parts)

    def _one(self, spot_key: str, prompt: str, basis: str) -> EstimatedRange | None:
        """A single pass. None for every reason a pass can fail."""
        if self.budget.exhausted():
            return None
        reply = self.llm.complete(
            system=self.system_prompt(basis),
            prompt=prompt,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        if reply is None:
            return None
        self.budget.requests += 1
        # A malformed range is a miss, not a crash. The model is asked for a
        # strict format precisely so this stays detectable.
        for candidate in _candidates(reply.text):
            try:
                weights = parse_range(candidate)
            except ValueError:
                continue
            if weights:
                return EstimatedRange(
                    weights=weights,
                    spot_key=spot_key,
                    model=reply.model,
                    raw=reply.text,
                    basis=basis,
                )
        return None

    def estimate(self, spot_key: str, description: str, board: str = "") -> RangePair:
        """Both ranges for one decision: equilibrium first, then the pool.

        Two calls. The second is given the first as its starting point, so it
        describes a deviation rather than answering afresh -- which makes the
        two comparable, and makes "no change" an answer the model can give.

        The pool pass is skipped when the equilibrium one failed: there is
        nothing to adjust, and asking anyway would produce a second unanchored
        guess rather than a read.
        """
        key = f"{spot_key}|{board}"
        if key in self._cache:
            return self._cache[key]

        gto = self._one(spot_key, description, "gto")
        exploit = None
        if gto is not None:
            exploit = self._one(
                spot_key,
                f"{description}\n\n"
                f"Your equilibrium estimate for this spot was:\n{_as_text(gto.weights)}",
                "exploit",
            )
        pair = RangePair(gto=gto, exploit=exploit)
        self._cache[key] = pair
        return pair


def _as_text(weights: dict[str, float]) -> str:
    """A weight map back into the range text the model was asked to produce.

    Round-tripping through its own format keeps the second prompt in the
    vocabulary the first answered in.
    """
    return ", ".join(
        k if w >= 1.0 else f"{k}:{w:g}" for k, w in sorted(weights.items())
    )


def _candidates(text: str) -> list[str]:
    """Ways to read a model's answer as a range, best guess first.

    Models wrap the answer despite being told not to: a code fence, a sentence
    of preamble, occasionally a range split over several lines. Fences are
    dropped, then the last line is tried (preamble above it) and the whole body
    after that (a range that wrapped).

    Lenient here on purpose, and strict in `parse_range` on purpose. A typo in a
    checked-in chart should stop the run; a model adding "Here is the range:"
    should not.
    """
    lines = [
        ln.strip()
        for ln in text.strip().splitlines()
        if ln.strip() and not ln.strip().startswith("```")
    ]
    if not lines:
        return []
    return [lines[-1], " ".join(lines)]
