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

from ...models import PHH_HERO_INDEX, PHH_SITE, PHH_SITE_HAND_ID, Cents

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
    """
    by_seat = list(h.seats.items())
    if not h.sb_name:
        raise ParseError(f"hand {h.hand_id}: no small blind posted")
    start = next(i for i, (_, name) in enumerate(by_seat) if name == h.sb_name)
    rotated = by_seat[start:] + by_seat[:start]
    return [name for _, name in rotated]


def to_phh(block: str, *, source_file: str = "") -> HandHistory:
    """Convert one ACR hand block to a validated `HandHistory`."""
    h = _scan(block)
    if not h.hand_id:
        raise ParseError("no hand header")
    if h.dead_posts:
        # A dead blind -- a player buying in mid-orbit -- has no PHH equivalent.
        # Putting the amount in `blinds_or_straddles` makes pokerkit read it as a
        # straddle, which moves where the action starts: in hand 2794090528 that
        # shifted first-to-act from index 2 to index 4. Modelling it as an ante
        # is also wrong, because the post is live (the poster can check behind
        # the big blind). Refused explicitly rather than converted incorrectly:
        # a hand that silently replays with the wrong action order is worse than
        # one that is skipped.
        raise ParseError(
            f"hand {h.hand_id}: dead blind posted by "
            f"{', '.join(sorted(h.dead_posts))}; no PHH representation"
        )
    order = _phh_order(h)
    index = {name: i for i, name in enumerate(order)}
    if h.hero not in index:
        raise ParseError(f"hand {h.hand_id}: hero {h.hero!r} not among dealt players")

    # The Seat line shows the stack at the START of the hand, before blinds are
    # posted -- verified across consecutive hands: a player at $10.00 who posts
    # the $0.10 big blind and folds appears at $9.90 in the next hand. Adding the
    # post back double-counts it.
    starting = [h.stacks[n] for n in order]
    blinds = [h.posts.get(n, 0) for n in order]
    if len(order) == 2:
        # pokerkit swaps the two entries heads-up: passing (5, 10) yields
        # bets=[10, 5], i.e. it reads index 0 as the big blind. Verified
        # directly, and undocumented. `order` stays [SB, BB] -- which is what
        # position_of expects, since the heads-up button posts the small blind --
        # so only the amounts are reversed.
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
            # The original ACR text, kept verbatim. The parser will improve and
            # PHH is lossy about anything the format does not model (summary
            # lines, hand descriptions, the site's own rake breakdown), so the
            # source is the only thing that makes a later re-parse possible
            # without going back to the client's export folder.
            "_pc_source_text": block,
            "_pc_source_file": source_file,
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
        # pokerkit ran with no rake configured, so the winner was paid the whole
        # pot. ACR states the real figure, so take it off the biggest winner.
        # Exact for a single pot; a split pot would need per-pot attribution,
        # which this sample has no example of.
        if h.rake:
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
    source = udf.pop("_pc_source_text", "")
    text = replace(hh, user_defined_fields=udf).dumps()
    if source:
        # ACR never emits a triple quote; guard anyway rather than produce a
        # file that silently truncates.
        if "\'\'\'" in source:
            raise ParseError("source text contains a TOML multi-line delimiter")
        text = text.rstrip("\n") + f"\n_pc_source_text = '''\n{source}\n'''\n"
    return text
