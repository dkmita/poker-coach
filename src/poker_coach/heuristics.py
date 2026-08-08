"""The standing guidance handed to the range-estimating model.

Files on disk, not string literals, for the same reason ranges are: they are
edited far more often than the code around them, they want a diff when they
change, and the thing that reads them should not need redeploying.

Each file is one topic. Order is fixed by a numeric filename prefix rather than
by directory listing, because these are assembled into a prompt and the order is
part of the prompt.

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


@dataclass(frozen=True, slots=True)
class Heuristic:
    slug: str
    title: str
    order: int
    body: str


class Heuristics:
    """Read/write access to the heuristics directory.

    Re-read per call rather than cached: they are edited from the UI while the
    server runs, and a stale copy served back into an edit box loses the edit.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, slug: str) -> Path | None:
        for p in self.root.glob("*.md"):
            m = _FILENAME.match(p.name)
            if m and m["slug"] == slug:
                return p
        return None

    def all(self) -> list[Heuristic]:
        out: list[Heuristic] = []
        if not self.root.is_dir():
            return out
        for p in sorted(self.root.glob("*.md")):
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
                Heuristic(slug=m["slug"], title=head, order=int(m["order"]), body=body)
            )
        return sorted(out, key=lambda h: (h.order, h.slug))

    def get(self, slug: str) -> Heuristic | None:
        return next((h for h in self.all() if h.slug == slug), None)

    def write(self, slug: str, body: str) -> None:
        path = self._path(slug)
        if path is None:
            raise KeyError(slug)
        path.write_text(body)

    def prompt(self) -> str:
        """Everything, in order, as one block for the model's system prompt.

        Byte-stable for a given set of files -- see the module docstring on why
        that matters.
        """
        return "\n\n".join(h.body.rstrip() for h in self.all())
