"""The pokerkit boundary: a `.phh` file in, `Decision`s and a `HandIndex` out.

Everything that needs to know how a hand *unfolds* goes through here. Triage asks
this module what hero faced at each decision; the indexer asks it for the handful
of dimensions worth putting in SQL. Nothing else imports `pokerkit`.

Keeping the dependency behind one module matters for two reasons. pokerkit is
0.x, so its API will move, and this is the only file that should have to change
when it does. And game logic -- pot sizes, side pots, betting legality, stack
tracking -- is genuinely hard to get right; there must be exactly one
implementation of it in the system, and it must not be ours.

Semantics worth knowing before editing this file, all verified against pokerkit
0.7.4 rather than assumed:

* **`hh.state_actions` yields one `State` object, mutated in place.** Every pair
  hands back the same instance, so `[s for s, _ in hh.state_actions]` is a list
  of N references to the *final* state, not a history. Read what you need during
  the pass, before the next step mutates it. This fails silently and
  plausibly -- you get real-looking numbers from the wrong moment in the hand.

* **The pairs are not positionally aligned with `hh.actions` at all.** Pair `i`
  is `(state_after_action_i, action_i)`, so the state facing an action is the
  one from the *previous* pair. That much looks like a plain off-by-one, and
  treating it as one works right up to the flop -- but pokerkit also emits
  states for its own automatic operations, which have no PHH action and come
  back with `None` in the action half. A heads-up hand that reaches the river
  yields 21 pairs for 16 actions, and the drift grows by one at every street.
  Reading `hh.actions[i]` against pair `i` therefore judges each postflop action
  against the state one action too early: `to_call` reads 0, so calls render as
  checks and raises as bets, and the numbers stay plausible throughout.
  `iter_action_states` pairs each action with the last state seen before it and
  ignores positional indexing entirely; it is the only correct way to walk a
  replay, and everything in this codebase goes through it.
* `state.street_index` maps directly onto `Street` by position (0 preflop, 1
  flop, 2 turn, 3 river). It is *not* derivable from board length -- at the first
  state of each street the board card hasn't been dealt yet.
* `state.checking_or_calling_amount` is the cost to continue, and it is what
  resolves PHH's deliberately ambiguous tokens: `cc` is a check at 0 and a call
  above it, `cbr` a bet at 0 and a raise above it.
* pokerkit **validates** the action sequence and raises `ValueError` on an
  illegal one. That is free correctness checking for the site parser -- a hand
  that replays at all is a hand whose betting sequence is internally consistent.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pokerkit.notation import HandHistory

from .models import (
    PHH_HERO_INDEX,
    PHH_SITE,
    PHH_SITE_HAND_ID,
    ActionType,
    Cents,
    Decision,
    HandIndex,
    Street,
    position_of,
)

# street_index is a positional index into the streets of the variant.
_STREETS: tuple[Street, ...] = (Street.PREFLOP, Street.FLOP, Street.TURN, Street.RIVER)

# "p3 cbr 250 # comment" -> actor 3, verb cbr, arg 250. Dealer lines ("d dh ...",
# "d db ...") deliberately don't match: they aren't decisions.
_PLAYER_ACTION = re.compile(
    r"^p(?P<actor>\d+)\s+(?P<verb>f|cc|cbr|sm|sd|pb)(?:\s+(?P<arg>\S+))?"
)


class ReplayError(ValueError):
    """A `.phh` file that pokerkit could not replay.

    Almost always a parser bug rather than a corrupt archive: the site parser
    emitted an action sequence that isn't legal poker.
    """


def load(path: str | Path) -> HandHistory:
    """Parse a `.phh` file. Raises `ReplayError` if it won't replay."""
    path = Path(path)
    try:
        hh = HandHistory.loads(path.read_text())
        # Force the replay now so a bad hand fails here, at a point where we can
        # name the file, rather than deep inside a triage loop.
        _ = list(hh.state_actions)
    except ValueError as exc:
        raise ReplayError(f"{path}: {exc}") from exc
    return hh


def hero_index(hh: HandHistory) -> int:
    """0-based index of the hero seat.

    PHH has no hero concept, so this is carried as a user-defined field written
    at ingest.
    """
    raw = hh.user_defined_fields.get(PHH_HERO_INDEX)
    if raw is None:
        raise ReplayError(f"missing {PHH_HERO_INDEX}; ingest must record the hero seat")
    return int(raw)


def big_blind(hh: HandHistory) -> Cents:
    """The big blind, in cents.

    PHH's `blinds_or_straddles` is indexed by posting order -- index 0 posts the
    small blind, index 1 the big blind -- so later non-zero entries are straddles
    and must not be mistaken for it.
    """
    blinds = hh.blinds_or_straddles or []
    if len(blinds) < 2:
        raise ReplayError("blinds_or_straddles must have at least two entries")
    # Index 1 at every table size. Heads-up pokerkit posts the array reversed --
    # entry 1 goes to the player at index 0 -- so the entry is not that player's
    # own post, but it is still the big blind, which is all this needs.
    return int(blinds[1])


def _card(card: object) -> str:
    """Canonical card string: uppercase rank, lowercase suit ('Ah')."""
    return f"{card.rank.value}{card.suit.value}"  # type: ignore[attr-defined]


def _board(state: object) -> str:
    """Board as of this state, concatenated in dealt order.

    `state.board_cards` is a list of one-card groups, not a flat list.
    """
    return "".join(
        _card(c) for group in state.board_cards for c in group  # type: ignore[attr-defined]
    )


def _hole(state: object, actor: int) -> str:
    cards = state.hole_cards[actor]  # type: ignore[attr-defined]
    return "".join(_card(c) for c in cards)


@dataclass(frozen=True, slots=True)
class ActionState:
    """One action, and the state it actually faced.

    A snapshot rather than a live `State`: pokerkit mutates one object in place,
    so anything read after the walk moves on is read from the wrong moment.
    Scalars are copied out at the moment they are true.
    """

    action_index: int
    """Index into `hh.actions`. Stable across re-analysis, which is what makes it
    usable as the key of a `flagged_decisions` row."""
    raw: str
    actor_index: int | None
    street: Street
    to_call: Cents
    bets: tuple[Cents, ...]
    stacks: tuple[Cents, ...]
    pot: Cents
    board: str
    hole: tuple[str, ...]


def _snapshot(state: object) -> dict:
    street = getattr(state, "street_index", None)
    return {
        "actor_index": state.actor_index,  # type: ignore[attr-defined]
        "street": _STREETS[street] if street is not None else _STREETS[0],
        "to_call": int(state.checking_or_calling_amount or 0),  # type: ignore[attr-defined]
        "bets": tuple(int(b) for b in state.bets),  # type: ignore[attr-defined]
        "stacks": tuple(int(s) for s in state.stacks),  # type: ignore[attr-defined]
        "pot": int(state.total_pot_amount),  # type: ignore[attr-defined]
        "board": _board(state),
        "hole": tuple(
            _hole(state, i)
            for i in range(len(state.hole_cards))  # type: ignore[attr-defined]
        ),
    }


def iter_action_states(hh: HandHistory) -> Iterator[ActionState]:
    """Every action in the hand, paired with the state that faced it.

    Pairing comes from the walk itself -- the state carried forward from the
    previous pair -- never from indexing `hh.actions`, because pokerkit
    interleaves states for its own automatic operations and the two sequences
    are different lengths. See the module docstring.
    """
    # pokerkit does not reject an action sequence it disagrees with -- it
    # *repairs* it, silently inserting the action it expected. That is how a
    # heads-up encoding that had the blinds the wrong way round survived: a
    # phantom check for the button at the top of every postflop street, which
    # costs no chips, so even reconciling against the site's finishing stacks
    # did not notice. A repair shows up as a state with no action while a player
    # still has one pending; the automatic operations that are legitimate
    # (runout selection, showdown, pushing chips) all come after the last one.
    last_player = -1
    for i, raw in enumerate(hh.actions):
        m = _PLAYER_ACTION.match(raw.strip())
        if m is not None and m["verb"] not in ("sm", "sd", "pb"):
            last_player = i

    facing: dict | None = None
    index = -1
    for state, applied in hh.state_actions:
        if applied is None:
            if 0 <= index < last_player:
                raise ReplayError(
                    f"pokerkit repaired the hand after action {index} "
                    f"({hh.actions[index]!r}): it inserted an operation the file "
                    f"does not contain, so the file disagrees with it about who "
                    f"acts or what they did"
                )
        else:
            index += 1
            if index >= len(hh.actions):
                break  # pokerkit replayed more than the file declared
            # The action half is pokerkit's echo of what it applied. If it has
            # stopped matching the file we are pairing against the wrong hand,
            # and every number downstream is quietly wrong.
            if applied.strip() != hh.actions[index].strip():
                raise ReplayError(
                    f"replay diverged at action {index}: file has "
                    f"{hh.actions[index]!r}, pokerkit applied {applied!r}"
                )
            if facing is not None:
                yield ActionState(action_index=index, raw=applied, **facing)
        facing = _snapshot(state)


def parse_player_action(
    st: ActionState,
) -> tuple[int, ActionType, Cents, Cents | None] | None:
    """`(seat, kind, amount, to_amount)` for a voluntary action, else `None`.

    `None` covers everything that is not a decision we judge: dealer actions,
    shows and mucks, discards, bring-ins.

    Resolving PHH's collapsed verbs lives here rather than in each caller
    because `to_call` is the only thing separating a check from a call and a bet
    from a raise, and a second copy of that rule is a second chance to pair it
    with the wrong state -- which is exactly how postflop calls came to render
    as checks.
    """
    match = _PLAYER_ACTION.match(st.raw.strip())
    if match is None:
        return None  # dealer action
    verb = match["verb"]
    if verb in ("sm", "sd", "pb"):
        return None  # show/muck, stand-pat/discard, bring-in

    seat = int(match["actor"]) - 1
    # The state must be the one this action faced. This is the check that turns
    # a pairing regression into a loud failure instead of plausible numbers read
    # from the wrong moment in the hand.
    if st.actor_index != seat:
        raise ReplayError(
            f"replay misaligned at action {st.action_index} ({st.raw!r}): "
            f"state expects seat {st.actor_index}, action is seat {seat}"
        )

    committed = st.bets[seat]
    if verb == "f":
        return seat, ActionType.FOLD, 0, None
    if verb == "cc":
        kind = ActionType.CALL if st.to_call > 0 else ActionType.CHECK
        return seat, kind, st.to_call, committed + st.to_call
    kind = ActionType.RAISE if st.to_call > 0 else ActionType.BET
    to_amount = int(match["arg"] or 0)
    return seat, kind, to_amount - committed, to_amount


def iter_decisions(
    hh: HandHistory, *, hand_id: int = 0, actor: int | None = None
) -> Iterator[Decision]:
    """Yield one `Decision` per voluntary action, in dealt order.

    `actor` restricts output to one seat (pass `hero_index(hh)` for triage).
    Dealer actions, showdowns, discards, and bring-ins are skipped -- none of them
    are decisions we judge.

    `Decision.action_index` is the index into `hh.actions`, which is pokerkit's
    ordering rather than ours. That is what makes it a stable reference for
    `flagged_decisions` across re-analysis.
    """
    for st in iter_action_states(hh):
        parsed = parse_player_action(st)
        if parsed is None:
            continue
        seat, kind, amount, to_amount = parsed
        if actor is not None and seat != actor:
            continue

        yield Decision(
            hand_id=hand_id,
            action_index=st.action_index,
            street=st.street,
            position=position_of(seat, len(hh.starting_stacks)),
            action=kind,
            hole_cards=st.hole[seat],
            board=st.board,
            amount=amount,
            to_amount=to_amount,
            pot_before=st.pot,
            stack_before=st.stacks[seat],
            to_call=st.to_call,
            # A fold or check commits nothing, so it is never all-in -- without
            # the first clause a player already at zero chips would be reported
            # all-in for folding.
            is_all_in=amount > 0 and amount >= st.stacks[seat],
        )


def _played_at(hh: HandHistory) -> datetime:
    """Best available timestamp, as timezone-aware UTC.

    PHH's date and time fields are all optional and independent, so a hand can
    legitimately carry a date with no time. Falls back to epoch rather than
    guessing, so a missing timestamp is visible in the corpus instead of silently
    becoming "now".
    """
    if hh.year and hh.month and hh.day:
        t = hh.time
        return datetime(
            hh.year, hh.month, hh.day, t.hour if t else 0, t.minute if t else 0,
            t.second if t else 0, tzinfo=UTC,
        )
    return datetime(1970, 1, 1, tzinfo=UTC)


def project_index(
    hh: HandHistory, *, phh_path: str, phh_sha256: str, hand_id: int | None = None
) -> HandIndex:
    """Build the `hands` row: the filterable dimensions plus a pointer.

    Deliberately narrow. Everything else is recoverable by replaying `phh_path`,
    and mirroring more into SQL would be duplicated state that can drift from the
    archive it copies.
    """
    hero = hero_index(hh)
    bb = big_blind(hh)
    players = len(hh.starting_stacks)

    # Effective stack: hero against the deepest opponent, from starting stacks.
    # Deliberately not pokerkit's `get_effective_stack`, which reads the current
    # state -- and the state it hands back has already posted blinds, so a 100bb
    # hand reports 99bb from the big blind. Charts are indexed on round stack
    # depths, so that one-blind drift would bucket hands wrong.
    stacks = [int(s) for s in hh.starting_stacks]
    opponents = [s for i, s in enumerate(stacks) if i != hero]
    eff_stack = min(stacks[hero], max(opponents)) if opponents else stacks[hero]

    # Track the deepest street during the pass rather than inspecting the final
    # state: the state object is mutated in place, so "the last one" is a
    # reference, not a snapshot, and its board is easy to misread.
    board_len = 0
    for state, _ in hh.state_actions:
        board_len = max(board_len, len(state.board_cards))
    # Board card count identifies the street reached: 0 / 3 / 4 / 5.
    street_reached = _STREETS[0] if board_len == 0 else _STREETS[board_len - 2]

    # finishing_stacks is optional in PHH but is the ground truth we reconcile
    # against; without it hero_net and rake are not knowable from the archive.
    if hh.finishing_stacks:
        hero_net = int(hh.finishing_stacks[hero]) - int(hh.starting_stacks[hero])
        rake = max(0, sum(hh.starting_stacks) - sum(hh.finishing_stacks))
    else:
        hero_net, rake = 0, 0

    udf = hh.user_defined_fields
    return HandIndex(
        id=hand_id,
        site=str(udf.get(PHH_SITE, "unknown")),
        site_hand_id=str(udf.get(PHH_SITE_HAND_ID, hh.hand or "")),
        phh_path=phh_path,
        phh_sha256=phh_sha256,
        played_at=_played_at(hh),
        bb=bb,
        currency=hh.currency or "USD",
        players_dealt=players,
        hero_position=position_of(hero, players),
        eff_stack_bb=eff_stack / bb,
        street_reached=street_reached,
        hero_net=hero_net,
        rake=rake,
    )
