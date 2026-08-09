"""Turning one decision into the text a model is asked about.

Two rules shape this, and both are about the cache.

**Hero's cards never appear.** The question is what the *opponent* holds, and
hero's hand does not change that -- only which combos are blocked, which is
applied afterwards in `equity.combos`. Putting them in would also make the text
unique per hand, so a spot that recurs all session would be paid for every time.

**Nothing else varies per hand either.** No hand id, no timestamp, no player
names. The description is a function of the abstract spot, which is what makes
`spot_key` a legitimate cache key for the answer.

What is left is the sequence: stakes, table size, stack depth, and who did what
in what order, by position.
"""

from __future__ import annotations

from typing import Any


def _amount(action: dict) -> str:
    if action["action"] in ("bet", "raise"):
        size = f" to {action['to_bb']}bb"
        if action["pct_pot"]:
            size += f" ({action['pct_pot']}% pot)"
        return size
    if action["action"] == "call":
        return f" {action['amount_bb']}bb"
    return ""


def target_of(view: dict[str, Any], action_index: int) -> str | None:
    """Whose range is worth asking about at this decision.

    The player who bet into hero. Facing no bet there is no single opponent to
    describe -- everyone still in has a range, and a question about "their"
    range would get an answer about nobody's.
    """
    for street in view["streets"]:
        for a in street["actions"]:
            if a["action_index"] != action_index:
                continue
            decision = next(
                (d for d in view["hero_decisions"] if d["action_index"] == action_index),
                None,
            )
            facing = (decision or {}).get("facing")
            return facing["position"] if facing and decision["to_call_bb"] else None
    return None


def describe(
    view: dict[str, Any], action_index: int, kind: str = "opponent"
) -> str | None:
    """The prompt body for one decision, or None if there is nobody to ask about.

    `kind="opponent"` asks about the player who bet into hero -- the range
    equity is computed against.

    `kind="hero"` asks about hero's *own* range: the hands somebody playing this
    line would arrive here with. Worth asking precisely because hero's actual
    cards are known -- the question is whether the hand belongs in the range the
    line represents, which is a different question from whether the hand is
    strong, and it cannot be asked of a model that has been shown the answer.
    That range is defined at every decision, including with no bet to face,
    where there is no single opponent to ask about.
    """
    if kind == "hero":
        target = view["hand"]["hero"]["position"]
    else:
        target = target_of(view, action_index)
    if target is None:
        return None

    hand = view["hand"]
    lines = [
        f"{hand['table']['players_dealt']}-handed {hand['stakes']['label']}, "
        f"{hand['hero']['eff_stack_bb']}bb effective.",
        "",
    ]
    done = False
    for street in view["streets"]:
        if done:
            break
        board = " ".join(street["board"])
        lines.append(
            f"{street['street'].upper()}"
            + (f" [{board}]" if board else "")
            + f" — pot {street['pot_start_bb']}bb"
        )
        for a in street["actions"]:
            if a["action_index"] == action_index:
                # Stop at the decision. What came after it is the future, and a
                # range read with hindsight is not a range read.
                done = True
                break
            lines.append(f"  {a['position']} {a['action']}{_amount(a)}"
                         + ("  (all-in)" if a["is_all_in"] else ""))
        lines.append("")

    if kind == "hero":
        lines.append(
            f"Estimate {target}'s range here — the hands a competent player "
            f"would have arrived at this point with, having taken this line. "
            f"Not the hand they hold; the range they represent."
        )
    else:
        lines.append(f"Estimate {target}'s range for the last action they took.")
    return "\n".join(lines)
