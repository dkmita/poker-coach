"""Types shared across pipeline stages.

This is deliberately thin. Hands are **not** represented here — a hand is a
`.phh` file on disk (the PHH open standard, TOML-based) and `pokerkit` replays it
to produce game state. What lives here is only what we add on top of that:

  * the semantic enums PHH doesn't give us directly (`Street`, `Position`,
    `ActionType`)
  * `HandIndex` -- the handful of filterable dimensions we mirror into SQLite so
    "all my BB folds vs a button open, 100bb deep" is a query and not a full
    corpus rescan
  * `Decision` -- one hero decision point with replayed state, derived on demand
    from a `.phh` file and never persisted in full
  * the pipeline's own output types, which PHH has no opinion about

The division of labour:

    .phh file      system of record for a hand. Immutable, human-readable,
                   diffable, testable against the public PHH dataset.
    pokerkit       replay engine. Pot sizes, side pots, legal actions, stack
                   tracking, hand evaluation. We do not reimplement any of it.
    SQLite         index over the archive + all mutable pipeline state.

Money is integer cents everywhere, including EV. Never float: SQLite has no
decimal type and a session's worth of pot arithmetic in binary floating point
accumulates error. Big blinds are the analytical unit (charts and solver output
are denominated in bb) but are derived late, for comparison and display only --
`0.74bb` is a terminating decimal and a repeating binary fraction, so bb values
are never summed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

Cents = int

RANKS = "23456789TJQKA"
SUITS = "cdhs"

# PHH permits user-defined fields. Ours carry a namespace prefix so a future
# revision of the spec can add fields without colliding with them.
PHH_SITE = "_pc_site"
PHH_SITE_HAND_ID = "_pc_site_hand_id"
PHH_SOURCE_FILE = "_pc_source_file"
# The site's own text for this hand, verbatim. Kept because the parser is the
# one place that can be wrong in a way nothing downstream can detect: if a
# pot or a position looks off, the only way to settle it is the original.
PHH_SOURCE_TEXT = "_pc_source_text"
# PHH is player-neutral by design -- it archives a hand, not one player's view of
# it -- so "hero" is our concept and has to be carried as an extension. 0-based,
# matching PHH's player ordering (`p1` is index 0).
PHH_HERO_INDEX = "_pc_hero_index"


class Street(Enum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"

    @property
    def board_length(self) -> int:
        """Number of board cards visible on this street."""
        return {"preflop": 0, "flop": 3, "turn": 4, "river": 5}[self.value]


class Position(Enum):
    """Seat position relative to the button.

    Ordered as preflop action proceeds, which is also how charts are indexed.
    `SB`/`BB` act last preflop and first afterwards; that asymmetry lives in the
    replayed action order, not here.
    """

    UTG = "UTG"
    UTG1 = "UTG+1"
    HJ = "HJ"
    CO = "CO"
    BTN = "BTN"
    SB = "SB"
    BB = "BB"


# Which positions exist for a given number of players dealt in. Assigned from the
# button backwards, so short tables drop the earliest seats rather than renaming
# the late ones -- a CO is a CO whether the table is 6-handed or 4-handed, which
# is what keeps charts comparable across table sizes.
POSITIONS_BY_PLAYER_COUNT: dict[int, tuple[Position, ...]] = {
    2: (Position.BTN, Position.BB),  # heads up: the button posts the small blind
    3: (Position.BTN, Position.SB, Position.BB),
    4: (Position.CO, Position.BTN, Position.SB, Position.BB),
    5: (Position.HJ, Position.CO, Position.BTN, Position.SB, Position.BB),
    6: (Position.UTG, Position.HJ, Position.CO, Position.BTN, Position.SB, Position.BB),
    7: (
        Position.UTG,
        Position.UTG1,
        Position.HJ,
        Position.CO,
        Position.BTN,
        Position.SB,
        Position.BB,
    ),
}


def position_of(player_index: int, players_dealt: int) -> Position:
    """Position of a PHH player index (0-based) in a button game.

    PHH orders players by posting order: index 0 posts the small blind, index 1
    the big blind, and the button is last. `blinds_or_straddles` encodes this, so
    position follows from the index and the player count alone.

    Heads up is the special case worth remembering -- the button posts the small
    blind, so index 0 *is* the button.
    """
    try:
        layout = POSITIONS_BY_PLAYER_COUNT[players_dealt]
    except KeyError:
        raise ValueError(f"no position layout for {players_dealt} players") from None
    if not 0 <= player_index < players_dealt:
        raise ValueError(f"player index {player_index} out of range for {players_dealt}")

    if players_dealt == 2:
        return (Position.BTN, Position.BB)[player_index]
    # index 0 -> SB, 1 -> BB, then wrap to the earliest position and run to BTN.
    order = (Position.SB, Position.BB, *layout[: layout.index(Position.SB)])
    return order[player_index]


class ActionType(Enum):
    """Semantic action, resolved from PHH notation plus replayed state.

    PHH collapses check and call into one token (`cc`) and bet and raise into
    another (`cbr`), because both distinctions are recoverable from game state.
    They are not interchangeable for our purposes -- checking and calling are
    entirely different decisions, and a detector keyed on the wrong one is
    measuring nothing -- so the replay resolves each to one of these using
    `to_call`.
    """

    FOLD = "fold"
    CHECK = "check"  # PHH cc with to_call == 0
    CALL = "call"  # PHH cc with to_call > 0
    BET = "bet"  # PHH cbr with to_call == 0
    RAISE = "raise"  # PHH cbr with to_call > 0

    @property
    def is_aggressive(self) -> bool:
        return self in (ActionType.BET, ActionType.RAISE)

    @property
    def puts_money_in(self) -> bool:
        return self not in (ActionType.FOLD, ActionType.CHECK)


class Verdict(Enum):
    MISTAKE = "mistake"
    MARGINAL = "marginal"
    FINE = "fine"
    UNCLEAR = "unclear"


@dataclass(frozen=True, slots=True)
class HandIndex:
    """The filterable projection of a hand -- one row in the `hands` table.

    Only dimensions worth querying across the corpus, plus a pointer to the
    archive. The rule: index what you *filter* on, keep detail in the `.phh`
    file, and add a column only when a query proves slow. Mirroring the action
    list into SQL would be duplicated state with a drift failure mode, and
    pokerkit already owns the replay.
    """

    site: str
    site_hand_id: str
    phh_path: str
    phh_sha256: str

    played_at: datetime  # timezone-aware, stored UTC

    bb: Cents
    currency: str = "USD"
    players_dealt: int = 6

    hero_position: Position = Position.BTN
    # Effective stack in big blinds: hero vs the deepest opponent still live
    # preflop. Stored because it selects a strategy (100bb and 40bb are different
    # games) and is therefore a constant filter, not a detail.
    eff_stack_bb: float = 100.0
    street_reached: Street = Street.PREFLOP

    # Hero's money result, net of rake. Not an EV judgement -- a correct fold
    # shows 0 and a bad call can show a win. Its job is reconciliation: PHH
    # `finishing_stacks` is ground truth, so a mismatch means a parser bug.
    hero_net: Cents = 0
    # PHH has no rake field; the spec's position is that it is reconstructed from
    # finishing stacks. Reconstructed at ingest and stored, because rake is what
    # makes marginal opens unprofitable and a model that can't express it will
    # rate the loosest opens as fine.
    rake: Cents = 0

    id: int | None = None

    def to_bb(self, amount: Cents) -> float:
        """Normalize cents to big blinds. For comparison and display only."""
        return amount / self.bb


@dataclass(frozen=True, slots=True)
class Decision:
    """One hero decision with the game state that faced it.

    Derived on demand by replaying a `.phh` file; never stored in full. The only
    part that persists is `action_index`, which is how a flagged decision or
    finding refers back to a specific spot. That index is pokerkit's action
    ordering rather than ours, so it stays stable across re-analysis.
    """

    hand_id: int
    action_index: int
    street: Street
    position: Position
    action: ActionType

    hole_cards: str  # canonical, e.g. "AhKd"
    board: str  # as of this decision; "" preflop

    # Chips this action adds. 0 for fold and check. For a call, the amount needed
    # to match -- not the total already invested this street.
    amount: Cents = 0
    # Total commitment on this street after the action; None for fold and check.
    # This is PHH's `cbr` argument and the figure charts use ("3-bet to 9bb").
    # Both are kept because converting between them at each call site is where
    # off-by-one and sign errors come from.
    to_amount: Cents | None = None

    pot_before: Cents = 0
    stack_before: Cents = 0
    # Cost to continue. 0 when unopened, which is what separates a bet from a
    # raise and a check from a call.
    to_call: Cents = 0
    is_all_in: bool = False

    @property
    def pot_odds(self) -> float | None:
        """Share of the resulting pot hero must contribute to continue.

        `None` facing a check, where pot odds are undefined.
        """
        if self.to_call <= 0:
            return None
        return self.to_call / (self.pot_before + self.to_call)


@dataclass(frozen=True, slots=True)
class DetectorHit:
    """One triage rule firing on one decision."""

    # Stable slug, e.g. 'bb_defend_underfold'. Reports and precision metrics
    # group by this, so a rename silently splits a detector's history in two.
    detector: str
    # Rough pre-agent estimate, used only to order the queue so the most
    # expensive analysis goes to the most expensive-looking mistakes. The
    # authoritative figure is Finding.ev_lost.
    est_ev_lost: Cents | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class FlaggedDecision:
    """A hero decision that triage thinks is worth paying an agent to judge.

    A suspicion, not a verdict. The unit is the *decision*, not the detector: two
    rules can fire on the same call, and one row per detector would put that
    mistake in the report twice and count its cost twice in any total.
    """

    hand_id: int
    action_index: int
    # Canonical spot description, used as the chart and solver lookup key, e.g.
    # 'BB_vs_BTN_open_2.5bb'.
    spot_key: str
    hits: tuple[DetectorHit, ...] = ()
    priority: float = 0.0
    id: int | None = None

    @property
    def est_ev_lost(self) -> Cents | None:
        """Worst single-detector estimate, for queue ordering."""
        estimates = [h.est_ev_lost for h in self.hits if h.est_ev_lost is not None]
        return max(estimates) if estimates else None


@dataclass(frozen=True, slots=True)
class Finding:
    """The agent's judgement of one flagged decision."""

    flagged_decision_id: int
    hand_id: int
    action_index: int
    verdict: Verdict

    hero_action: str
    recommended_action: str | None = None

    # The ranking currency. Reports rank by money, never by error count -- that
    # is what keeps output actionable instead of a wall of nitpicks. 0 for a
    # 'fine' verdict; None when genuinely not estimable.
    ev_lost: Cents | None = None
    confidence: float | None = None

    rationale: str = ""
    # Equity numbers, chart frequencies, solver output the verdict rests on.
    # Retained so a finding can be audited without re-running the agent.
    evidence: dict = field(default_factory=dict)

    model: str | None = None
    id: int | None = None
