"""What equilibrium does at a node, and where that answer comes from.

Solver output is a **fact**, not a judgment. "The big blind defends this hand
100% versus a 2.5x button open at 100bb" is a lookup: deterministic, cacheable,
and the same answer no matter who asks. That is why it belongs in the facts layer
of a hand view alongside pot odds, and not in the analyze stage's commentary.

The practical consequence is that a hand view is useful with no model call at
all. "You folded, laying 27%; equilibrium calls 100% here" is a complete and
actionable statement. The LLM's job is explaining *why* and what it costs, not
supplying the reference.

Everything is keyed on `spot_key` -- the abstract situation, never the specific
hand -- so a solution fetched once serves every hand that reaches the same node.
That is what makes an expensive or rate-limited provider viable: the first lookup
costs, the rest are free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..models import Cents


@dataclass(frozen=True, slots=True)
class ActionFrequency:
    """How often equilibrium takes one action with one holding at one node."""

    action: str  # "fold" | "check" | "call" | "bet" | "raise"
    frequency: float  # 0..1
    # EV in chips, if the provider reports it. Charts usually don't; solvers do.
    # None means unknown, which a client must not render as zero.
    ev: Cents | None = None
    # Sizing for aggressive actions, in big blinds. Solvers offer several.
    to_bb: float | None = None


@dataclass(frozen=True, slots=True)
class Solution:
    """Equilibrium strategy for one holding at one node.

    Deliberately narrow: the strategy for *hero's actual hand*, not the whole
    range matrix. A full node solution is large, and the view only ever needs the
    row for the hand that was held. Providers that fetch a matrix should cache the
    matrix and return this slice.
    """

    spot_key: str
    hand: str  # canonical, e.g. "AdJs"
    provider: str
    actions: tuple[ActionFrequency, ...] = ()
    # Free-text note from the provider (e.g. which chart or sim this came from).
    source: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def best(self) -> ActionFrequency | None:
        """Highest-frequency action. The headline number for a UI."""
        return max(self.actions, key=lambda a: a.frequency, default=None)

    def frequency_of(self, action: str) -> float | None:
        for a in self.actions:
            if a.action == action:
                return a.frequency
        return None

    def is_mixed(self, threshold: float = 0.15) -> bool:
        """True when equilibrium splits meaningfully between actions.

        Matters for how a client should present a deviation: taking the 30% option
        at a genuinely mixed node is not a mistake, and calling it one is the
        fastest way to lose a user's trust in the tool.
        """
        return sum(1 for a in self.actions if a.frequency >= threshold) > 1

    def as_fact(self) -> dict:
        """The `gto` block of a hand view's decision. JSON-serializable."""
        return {
            "provider": self.provider,
            "source": self.source,
            "spot_key": self.spot_key,
            "hand": self.hand,
            "mixed": self.is_mixed(),
            "actions": [
                {"action": a.action, "frequency": round(a.frequency, 3),
                 "ev_cents": a.ev, "to_bb": a.to_bb}
                for a in sorted(self.actions, key=lambda a: -a.frequency)
            ],
        }


@runtime_checkable
class SolutionProvider(Protocol):
    """Source of equilibrium strategy for a node.

    Implementations must be **cache-first and failure-tolerant**. A provider that
    is offline, rate-limited, or broken returns `None`; it does not raise and it
    does not block. Nothing else in the pipeline may depend on a provider being
    reachable — a scraper breaking must degrade the output, never stop the run.
    """

    name: str

    def lookup(self, spot_key: str, hand: str) -> Solution | None:
        """Equilibrium strategy for `hand` at `spot_key`, or None if unavailable.

        `None` means "no answer", which is different from "equilibrium is
        indifferent here" — the latter is a Solution with mixed frequencies. A
        client has to render those two differently or it will imply knowledge it
        doesn't have.
        """
        ...


class NullProvider:
    """Answers nothing. The default, and the honest one until a real source lands.

    Exists so the pipeline can be built and tested end to end before the
    chart-versus-solver question is settled. Hand views render fully with `gto:
    null` on every decision; wiring a real provider fills them in with no change
    to any caller.
    """

    name = "null"

    def lookup(self, spot_key: str, hand: str) -> Solution | None:
        return None
