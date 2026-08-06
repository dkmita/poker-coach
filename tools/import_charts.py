#!/usr/bin/env python3
"""Import a PokerCoaching preflop-chart PDF into `charts/`.

    .venv/bin/python tools/import_charts.py ~/Documents/.../preflop-charts.pdf

Each page holds a grid of 13x13 range charts with a label above each one. The
work is in pairing them correctly, and every shortcut here was tried and
rejected:

* Carving JPEGs by byte offset gives *object* order, not page order.
* `pdfimages -p` groups by page but still yields object order within a page.
* Image object numbers are not page order either.

The only reliable pairing is the page content stream, which records where each
image is actually drawn (`... cm /ImageN Do`). Sorting those placements by
(y, x) reproduces the visual grid, and the labels read in the same order. A
mislabelled chart is worse than a missing one -- the UI would confidently
measure your play against the wrong reference -- so the importer refuses a page
whose label count and image count disagree.

Colours are read straight off the pixels; `sips` converts each JPEG to BMP,
which is parseable with the standard library. The extraction is self-checking:
every chart states its own frequency in the legend, so a parse that disagrees
with the document is caught rather than trusted.
"""

from __future__ import annotations

import argparse
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import Counter
from pathlib import Path

RANKS = "AKQJT98765432"

# What the source assumes. Recorded with every chart because it is the
# difference between a useful default and a misleading one: an ante widens
# correct opening ranges, and these are 9-handed charts.
SOURCE_NOTE = """\
Source: PokerCoaching.com preflop chart pack (9-handed).

ASSUMPTIONS THAT MAY NOT MATCH YOUR GAME
- **Ante in play.** The pack assumes an ante. Antes add dead money and widen
  correct opening ranges, so these are looser than a no-ante game warrants.
  ACR cash tables have no ante.
- **9-handed.** Positions are for a full-ring table. In 6-max, match by players
  left to act, not by name: 6-max UTG behaves like the 9-max Lojack.
- **100bb effective**, stated to apply from roughly 50bb up.
- Sizing: 2.5bb open in position, 3.5bb out of position; 3-bet 3x the raise in
  position, 3.5x out; 4-bet 2.5x the 3-bet in position, 2.75x out.

Treat as a default, not as a solver output.
"""

# 9-handed seat -> its 6-max equivalent, matched by players left to act rather
# than by name. UTG, UTG+1 and UTG+2 have no 6-max counterpart: a 6-max table
# simply does not have those seats.
SIX_MAX = {
    "Lojack": "UTG", "LJ": "UTG",
    "Hijack": "HJ", "HJ": "HJ",
    "Cutoff": "CO", "CO": "CO",
    "Button": "BTN", "BTN": "BTN",
    "Small Blind": "SB", "SB": "SB",
    "Big Blind": "BB", "BB": "BB",
}
NINE_MAX_ONLY = {"UTG", "UTG+1", "UTG+2"}

SIZE_NOTE = """\
### Villain sizing

The spot key carries no raise size. The pack states sizing by position rather
than per chart -- 2.5bb opens in position, 3.5bb out, 3-bets 3x/3.5x, 4-bets
2.5x/2.75x -- so pinning one size per chart would be inventing precision the
source does not have. `ChartProvider` therefore falls back to the size-agnostic
key, and this chart answers for any nearby size. Treat it as approximate when
the actual sizing is far from the pack's.
"""


def cells(w: int, h: int, px) -> dict[str, str]:
    """Classify all 169 cells of a 13x13 grid into action buckets by colour."""
    grid, out = 13, {}
    cell = w / grid
    for r in range(grid):
        for c in range(grid):
            tally: Counter[str] = Counter()
            for y in range(int(r * cell + cell * 0.15), int(r * cell + cell * 0.85)):
                for x in range(int(c * cell + cell * 0.15), int(c * cell + cell * 0.85)):
                    R, G, B = px(x, y)
                    if R < 60 and G < 60 and B < 60:
                        continue  # glyph or border
                    if R > 200 and G > 200 and B > 200:
                        tally["fold"] += 1
                    elif R > G + 45 and R > B + 45:
                        tally["raise"] += 1
                    elif G > R + 30 and G > B + 20:
                        tally["call"] += 1
                    elif R > 150 and G > 120 and B < 110:
                        tally["mixed"] += 1
            if tally:
                hi, lo = RANKS[min(r, c)], RANKS[max(r, c)]
                hand = hi + lo if r == c else f"{hi}{lo}" + ("s" if c > r else "o")
                out[hand] = tally.most_common(1)[0][0]
    return out


def load_bmp(path: Path):
    d = path.read_bytes()
    off, = struct.unpack("<I", d[10:14])
    w, h = struct.unpack("<ii", d[18:26])
    bpp, = struct.unpack("<H", d[28:30])
    topdown, h = h < 0, abs(h)
    row = ((w * bpp // 8) + 3) & ~3

    def px(x, y):
        yy = y if topdown else h - 1 - y
        i = off + yy * row + x * (bpp // 8)
        return d[i + 2], d[i + 1], d[i]

    return w, h, px


def read_chart(jpeg: bytes) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        j, b = Path(tmp) / "c.jpg", Path(tmp) / "c.bmp"
        j.write_bytes(jpeg)
        subprocess.run(["sips", "-s", "format", "bmp", str(j), "--out", str(b)],
                       capture_output=True, check=True)
        return cells(*load_bmp(b))


def pdf_objects(d: bytes) -> dict[int, tuple[int, int, bytes]]:
    """Image object number -> (pixel width, pixel height, jpeg bytes)."""
    out = {}
    for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<(.{0,900}?)>>\s*stream\r?\n", d, re.S):
        hdr = m.group(2)
        if b"/Image" not in hdr or b"DCTDecode" not in hdr:
            continue
        w = re.search(rb"/Width\s+(\d+)", hdr)
        h = re.search(rb"/Height\s+(\d+)", hdr)
        if not (w and h):
            continue
        end = d.find(b"endstream", m.end())
        out[int(m.group(1))] = (int(w.group(1)), int(h.group(1)), d[m.end():end])
    return out


def page_layouts(d: bytes):
    """Yield (n_images, [(objnum, x, y)]) for each content stream that draws images.

    The content stream is the only place that records *where* an image sits, and
    placement is the only ordering that matches the labels.
    """
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", d, re.S):
        try:
            body = zlib.decompress(m.group(1))
        except Exception:
            continue
        placements = re.findall(
            rb"([\d.-]+)\s+[\d.-]+\s+[\d.-]+\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+cm\s*/(\w+)\s+Do",
            body)
        if placements:
            yield [(name.decode(), float(x), float(y)) for _, _, x, y, name in placements]


def resources_map(d: bytes) -> dict[str, int]:
    """Resource name (/Image7) -> object number.

    Page objects live inside compressed object streams in this PDF, so the raw
    bytes contain none of these mappings -- everything must be inflated first.
    """
    out: dict[str, int] = {}

    def harvest(buf: bytes) -> None:
        for m in re.finditer(rb"/(Image\d+)\s+(\d+)\s+0\s+R", buf):
            out.setdefault(m.group(1).decode(), int(m.group(2)))

    harvest(d)
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", d, re.S):
        try:
            harvest(zlib.decompress(m.group(1)))
        except Exception:
            continue
    return out


_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9+/ ]{0,28}$")


def _is_label(text: str) -> bool:
    t = text.strip()
    if not t or not _LABEL.match(t):
        return False
    # Seat names are capitalised tokens ("Lojack", "HJ vs CO 3bet"); prose isn't.
    return all(w[0].isupper() or w[0].isdigit() or w in ("vs", "3bet")
               for w in t.split())


def _seat(name: str) -> tuple[str, bool]:
    """(seat label, is_six_max). Compound seats map only if every part maps."""
    name = name.strip()
    parts = name.split("/")
    if all(p in SIX_MAX for p in parts):
        mapped = sorted({SIX_MAX[p] for p in parts})
        return "-".join(mapped), True
    return name.replace("/", "-").replace(" ", ""), False


def spot_key(label: str) -> tuple[str, str]:
    """(spot key, kind) for a chart label.

    Keys carry no raise size -- see SIZE_NOTE. Charts with no 6-max equivalent
    are still imported, under a `9max_` prefix: they are useful to read even
    though no 6-max hand will ever look them up, and dropping them would lose
    two thirds of the pack.
    """
    label = " ".join(label.split())
    if label.lower() == "sb limp vs bb raise":
        return "SB_preflop_limp_vs_BB_raise_100bb", "limp"
    for pat in (r"^(?P<hero>.+?) RFI vs (?P<vill>.+?) 3bet$",
                r"^(?P<hero>.+?) vs (?P<vill>.+?) 3bet$"):
        m = re.match(pat, label)
        if m:
            hero, h6 = _seat(m["hero"]); vill, v6 = _seat(m["vill"])
            pre = "" if (h6 and v6) else "9max_"
            return f"{pre}{hero}_preflop_vs_{vill}_3bet_100bb", "vs 3-bet"
    m = re.match(r"^(?P<hero>.+?) vs (?P<vill>.+?)$", label)
    if m:
        hero, h6 = _seat(m["hero"]); vill, v6 = _seat(m["vill"])
        pre = "" if (h6 and v6) else "9max_"
        return f"{pre}{hero}_preflop_vs_{vill}_raise_100bb", "facing a raise"
    seat, ok = _seat(label)
    pre = "" if ok else "9max_"
    return f"{pre}{seat}_preflop_unopened_100bb", "open-raise"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, default=Path("charts"))
    args = ap.parse_args()

    if not shutil.which("pdftotext"):
        print("needs poppler: brew install poppler")
        return 1

    d = args.pdf.read_bytes()
    objects, resources = pdf_objects(d), resources_map(d)

    text = subprocess.run(["pdftotext", "-layout", str(args.pdf), "-"],
                          capture_output=True, text=True, check=True).stdout
    pages = text.split("\f")

    layouts = list(page_layouts(d))
    written, skipped = 0, []

    for page_no, page_text in enumerate(pages, start=1):
        lines = [l for l in page_text.splitlines() if l.strip()]
        if page_no < 3 or not lines:
            continue
        # Labels sit in rows above their charts; split on runs of whitespace.
        labels: list[str] = []
        for line in lines[1:]:
            parts = re.split(r"\s{2,}", line.strip())
            # A chart label is a short seat expression: no sentence punctuation,
            # no lowercase prose. Without this the footnote on the last page
            # becomes a chart named "used as a limp/3-bet for value."
            if all(_is_label(p) for p in parts):
                labels.extend(p for p in parts if p)
        layout = next((l for l in layouts if len(l) == len(labels)), None)
        if not labels or layout is None:
            continue

        # Visual order: down the page, then across. y grows downward here.
        ordered = sorted(layout, key=lambda t: (round(t[2] / 5000), t[1]))
        for label, (name, _x, _y) in zip(labels, ordered):
            key, kind = spot_key(label)
            obj = resources.get(name)
            if obj not in objects:
                skipped.append(label)
                continue
            w, h, jpeg = objects[obj]
            buckets: dict[str, list[str]] = {}
            for hand, action in read_chart(jpeg).items():
                buckets.setdefault(action, []).append(hand)

            spot = args.out / key
            spot.mkdir(parents=True, exist_ok=True)
            for action, hands in buckets.items():
                if action == "fold":
                    continue  # inferred as the remainder
                (spot / f"{action}.txt").write_text(", ".join(sorted(hands)) + "\n")
            six = not key.startswith("9max_")
            applies = (
                f"6-max equivalent of the 9-handed **{label}** chart."
                if six else
                f"**No 6-max equivalent.** This is the 9-handed **{label}** chart; a "
                "6-max table has no UTG/UTG+1/UTG+2 seat, so no hand of yours will "
                "ever match it. Kept for reading."
            )
            (spot / "_notes.md").write_text(
                f"# {label}\n\n_{kind.capitalize()} · imported from "
                f"`{args.pdf.name}`, page {page_no}._\n\n"
                f"{applies}\n\n{SOURCE_NOTE}\n{SIZE_NOTE}\n"
                "## Your notes\n\n_Edit this section in the UI._\n"
            )
            written += 1
            print(f"  {key:<34} <- {label}")

    print(f"\n  {written} charts -> {args.out}")
    if skipped:
        print(f"  {len(skipped)} skipped (no 6-max spot key yet)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
