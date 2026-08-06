"""One hand, rendered for a human (or a UI) to read.

`build()` turns a `.phh` file into a JSON-ready dict describing everything about
the hand that can be known for free: seats and positions, the action street by
street, pot sizes, bet sizing as a fraction of the pot, pot odds, stack-to-pot
ratio, and the result. `render_text()` prints the same thing for a terminal.

The shape deliberately splits **facts** from **judgments**:

* Facts are deterministic, free, and always present. A client can render a
  complete, useful hand with no model call and no cost.
* Judgments — "this call was loose", a verdict, an $EV-lost figure — are
  expensive and fallible. They attach to the `analysis` slots, which are `None`
  here and filled in later by the analyze stage.

Keeping them apart means a UI degrades gracefully: an un-analyzed hand still
renders, a partially-analyzed hand renders with some commentary, and a failed
model call costs the user nothing but a missing paragraph.

Money appears twice on purpose. `*_cents` is authoritative (integers, exact,
summable) and `*_bb` is derived for display, rounded, and never summed. Poker is
discussed in big blinds — "3-bet to 9bb", "100bb deep" — so a view layer that
only spoke cents would be unreadable, and one that only spoke big blinds would
accumulate floating-point error. This is the boundary where deriving bb is
correct, because nothing downstream does arithmetic on it.
"""

from __future__ import annotations

from typing import Any

from pokerkit.notation import HandHistory

from .models import ActionType, Cents, Street
from .solvers.base import SolutionProvider
from .replay import (
    _PLAYER_ACTION,
    _STREETS,
    _board,
    big_blind,
    hero_index,
    project_index,
)

SCHEMA_VERSION = 1

_STREET_ORDER = {s: i for i, s in enumerate(_STREETS)}


def _bb(amount: Cents | None, bb: Cents) -> float | None:
    """Display-only conversion. Rounded, and never summed."""
    if amount is None:
        return None
    return round(amount / bb, 2)


def _dollars(amount: Cents) -> str:
    return f"${amount / 100:,.2f}"


def stakes_label(sb: Cents, bb: Cents) -> str:
    return f"{_dollars(sb)}/{_dollars(bb)}"


def build(
    hh: HandHistory,
    *,
    hand_id: int | None = None,
    provider: SolutionProvider | None = None,
) -> dict[str, Any]:
    """Full renderable description of one hand. JSON-serializable.

    `provider` supplies the reference facts. Omit it and every decision's
    `gto` stays None -- the view still renders completely, which is the point
    of keeping the tiers separate.
    """
    bb = big_blind(hh)
    hero = hero_index(hh)
    index = project_index(hh, phh_path="", phh_sha256="", hand_id=hand_id)
    players = len(hh.starting_stacks)
    blinds = hh.blinds_or_straddles or []

    seats = [
        {
            "index": i,
            "position": _position(i, players),
            "is_hero": i == hero,
            "name": (hh.players[i] if hh.players else f"p{i + 1}"),
            "starting_stack_cents": int(s),
            "starting_stack_bb": _bb(int(s), bb),
            "posted_cents": int(blinds[i]) if i < len(blinds) else 0,
            # Only hero's cards are guaranteed. `None` means unknown, which is
            # different from a villain who showed nothing.
            "hole_cards": _hole_from_actions(hh, i),
        }
        for i, s in enumerate(hh.starting_stacks)
    ]

    streets, hero_decisions = _walk(hh, bb, hero, players)
    _attach_gto(hero_decisions, provider, hh, hero)

    return {
        "schema_version": SCHEMA_VERSION,
        "hand": {
            "id": hand_id,
            "site": index.site,
            "site_hand_id": index.site_hand_id,
            "played_at": index.played_at.isoformat(),
            "stakes": {
                "sb_cents": int(blinds[0]) if blinds else 0,
                "bb_cents": bb,
                "currency": index.currency,
                "label": stakes_label(int(blinds[0]) if blinds else 0, bb),
            },
            "table": {"players_dealt": players},
            "hero": {
                "seat": hero,
                "position": index.hero_position.value,
                "hole_cards": _hole_from_actions(hh, hero),
                "eff_stack_bb": round(index.eff_stack_bb, 1),
            },
        },
        "seats": seats,
        "streets": streets,
        "hero_decisions": hero_decisions,
        "result": {
            "street_reached": index.street_reached.value,
            "hero_net_cents": index.hero_net,
            "hero_net_bb": _bb(index.hero_net, bb),
            "rake_cents": index.rake,
            "went_to_showdown": any(" sm " in a for a in hh.actions),
        },
        # Filled by the analyze stage. Present and null so a client can rely on
        # the key existing rather than probing for it.
        "analysis": None,
    }


def _position(i: int, players: int) -> str:
    from .models import position_of

    return position_of(i, players).value


def _hole_from_actions(hh: HandHistory, seat: int) -> list[str] | None:
    """Hole cards from the dealing action, or None if never revealed.

    PHH writes unknown cards as `??`, which we normalize to None rather than
    passing a sentinel up to the UI.
    """
    token = f"d dh p{seat + 1} "
    for action in hh.actions:
        if action.startswith(token):
            cards = action[len(token) :].split("#")[0].strip()
            if "?" in cards:
                return None
            return [cards[i : i + 2] for i in range(0, len(cards), 2)]
    return None


def _walk(
    hh: HandHistory, bb: Cents, hero: int, players: int
) -> tuple[list[dict], list[dict]]:
    """Single pass over the replay, producing per-street action and hero decisions.

    One pass because pokerkit mutates its `State` in place — see `replay.py`.
    """
    streets: dict[Street, dict] = {}
    hero_decisions: list[dict] = []
    # Last aggressive action on the current street, so a decision can say what it
    # was actually facing rather than just a number.
    last_aggressor: dict[str, Any] | None = None
    current_street: Street | None = None

    for idx, (state, _stale) in enumerate(hh.state_actions):
        if idx >= len(hh.actions):
            break
        raw = hh.actions[idx]
        match = _PLAYER_ACTION.match(raw.strip())
        if match is None or match["verb"] in ("sm", "sd", "pb"):
            continue

        seat = int(match["actor"]) - 1
        street = _STREETS[state.street_index]
        if street is not current_street:
            last_aggressor = None
            current_street = street

        to_call: Cents = int(state.checking_or_calling_amount or 0)
        committed: Cents = int(state.bets[seat])
        pot_before: Cents = int(state.total_pot_amount)
        stack_before: Cents = int(state.stacks[seat])
        board = _board(state)

        verb = match["verb"]
        if verb == "f":
            kind, amount, to_amount = ActionType.FOLD, 0, None
        elif verb == "cc":
            kind = ActionType.CALL if to_call > 0 else ActionType.CHECK
            amount, to_amount = to_call, committed + to_call
        else:
            kind = ActionType.RAISE if to_call > 0 else ActionType.BET
            to_amount = int(match["arg"] or 0)
            amount = to_amount - committed

        entry = {
            "action_index": idx,
            "seat": seat,
            "position": _position(seat, players),
            "is_hero": seat == hero,
            "action": kind.value,
            "amount_cents": amount,
            "amount_bb": _bb(amount, bb),
            "to_cents": to_amount,
            "to_bb": _bb(to_amount, bb),
            "pot_before_cents": pot_before,
            "pot_before_bb": _bb(pot_before, bb),
            # How big the bet was relative to the pot -- how a human reads a
            # postflop sizing. Suppressed preflop, where raises are conventionally
            # described as a multiple of the blind ("2.5x"), never as a fraction
            # of a pot that is mostly dead blind money. A "167% pot" open would
            # read as a mistake in the renderer to anyone who plays.
            "pct_pot": (
                round(100 * amount / pot_before)
                if amount and pot_before and street is not Street.PREFLOP
                else None
            ),
            "is_all_in": amount > 0 and amount >= stack_before,
        }

        bucket = streets.setdefault(
            street,
            {
                "street": street.value,
                "board": [board[i : i + 2] for i in range(0, len(board), 2)],
                "pot_start_cents": pot_before,
                "pot_start_bb": _bb(pot_before, bb),
                "actions": [],
            },
        )
        bucket["actions"].append(entry)

        if seat == hero:
            hero_decisions.append(
                _decision(entry, street, bb, to_call, pot_before, stack_before, board, last_aggressor)
            )

        if kind.is_aggressive:
            last_aggressor = {
                "position": entry["position"],
                "action": kind.value,
                "to_bb": entry["to_bb"],
                "pct_pot": entry["pct_pot"],
            }

    ordered = sorted(streets.values(), key=lambda s: _STREET_ORDER[Street(s["street"])])
    return ordered, hero_decisions


def _decision(
    entry: dict,
    street: Street,
    bb: Cents,
    to_call: Cents,
    pot_before: Cents,
    stack_before: Cents,
    board: str,
    last_aggressor: dict | None,
) -> dict:
    pot_odds = to_call / (pot_before + to_call) if to_call > 0 else None
    return {
        "action_index": entry["action_index"],
        "street": street.value,
        "board": [board[i : i + 2] for i in range(0, len(board), 2)],
        "hero_action": entry["action"],
        "amount_bb": entry["amount_bb"],
        "to_bb": entry["to_bb"],
        "pot_before_bb": _bb(pot_before, bb),
        "to_call_bb": _bb(to_call, bb),
        "pct_pot": entry["pct_pot"],
        # Undefined facing a check, which is why it's nullable rather than 0.
        "pot_odds": round(pot_odds, 3) if pot_odds is not None else None,
        # Stack-to-pot ratio: how deep the remaining play is relative to what is
        # already contested, and the standard shorthand for how committed a
        # postflop pot is. Deliberately null preflop -- it is arithmetically
        # defined there but carries no meaning, since the pot is still just
        # blinds. Reporting "SPR 24.8" on a preflop fold is noise dressed as
        # information.
        "spr": (
            round(stack_before / pot_before, 1)
            if pot_before and street is not Street.PREFLOP
            else None
        ),
        "stack_before_bb": _bb(stack_before, bb),
        "facing": last_aggressor,
        # Canonical description of the spot. This is the cache key a
        # SolutionProvider is queried with, and it must depend only on the
        # abstract situation -- position, action sequence, stack depth, board --
        # never on this specific hand, or every lookup is a cache miss.
        "spot_key": _spot_key(street, entry, last_aggressor, stack_before, pot_before, bb),
        # Reference facts: what equilibrium does at this node. Deterministic and
        # cacheable, so it belongs with the facts rather than with the judgments
        # -- it is a lookup, not an opinion. Populated by a SolutionProvider;
        # None means "not looked up", which a client must render differently from
        # "looked up and equilibrium is indifferent".
        "gto": None,
        # Slot for the analyze stage's judgment.
        "analysis": None,
    }


def _spot_key(
    street: Street,
    entry: dict,
    facing: dict | None,
    stack_before: Cents,
    pot_before: Cents,
    bb: Cents,
) -> str:
    """Abstract identifier for the situation, independent of hero's cards.

    Bucketed on purpose. An exact stack depth would make every spot unique and
    the solution cache useless, so depth is rounded to the nearest 25bb -- the
    granularity charts are actually published at.
    """
    depth = max(25, int(round((stack_before / bb) / 25.0) * 25))
    parts = [entry["position"], street.value]
    if facing is None:
        parts.append("unopened")
    else:
        parts.append(f"vs_{facing['position']}_{facing['action']}_{facing['to_bb']}bb")
    parts.append(f"{depth}bb")
    return "_".join(str(p) for p in parts)


def render_text(view: dict[str, Any]) -> str:
    """Terminal rendering of a built view. Debug and eyeball use, not a product."""
    h = view["hand"]
    out: list[str] = []
    hero_cards = " ".join(h["hero"]["hole_cards"] or ["??"])
    out.append(
        f"{h['site']}/{h['site_hand_id']}  {h['stakes']['label']}  "
        f"{h['table']['players_dealt']}-handed"
    )
    out.append(
        f"hero {h['hero']['position']} with {hero_cards}, "
        f"{h['hero']['eff_stack_bb']}bb effective"
    )
    out.append("")

    for street in view["streets"]:
        board = " ".join(street["board"]) or "-"
        out.append(f"  {street['street'].upper():<8} board {board}  (pot {street['pot_start_bb']}bb)")
        for a in street["actions"]:
            marker = "*" if a["is_hero"] else " "
            size = ""
            if a["to_bb"] is not None and a["action"] in ("bet", "raise"):
                size = f" to {a['to_bb']}bb"
                if a["pct_pot"]:
                    size += f" ({a['pct_pot']}% pot)"
            elif a["action"] == "call":
                size = f" {a['amount_bb']}bb"
            allin = "  ALL-IN" if a["is_all_in"] else ""
            out.append(f"   {marker} {a['position']:<4} {a['action']}{size}{allin}")
        out.append("")

    out.append("  hero decisions")
    for d in view["hero_decisions"]:
        bits = [f"{d['street']:<8} {d['hero_action']}"]
        if d["pot_odds"] is not None:
            bits.append(f"pot odds {d['pot_odds']:.1%}")
        if d["spr"] is not None:
            bits.append(f"SPR {d['spr']}")
        if d["facing"]:
            f = d["facing"]
            bits.append(f"facing {f['position']} {f['action']} to {f['to_bb']}bb")
        note = d["analysis"]["comment"] if d.get("analysis") else "(not analyzed)"
        out.append(f"    {'  |  '.join(bits)}")
        out.append(f"        {note}")

    r = view["result"]
    out.append("")
    out.append(
        f"  result: {r['street_reached']}, hero {r['hero_net_bb']:+}bb"
        f"{', showdown' if r['went_to_showdown'] else ''}"
    )
    return "\n".join(out)


def _attach_gto(
    decisions: list[dict],
    provider: SolutionProvider | None,
    hh: HandHistory,
    hero: int,
) -> None:
    """Fill the `gto` block on each decision, where the provider has an answer.

    A provider that returns None leaves the slot None. A provider that raises is
    a bug in the provider, not a reason to lose the hand -- so failures are
    swallowed per decision and the rest of the view survives.
    """
    if provider is None:
        return
    cards = _hole_from_actions(hh, hero)
    if not cards:
        return
    hand = "".join(cards)
    for d in decisions:
        try:
            solution = provider.lookup(d["spot_key"], hand)
        except Exception:
            solution = None
        if solution is not None:
            d["gto"] = solution.as_fact()
