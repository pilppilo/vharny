"""Recorded ARC-AGI-3 fixtures and their conformance to the pinned toolkit (COD-34).

The structural checks always run. The conformance checks validate every
recorded frame and action against the installed toolkit's own models, so they
need the ``arc`` extra (Python 3.12+) and skip without it.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.record_arc_fixtures import decode_grid, encode_grid  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "arc_agi" / "dc22-random-seed0.json"


@pytest.fixture(scope="module")
def recording() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _frames(recording: dict[str, Any]) -> list[dict[str, Any]]:
    return [recording["reset"]] + [s["frame"] for s in recording["steps"] if s["frame"]]


def test_fixture_identifies_game_toolkit_and_policy(recording):
    assert recording["game_id"] == "dc22-fdcac232"
    assert recording["toolkit"]["arc-agi"].startswith("0.9.")
    assert recording["seed"] == 0
    assert "random.Random(0)" in recording["policy"]


def test_fixture_covers_reset_steps_and_a_terminal_state(recording):
    steps = recording["steps"]
    assert recording["reset"]["state"] == "NOT_FINISHED"
    kept = [s["index"] for s in steps if s["frame"]]
    assert kept[:6] == [0, 1, 2, 3, 4, 5]
    assert recording["final_state"] == "GAME_OVER"
    assert steps[-1]["frame"]["state"] == "GAME_OVER"
    assert recording["scorecard"]["total_actions"] == len(steps)


def test_frames_are_64x64_grids_of_colours(recording):
    for frame in _frames(recording):
        assert frame["grids"], "every frame carries at least one grid"
        for grid in frame["grids"]:
            cells = decode_grid(grid)
            assert len(cells) == 64 and all(len(row) == 64 for row in cells)
            assert encode_grid(cells) == grid


def test_trace_uses_legal_actions_and_click_coordinates(recording):
    legal = set(recording["reset"]["available_actions"])
    assert legal == {1, 2, 3, 4, 6}
    for step in recording["steps"]:
        assert step["action"] in legal
        if step["action"] == 6:
            assert set(step["data"]) == {"x", "y"}
            assert all(0 <= step["data"][k] <= 63 for k in ("x", "y"))
        else:
            assert step["data"] is None


def test_scorecard_carries_no_api_key(recording):
    assert "api_key" not in json.dumps(recording)


def test_encode_grid_rejects_out_of_range_cells():
    with pytest.raises(ValueError, match="outside 0-15"):
        encode_grid([[0, 16]])


def test_fixture_conforms_to_the_installed_toolkit(recording):
    arcengine = pytest.importorskip(
        "arcengine", reason="needs the arc extra (arc-agi, Python 3.12+)"
    )
    assert importlib.metadata.version("arc-agi") == recording["toolkit"]["arc-agi"], (
        "the installed toolkit differs from the recorded one; re-record with "
        "tools/record_arc_fixtures.py"
    )
    for frame in _frames(recording):
        raw = arcengine.FrameDataRaw.model_validate(
            {
                "game_id": recording["game_id"],
                "state": frame["state"],
                "levels_completed": frame["levels_completed"],
                "win_levels": frame["win_levels"],
                "available_actions": frame["available_actions"],
                "full_reset": frame["full_reset"],
                "action_input": frame["action_input"],
            }
        )
        assert raw.state.value == frame["state"]
        for action_id in frame["available_actions"]:
            arcengine.GameAction.from_id(action_id)
    for step in recording["steps"]:
        action = arcengine.GameAction.from_id(step["action"])
        if step["data"] is not None:
            assert action.is_complex()
            arcengine.ComplexAction.model_validate(step["data"])
        else:
            assert action.is_simple()
