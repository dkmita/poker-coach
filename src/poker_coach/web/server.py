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
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import handview
from ..replay import ReplayError, hero_index, load, project_index
from ..solvers.base import NullProvider, SolutionProvider
from ..solvers.ranges import ChartProvider

APP_HTML = Path(__file__).with_name("app.html")

# Hand identifiers come from the URL, so they are untrusted. Rather than
# sanitizing a path, names are matched against this and then looked up in the
# scanned file list -- a name that isn't already known is never touched.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
# Spot keys carry '+' (UTG+1) but must never carry a path separator.
_SAFE_SPOT = re.compile(r"^[A-Za-z0-9._+-]+$")


class Archive:
    """The .phh files on disk, plus a memo of the views built from them."""

    def __init__(self, root: Path, provider: SolutionProvider):
        self.root = root
        self.provider = provider
        self.files: dict[str, Path] = {
            p.name: p for p in sorted(root.glob("*.phh"))
        }

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

    def summaries(self, offset: int, limit: int) -> dict:
        """Lightweight rows for the list pane.

        Built per page rather than for the whole archive at startup: a 2000-hand
        corpus would cost a few seconds of replay to show fifty rows.
        """
        names = list(self.files)[offset : offset + limit]
        rows = []
        for name in names:
            try:
                hh = load(self.files[name])
                idx = project_index(hh, phh_path=name, phh_sha256="")
                hero = hero_index(hh)
                cards = handview._hole_from_actions(hh, hero)
                # Hero's own street, not the hand's: hero can fold preflop while
                # the others run it to the river, and listing that as "river"
                # badly overstates how often you saw a flop.
                view = handview.build(hh)
                rows.append(
                    {
                        "file": name,
                        "site_hand_id": idx.site_hand_id,
                        "position": idx.hero_position.value,
                        "hole_cards": cards,
                        "hero_street_reached": view["result"]["hero_street_reached"],
                        "street_reached": idx.street_reached.value,
                        "hero_net_bb": round(idx.hero_net / idx.bb, 2),
                        "eff_stack_bb": round(idx.eff_stack_bb),
                    }
                )
            except ReplayError as exc:
                rows.append({"file": name, "error": str(exc)})
        return {"total": len(self.files), "offset": offset, "rows": rows}


def make_handler(archive: Archive):
    class Handler(BaseHTTPRequestHandler):
        # Default logging prints a line per request, which drowns the startup
        # banner the moment the page loads its list.
        def log_message(self, *args) -> None:
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_PUT(self) -> None:  # noqa: N802 - stdlib naming
            url = urlparse(self.path)
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
                self._json(archive.summaries(offset, limit))
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
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(archive)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
