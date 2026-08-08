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

from ..heuristics import Heuristics
from ..llm import DEFAULT_MAX_TOKENS, LLM, Budget, NullLLM
from ..solvers.ranges import parse_range

_PROMPT = Path(__file__).with_name("prompts") / "estimate_range.md"


@dataclass(frozen=True, slots=True)
class EstimatedRange:
    weights: dict[str, float]
    spot_key: str
    model: str
    raw: str


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
    _cache: dict[str, EstimatedRange | None] = field(default_factory=dict, repr=False)

    def system_prompt(self) -> str:
        """Task description then heuristics, in that order, and nothing else.

        Byte-stable across hands so the provider can cache it. Nothing about a
        specific hand may appear here -- that is what the user turn is for.
        """
        parts = [_PROMPT.read_text().rstrip()]
        if self.heuristics is not None:
            body = self.heuristics.prompt().strip()
            if body:
                parts.append("# Standing guidance\n\n" + body)
        return "\n\n".join(parts)

    def estimate(self, spot_key: str, description: str, board: str = "") -> EstimatedRange | None:
        """A range for one decision, or None if unavailable.

        None covers every reason: no model configured, no key, the budget is
        spent, the call failed, the answer did not parse. Callers treat all of
        them the same way, which is to carry on without an estimate.
        """
        key = f"{spot_key}|{board}"
        if key in self._cache:
            return self._cache[key]
        if self.budget.exhausted():
            return None

        reply = self.llm.complete(
            system=self.system_prompt(), prompt=description, max_tokens=DEFAULT_MAX_TOKENS
        )
        result: EstimatedRange | None = None
        if reply is not None:
            self.budget.requests += 1
            # A malformed range is a miss, not a crash. The model is asked for
            # a strict format precisely so this stays detectable.
            weights = {}
            for candidate in _candidates(reply.text):
                try:
                    weights = parse_range(candidate)
                except ValueError:
                    continue
                if weights:
                    break
            if weights:
                result = EstimatedRange(
                    weights=weights, spot_key=spot_key, model=reply.model, raw=reply.text
                )
        self._cache[key] = result
        return result


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
