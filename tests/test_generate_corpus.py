"""Guards on the synthetic corpus generator.

The corpus is only useful if it is reproducible: `manifest.json` locates planted
mistakes by the ordinal of hero's decision within a hand, so if regenerating from
the same seed produces different hands, every one of those pointers is wrong.
That broke once already — pokerkit shuffles from the global `random` module, so
seeding a local `Random()` was not enough.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from poker_coach.models import ActionType
from poker_coach.replay import hero_index, iter_decisions, load, project_index

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "generate_corpus.py"


def _generate(out: Path, count: int = 24, seed: int = 3) -> list[dict]:
    subprocess.run(
        [sys.executable, str(SCRIPT), "--count", str(count), "--out", str(out),
         "--seed", str(seed)],
        check=True, capture_output=True, cwd=ROOT,
    )
    return json.loads((out / "manifest.json").read_text())


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    out = tmp_path_factory.mktemp("corpus")
    return out, _generate(out)


def test_every_hand_replays(corpus):
    """pokerkit validates as it plays, so a generated hand must round-trip."""
    out, manifest = corpus
    for entry in manifest:
        hh = load(out / entry["file"])
        idx = project_index(hh, phh_path=entry["file"], phh_sha256="x")
        assert idx.site == "synthetic"
        assert idx.bb == 100
        assert hero_index(hh) == entry["hero_index"]


def test_reproducible_from_seed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _generate(a, seed=11)
    _generate(b, seed=11)
    for f in sorted(a.glob("*.phh")):
        assert f.read_text() == (b / f.name).read_text(), f.name


def test_hero_rotates_through_every_position(corpus):
    _, manifest = corpus
    assert {e["hero_index"] for e in manifest} == set(range(6))


def test_planted_ordinals_point_at_real_decisions(corpus):
    """The manifest's whole purpose: each ordinal must resolve to a decision."""
    out, manifest = corpus
    checked = 0
    for entry in manifest:
        if not entry["planted"]:
            continue
        hh = load(out / entry["file"])
        decisions = list(iter_decisions(hh, actor=entry["hero_index"]))
        for plant in entry["planted"]:
            assert plant["hero_decision"] < len(decisions)
            d = decisions[plant["hero_decision"]]
            if plant["leak"] == "bb_overfold":
                assert d.action is ActionType.FOLD
            elif plant["leak"] == "utg_open_too_wide":
                assert d.action is ActionType.RAISE
            elif plant["leak"] == "river_overcall":
                assert d.action is ActionType.CALL
            checked += 1
    assert checked, "corpus planted no mistakes; the ground truth is empty"


def test_no_endless_raise_wars(corpus):
    """Regression: uncapped re-raising produced 26-decision hands."""
    out, manifest = corpus
    for entry in manifest:
        hh = load(out / entry["file"])
        assert len(list(iter_decisions(hh))) < 40, entry["file"]
