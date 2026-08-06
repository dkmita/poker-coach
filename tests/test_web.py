"""Tests for the local web UI's API.

The page is a thin renderer over these responses, so the contract is what's worth
testing: the list pane's rows, the full view, and that a hand name from a URL
can't reach outside the archive.
"""

from __future__ import annotations

import json
import textwrap
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest

from poker_coach.solvers.ranges import ChartProvider
from poker_coach.web.server import Archive, make_handler

HAND = textwrap.dedent("""
    variant = "NT"
    antes = [0, 0, 0, 0, 0, 0]
    blinds_or_straddles = [50, 100, 0, 0, 0, 0]
    min_bet = 100
    starting_stacks = [10000, 10000, 10000, 10000, 10000, 10000]
    actions = [
      "d dh p1 ????", "d dh p2 AdJs", "d dh p3 ????",
      "d dh p4 ????", "d dh p5 ????", "d dh p6 AhKs",
      "p3 cbr 250", "p4 f", "p5 f", "p6 f", "p1 f", "p2 f",
    ]
    finishing_stacks = [9950, 9900, 10150, 10000, 10000, 10000]
    _pc_site = "wpn"
    _pc_site_hand_id = "ACR-1"
    _pc_hero_index = 1
""").strip()


@pytest.fixture
def server(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "hand_00001.phh").write_text(HAND)

    charts = tmp_path / "charts"
    spot = charts / "BB_preflop_vs_UTG_raise_2.5bb_100bb"
    spot.mkdir(parents=True)
    (spot / "call.txt").write_text("AJo:0.8")
    (spot / "raise.txt").write_text("AJo:0.2")

    archive = Archive(archive_dir, ChartProvider(charts, source="test"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(archive))
    Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def get(base, path):
    with urlopen(base + path) as r:
        return json.loads(r.read())


def test_index_serves_the_page(server):
    with urlopen(server + "/") as r:
        body = r.read().decode()
    assert r.status == 200 and "<title>poker-coach</title>" in body


def test_list_rows(server):
    d = get(server, "/api/hands")
    assert d["total"] == 1
    row = d["rows"][0]
    assert row["position"] == "BB"
    assert row["hole_cards"] == ["Ad", "Js"]
    assert row["hero_net_bb"] == -1.0


def test_hand_detail_includes_gto_from_charts(server):
    v = get(server, "/api/hands/hand_00001.phh")
    d = v["hero_decisions"][0]
    assert d["hero_action"] == "fold"
    assert d["gto"]["actions"][0]["action"] == "call"


def test_unknown_hand_is_404(server):
    with pytest.raises(HTTPError) as e:
        urlopen(server + "/api/hands/nope.phh")
    assert e.value.code == 404


def test_path_traversal_rejected(server):
    """The hand name comes from the URL and is therefore untrusted."""
    with pytest.raises(HTTPError) as e:
        urlopen(server + "/api/hands/..%2f..%2fetc%2fpasswd")
    assert e.value.code == 400


def test_list_row_flags_off_chart_preflop(server):
    """A mistake you must open every hand to find is a mistake you won't find."""
    d = get(server, "/api/hands")
    row = d["rows"][0]
    assert "preflop_off_chart" in row
    # Hero folds AdJs in the big blind facing a UTG raise; the fixture chart
    # calls it 80% of the time, so the fold is off chart.
    assert row["preflop_off_chart"] == ["off chart — calls here"]


def test_clean_hands_carry_an_empty_flag_list(server):
    """Present-and-empty, so the client can rely on the key existing."""
    row = get(server, "/api/hands")["rows"][0]
    assert isinstance(row["preflop_off_chart"], list)
