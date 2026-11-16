"""ARC-AGI-3 contract and typed models (COD-35, hardened in COD-70).

Checked against the recorded dc22 game and against malformed toolkit objects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from vharness.agent.adapters.arc_agi import (
    ARC_OBSERVATION_SCHEMA,
    ARC_STEP_SCHEMA,
    CLICK_ACTION,
    RESET_DESCRIPTION,
    STEP_DESCRIPTION,
    ArcAction,
    ArcActionEcho,
    ArcFrame,
    ArcGameState,
    ArcGoal,
    ArcStepOutcome,
    arc_environment_contract,
    decode_grid,
    encode_grid,
)
from vharness.agent.codec import canonical_json
from vharness.agent.errors import ContractError
from vharness.agent.models import StateRef
from vharness.agent.session import _validate_action_arguments

FIXTURE = Path(__file__).parent / "fixtures" / "arc_agi" / "dc22-random-seed0.json"
STATE = StateRef("arc-agi-3", "initial", restorable=False)
BLANK = tuple(tuple(0 for _ in range(64)) for _ in range(64))


@pytest.fixture(scope="module")
def recording() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _echo(record: dict[str, Any]) -> ArcActionEcho:
    data = record["action_input"]["data"] or {}
    return ArcActionEcho(record["action_input"]["id"], data.get("x"), data.get("y"))


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
        guid="run-guid-1",
        action_input=_echo(record),
    )


def _recorded_frames(recording: dict[str, Any]) -> list[dict[str, Any]]:
    return [recording["reset"]] + [s["frame"] for s in recording["steps"] if s["frame"]]


# ---- toolkit-shaped fakes ------------------------------------------------------


@dataclass
class _FakeArray:
    rows: Any

    def tolist(self) -> Any:
        return self.rows


@dataclass
class _FakeEnum:
    value: Any


@dataclass
class _FakeActionInput:
    id: _FakeEnum
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeToolkitFrame:  # pylint: disable=too-many-instance-attributes
    """Shaped like arcengine.FrameDataRaw, for runs without the arc extra."""

    game_id: Any
    state: _FakeEnum
    levels_completed: Any
    win_levels: Any
    available_actions: Any
    full_reset: Any
    guid: Any
    action_input: _FakeActionInput | None
    frame: list[_FakeArray]


def _fake(recording: dict[str, Any], record: dict[str, Any]) -> _FakeToolkitFrame:
    echo = record["action_input"]
    return _FakeToolkitFrame(
        game_id=recording["game_id"],
        state=_FakeEnum(record["state"]),
        levels_completed=record["levels_completed"],
        win_levels=record["win_levels"],
        available_actions=list(record["available_actions"]),
        full_reset=record["full_reset"],
        guid="run-guid-1",
        action_input=_FakeActionInput(_FakeEnum(echo["id"]), dict(echo["data"] or {})),
        frame=[_FakeArray([list(r) for r in decode_grid(g)]) for g in record["grids"]],
    )


# ---- frames from the recording ---------------------------------------------


def test_recorded_frames_convert_and_keep_guid_and_echo(recording):
    for record in _recorded_frames(recording):
        frame = ArcFrame.from_toolkit(_fake(recording, record))
        assert frame == _frame(recording, record)
        evidence = frame.evidence_json()
        assert evidence["grids"] == tuple(tuple(rows) for rows in record["grids"])
        assert evidence["guid"] == "run-guid-1"
        data = record["action_input"]["data"] or {}
        assert evidence["action_input"] == {
            "action": record["action_input"]["id"],
            "x": data.get("x"),
            "y": data.get("y"),
        }


def test_observation_matches_its_schema(recording):
    required = ARC_OBSERVATION_SCHEMA["required"]
    assert isinstance(required, tuple)
    for record in _recorded_frames(recording):
        observation = _frame(recording, record).to_json()
        assert set(observation) == set(required)
        assert observation["grid"] == tuple(record["grids"][-1])
        assert observation["layer_count"] == len(record["grids"])


def test_from_toolkit_accepts_real_toolkit_frames(recording):
    arcengine = pytest.importorskip(
        "arcengine", reason="needs the arc extra (arc-agi, Python 3.12+)"
    )
    numpy = pytest.importorskip("numpy")
    for record in _recorded_frames(recording):
        echo = record["action_input"]
        raw = arcengine.FrameDataRaw(
            game_id=recording["game_id"],
            state=arcengine.GameState(record["state"]),
            levels_completed=record["levels_completed"],
            win_levels=record["win_levels"],
            available_actions=record["available_actions"],
            full_reset=record["full_reset"],
            guid="run-guid-1",
            action_input=arcengine.ActionInput(
                id=arcengine.GameAction.from_id(echo["id"]), data=echo["data"] or {}
            ),
        )
        raw.frame = [numpy.array(decode_grid(g)) for g in record["grids"]]
        assert ArcFrame.from_toolkit(raw) == _frame(recording, record)


def test_terminated_frame_offers_no_legal_actions(recording):
    reset = _frame(recording, recording["reset"])
    last = _frame(recording, recording["steps"][-1]["frame"])
    assert not reset.terminated and reset.legal_actions == (1, 2, 3, 4, 6)
    assert last.state is ArcGameState.GAME_OVER and last.terminated
    assert last.available_actions  # the toolkit still lists them...
    assert last.legal_actions == ()  # ...but none is legal
    assert last.to_json()["legal_actions"] == ()


# ---- animation layers --------------------------------------------------------


def test_observation_shows_only_the_final_layer_of_an_animation(recording):
    base = _frame(recording, recording["reset"])
    layers = tuple(
        tuple(tuple((i + x) % 16 for x in range(64)) for _ in range(64))
        for i in range(40)
    )
    animated = replace(base, grids=layers)
    observation = animated.to_json()
    assert observation["layer_count"] == 40
    assert observation["grid"] == encode_grid(layers[-1])
    assert len(canonical_json(observation)) < 6_000
    kept = animated.evidence_json()["grids"]
    assert isinstance(kept, tuple) and len(kept) == 40  # all kept as evidence


# ---- strict boundary ---------------------------------------------------------


@pytest.mark.parametrize(
    "change, message",
    [
        ({"game_id": None}, "game_id must be a non-empty string"),
        ({"game_id": ""}, "game_id must be a non-empty string"),
        ({"state": _FakeEnum("PAUSED")}, "ValueError"),
        ({"state": _FakeEnum(3)}, "state must be a non-empty string"),
        ({"levels_completed": 1.0}, "levels_completed must be an integer"),
        ({"levels_completed": True}, "levels_completed must be an integer"),
        ({"levels_completed": -1}, "levels_completed -1 is below 0"),
        ({"win_levels": -2}, "win_levels -2 is below 0"),
        ({"levels_completed": 7}, "levels_completed 7 is above 6"),
        ({"available_actions": [1, 9]}, "available action 9 is above 7"),
        ({"available_actions": [1, "2"]}, "available action must be an integer"),
        ({"available_actions": [1, 1]}, "twice"),
        ({"full_reset": 0}, "full_reset must be a boolean"),
        ({"guid": ""}, "guid must be a non-empty string"),
        ({"frame": []}, "no grid layers"),
        ({"frame": [_FakeArray([[3.9] * 64] * 64)]}, "grid cell must be an integer"),
        ({"frame": [_FakeArray([[16] * 64] * 64)]}, "grid cell 16 is above 15"),
        ({"frame": [_FakeArray([[0] * 64] * 63)]}, "64x64"),
        ({"frame": [_FakeArray("not a grid")]}, "not a list of rows"),
        ({"action_input": _FakeActionInput(_FakeEnum(6), {"x": 3})}, "both x and y"),
        ({"action_input": _FakeActionInput(_FakeEnum(9))}, "action_input id 9"),
    ],
)
def test_malformed_toolkit_frames_raise_contract_error(recording, change, message):
    fake = _fake(recording, recording["reset"])
    for name, value in change.items():
        setattr(fake, name, value)
    with pytest.raises(ContractError, match=f"malformed.*{message}"):
        ArcFrame.from_toolkit(fake)


def test_missing_toolkit_fields_raise_contract_error():
    @dataclass
    class _Partial:
        game_id: str
        state: _FakeEnum

    partial: Any = _Partial("dc22", _FakeEnum("NOT_FINISHED"))
    with pytest.raises(ContractError, match="malformed: AttributeError"):
        ArcFrame.from_toolkit(partial)


@pytest.mark.parametrize(
    "change",
    [
        {"grids": ()},
        {"grids": (tuple(tuple(float(c) for c in row) for row in BLANK),)},
        {"available_actions": [1, 2]},
        {"state": "NOT_FINISHED"},
        {"levels_completed": -1},
        {"guid": ""},
    ],
)
def test_frame_constructor_refuses_coerced_values(recording, change):
    base = _frame(recording, recording["reset"])
    with pytest.raises(ContractError):
        replace(base, **change)


def test_grid_encoding_round_trips_and_rejects_non_hex():
    grid = tuple(tuple((x + y) % 16 for x in range(64)) for y in range(64))
    assert decode_grid(encode_grid(grid)) == grid
    with pytest.raises(ContractError, match="not hex"):
        decode_grid(["g" * 64] * 64)


# ---- actions -----------------------------------------------------------------


def test_every_recorded_action_validates_and_maps_to_toolkit_data(recording):
    frame = _frame(recording, recording["reset"])
    for step in recording["steps"]:
        arguments: dict[str, Any] = {"action": step["action"]}
        if step["data"]:
            arguments.update(step["data"])
        action = ArcAction.from_arguments(arguments, frame)
        assert action.toolkit_data() == step["data"]


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"action": 5}, "not legal now; legal: \\[1, 2, 3, 4, 6\\]"),
        ({"action": 6, "x": 3}, "needs y"),
        ({"action": 6, "x": 64, "y": 0}, "x 64 is above 63"),
        ({"action": 6, "x": -1, "y": 0}, "x -1 is below 0"),
        ({"action": 1, "x": 3, "y": 3}, "takes no x or y"),
        ({"action": True}, "action must be an integer"),
        ({"action": 6.0, "x": 3, "y": 4}, "action must be an integer"),
        ({"action": 6, "x": "3", "y": 4}, "x must be an integer"),
        ({"action": 1, "z": 2}, "unexpected arguments"),
        ({}, "action must be an integer"),
    ],
)
def test_actions_are_rejected_before_the_toolkit(recording, arguments, message):
    frame = _frame(recording, recording["reset"])
    with pytest.raises(ContractError, match=message):
        ArcAction.from_arguments(arguments, frame)


def test_legality_follows_the_current_frame(recording):
    base = _frame(recording, recording["reset"])
    later = replace(base, available_actions=(5, 7))
    assert ArcAction.from_arguments({"action": 5}, later).action_id == 5
    with pytest.raises(ContractError, match="legal: \\[5, 7\\]"):
        ArcAction.from_arguments({"action": 1}, later)


def test_step_is_refused_once_the_game_is_over(recording):
    last = _frame(recording, recording["steps"][-1]["frame"])
    with pytest.raises(ContractError, match="game is over \\(GAME_OVER\\); only reset"):
        ArcAction.from_arguments({"action": 1}, last)


def test_reset_is_not_a_step_action():
    with pytest.raises(ContractError, match="action 0 is below 1"):
        ArcAction(0)


# ---- contract ----------------------------------------------------------------


def test_contract_is_fixed_and_independent_of_any_frame(recording):
    first = arc_environment_contract("dc22-fdcac232", STATE)
    again = arc_environment_contract("dc22-fdcac232", STATE)
    assert canonical_json(first) == canonical_json(again)
    assert first.environment_id == "arc-agi-3:dc22-fdcac232"
    assert [a.name for a in first.actions] == ["reset", "step"]
    assert first.actions[1].argument_schema == ARC_STEP_SCHEMA
    # Frames with different legal sets are all served by the same contract.
    later = replace(_frame(recording, recording["reset"]), available_actions=(5, 7))
    assert ArcAction.from_arguments({"action": 7}, later).action_id == 7
    _validate_action_arguments({"action": 7}, first.actions[1].argument_schema)


def test_contract_text_is_exactly_the_mechanics():
    """ADR-0003: the model reads these words and no others; no game hints."""
    contract = arc_environment_contract("dc22-fdcac232", STATE)
    assert [a.description for a in contract.actions] == [
        RESET_DESCRIPTION,
        STEP_DESCRIPTION,
    ]
    assert RESET_DESCRIPTION == "Restart the game from its first frame"
    assert STEP_DESCRIPTION == (
        "Take one game action, id 1-7. The current observation lists the legal "
        "ids; any other id is refused. Action 6 acts on one cell and needs x "
        "(column) and y (row), each 0-63; other actions take no x or y. When the "
        "game is over, only reset is possible."
    )


def test_kernel_validator_enforces_the_fixed_schema():
    schema = ARC_STEP_SCHEMA
    properties = schema["properties"]
    assert isinstance(properties, Mapping)
    assert properties["action"] == {"type": "integer", "minimum": 1, "maximum": 7}
    _validate_action_arguments({"action": CLICK_ACTION, "x": 5, "y": 9}, schema)
    _validate_action_arguments({"action": 3}, schema)
    for bad in ({"action": 8}, {"action": 6, "x": 64, "y": 0}, {"action": 1, "z": 1}):
        with pytest.raises(ContractError):
            _validate_action_arguments(bad, schema)


def test_contract_needs_a_game_id():
    with pytest.raises(ContractError, match="game_id"):
        arc_environment_contract("", STATE)


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
    won = replace(reset, state=ArcGameState.WIN, levels_completed=reset.win_levels)
    assert goal.accepts(won)
    assert not goal.accepts(replace(won, levels_completed=reset.win_levels - 1))
