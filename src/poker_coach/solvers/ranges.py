"""Read solver strategy from PioSolver-format range text.

This is the supported way to get GTO Wizard data into poker-coach. Its Ranges tab
exports any node's range as standard PioSolver/GTO+ text; save one file per
action and this reads them. The same format comes out of TexasSolver and GTO+, so
one importer serves every source — which is the main reason to prefer it over
automating a UI. It also keeps working offline, in tests, and in CI, needs no
credentials, and does not break when a vendor redesigns a page.

Layout on disk — one directory per spot, named by `spot_key`, one file per
action:

    charts/
      BB_preflop_vs_UTG_raise_2.5bb_100bb/
        call.txt
        raise.txt
        fold.txt        # optional; inferred as the remainder if absent

Each file is a weighted range in the usual notation, comma or whitespace
separated:

    AA:1.0, KK:1.0, AKs:0.75, AJo:0.5, 77+, A2s+

A bare hand means weight 1.0. `+` expands upward the way a human writes it:
`77+` is every pair from sevens up, `A2s+` every suited ace, `ATo+` offsuit ace-ten
through ace-king.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import ActionFrequency, Solution

RANKS = "23456789TJQKA"
_RANK_INDEX = {r: i for i, r in enumerate(RANKS)}

# "AKs:0.75" / "77+" / "AJo"
_ENTRY = re.compile(r"^(?P<hand>[2-9TJQKA]{2}[so]?)(?P<plus>\+?)(?::(?P<weight>[\d.]+))?$")

# Actions we accept as filenames, mapped to the vocabulary the rest of the system
# uses. `bet`/`raise` and `check`/`call` are kept distinct because a view
# distinguishes them and conflating them would silently mislabel a decision.
KNOWN_ACTIONS = ("fold", "check", "call", "bet", "raise")


def canonical_class(hand: str) -> str:
    """Two dealt cards to their strategy class: 'AdJs' -> 'AJo', 'KdQd' -> 'KQs'.

    Charts are indexed by class, not by suit combination, so a lookup that
    forgets to collapse suits misses every time.
    """
    if len(hand) != 4:
        raise ValueError(f"expected two cards like 'AdJs', got {hand!r}")
    r1, s1, r2, s2 = hand[0], hand[1], hand[2], hand[3]
    if _RANK_INDEX[r1] < _RANK_INDEX[r2]:
        r1, s1, r2, s2 = r2, s2, r1, s1
    if r1 == r2:
        return r1 + r2
    return f"{r1}{r2}{'s' if s1 == s2 else 'o'}"


def _expand(hand: str, plus: bool) -> list[str]:
    """Expand `+` notation into explicit classes."""
    if not plus:
        return [hand]
    hi, lo = hand[0], hand[1]
    suffix = hand[2:] if len(hand) > 2 else ""
    if hi == lo:  # pairs: 77+ -> 77,88,...,AA
        return [RANKS[i] * 2 for i in range(_RANK_INDEX[hi], len(RANKS))]
    # A2s+ -> A2s..AKs: hold the high card, walk the low card up to just under it
    return [
        f"{hi}{RANKS[i]}{suffix}"
        for i in range(_RANK_INDEX[lo], _RANK_INDEX[hi])
    ]


def parse_range(text: str) -> dict[str, float]:
    """Weighted range text to `{class: weight}`.

    Unparseable tokens raise rather than being skipped — a typo in a chart file
    that silently drops half a range would show up later as a confidently wrong
    frequency, which is worse than a crash at load.
    """
    # Strip comments per line, before tokenizing. Dropping `#`-prefixed *tokens*
    # instead would leave the rest of the comment's words as garbage entries --
    # and since unparseable entries raise, a single note at the top of an
    # exported chart would take the whole provider down at startup.
    body = "\n".join(line.split("#", 1)[0] for line in text.splitlines())

    weights: dict[str, float] = {}
    for token in re.split(r"[,\s]+", body.strip()):
        if not token:
            continue
        m = _ENTRY.match(token)
        if m is None:
            raise ValueError(f"unparseable range entry: {token!r}")
        weight = float(m["weight"]) if m["weight"] else 1.0
        if not 0.0 <= weight <= 1.0:
            raise ValueError(f"weight out of range in {token!r}")
        for hand in _expand(m["hand"], bool(m["plus"])):
            weights[hand] = weight
    return weights


class ChartProvider:
    """Serves strategy from exported range files on disk.

    Cheap and offline: everything is read once at construction and held in
    memory. Charts are small — a preflop node is at most 169 entries per action —
    so there is nothing to gain from lazy loading and a lot to gain from failing
    loudly at startup if a file is malformed.
    """

    name = "charts"

    def __init__(self, root: str | Path, *, source: str = ""):
        self.root = Path(root)
        self.source = source or str(self.root)
        self._spots: dict[str, dict[str, dict[str, float]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.root.is_dir():
            return
        for spot_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            actions: dict[str, dict[str, float]] = {}
            for file in sorted(spot_dir.glob("*.txt")):
                action = file.stem.lower()
                # A leading underscore marks a file as not-a-strategy, so notes
                # can live beside the ranges. Everything else must be a known
                # action: the point of the check is catching `calls.txt` or
                # `shove.txt`, which would otherwise vanish silently and leave a
                # range quietly incomplete.
                if action.startswith("_"):
                    continue
                if action not in KNOWN_ACTIONS:
                    raise ValueError(
                        f"{file}: unknown action {action!r}; expected one of "
                        f"{', '.join(KNOWN_ACTIONS)}"
                    )
                actions[action] = parse_range(file.read_text())
            if actions:
                self._spots[spot_dir.name] = actions

    @property
    def spot_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._spots))

    def notes(self, spot_key: str) -> str:
        """Free-text notes for a spot. Editable; not part of the strategy."""
        f = self.root / spot_key / "_notes.md"
        return f.read_text() if f.is_file() else ""

    def write_notes(self, spot_key: str, text: str) -> None:
        d = self.root / spot_key
        if not d.is_dir():
            raise KeyError(spot_key)
        (d / "_notes.md").write_text(text)

    def grid(self, spot_key: str) -> dict[str, dict[str, float]]:
        """Every hand's action mix at a spot, for rendering the 13x13 view.

        Normalized the same way `lookup` does, and with the fold remainder filled
        in, so the UI never has to reimplement that and drift from it.
        """
        actions = self._spots.get(spot_key)
        if actions is None:
            return {}
        out: dict[str, dict[str, float]] = {}
        # Descending, matching how a range grid is drawn (A top-left, 2
        # bottom-right). `RANKS` is ascending, and using it here produced "27s"
        # where the canonical class is "72s" -- every off-diagonal cell missed.
        order = RANKS[::-1]
        for i, hi in enumerate(order):
            for j, lo in enumerate(order):
                klass = (hi + lo if i == j
                         else f"{hi}{lo}s" if j > i else f"{lo}{hi}o")
                freqs = {a: w.get(klass, 0.0) for a, w in actions.items()}
                total = sum(freqs.values())
                if total <= 0:
                    out[klass] = {"fold": 1.0}
                    continue
                if "fold" not in freqs and total < 1.0:
                    freqs["fold"] = 1.0 - total
                    total = 1.0
                out[klass] = {a: f / total for a, f in freqs.items() if f > 0}
        return out

    # "BB_preflop_vs_UTG_raise_2.5bb_100bb" -> "BB_preflop_vs_UTG_raise_100bb"
    _SIZE = re.compile(r"^(?P<head>.*)_[\d.]+bb(?P<depth>_\d+bb)$")
    _DEPTH = re.compile(r"^(?P<head>.*)_(?P<depth>\d+)bb$")

    def resolve(self, spot_key: str) -> str | None:
        """The charted spot answering for `spot_key`, if any.

        Falls back to a size-agnostic key. Charts are published per action, not
        per raise size, so demanding an exact size match would miss on every
        lookup; a chart for "BB vs a UTG raise" answers usefully whether the open
        was 2.2bb or 2.5bb. Far-off sizings are a real approximation, which the
        chart's notes say out loud.
        """
        if spot_key in self._spots:
            return spot_key
        m = self._SIZE.match(spot_key)
        if m:
            generic = m["head"] + m["depth"]
            if generic in self._spots:
                return generic
            spot_key = generic

        # Fall back on stack depth. Charts are published at a depth, not per
        # stack -- this pack is 100bb and states it applies from roughly 50bb up
        # -- so a 175bb spot should still consult the 100bb chart rather than
        # silently having no reference at all. Nearest charted depth wins.
        m = self._DEPTH.match(spot_key)
        if not m:
            return None
        want, head = int(m["depth"]), m["head"]
        candidates = []
        for key in self._spots:
            k = self._DEPTH.match(key)
            if k and k["head"] == head:
                candidates.append((abs(int(k["depth"]) - want), int(k["depth"]), key))
        return min(candidates)[2] if candidates else None

    def lookup(self, spot_key: str, hand: str) -> Solution | None:
        resolved = self.resolve(spot_key)
        actions = self._spots.get(resolved) if resolved else None
        if actions is None:
            return None
        spot_key = resolved
        klass = canonical_class(hand)

        freqs = {a: w.get(klass, 0.0) for a, w in actions.items()}
        total = sum(freqs.values())
        if total <= 0:
            # Absent from every action range means fold. A published chart
            # colours all 169 cells and the uncoloured ones are the fold region,
            # so absence is a statement, not a gap. Returning None here instead
            # withheld a verdict from 37 of 50 real preflop decisions -- every
            # hand the charts simply fold.
            return Solution(
                spot_key=spot_key, hand=klass, provider=self.name,
                source=self.source,
                actions=(ActionFrequency(action="fold", frequency=1.0),),
            )

        # An absent fold.txt is the common case: exports usually cover only the
        # continuing actions, and folding is the remainder.
        if "fold" not in freqs and total < 1.0:
            freqs["fold"] = round(1.0 - total, 6)
            total = 1.0

        # Normalize so frequencies sum to 1. Exported weights are often rounded
        # per action and drift a little; a UI showing "calls 82%, raises 19%" for
        # a two-action node looks broken even though the source was fine.
        return Solution(
            spot_key=spot_key,
            hand=klass,
            provider=self.name,
            source=self.source,
            actions=tuple(
                ActionFrequency(action=a, frequency=f / total)
                for a, f in freqs.items()
                if f > 0
            ),
        )
