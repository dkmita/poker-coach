"""The standing guidance handed to the range-estimating model.

Files on disk, not string literals, for the same reason ranges are: they are
edited far more often than the code around them, they want a diff when they
change, and the thing that reads them should not need redeploying.

One directory per **group**, one file per topic inside it. Order is fixed by a
numeric filename prefix rather than by directory listing, because these are
assembled into a prompt and the order is part of the prompt.

    heuristics/
      shared/    how to answer at all -- both passes get this
      gto/       what equilibrium does
      exploit/   what this pool does instead

The split is the point, not filing. A range estimated from equilibrium and a
range estimated from population tendencies are different claims, and mixing the
two sources into one prompt produces something that is neither -- you cannot
tell afterwards which part of the answer came from theory and which from a read.
Estimated separately, the *difference* between them is the exploit, and it is
inspectable.

They are a **prompt prefix**, which has two consequences worth stating:

* Prompt caching keys on a byte-identical prefix, so the assembled text must be
  stable. It carries no timestamps, no hand ids, and no counts -- nothing that
  varies per hand.
* Editing one invalidates that cache for every subsequent hand in the run. That
  is the right trade (a stale heuristic is worse than a cache miss) but it is
  why editing mid-run is worth avoiding.

The content is deliberately *priors*, not rules. A heuristic that says "the
button opens about half of hands" is a starting point for reading a line, and
the pool this is pointed at deviates from every one of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# `01-name.md` -- the number orders the prompt, the name is what the UI shows.
_FILENAME = re.compile(r"^(?P<order>\d+)-(?P<slug>[a-z0-9-]+)\.md$")

# Both passes get `shared`. The equilibrium pass gets `gto`; the exploitative
# pass gets everything, because it is adjusting a baseline rather than starting
# over, and needs to know what it is adjusting away from.
SHARED = "shared"
GTO = "gto"
EXPLOIT = "exploit"
GTO_GROUPS = (SHARED, GTO)
EXPLOIT_GROUPS = (SHARED, GTO, EXPLOIT)


@dataclass(frozen=True, slots=True)
class Heuristic:
    slug: str
    title: str
    order: int
    body: str
    group: str


class Heuristics:
    """Read/write access to the heuristics directory.

    Re-read per call rather than cached: they are edited from the UI while the
    server runs, and a stale copy served back into an edit box loses the edit.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, slug: str) -> Path | None:
        for p in self.root.glob("*/*.md"):
            m = _FILENAME.match(p.name)
            if m and m["slug"] == slug:
                return p
        return None

    def all(self) -> list[Heuristic]:
        """Every heuristic, ordered by group then by filename prefix.

        Groups are ordered `shared`, `gto`, `exploit` -- prompt order, which
        runs general to specific -- and anything else alphabetically after them,
        so a new directory appears rather than silently disappearing.
        """
        out: list[Heuristic] = []
        if not self.root.is_dir():
            return out
        for p in sorted(self.root.glob("*/*.md")):
            m = _FILENAME.match(p.name)
            if not m:
                continue
            body = p.read_text()
            # The first markdown heading titles the file; falling back to the
            # slug means a heading-less file still lists rather than vanishing.
            head = next(
                (ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")),
                m["slug"].replace("-", " "),
            )
            out.append(
                Heuristic(
                    slug=m["slug"],
                    title=head,
                    order=int(m["order"]),
                    body=body,
                    group=p.parent.name,
                )
            )
        order = {SHARED: 0, GTO: 1, EXPLOIT: 2}
        return sorted(
            out, key=lambda h: (order.get(h.group, 9), h.group, h.order, h.slug)
        )

    def get(self, slug: str) -> Heuristic | None:
        return next((h for h in self.all() if h.slug == slug), None)

    def write(self, slug: str, body: str) -> None:
        path = self._path(slug)
        if path is None:
            raise KeyError(slug)
        path.write_text(body)

    def prompt(self, *groups: str) -> str:
        """The named groups, in order, as one block for a system prompt.

        No groups means all of them. Byte-stable for a given set of files -- see
        the module docstring on why that matters.
        """
        wanted = set(groups) if groups else None
        return "\n\n".join(
            h.body.rstrip()
            for h in self.all()
            if wanted is None or h.group in wanted
        )
