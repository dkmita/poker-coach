"""Americas Cardroom / Winning Poker Network hand histories -> PHH.

ACR writes one `.txt` per table session, hands separated by blank lines. The
format is undocumented, so this is written against real files and validated by
replaying every hand through pokerkit — an illegal betting sequence raises, so a
hand that converts is a hand whose action is internally consistent. That check is
the main defense against a silently wrong parse.

Four things in the format are easy to get wrong:

1. **A seat line does not mean a player was dealt in.** `Seat 2: name` with no
   stack means "will be allowed to play after the button", and a player with a
   stack can still be sitting out (`name waits for big blind`). Counting seat
   lines overstates the table and shifts every position by one.

2. **Dead blinds exist.** `name posts $0.10` — no "small"/"big" — is a player
   buying in mid-orbit. They are dealt in and act, but they are not the big
   blind.

3. **Seat numbers are absolute and sparse**, while PHH orders players by posting
   order starting at the small blind. The mapping is derived from the actual
   `posts the small/big blind` lines rather than from the button, because the
   button-relative rule differs heads-up (the button posts the small blind) and
   getting that backwards silently mislabels every position in the hand.

4. **Rake is stated, and split.** In-hand lines show a running
   `Main pot $X | Rake $Y` where Y already includes the jackpot drop; the summary
   separates `Rake` from `JP Fee`. The summary total is what left the table.

Money is parsed to integer cents immediately. Dollar strings never become floats.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from pokerkit.notation import HandHistory

from ...models import (
    PHH_COLLECTED,
    PHH_HERO_INDEX,
    PHH_SITE,
    PHH_SITE_HAND_ID,
    PHH_SOURCE_TEXT,
    Cents,
)

SITE = "acr"

_HEADER = re.compile(
    r"^Hand #(?P<id>\d+) - Holdem \((?P<limit>[^)]+)\) - "
    r"\$(?P<sb>[\d.]+)/\$(?P<bb>[\d.]+) - (?P<ts>[\d/]+ [\d:]+) UTC"
)
_TABLE = re.compile(r"^(?P<table>.+?) (?P<max>\d+)-max Seat #(?P<button>\d+) is the button")
_SEAT = re.compile(r"^Seat (?P<no>\d+): (?P<name>.+?) \(\$(?P<stack>[\d.]+)\)\s*$")
_SEAT_INACTIVE = re.compile(r"^Seat (?P<no>\d+): (?P<name>.+?) will be allowed to play")
_POST_BLIND = re.compile(r"^(?P<name>.+?) posts the (?P<which>small|big) blind \$(?P<amt>[\d.]+)")
_POST_DEAD = re.compile(r"^(?P<name>.+?) posts \$(?P<amt>[\d.]+)\s*$")
_WAITS = re.compile(r"^(?P<name>.+?) waits for big blind")
_DEALT = re.compile(r"^Dealt to (?P<name>.+?) \[(?P<cards>[^\]]+)\]")
_STREET = re.compile(r"^\*\*\* (?P<street>FLOP|TURN|RIVER) \*\*\*(?P<rest>.*)")
_CARDS_IN = re.compile(r"\[([^\]]+)\]")
_SHOWS = re.compile(r"^(?P<name>.+?) shows \[(?P<cards>[^\]]+)\]")
_RAKE = re.compile(r"Rake \$(?P<rake>[\d.]+)(?: \| JP Fee \$(?P<jp>[\d.]+))?")
# "kiniatim collected $1.05 from main pot" -- one line per player per pot, so a
# split pot produces two and a side pot adds more. Summed per player.
_COLLECTED = re.compile(r"^(?P<name>.+?) collected \$(?P<amt>[\d.]+) from ")

_FOLD = re.compile(r"^(?P<name>.+?) folds\s*$")
_CHECK = re.compile(r"^(?P<name>.+?) checks\s*$")
_CALL = re.compile(r"^(?P<name>.+?) calls \$(?P<amt>[\d.]+)")
_BET = re.compile(r"^(?P<name>.+?) bets \$(?P<amt>[\d.]+)")
_RAISE = re.compile(r"^(?P<name>.+?) raises \$(?P<amt>[\d.]+) to \$(?P<to>[\d.]+)")


class ParseError(ValueError):
    """A hand this parser could not turn into valid PHH."""


def cents(text: str) -> Cents:
    """'$0.05' -> 5. Never via float: 0.29 * 100 is 28.999... in binary."""
    whole, _, frac = text.strip().lstrip("$").partition(".")
    return int(whole or 0) * 100 + int((frac + "00")[:2])


@dataclass
class _Hand:
    """Intermediate form: ACR's view of the hand, before reordering for PHH."""

    hand_id: str = ""
    sb: Cents = 0
    bb: Cents = 0
    played_at: datetime | None = None
    table: str = ""
    button_seat: int = 0
    seats: dict[int, str] = field(default_factory=dict)  # seat -> name, dealt only
    stacks: dict[str, Cents] = field(default_factory=dict)
    posts: dict[str, Cents] = field(default_factory=dict)
    sb_name: str = ""
    bb_name: str = ""
    hole: dict[str, str] = field(default_factory=dict)  # name -> "AsJd"
    hero: str = ""
    # (name, verb, amount) in dealt order, with street markers interleaved as
    # ("", "board", cards).
    actions: list[tuple[str, str, str]] = field(default_factory=list)
    rake: Cents = 0
    # What each player was actually paid, net of rake, as the site states it.
    # A player can collect and still finish the hand down: in a raked chop both
    # winners get back less than they put in.
    collected: dict[str, Cents] = field(default_factory=dict)
    dead_posts: set[str] = field(default_factory=set)
    source: str = ""


def split_hands(text: str) -> Iterator[str]:
    """Yield each hand block from a session file."""
    for block in re.split(r"\n\s*\n", text):
        if block.strip().startswith("Hand #"):
            yield block.strip("\n")


def _scan(block: str) -> _Hand:
    h = _Hand()
    waiting: set[str] = set()
    seat_of: dict[str, int] = {}

    for line in block.splitlines():
        line = line.rstrip()
        if not line:
            continue

        if m := _HEADER.match(line):
            h.hand_id, h.sb, h.bb = m["id"], cents(m["sb"]), cents(m["bb"])
            h.played_at = datetime.strptime(m["ts"], "%Y/%m/%d %H:%M:%S").replace(tzinfo=UTC)
            continue
        if m := _TABLE.match(line):
            h.table, h.button_seat = m["table"], int(m["button"])
            continue
        if m := _SEAT.match(line):
            seat_of[m["name"]] = int(m["no"])
            h.stacks[m["name"]] = cents(m["stack"])
            continue
        if _SEAT_INACTIVE.match(line):
            continue  # no stack: not in the hand at all
        if m := _WAITS.match(line):
            waiting.add(m["name"])
            continue
        if m := _POST_BLIND.match(line):
            h.posts[m["name"]] = cents(m["amt"])
            if m["which"] == "small":
                h.sb_name = m["name"]
            else:
                h.bb_name = m["name"]
            continue
        if m := _POST_DEAD.match(line):
            # Buying in mid-orbit: dealt and acting, but not the big blind.
            h.posts[m["name"]] = cents(m["amt"])
            h.dead_posts.add(m["name"])
            continue
        if m := _DEALT.match(line):
            h.hero = m["name"]
            h.hole[m["name"]] = m["cards"].replace(" ", "")
            continue
        if m := _STREET.match(line):
            groups = _CARDS_IN.findall(m["rest"])
            # FLOP prints one group; TURN/RIVER print the old board then the new
            # card, so the last group is always what was just dealt.
            h.actions.append(("", "board", groups[-1].replace(" ", "")))
            continue
        if m := _SHOWS.match(line):
            cards = m["cards"].replace(" ", "")
            h.hole.setdefault(m["name"], cards)
            # Emit the showdown, don't just harvest the cards. pokerkit repairs
            # a missing showdown most of the time, which is why this was
            # invisible on 46 of 49 hands -- but when the repair fails you get a
            # bare "Unable to repair the hand history" with nothing pointing at
            # the cause.
            h.actions.append((m["name"], "sm", cards))
            continue
        if m := _COLLECTED.match(line):
            h.collected[m["name"]] = h.collected.get(m["name"], 0) + cents(m["amt"])
            continue
        if m := _RAKE.search(line) and line.startswith("Total pot"):
            m = _RAKE.search(line)
            h.rake = cents(m["rake"]) + (cents(m["jp"]) if m["jp"] else 0)
            continue

        for pattern, verb in (
            (_FOLD, "f"), (_CHECK, "cc"), (_CALL, "cc"), (_BET, "cbr"), (_RAISE, "cbr")
        ):
            if m := pattern.match(line):
                amount = m.groupdict().get("to") or m.groupdict().get("amt") or ""
                h.actions.append((m["name"], verb, amount))
                break

    h.seats = {
        seat: name
        for name, seat in seat_of.items()
        if name not in waiting or name in h.posts
    }
    h.seats = dict(sorted(h.seats.items()))
    return h


def _phh_order(h: _Hand) -> list[str]:
    """Players in PHH order: small blind first, then clockwise.

    Derived from who actually posted rather than from the button, because the
    button-relative rule flips heads-up and an inverted position map is the kind
    of bug that produces plausible output.

    Heads up the order starts from the **big** blind instead. pokerkit opens
    postflop betting at index 0 regardless of who posted what, so the seat put
    there is the seat it will have act first on the flop -- and heads up that is
    the big blind, not the button. Getting this backwards does not fail loudly:
    pokerkit inserts a check for the button to reconcile the disagreement, which
    is invisible unless the button really did check behind, and then the whole
    hand dies with "Unable to repair the hand history".
    """
    by_seat = list(h.seats.items())
    first = h.bb_name if len(by_seat) == 2 else h.sb_name
    if not first:
        raise ParseError(f"hand {h.hand_id}: no blind posted")
    start = next(i for i, (_, name) in enumerate(by_seat) if name == first)
    rotated = by_seat[start:] + by_seat[:start]
    return [name for _, name in rotated]


def _live_post(h: _Hand) -> tuple[str, Cents] | None:
    """The out-of-turn post this hand can be converted with, or None.

    A player buying in mid-orbit posts a big blind out of position. It has no
    PHH equivalent: `blinds_or_straddles` is the only place to put it, and
    pokerkit reads a third entry as a straddle, which moves where the action
    starts -- in hand 2794090528 from index 2 to index 4. An ante is wrong too,
    because the post is live: the poster can check behind the big blind.

    So it is not posted at all. The money enters instead at the poster's own
    turn, where their recorded `cc` -- a check, because the post already covers
    the blind -- resolves to a call of exactly the amount they posted. Same
    contribution, same finishing stack, same action order, and unlike the
    straddle reading, one pokerkit agrees with.

    Two costs, both real. Anyone acting *before* the poster sees a pot short by
    the post. And the poster's check is recorded as a call -- the distinction
    PHH already blurs and this codebase refuses to blur, because a limp and a
    check from the blind are different decisions.

    Which is why **hero may not be the poster**. For a villain this costs a
    misnamed action in a hand hero is being studied in; for hero it invents a
    voluntary call that never happened, and the preflop chart layer would judge
    it -- hand 2794340475 would report hero limping J7o under the gun when hero
    posted to sit down and checked their option. Refused rather than
    approximated whenever the shape does not hold exactly.
    """
    if not h.dead_posts:
        return None
    if h.hero in h.dead_posts:
        raise ParseError(
            f"hand {h.hand_id}: hero posted out of turn; carrying it on hero's "
            "own action would record a check as a voluntary call"
        )
    if len(h.dead_posts) > 1:
        raise ParseError(
            f"hand {h.hand_id}: {len(h.dead_posts)} out-of-turn posts; "
            "only a single one can be carried on the poster's own action"
        )
    name = next(iter(h.dead_posts))
    amount = h.posts.get(name, 0)
    if amount != h.bb:
        raise ParseError(
            f"hand {h.hand_id}: {name} posted {amount} out of turn, not the "
            f"big blind ({h.bb}); no representation for a partial or dead post"
        )
    first = next((a for a in h.actions if a[0] == name), None)
    if first is None or first[1] != "cc":
        raise ParseError(
            f"hand {h.hand_id}: {name} posted out of turn but their first "
            f"action is {first[1] if first else 'absent'!r}, not a check; the "
            "post has no action to ride in on"
        )
    return name, amount


def to_phh(block: str, *, source_file: str = "") -> HandHistory:
    """Convert one ACR hand block to a validated `HandHistory`."""
    h = _scan(block)
    if not h.hand_id:
        raise ParseError("no hand header")
    live_post = _live_post(h)
    order = _phh_order(h)
    index = {name: i for i, name in enumerate(order)}
    if h.hero not in index:
        raise ParseError(f"hand {h.hand_id}: hero {h.hero!r} not among dealt players")

    # The Seat line shows the stack at the START of the hand, before blinds are
    # posted -- verified across consecutive hands: a player at $10.00 who posts
    # the $0.10 big blind and folds appears at $9.90 in the next hand. Adding the
    # post back double-counts it.
    starting = [h.stacks[n] for n in order]
    # The out-of-turn post is deliberately not posted; see `_live_post`.
    blinds = [
        0 if (live_post and n == live_post[0]) else h.posts.get(n, 0) for n in order
    ]
    if len(order) == 2:
        # pokerkit posts the two entries reversed heads-up: passing (5, 10)
        # yields bets=[10, 5]. Verified directly, and undocumented. `order` is
        # [BB, button] (see `_phh_order`), so the amounts have to be written
        # [small, big] for the big blind to land on index 0.
        blinds = [blinds[1], blinds[0]]

    actions: list[str] = [
        f"d dh p{i + 1} {h.hole.get(name, '????')}" for i, name in enumerate(order)
    ]
    for name, verb, amount in h.actions:
        if verb == "board":
            actions.append(f"d db {amount}")
        elif verb == "cbr":
            actions.append(f"p{index[name] + 1} cbr {cents(amount)}")
        elif verb == "sm":
            actions.append(f"p{index[name] + 1} sm {amount}")
        else:
            actions.append(f"p{index[name] + 1} {verb}")

    hh = HandHistory(
        variant="NT",
        ante_trimming_status=False,
        antes=[0] * len(order),
        blinds_or_straddles=blinds,
        bring_in=None,
        small_bet=None,
        big_bet=None,
        min_bet=h.bb,
        starting_stacks=starting,
        actions=actions,
        currency="USD",
        table=h.table,
        year=h.played_at.year,
        month=h.played_at.month,
        day=h.played_at.day,
        time=h.played_at.time(),
        time_zone="UTC",
        players=order,
        user_defined_fields={
            PHH_SITE: SITE,
            PHH_SITE_HAND_ID: h.hand_id,
            PHH_HERO_INDEX: index[h.hero],
            # PHH has no rake field, and ACR states it -- so it is recorded
            # rather than reconstructed from finishing stacks.
            "_pc_rake_cents": h.rake,
            # Who was paid, and how much, net of rake. Recorded because it is
            # not recoverable from finishing stacks: a winner in a raked chop
            # finishes *down*, so "ended ahead" and "won the pot" are different
            # questions and only the site can answer the second.
            PHH_COLLECTED: ",".join(
                f"{index[n]}:{c}" for n, c in sorted(h.collected.items())
                if n in index
            ),
            # The original ACR text, kept verbatim. The parser will improve and
            # PHH is lossy about anything the format does not model (summary
            # lines, hand descriptions, the site's own rake breakdown), so the
            # source is the only thing that makes a later re-parse possible
            # without going back to the client's export folder.
            PHH_SOURCE_TEXT: block,
            "_pc_source_file": source_file,
            # An out-of-turn post carried on the poster's own action instead of
            # being posted before the deal (see `_live_post`). Recorded because
            # it is the one place this archive knowingly differs from the site:
            # players acting before this seat faced a pot larger by this amount
            # than the replay reports.
            **(
                {"_pc_live_post": f"{live_post[0]}:{live_post[1]}"}
                if live_post
                else {}
            ),
        },
    )
    try:
        # pokerkit validates the betting sequence here. It also settles the pot,
        # so the final state carries the finishing stacks -- which PHH treats as
        # optional but everything downstream needs: hero_net and the
        # reconciliation check against the site both read them.
        final = None
        for state, _ in hh.state_actions:
            final = state
    except ValueError as exc:
        raise ParseError(f"hand {h.hand_id}: {exc}") from exc

    if final is not None:
        finishing = [int(x) for x in final.stacks]
        # pokerkit ran with no rake configured, so the pot was paid out whole.
        # ACR states the real figure, and states who collected -- so charge it to
        # the players who were actually paid, split in proportion to what each
        # took. Charging it all to the biggest *gainer* was wrong twice over in a
        # raked chop: both winners can finish behind, so the biggest gainer may
        # be someone who only posted a blind, and even when it isn't, one winner
        # ends up paying the other's rake. Hand 2794341035 chopped a $2.19 pot
        # and reported the two winners at 0 and -9 instead of -5 and -4.
        if h.rake:
            paid = {index[n]: c for n, c in h.collected.items() if n in index}
            total = sum(paid.values())
            if total > 0:
                # Largest-remainder, so the shares sum to the rake exactly rather
                # than drifting a cent on every chopped pot.
                exact = {i: h.rake * c / total for i, c in paid.items()}
                share = {i: int(v) for i, v in exact.items()}
                for i in sorted(
                    share, key=lambda i: exact[i] - share[i], reverse=True
                )[: h.rake - sum(share.values())]:
                    share[i] += 1
                for i, amount in share.items():
                    finishing[i] -= amount
            else:
                # No collected lines (older exports, or a hand that never got
                # to a showdown summary). Fall back to the biggest gainer.
                gains = [f - s for f, s in zip(finishing, starting)]
                finishing[gains.index(max(gains))] -= h.rake
        hh.finishing_stacks = finishing
    return hh


def parse(
    text: str, *, source_file: str = ""
) -> Iterator[tuple[str, HandHistory | ParseError]]:
    """Convert a session file. Yields `(hand_id, HandHistory | ParseError)`.

    Failures are yielded rather than raised so one unparseable hand doesn't cost
    the rest of the session.
    """
    for block in split_hands(text):
        m = _HEADER.match(block.splitlines()[0])
        hand_id = m["id"] if m else "?"
        try:
            yield hand_id, to_phh(block, source_file=source_file)
        except (ParseError, ValueError, KeyError, StopIteration) as exc:
            yield hand_id, ParseError(f"hand {hand_id}: {exc}")


def dumps_phh(hh: HandHistory) -> str:
    """Serialize to PHH text, with the retained source written correctly.

    pokerkit's `dumps()` emits every string as a single-quoted TOML value on one
    line. That is invalid for the multi-line ACR source we keep, and it fails
    silently in the worst way: the hand validates in memory, gets written, and
    then cannot be read back. Written here as a TOML multi-line literal
    (`'''...'''`) instead, which preserves newlines verbatim and keeps the file
    diffable rather than base64.
    """
    udf = dict(hh.user_defined_fields)
    source = udf.pop(PHH_SOURCE_TEXT, "")
    text = replace(hh, user_defined_fields=udf).dumps()
    if source:
        # ACR never emits a triple quote; guard anyway rather than produce a
        # file that silently truncates.
        if "\'\'\'" in source:
            raise ParseError("source text contains a TOML multi-line delimiter")
        text = text.rstrip("\n") + f"\n{PHH_SOURCE_TEXT} = '''\n{source}\n'''\n"
    return text
