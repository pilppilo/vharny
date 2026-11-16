"""ARC-AGI-3 runs through the application and CLI (COD-38), with a fake toolkit.

These tests never reach the network: the toolkit opener is replaced with a
fake game that replays recorded frames and answers a scripted scorecard.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from io import StringIO
from pathlib import Path
from typing import Any, Mapping

import pytest

from vharness.agent import (
    ActionChoice,
    EvaluationChoice,
    ScriptedModel,
    SessionStatus,
)
from vharness.agent.adapters import arc_agi_setup
from vharness.agent.adapters.arc_agi import (
    RESET_DESCRIPTION,
    STEP_DESCRIPTION,
    decode_grid,
)
from vharness.agent.adapters.arc_agi_setup import ARC_EVALUATION_CONTRACT
from vharness.agent.adapters.arc_agi_toolkit import ArcToolkitInfo
from vharness.agent.application import Application, RunDirectory, RunSettings
from vharness.agent.cli import main
from vharness.agent.context import POLICY_VERSION, SYSTEM_PROMPT, TOKEN_COUNTER
from vharness.agent.control import ACCEPTED, command_frame
from vharness.agent.errors import ContractError
from vharness.agent.models import EvaluationMode, Pause, Resume, Stop
from vharness.agent.run_settings import ArcRunSettings

FIXTURE = Path(__file__).parent / "fixtures" / "arc_agi" / "dc22-random-seed0.json"
RECORDING: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
GAME_ID = RECORDING["game_id"]


# ---- a fake toolkit game with a scorecard ---------------------------------------


@dataclass
class _Array:
    rows: list[list[int]]

    def tolist(self) -> list[list[int]]:
        return self.rows


@dataclass
class _Enum:
    value: Any


@dataclass
class _Echo:
    id: _Enum
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Frame:  # pylint: disable=too-many-instance-attributes
    game_id: str
    state: _Enum
    levels_completed: int
    win_levels: int
    available_actions: list[int]
    full_reset: bool
    guid: str | None
    action_input: _Echo | None
    frame: list[_Array]


def _frame(record: dict[str, Any]) -> _Frame:
    echo = record["action_input"]
    return _Frame(
        GAME_ID,
        _Enum(record["state"]),
        record["levels_completed"],
        record["win_levels"],
        list(record["available_actions"]),
        record["full_reset"],
        "guid-1",
        _Echo(_Enum(echo["id"]), dict(echo["data"] or {})),
        [_Array([list(r) for r in decode_grid(g)]) for g in record["grids"]],
    )


RESET = _frame(RECORDING["reset"])
STEP = _frame(RECORDING["steps"][0]["frame"])


def _scorecard(*, completed: bool) -> dict[str, Any]:
    card = copy.deepcopy(RECORDING["scorecard"])
    if completed:
        card["environments"][0].update(completed=True, levels_completed=6, score=80.0)
    return card


class FakeGame:
    """Replays the reset frame and then one recorded step frame, forever."""

    def __init__(self, *, completed: bool = True) -> None:
        self.calls: list[str] = []
        self.completed = completed
        self.closed = False

    @property
    def info(self) -> ArcToolkitInfo:
        return ArcToolkitInfo(
            "0.9.10", "0.9.3", GAME_ID, 0, "normal", "https://three.arcprize.org"
        )

    def reset(self) -> _Frame:
        self.calls.append("reset")
        return RESET

    def step(self, action_id: int, data: Mapping[str, int] | None) -> _Frame:
        self.calls.append(f"step {action_id} {dict(data or {})}")
        return STEP

    def scorecard(self) -> dict[str, Any]:
        self.calls.append("scorecard")
        return _scorecard(completed=self.completed)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def games(monkeypatch: pytest.MonkeyPatch) -> list[FakeGame]:
    """Every game the application opens, in order; no toolkit, no network."""
    opened: list[FakeGame] = []

    def open_fake(game_id: str, **options: Any) -> FakeGame:
        assert game_id == GAME_ID
        assert Path(options["environments_dir"]).is_dir()
        game = FakeGame()
        opened.append(game)
        return game

    monkeypatch.setattr(arc_agi_setup, "open_toolkit_game", open_fake)
    return opened


def _settings(run_dir: Path, **overrides: Any) -> RunSettings:
    settings = RunSettings(
        objective="complete the game",
        workspace=str(run_dir.resolve() / "arc-games"),
        base_url="http://localhost:1/v1",
        model="test-model",
        api_key_env="TEST_KEY",
        max_actions=8,
        max_model_calls=8,
        max_evaluations=2,
        max_steps=20,
        token_budget=16384,
        max_output_tokens=256,
        repair_budget=0,
        arc=ArcRunSettings(GAME_ID),
    )
    return replace(settings, **overrides) if overrides else settings


def _create(tmp_path: Path, model: ScriptedModel, session_id: str) -> Application:
    return Application.create(
        _settings(tmp_path / "run"),
        tmp_path / "run",
        session_id=session_id,
        model=model,
        poll_seconds=0,
    )


# ---- settings -------------------------------------------------------------------


def test_arc_settings_round_trip_and_leave_other_manifests_unchanged(tmp_path):
    settings = _settings(tmp_path / "run")
    assert RunSettings.from_payload(settings.to_payload()) == settings
    assert settings.evaluation_mode is EvaluationMode.EXTERNAL_CHECK
    assert "scorecard" in settings.success_evidence
    plain = replace(settings, arc=None)
    assert "arc" not in plain.to_payload()
    assert RunSettings.from_payload(plain.to_payload()).arc is None


@pytest.mark.parametrize(
    "arc, message",
    [
        ({"game_id": ""}, "game_id"),
        ({"game_id": "g", "seed": -1}, "seed"),
        ({"game_id": "g", "seed": True}, "seed"),
        ({"game_id": "g", "operation_mode": "competition"}, "operation_mode"),
        ({"game_id": "g", "call_timeout_seconds": 0}, "call_timeout_seconds"),
    ],
)
def test_arc_settings_are_strict(arc, message):
    with pytest.raises(ContractError, match=message):
        ArcRunSettings(**arc)


def test_arc_runs_take_no_check_command_or_target(tmp_path):
    with pytest.raises(ContractError, match="no check_command or target"):
        _settings(tmp_path, check_command=("true",))
    with pytest.raises(ContractError, match="no check_command or target"):
        _settings(tmp_path, target="main.py")


# ---- a whole run, and kernel conformance ----------------------------------------


def test_a_run_plays_the_game_and_the_scorecard_decides(tmp_path, games):
    model = ScriptedModel(
        (
            ActionChoice("step", "try an action", {"action": 4}),
            EvaluationChoice("ask the scorecard"),
        )
    )
    app = _create(tmp_path, model, "arc-session-1")
    try:
        view = app.run()
    finally:
        app.close()
    assert view.status is SessionStatus.COMPLETED
    game = games[0]
    assert game.calls == ["reset", "step 4 {}", "scorecard"]
    assert game.closed
    manifest = json.loads((tmp_path / "run" / "run.json").read_text())
    assert manifest["settings"]["arc"]["game_id"] == GAME_ID
    assert manifest["evaluation_mode"] == "external-check"


def test_the_kernel_sees_only_the_contract_and_observation(tmp_path, games):
    """Same kernel, prompt and context policy as every environment (ADR-0003)."""
    model = ScriptedModel((ActionChoice("step", "try", {"action": 4}),))
    app = _create(tmp_path, model, "arc-session-2")
    try:
        app.tick()
    finally:
        app.close()
    request = model.requests[0]
    assert [(a.name, a.description) for a in request.actions] == [
        ("reset", RESET_DESCRIPTION),
        ("step", STEP_DESCRIPTION),
    ]
    assert request.policy_version == POLICY_VERSION
    assert request.token_counter == TOKEN_COUNTER
    assert request.task.evaluation_contract_id == ARC_EVALUATION_CONTRACT
    assert request.task.environment_id == f"arc-agi-3:{GAME_ID}"
    for word in ("ARC", "arc-agi", "grid", "level"):
        assert word not in SYSTEM_PROMPT
    observation = json.dumps(dict(request.observation), default=str)
    assert "legal_actions" in observation and GAME_ID in observation
    assert games[0].calls == ["reset"]


def test_pause_resume_and_stop_mid_game(tmp_path, games):
    model = ScriptedModel(
        tuple(ActionChoice("step", "move", {"action": 1}) for _ in range(4))
    )
    app = _create(tmp_path, model, "arc-session-3")
    try:
        assert app.tick() == "decided"
        assert app.tick() == "dispatched"
        frame = command_frame(Pause("pause-1", "arc-session-3"))
        assert app.submit_frame(frame).status == ACCEPTED
        assert app.tick() == "idle"
        assert app.view().status is SessionStatus.PAUSED
        steps = [call for call in games[0].calls if call.startswith("step")]
        assert app.tick() == "idle"
        assert [c for c in games[0].calls if c.startswith("step")] == steps

        frame = command_frame(Resume("resume-1", "arc-session-3"))
        assert app.submit_frame(frame).status == ACCEPTED
        assert app.tick() == "decided"
        frame = command_frame(Stop("stop-1", "arc-session-3"))
        assert app.submit_frame(frame).status == ACCEPTED
        assert app.tick() == "terminal"
        assert app.view().status is SessionStatus.STOPPED
    finally:
        app.close()


def test_reopening_a_run_does_not_reset_the_game(tmp_path, games):
    model = ScriptedModel((ActionChoice("step", "move", {"action": 4}),))
    app = _create(tmp_path, model, "arc-session-4")
    try:
        app.tick()
        app.tick()
    finally:
        app.close()
    assert games[0].calls == ["reset", "step 4 {}"]

    reopened = Application.open(
        tmp_path / "run", model=ScriptedModel(()), poll_seconds=0
    )
    try:
        assert len(games) == 2
        assert games[1].calls == []  # resume never resets on its own
        assert reopened.view().status is not SessionStatus.FAILED
    finally:
        reopened.close()
    _, manifest = RunDirectory.open(tmp_path / "run")
    assert manifest.settings.arc == ArcRunSettings(GAME_ID)


# ---- command line -------------------------------------------------------------------


def _cli(*arguments: str) -> tuple[int, str, str]:
    output, error = StringIO(), StringIO()
    code = main(list(arguments), output=output, error=error)
    return code, output.getvalue(), error.getvalue()


def test_cli_starts_an_arc_run_by_game_id(tmp_path, games, monkeypatch):
    model = ScriptedModel(
        (ActionChoice("step", "move", {"action": 4}), EvaluationChoice("score"))
    )
    monkeypatch.setattr("vharness.agent.cli.build_model", lambda _s: (model, None))
    code, output, error = _cli(
        "run",
        "--objective",
        "complete the game",
        "--arc-game",
        GAME_ID,
        "--arc-seed",
        "0",
        "--run-dir",
        str(tmp_path / "run"),
        "--max-actions",
        "8",
    )
    assert code == 0, error
    assert "mode=arc-agi-3" in output
    assert f"GAME {GAME_ID} seed=0 mode=normal" in output
    assert games[0].calls == ["reset", "step 4 {}", "scorecard"]
    assert (tmp_path / "run" / "arc-games").is_dir()


@pytest.mark.parametrize(
    "extra, message",
    [
        (("--arc-seed", "1"), "need --arc-game"),
        (("--arc-game", GAME_ID, "--workspace", "."), "cannot be combined"),
        (("--arc-game", GAME_ID), "require --run-dir"),
        (("--arc-game", GAME_ID, "--check-command", "true"), "check_command"),
    ],
)
def test_cli_rejects_mixed_arc_options(tmp_path, extra, message):
    arguments = ["run", "--objective", "o", *extra]
    if "--run-dir" not in extra and message != "require --run-dir":
        arguments += ["--run-dir", str(tmp_path / "run")]
    code, _output, error = _cli(*arguments)
    assert code == 2
    assert message in error
