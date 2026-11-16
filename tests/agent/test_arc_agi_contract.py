"""ARC-AGI-3 contract and typed models, checked against the recorded game (COD-35)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pytest

from vharness.agent.adapters.arc_agi import (
    ARC_OBSERVATION_SCHEMA,
    CLICK_ACTION,
    ArcAction,
    ArcFrame,
    ArcGameState,
    ArcGoal,
    ArcStepOutcome,
    arc_environment_contract,
    arc_step_schema,
    decode_grid,
    encode_grid,
)
from vharness.agent.errors import ContractError
from vharness.agent.models import StateRef
from vharness.agent.session import _validate_action_arguments

FIXTURE = Path(__file__).parent / "fixtures" / "arc_agi" / "dc22-random-seed0.json"
STATE = StateRef("arc-agi-3", "initial", restorable=False)


@pytest.fixture(scope="module")
def recording() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _frame(recording: dict[str, Any], record: dict[str, Any]) -> ArcFrame:
    """Build an ArcFrame from one recorded frame (the fixture's own encoding)."""
    return ArcFrame(
        game_id=recording["game_id"],
        state=ArcGameState(record["state"]),
        levels_completed=record["levels_completed"],
        win_levels=record["win_levels"],
        available_actions=tuple(record["available_actions"]),
        full_reset=record["full_reset"],
        grids=tuple(decode_grid(rows) for rows in record["grids"]),
    )


@dataclass
class _FakeArray:
    rows: list[list[int]]

    def tolist(self) -> list[list[int]]:
        return self.rows


@dataclass
class _FakeState:
    value: str


@dataclass
class _FakeToolkitFrame:  # pylint: disable=too-many-instance-attributes
    """Shaped like arcengine.FrameDataRaw, for runs without the arc extra."""

    game_id: str
    state: _FakeState
    levels_completed: int
    win_levels: int
    available_actions: list[int]
    full_reset: bool
    frame: list[_FakeArray] = field(default_factory=list)


def _fake_toolkit_frame(recording, record) -> _FakeToolkitFrame:
    return _FakeToolkitFrame(
        game_id=recording["game_id"],
        state=_FakeState(record["state"]),
        levels_completed=record["levels_completed"],
        win_levels=record["win_levels"],
        available_actions=list(record["available_actions"]),
        full_reset=record["full_reset"],
        frame=[_FakeArray([list(r) for r in decode_grid(g)]) for g in record["grids"]],
    )


def _recorded_frames(recording):
    return [recording["reset"]] + [s["frame"] for s in recording["steps"] if s["frame"]]


# ---- frames ------------------------------------------------------------------


def test_recorded_frames_convert_and_round_trip(recording):
    for record in _recorded_frames(recording):
        frame = _frame(recording, record)
        published = frame.to_json()
        assert published["grids"] == tuple(tuple(rows) for rows in record["grids"])
        assert published["available_actions"] == tuple(record["available_actions"])
        assert published["state"] == record["state"]
        required = ARC_OBSERVATION_SCHEMA["required"]
        assert isinstance(required, tuple)
        assert set(published) == set(required)


def test_from_toolkit_matches_the_recorded_frames(recording):
    for record in _recorded_frames(recording):
        converted = ArcFrame.from_toolkit(_fake_toolkit_frame(recording, record))
        assert converted == _frame(recording, record)


def test_from_toolkit_accepts_real_toolkit_frames(recording):
    arcengine = pytest.importorskip(
        "arcengine", reason="needs the arc extra (arc-agi, Python 3.12+)"
    )
    numpy = pytest.importorskip("numpy")
    for record in _recorded_frames(recording):
        raw = arcengine.FrameDataRaw(
            game_id=recording["game_id"],
            state=arcengine.GameState(record["state"]),
            levels_completed=record["levels_completed"],
            win_levels=record["win_levels"],
            available_actions=record["available_actions"],
            full_reset=record["full_reset"],
        )
        raw.frame = [numpy.array(decode_grid(g)) for g in record["grids"]]
        assert ArcFrame.from_toolkit(raw) == _frame(recording, record)


def test_terminal_states(recording):
    assert not _frame(recording, recording["reset"]).terminated
    last = _frame(recording, recording["steps"][-1]["frame"])
    assert last.state is ArcGameState.GAME_OVER and last.terminated


@pytest.mark.parametrize(
    "change, message",
    [
        ({"available_actions": (1, 9)}, "unknown actions"),
        ({"available_actions": (1, 1)}, "twice"),
        ({"levels_completed": 7}, "levels completed"),
        ({"grids": (((0,) * 64,) * 63,)}, "64x64"),
        ({"grids": (((16,) * 64,) * 64,)}, "outside 0-15"),
        ({"game_id": ""}, "game_id"),
    ],
)
def test_frame_rejects_malformed_data(recording, change, message):
    base = _frame(recording, recording["reset"])
    fields = {
        "game_id": base.game_id,
        "state": base.state,
        "levels_completed": base.levels_completed,
        "win_levels": base.win_levels,
        "available_actions": base.available_actions,
        "full_reset": base.full_reset,
        "grids": base.grids,
    }
    fields.update(change)
    with pytest.raises(ContractError, match=message):
        ArcFrame(**fields)


def test_grid_encoding_round_trips_and_rejects_non_hex():
    grid = tuple(tuple((x + y) % 16 for x in range(64)) for y in range(64))
    assert decode_grid(encode_grid(grid)) == grid
    with pytest.raises(ContractError, match="not hex"):
        decode_grid(["g" * 64] * 64)


# ---- actions -----------------------------------------------------------------


def test_every_recorded_action_validates_and_maps_to_toolkit_data(recording):
    legal = recording["reset"]["available_actions"]
    for step in recording["steps"]:
        arguments: dict[str, Any] = {"action": step["action"]}
        if step["data"]:
            arguments.update(step["data"])
        action = ArcAction.from_arguments(arguments, legal)
        assert action.toolkit_data() == step["data"]


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"action": 5}, "not legal now; legal: \\[1, 2, 3, 4, 6\\]"),
        ({"action": 6, "x": 3}, "needs y"),
        ({"action": 6, "x": 64, "y": 0}, "x=64 is outside 0-63"),
        ({"action": 6, "x": -1, "y": 0}, "outside 0-63"),
        ({"action": 1, "x": 3, "y": 3}, "takes no x or y"),
        ({"action": True}, "must be an integer"),
        ({"action": 6, "x": "3", "y": 4}, "x must be an integer"),
        ({"action": 1, "z": 2}, "unexpected arguments"),
        ({}, "action must be an integer"),
    ],
)
def test_actions_are_rejected_before_the_toolkit(arguments, message):
    with pytest.raises(ContractError, match=message):
        ArcAction.from_arguments(arguments, (1, 2, 3, 4, 6))


def test_reset_is_not_a_step_action():
    with pytest.raises(ContractError, match="not one of"):
        ArcAction(0)


# ---- contract ----------------------------------------------------------------


def test_contract_declares_reset_and_a_step_from_the_legal_actions(recording):
    frame = _frame(recording, recording["reset"])
    contract = arc_environment_contract(frame, STATE)
    assert contract.environment_id == "arc-agi-3:dc22-fdcac232"
    assert [a.name for a in contract.actions] == ["reset", "step"]
    step = contract.actions[1]
    schema = step.argument_schema
    properties = schema["properties"]
    assert isinstance(properties, Mapping)
    assert properties["action"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 6,
    }
    assert properties["x"] == {"type": "integer", "minimum": 0, "maximum": 63}
    assert "Legal action ids now: 1, 2, 3, 4, 6." in step.description
    _validate_action_arguments({"action": CLICK_ACTION, "x": 5, "y": 9}, schema)
    with pytest.raises(ContractError):
        _validate_action_arguments({"action": CLICK_ACTION, "x": 64, "y": 9}, schema)


def test_schema_has_no_coordinates_without_a_click_action():
    schema = arc_step_schema((1, 2, 3, 4))
    properties = schema["properties"]
    assert isinstance(properties, Mapping) and set(properties) == {"action"}
    with pytest.raises(ContractError):
        _validate_action_arguments({"action": 1, "x": 1}, schema)


def test_contract_text_is_mechanics_only(recording):
    """ADR-0003: no game-specific hints or strategy in what the model reads."""
    contract = arc_environment_contract(_frame(recording, recording["reset"]), STATE)
    text = " ".join(a.description for a in contract.actions).lower()
    for word in ("dc22", "strategy", "hint", "solve", "should", "try"):
        assert word not in text


def test_no_legal_actions_is_a_contract_error():
    with pytest.raises(ContractError, match="no legal actions"):
        arc_step_schema(())


# ---- episode end and goal ----------------------------------------------------


def test_truncation_is_the_limit_reached_before_the_game_ends(recording):
    running = _frame(recording, recording["reset"])
    ended = _frame(recording, recording["steps"][-1]["frame"])
    at_limit = ArcStepOutcome(running, actions_taken=10, max_actions=10)
    assert at_limit.truncated and not at_limit.terminated
    assert not ArcStepOutcome(running, 9, 10).truncated
    over = ArcStepOutcome(ended, actions_taken=10, max_actions=10)
    assert over.terminated and not over.truncated
    with pytest.raises(ContractError):
        ArcStepOutcome(running, actions_taken=11, max_actions=10)


def test_goal_accepts_only_a_win_with_every_level(recording):
    goal = ArcGoal()
    reset = _frame(recording, recording["reset"])
    assert not goal.accepts(reset)
    assert not goal.accepts(_frame(recording, recording["steps"][-1]["frame"]))
    won = ArcFrame(
        reset.game_id,
        ArcGameState.WIN,
        reset.win_levels,
        reset.win_levels,
        reset.available_actions,
        reset.full_reset,
        reset.grids,
    )
    assert goal.accepts(won)
