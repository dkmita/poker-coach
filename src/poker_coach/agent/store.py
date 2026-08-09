"""Estimated ranges, kept on disk.

Model output costs money and time, and it is keyed on the abstract spot rather
than on the hand -- so a spot that comes up eleven times in a session is paid
for once, and only the first time it is ever seen. That only holds if the cache
outlives the process, which is what this is.

Gitignored. It is regenerable, and it is tied to a particular model and a
particular version of `heuristics/` -- committing it would put answers in the
repo that no longer follow from the prompt that produced them. The `model` and
`heuristics_digest` fields on each entry are there so a stale one can be
recognised rather than trusted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


def _slug(spot_key: str, board: str, kind: str) -> str:
    """A filename for one question about a spot.

    Hashed because a spot key plus a board is long and a board is not
    filesystem-safe everywhere. `kind` is in the key because hero's range and
    the opponent's range at the same node are different questions.
    """
    raw = f"{spot_key}|{board}|{kind}"
    return f"{kind}-{spot_key[:44]}-{hashlib.sha256(raw.encode()).hexdigest()[:10]}.json"


@dataclass
class RangeStore:
    root: Path

    def path(self, spot_key: str, board: str, kind: str = "opponent") -> Path:
        return self.root / _slug(spot_key, board, kind)

    def get(self, spot_key: str, board: str, kind: str = "opponent") -> dict | None:
        try:
            return json.loads(self.path(spot_key, board, kind).read_text())
        except (OSError, ValueError):
            return None

    def put(self, spot_key: str, board: str, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path(spot_key, board, payload.get("kind", "opponent"))
        # Written whole then moved, so a run interrupted mid-write leaves the
        # previous answer rather than a truncated one.
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(target)

    def all(self) -> dict[str, dict]:
        out = {}
        for p in sorted(self.root.glob("*.json")) if self.root.is_dir() else []:
            try:
                entry = json.loads(p.read_text())
            except ValueError:
                continue
            key = (f"{entry.get('spot_key', '')}|{entry.get('board', '')}"
                   f"|{entry.get('kind', 'opponent')}")
            out[key] = entry
        return out


def heuristics_digest(text: str) -> str:
    """Short fingerprint of the guidance an answer was produced under."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]
