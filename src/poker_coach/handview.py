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

import re
from typing import Any

from pokerkit.notation import HandHistory

from .models import PHH_COLLECTED, PHH_SOURCE_TEXT, ActionType, Cents, Street
from .solvers.base import SolutionProvider
from .replay import (
    _STREETS,
    big_blind,
    hero_index,
    iter_action_states,
    parse_player_action,
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
    blinds_raw = hh.blinds_or_straddles or []
    sb = int(blinds_raw[0]) if blinds_raw else 0
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

    streets, hero_decisions, hero_last_street = _walk(hh, bb, hero, players)
    _attach_gto(hero_decisions, provider, hh, hero)

    return {
        "schema_version": SCHEMA_VERSION,
        "hand": {
            "id": hand_id,
            "site": index.site,
            "site_hand_id": index.site_hand_id,
            "played_at": index.played_at.isoformat(),
            "stakes": {
                "sb_cents": sb,
                "bb_cents": bb,
                "currency": index.currency,
                "label": stakes_label(sb, bb),
            },
            "table": {"players_dealt": players},
            # The site's own text, verbatim. Everything else in this view is
            # something we derived, and the parser is the one link in that chain
            # nothing downstream can check: if a pot or a position looks wrong,
            # this is what settles it. None for a hand with no recorded source
            # (the synthetic corpus), which is not the same as an empty one.
            "source_text": str(hh.user_defined_fields.get(PHH_SOURCE_TEXT) or "") or None,
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
            # How far the HAND went -- the board that was dealt. True even if
            # hero folded preflop and the others ran it out.
            "street_reached": index.street_reached.value,
            "showdown": any(" sm " in a for a in hh.actions),
            # How far HERO went. This is what a hero-centric view should lead
            # with: reporting the hand's street as if it were hero's turns "you
            # folded preflop" into "reached river, showdown", which is wrong in
            # a way that inflates how often you think you saw a flop.
            "hero_street_reached": (
                hero_last_street.value if hero_last_street else index.street_reached.value
            ),
            "hero_went_to_showdown": any(
                a.startswith(f"p{hero + 1} sm") for a in hh.actions
            ),
            "hero_net_cents": index.hero_net,
            "hero_net_bb": _bb(index.hero_net, bb),
            "rake_cents": index.rake,
        },
        "showdown": _showdown(hh, bb, hero, players),
        # Nodes whose cost is exactly computable without a solver. Every player's,
        # not just hero's -- see `_terminal`.
        #
        # Only where the actor's cards are known, because pricing the node needs
        # them: hero's always are, a villain's only if they showed. A terminal
        # node nobody can price is still terminal -- the raw fact stays on the
        # action itself -- but surfacing it would put a label on a hand offering
        # nothing to work out.
        "terminal": [
            {
                "action_index": a["action_index"],
                "street": s["street"],
                "position": a["position"],
                "is_hero": a["is_hero"],
                "action": a["action"],
                "reason": a["terminal"],
            }
            for s in streets
            for a in s["actions"]
            if a["terminal"] and a["cards"]
        ],
        "interest": _interest(streets, hero_decisions, index, bb),
        # Filled by the analyze stage. Present and null so a client can rely on
        # the key existing rather than probing for it.
        "analysis": None,
    }


def _showdown(hh: HandHistory, bb: Cents, hero: int, players: int) -> dict:
    """How the hand actually ended: final board, cards shown, who won what.

    A hand view without this is missing the answer to the first question anyone
    asks about a hand they lost. Cards are reported only where they were
    genuinely revealed -- a villain who folded keeps their hand, and inferring
    one would be inventing information.
    """
    board = ""
    showed: set[int] = set()
    for raw in hh.actions:
        if raw.startswith("d db "):
            board += raw.split(None, 2)[2].split("#")[0].strip()
        m = re.match(r"^p(\d+) sm\s", raw.strip())
        if m:
            showed.add(int(m.group(1)) - 1)

    finishing = hh.finishing_stacks or []
    nets = [
        int(finishing[i]) - int(s) if i < len(finishing) else 0
        for i, s in enumerate(hh.starting_stacks)
    ]

    # Winning the pot and finishing ahead are different questions, and only the
    # first is what "WON" means. In a raked chop both winners are paid less than
    # they put in, so a net-based test names nobody the winner -- and picks the
    # largest net, which can be a seat that folded its blind and lost least.
    collected = _collected(hh)
    if collected:
        def won(i: int) -> bool:
            return collected.get(i, 0) > 0
    else:
        # No record of who was paid (the synthetic corpus). Fall back to net,
        # which is right whenever the pot was not chopped.
        best = max(nets) if nets else 0
        def won(i: int) -> bool:
            return nets[i] > 0 and nets[i] == best

    # Only players who were actually in the hand. A seat that folded for free
    # has nothing to report, and four rows of "0bb" bury the two that matter.
    def took_part(i: int) -> bool:
        return nets[i] != 0 or i in showed or i in collected

    return {
        "board": [board[i : i + 2] for i in range(0, len(board), 2)],
        "went_to_showdown": bool(showed),
        "players": [
            {
                "seat": i,
                "position": _position(i, players),
                "name": (hh.players[i] if hh.players else f"p{i + 1}"),
                "is_hero": i == hero,
                # From the deal, not from the showdown: hero's cards are always
                # known even when the hand ends with everyone folding, and a
                # villain who showed has their cards recorded there too. None
                # still means genuinely unknown.
                "cards": _hole_from_actions(hh, i),
                "showed": i in showed,
                "net_cents": nets[i],
                "net_bb": _bb(nets[i], bb),
                # What this player was paid out of the pot, net of rake. None
                # when unrecorded -- which is not the same as zero.
                "collected_cents": collected.get(i) if collected else None,
                "collected_bb": _bb(collected[i], bb) if i in collected else None,
                "won": won(i),
            }
            for i in range(players)
            if took_part(i)
        ],
        # More than one player paid out of the pot. Worth saying out loud: a
        # chop where both winners finish behind reads as a bug otherwise.
        "chopped": len(collected) > 1,
    }


def _collected(hh: HandHistory) -> dict[int, Cents]:
    """Seat -> amount paid out of the pot, from the site's own summary.

    Empty when the archive does not record it. Not derivable from finishing
    stacks: they net a payout against what the player put in.
    """
    raw = str(hh.user_defined_fields.get(PHH_COLLECTED) or "")
    out: dict[int, Cents] = {}
    for part in raw.split(","):
        if ":" in part:
            seat, _, amount = part.partition(":")
            out[int(seat)] = int(amount)
    return out


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
) -> tuple[list[dict], list[dict], Street | None]:
    """Single pass over the replay, producing per-street action and hero decisions.

    One pass because pokerkit mutates its `State` in place — see `replay.py`.
    """
    # Board per street, taken from the `d db` deal actions. Reading it off the
    # state at the first *player* action gets the turn wrong (the state lags a
    # deal) and misses a street entirely when everyone is already all-in and
    # nobody acts on it.
    dealt = ""
    board_by_street: dict[Street, str] = {Street.PREFLOP: ""}
    street_seq: list[Street] = [Street.PREFLOP]
    for raw in hh.actions:
        if raw.startswith("d db "):
            dealt += raw.split(None, 2)[2].split("#")[0].strip()
            st = _STREETS[0] if not dealt else _STREETS[len(dealt) // 2 - 2]
            board_by_street[st] = dealt
            street_seq.append(st)

    # Cards for every seat that is ever revealed: hero always, a villain only if
    # they showed. Carried on each action so a row can be rendered on its own.
    # A villain's cards travel in the payload but the client keeps them
    # face-down behind a click -- reviewing a decision means judging it without
    # them, and this is post-session, so there is nothing being leaked live.
    known_cards = {i: _hole_from_actions(hh, i) for i in range(players)}

    streets: dict[Street, dict] = {}
    hero_decisions: list[dict] = []
    # The last street hero actually acted on. Distinct from how far the *hand*
    # went: hero can fold preflop and the remaining players run it to the river.
    hero_last_street: Street | None = None
    # Last aggressive action on the current street, so a decision can say what it
    # was actually facing rather than just a number.
    last_aggressor: dict[str, Any] | None = None
    current_street: Street | None = None
    raises_this_street = 0
    folded: set[int] = set()

    # Pairing each action with the state it faced is `replay`'s job, not this
    # module's. An earlier copy of that logic lived here, indexing `hh.actions`
    # positionally, and drifted one action per street: postflop calls rendered
    # as checks and raises as bets, with the pot numbers still adding up.
    for st in iter_action_states(hh):
        parsed = parse_player_action(st)
        if parsed is None:
            continue
        seat, kind, amount, to_amount = parsed

        street = st.street
        if street is not current_street:
            last_aggressor = None
            current_street = street
            raises_this_street = 0

        to_call: Cents = st.to_call
        pot_before: Cents = st.pot
        stack_before: Cents = st.stacks[seat]
        board = st.board

        entry = {
            "action_index": st.action_index,
            "seat": seat,
            "position": _position(seat, players),
            "is_hero": seat == hero,
            "cards": known_cards.get(seat),
            # Why this node is worth pricing exactly -- see `_terminal`.
            "terminal": _terminal(st, seat, street, folded, players),
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

        full = board_by_street.get(street, board)
        bucket = streets.setdefault(
            street,
            {
                "street": street.value,
                "board": [full[i : i + 2] for i in range(0, len(full), 2)],
                "pot_start_cents": pot_before,
                "pot_start_bb": _bb(pot_before, bb),
                "actions": [],
            },
        )
        bucket["actions"].append(entry)

        # After the entry is built, not before: `_terminal` asks who was still
        # live when this player had to act, and that includes them.
        if kind is ActionType.FOLD:
            folded.add(seat)

        if seat == hero:
            hero_last_street = street
            hero_decisions.append(
                _decision(entry, street, bb, to_call, pot_before, stack_before, board,
                          last_aggressor)
            )

        if kind.is_aggressive:
            raises_this_street += 1
            # Preflop, the big blind is already a bet, so the first voluntary
            # raise is the open, the second is a 3-bet, the third a 4-bet.
            # Calling them all "raise" makes every 3-bet spot look like an
            # open-raise spot and no 3-bet chart can ever match.
            escalation = ("raise", "3bet", "4bet", "5bet")
            level = min(raises_this_street - 1, len(escalation) - 1)
            last_aggressor = {
                "position": entry["position"],
                "action": kind.value,
                "level": escalation[level] if street is Street.PREFLOP else kind.value,
                "to_bb": entry["to_bb"],
                "pct_pot": entry["pct_pot"],
                "all_in": entry["is_all_in"],
            }

    # A street that was dealt but never acted on -- an all-in runout -- still
    # happened, and omitting it drops the card that decided the hand.
    for st in street_seq:
        if st not in streets and st is not Street.PREFLOP:
            full = board_by_street[st]
            streets[st] = {
                "street": st.value,
                "board": [full[i : i + 2] for i in range(0, len(full), 2)],
                "pot_start_cents": None,
                "pot_start_bb": None,
                "actions": [],
                "runout": True,
            }
    ordered = sorted(streets.values(), key=lambda s: _STREET_ORDER[Street(s["street"])])
    return ordered, hero_decisions, hero_last_street


def _terminal(st, seat: int, street: Street, folded: set[int], players: int) -> str | None:
    """Why calling here would end the hand, or None if it would not.

    A **terminal** node is one where calling ends all betting, so calling and
    folding are the only continuations and both have a closed-form value:

        EV(call) = equity × (pot + call) − (1 − equity) × call
        EV(fold) = 0

    Everywhere else the value of a call depends on how the remaining streets get
    played by both players, which is the thing solvers exist to compute and the
    thing no amount of pot-odds arithmetic substitutes for. So this is the set of
    decisions whose cost is knowable exactly, from a range estimate alone, with
    no solver and no model.

    It is marked for **every** player, not just hero. Villain's terminal calls
    are how you learn what the pool is actually calling off with, and hero is not
    the only person at the table making a priceable mistake.

    Three shapes qualify, all requiring a bet to face -- unopened is a check, and
    a check is free:

    * ``all-in`` -- nobody who is still in the hand has chips behind, so no
      further betting is possible whatever anyone does.
    * ``all-in`` -- heads up, and calling costs this player everything they have.
    * ``river`` -- heads up on the last street, where a call goes straight to
      showdown.

    Deliberately conservative about multiway. Three-handed on the river a call
    does not close the action, because the player behind can still raise; and a
    call for stack against two live opponents leaves them betting a side pot.
    Both are still *analysable*, but not with the two-branch formula above, and
    quietly including them would put estimates in a bucket labelled exact.

    Note what this does *not* claim: that the action taken was terminal, or that
    the best action was. A river bet can be raised, and pricing that raise needs
    the same solver everything else does. The guarantee is only that call and
    fold are exactly priceable at this node.
    """
    if st.to_call <= 0:
        return None
    live = [i for i in range(players) if i != seat and i not in folded]
    if not live:
        return None
    if all(st.stacks[i] == 0 for i in live):
        return "all-in"
    if len(live) == 1:
        if st.stacks[seat] <= st.to_call:
            return "all-in"
        if street is Street.RIVER:
            return "river"
    return None


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
        # Whether call and fold are exactly priceable here; see `_terminal`.
        "terminal": entry["terminal"],
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
        parts.append(
            f"vs_{facing['position']}_{facing.get('level', facing['action'])}_{facing['to_bb']}bb"
        )
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
    tail = ""
    if r["hero_street_reached"] != r["street_reached"]:
        tail = f" (hand ran to {r['street_reached']})"
    out.append(
        f"  result: hero out on {r['hero_street_reached']}"
        f"{', showdown' if r['hero_went_to_showdown'] else ''}, "
        f"{r['hero_net_bb']:+}bb{tail}"
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
            d["verdict"] = _verdict(d["hero_action"], solution)


# How often the chart has to take an action before we call it standard. Below
# the upper bound it is still part of the strategy -- deviating at a mixed node
# is not an error, and flagging it as one is how a tool loses a player's trust.
_STANDARD = 0.50
_IN_MIX = 0.05


def _verdict(hero_action: str, solution) -> dict:
    """Chart-derived judgement of one action. Deterministic, no model involved.

    Only as good as the chart behind it, so it carries the spot it consulted --
    the UI links to that chart, because a verdict you cannot check is worth less
    than no verdict.
    """
    freq = solution.frequency_of(hero_action) or 0.0
    if freq >= _STANDARD:
        label, tone = "standard", "good"
    elif freq >= _IN_MIX:
        label, tone = "in the mix", "ok"
    else:
        # The chart never takes this action with this hand. Sizing is ignored --
        # these charts are not published per size -- so this is a judgement about
        # the *choice*, not how much.
        best = solution.best
        label = f"off chart — {best.action}s here" if best else "off chart"
        tone = "bad"
    return {
        "label": label,
        "tone": tone,
        "frequency": round(freq, 3),
        "chart": solution.spot_key,
        # The class the chart was consulted for, so a link can point at the
        # exact square rather than dropping you into 169 of them.
        "hand": solution.hand,
        "source": solution.provider,
    }


# Thresholds for "worth another look". Set against a real session rather than
# picked: the median pot there was under 5bb and hero was out preflop in 80% of
# hands, so these pick out the tail without swamping it.
_DEEP_INVESTED_BB = 10.0
_BIG_SWING_BB = 10.0
_MANY_DECISIONS = 3


def _interest(streets: list[dict], decisions: list[dict], index: HandIndex, bb: Cents) -> dict:
    """Was this hand worth reviewing, and why.

    Every test is about **hero**, not the table. A 96bb pot hero folded out of on
    the first action has nothing in it to learn from, and an early version
    flagged exactly those -- the same hero-vs-hand confusion that made a preflop
    fold report as "reached river, showdown".

    Reasons are returned alongside the verdict because an opaque score is
    unusable: you cannot tell whether to trust a filter that will not say why it
    fired.
    """
    invested = sum(d["amount_bb"] or 0 for d in decisions)
    faced_all_in = any(
        (d["facing"] or {}).get("all_in") and (d["to_call_bb"] or 0) > 0
        for d in decisions
    )
    went_all_in = any(
        a["is_all_in"] for s in streets for a in s["actions"] if a["is_hero"]
    )

    reasons: list[str] = []
    # Name the street hero actually reached rather than the generic "postflop" --
    # "river" and "flop" are different amounts of hand to review, and the label
    # is what you scan the list by.
    deepest = max(
        (Street(d["street"]) for d in decisions), default=Street.PREFLOP,
        key=lambda st: _STREETS.index(st),
    )
    if deepest is not Street.PREFLOP:
        reasons.append(deepest.value)
    if invested >= _DEEP_INVESTED_BB:
        reasons.append(f"{invested:.0f}bb invested")
    if abs(index.hero_net / bb) >= _BIG_SWING_BB:
        reasons.append("big swing")
    if faced_all_in or went_all_in:
        reasons.append("all-in")
    if len(decisions) >= _MANY_DECISIONS:
        reasons.append(f"{len(decisions)} decisions")

    return {"interesting": bool(reasons), "reasons": reasons, "invested_bb": round(invested, 1)}
