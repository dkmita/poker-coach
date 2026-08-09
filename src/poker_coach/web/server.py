"""Local web UI for reading analyzed hands.

    .venv/bin/python -m poker_coach.web --archive archive/synthetic

Stdlib only, deliberately. This machine's pip defaults to an internal index that
doesn't carry common packages, and build isolation has no network — a FastAPI or
npm-based UI would be a dependency problem before it was a UI. `http.server` plus
one self-contained HTML file has no install step, no build, and nothing to break.

The server is a thin shell over `handview.build()`: it serves the same JSON
contract the CLI prints, so the page renders facts, and `gto` / `analysis` blocks
appear as soon as a provider or the analyze stage fills them. Nothing about the
UI needs to change when they do.

Binds to localhost. The archive is personal match data and there is no auth here;
do not expose it.
"""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import handview
from ..replay import ReplayError, hero_index, load, project_index
from ..solvers.base import NullProvider, SolutionProvider
from ..heuristics import Heuristics
from ..solvers.ranges import ChartProvider

APP_HTML = Path(__file__).with_name("app.html")

# Hand identifiers come from the URL, so they are untrusted. Rather than
# sanitizing a path, names are matched against this and then looked up in the
# scanned file list -- a name that isn't already known is never touched.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
# Spot keys carry '+' (UTG+1) but must never carry a path separator.
_SAFE_SPOT = re.compile(r"^[A-Za-z0-9._+-]+$")

# The review filters, in one place. An unknown name falls back to "all", so a
# filter missing from here does not fail -- it silently returns the whole
# archive, which is how `terminal` shipped looking like it matched every hand.
FILTERS = ("all", "flagged", "interesting", "terminal")


class Archive:
    """The .phh files on disk, plus a memo of the views built from them."""

    def __init__(self, root: Path, provider: SolutionProvider):
        self.root = root
        self.provider = provider
        self.files: dict[str, Path] = {
            p.name: p for p in sorted(root.glob("*.phh"))
        }
        self._index: dict[str, list[str]] = {}
        self._order: dict[int, list[str]] = {}
        self._played: dict[str, datetime] = {}

    def played_at(self, name: str) -> datetime:
        """When the hand was dealt, in UTC, read straight from the TOML.

        Not via `handview` on purpose: ordering the list must not cost a replay
        of the whole archive, and the timestamp is four scalar fields sitting at
        the top of the file. Epoch for a hand that carries no date, which sorts
        it to the end rather than guessing.
        """
        if name not in self._played:
            try:
                raw = tomllib.loads(self.files[name].read_text())
            except (OSError, tomllib.TOMLDecodeError):
                raw = {}
            y, m, d = raw.get("year"), raw.get("month"), raw.get("day")
            t = raw.get("time")
            self._played[name] = (
                datetime(y, m, d, getattr(t, "hour", 0), getattr(t, "minute", 0),
                         getattr(t, "second", 0), tzinfo=UTC)
                if y and m and d
                else datetime(1970, 1, 1, tzinfo=UTC)
            )
        return self._played[name]

    def ordered(self, tz_offset: int) -> list[str]:
        """File names newest *day* first, but in playing order within a day.

        Which is how a session reads: you want last night's hands before last
        week's, and within last night you want to follow the table forward, not
        watch it run backwards.

        The day boundary is the player's, not UTC's -- `tz_offset` is minutes
        east of UTC, sent by the browser. This sample is a single UTC day that
        locally is Wednesday evening and Thursday morning, so grouping on UTC
        would collapse the two sessions into one. A session spanning a DST
        change would use the wrong boundary by an hour; nothing here does.
        """
        if tz_offset not in self._order:
            shift = timedelta(minutes=tz_offset)
            self._order[tz_offset] = sorted(
                self.files,
                key=lambda n: (
                    # Negated so later days come first while the timestamp
                    # inside a day still sorts ascending.
                    -(self.played_at(n) + shift).date().toordinal(),
                    self.played_at(n),
                    n,
                ),
            )
        return self._order[tz_offset]

    def view(self, name: str) -> dict | None:
        path = self.files.get(name)
        if path is None:
            return None
        return self._build(name)

    @lru_cache(maxsize=512)
    def _build(self, name: str) -> dict | None:
        try:
            hh = load(self.files[name])
        except ReplayError as exc:
            return {"error": str(exc), "file": name}
        view = handview.build(hh, provider=self.provider)
        view["file"] = name
        return view

    def filtered(self, kind: str, tz_offset: int = 0) -> list[str]:
        """Files matching a review filter.

        Filtering has to happen before pagination or the count is a lie -- a page
        of 60 showing "3 flagged" when the archive holds 13 is worse than no
        filter. That means replaying every hand, so the index is memoised and
        built only when a filter is actually used.
        """
        if kind not in self._index:
            self._index[kind] = {
                name for name in self.files if self._matches(kind, self._build(name))
            }
        keep = self._index[kind]
        return [n for n in self.ordered(tz_offset) if n in keep]

    def _matches(self, kind: str, view: dict | None) -> bool:
        if not view or "error" in view:
            return False
        if kind == "flagged":
            return bool(self._off_chart(view))
        if kind == "interesting":
            return bool(view["interest"]["interesting"])
        if kind == "terminal":
            # Any player's, not only hero's -- a villain calling off is where
            # you learn what the pool actually stacks off with.
            return bool(view.get("terminal"))
        return True

    @staticmethod
    def _off_chart(view: dict | None) -> list[str]:
        if not view or "error" in view:
            return []
        return [
            d["verdict"]["label"]
            for d in view["hero_decisions"]
            if d["street"] == "preflop" and (d.get("verdict") or {}).get("tone") == "bad"
        ]

    def summaries(
        self, offset: int, limit: int, *, kind: str = "all", tz_offset: int = 0
    ) -> dict:
        """Lightweight rows for the list pane.

        Built per page rather than for the whole archive at startup: a 2000-hand
        corpus would cost a few seconds of replay to show fifty rows.
        """
        source = (
            self.ordered(tz_offset) if kind == "all" else self.filtered(kind, tz_offset)
        )
        rows = []
        for name in source[offset : offset + limit]:
            # The same cached view the detail pane uses. Building it here costs a
            # replay per row but keeps one source of truth, and it means the row
            # can carry the verdicts -- which is the point: a mistake you have to
            # open every hand to find is a mistake you will not find.
            view = self._build(name)
            if view is None or "error" in view:
                rows.append({"file": name, "error": (view or {}).get("error", "unknown")})
                continue
            h, r = view["hand"], view["result"]
            off = self._off_chart(view)
            rows.append(
                {
                    "file": name,
                    "site_hand_id": h["site_hand_id"],
                    # UTC, as the site records it. The client renders it in local
                    # time -- what matters is when *you* were at the table, and a
                    # session that reads as one evening locally is spread across
                    # two dates in UTC.
                    "played_at": h["played_at"],
                    "position": h["hero"]["position"],
                    "hole_cards": h["hero"]["hole_cards"],
                    "hero_street_reached": r["hero_street_reached"],
                    "street_reached": r["street_reached"],
                    "hero_net_bb": r["hero_net_bb"],
                    "preflop_off_chart": off,
                    # Reasons only; the detail pane carries the nodes themselves.
                    "terminal": sorted({
                        n["reason"] for n in view.get("terminal", [])
                    }),
                    "terminal_hero": any(
                        n["is_hero"] for n in view.get("terminal", [])
                    ),
                    "interest": view["interest"]["reasons"],
                }
            )
        return {
            "total": len(source),
            "archive_total": len(self.files),
            # Only known once a filter has been used; null until then, so the
            # first page load never pays for a full-archive replay.
            "counts": {k: len(v) for k, v in self._index.items()},
            "offset": offset,
            "rows": rows,
        }


def make_handler(archive: Archive, heuristics: Heuristics):
    class Handler(BaseHTTPRequestHandler):
        # Default logging prints a line per request, which drowns the startup
        # banner the moment the page loads its list.
        def log_message(self, *args) -> None:
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Everything here is generated per request and every one of it can
            # change under the browser: app.html is re-read from disk on each
            # load, and the hand JSON changes whenever the archive is
            # re-ingested. With no validator and no max-age the browser is
            # entitled to cache heuristically, and it does -- which shows up as
            # a UI edit that "did not take" until a hard reload.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_PUT(self) -> None:  # noqa: N802 - stdlib naming
            url = urlparse(self.path)
            if url.path.startswith("/api/heuristics/"):
                slug = url.path.rsplit("/", 1)[-1]
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                try:
                    if not _SAFE_NAME.match(slug):
                        raise KeyError(slug)
                    heuristics.write(slug, body.get("body", ""))
                except KeyError:
                    self._json({"error": "no such heuristic"}, 404)
                    return
                self._json({"ok": True})
                return
            if not url.path.startswith("/api/charts/") or not url.path.endswith("/notes"):
                self._json({"error": "not found"}, 404)
                return
            spot = url.path[len("/api/charts/") : -len("/notes")]
            if not _SAFE_SPOT.match(spot):
                self._json({"error": "bad spot"}, 400)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                archive.provider.write_notes(spot, body.get("notes", ""))
            except (KeyError, AttributeError):
                self._json({"error": "not found"}, 404)
                return
            self._json({"ok": True})

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            url = urlparse(self.path)
            query = parse_qs(url.query)

            if url.path in ("/", "/index.html"):
                self._send(200, APP_HTML.read_bytes(), "text/html; charset=utf-8")
                return

            if url.path == "/api/hands":
                offset = int(query.get("offset", ["0"])[0])
                limit = min(int(query.get("limit", ["60"])[0]), 200)
                kind = query.get("filter", ["all"])[0]
                if kind not in FILTERS:
                    kind = "all"
                # Minutes east of UTC, from the browser. Clamped to the real
                # range so a bad value cannot shift a hand into another week.
                try:
                    tz = max(-720, min(840, int(query.get("tz", ["0"])[0])))
                except ValueError:
                    tz = 0
                self._json(archive.summaries(offset, limit, kind=kind, tz_offset=tz))
                return

            if url.path == "/api/heuristics":
                self._json({
                    "items": [
                        {"slug": h.slug, "title": h.title, "order": h.order,
                         "group": h.group}
                        for h in heuristics.all()
                    ]
                })
                return

            if url.path.startswith("/api/heuristics/"):
                slug = url.path.rsplit("/", 1)[-1]
                item = heuristics.get(slug) if _SAFE_NAME.match(slug) else None
                if item is None:
                    self._json({"error": "no such heuristic"}, 404)
                    return
                self._json({
                    "slug": item.slug, "title": item.title,
                    "body": item.body, "group": item.group,
                })
                return

            if url.path == "/api/charts":
                charts = getattr(archive.provider, "spot_keys", ())
                self._json({"spots": list(charts)})
                return

            if url.path.startswith("/api/charts/"):
                spot = url.path[len("/api/charts/") :]
                if not _SAFE_SPOT.match(spot) or spot not in getattr(
                    archive.provider, "spot_keys", ()
                ):
                    self._json({"error": "not found"}, 404)
                    return
                self._json({
                    "spot": spot,
                    "grid": archive.provider.grid(spot),
                    "notes": archive.provider.notes(spot),
                })
                return

            if url.path.startswith("/api/hands/"):
                name = url.path[len("/api/hands/") :]
                if not _SAFE_NAME.match(name):
                    self._json({"error": "bad name"}, 400)
                    return
                view = archive.view(name)
                if view is None:
                    self._json({"error": "not found"}, 404)
                    return
                self._json(view)
                return

            self._json({"error": "not found"}, 404)

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--archive", type=Path, default=Path("archive/synthetic"))
    ap.add_argument("--charts", type=Path, default=Path("charts"))
    ap.add_argument("--heuristics", type=Path, default=Path("heuristics"))
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    if not args.archive.is_dir():
        print(f"no such archive: {args.archive}")
        return 1

    provider: SolutionProvider
    if args.charts.is_dir():
        provider = ChartProvider(args.charts, source=str(args.charts))
        charted = len(provider.spot_keys)
    else:
        provider, charted = NullProvider(), 0

    archive = Archive(args.archive, provider)
    if not archive.files:
        print(f"no .phh files in {args.archive}")
        return 1

    print(f"  {len(archive.files)} hands from {args.archive}")
    print(
        f"  {charted} charted spot(s)"
        + ("" if charted else "  (no GTO facts -- see charts/README.md)")
    )
    print(f"\n  http://127.0.0.1:{args.port}\n")

    # Localhost only: the archive is personal match data and there is no auth.
    ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(archive, Heuristics(args.heuristics))
    ).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
