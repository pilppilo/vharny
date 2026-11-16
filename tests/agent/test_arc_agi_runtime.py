"""ARC-AGI-3 runtime adapter (COD-36), against recorded frames and a fake toolkit."""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from vharness.agent.adapters.arc_agi import ArcFrame, decode_grid
from vharness.agent.adapters.arc_agi_runtime import ArcRuntime
from vharness.agent.adapters.arc_agi_toolkit import (
    ArcCallBusy,
    ArcCallError,
    ArcToolkitInfo,
    TimedCaller,
    require_arc_toolkit,
)
from vharness.agent.adapters.runtime_store import proposal_fingerprint
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import parse_json
from vharness.agent.errors import ContractError
from vharness.agent.models import (
    ActionProposal,
    ArtifactRef,
    ExecutionReceipt,
    ExecutionStatus,
    ResourceReservation,
    StateRef,
)
from vharness.agent.ports import ReconciliationKind

FIXTURE = Path(__file__).parent / "fixtures" / "arc_agi" / "dc22-random-seed0.json"
RECORDING: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
GAME_ID = RECORDING["game_id"]
INFO = ArcToolkitInfo(
    "0.9.10", "0.9.3", GAME_ID, 0, "normal", "https://three.arcprize.org"
)


# ---- a fake toolkit game -----------------------------------------------------


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
class _ToolkitFrame:  # pylint: disable=too-many-instance-attributes
    game_id: str
    state: _Enum
    levels_completed: int
    win_levels: int
    available_actions: list[int]
    full_reset: bool
    guid: str | None
    action_input: _Echo | None
    frame: list[_Array]


def _toolkit(record: dict[str, Any], **changes: Any) -> _ToolkitFrame:
    echo = record["action_input"]
    frame = _ToolkitFrame(
        game_id=GAME_ID,
        state=_Enum(record["state"]),
        levels_completed=record["levels_completed"],
        win_levels=record["win_levels"],
        available_actions=list(record["available_actions"]),
        full_reset=record["full_reset"],
        guid="guid-1",
        action_input=_Echo(_Enum(echo["id"]), dict(echo["data"] or {})),
        frame=[_Array([list(r) for r in decode_grid(g)]) for g in record["grids"]],
    )
    for name, value in changes.items():
        setattr(frame, name, value)
    return frame


RESET = _toolkit(RECORDING["reset"])
STEPS = [_toolkit(s["frame"]) for s in RECORDING["steps"] if s["frame"]]
GAME_OVER = STEPS[-1]


class _Block:
    """A scripted answer that blocks until released, then answers ``then``."""

    def __init__(self, then: Any) -> None:
        self.release = threading.Event()
        self.then = then


class FakeGame:
    """Answers each call with the next scripted response, recording the calls."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, Any, Any]] = []
        self.closed = False

    @property
    def info(self) -> ArcToolkitInfo:
        return INFO

    def reset(self) -> Any:
        self.calls.append(("reset", None, None))
        return self._answer()

    def step(self, action_id: int, data: Mapping[str, int] | None) -> Any:
        self.calls.append(("step", action_id, data))
        return self._answer()

    def close(self) -> None:
        self.closed = True

    def _answer(self) -> Any:
        response = self.responses.pop(0)
        if isinstance(response, _Block):
            response.release.wait(5)
            response = response.then
        if isinstance(response, BaseException):
            raise response
        return response


# ---- helpers -----------------------------------------------------------------


def _proposal(proposal_id: str, name: str, **arguments: Any) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        session_id="session-1",
        objective_version=1,
        attempt_id="attempt-1",
        action_name=name,
        arguments=arguments,
        expected_state=StateRef(f"arc-agi-3:{GAME_ID}", "initial", restorable=False),
        reservation=ResourceReservation("reservation-" + proposal_id, proposal_id, 1),
        rationale="ARC runtime test",
    )


def _content(artifacts: ArtifactStore, ref: ArtifactRef | None) -> Mapping[str, Any]:
    assert ref is not None
    parsed = parse_json(artifacts.read(ref).decode("utf-8"))
    assert isinstance(parsed, Mapping)
    return parsed


def _evidence(tmp_path: Path, digest: str) -> Mapping[str, Any]:
    """Read an evidence artifact by the digest named in a state cursor."""
    path = tmp_path / "artifacts" / digest[:2] / digest[2:]
    return json.loads(path.read_bytes())


@pytest.fixture
def artifacts(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


def _runtime(
    tmp_path: Path, artifacts: ArtifactStore, game: FakeGame, **options: Any
) -> ArcRuntime:
    options.setdefault("max_actions", 50)
    return ArcRuntime(game, artifacts, tmp_path / "runtime.sqlite3", **options)


def _started(tmp_path, artifacts, *responses, **options) -> tuple[ArcRuntime, FakeGame]:
    game = FakeGame(RESET, *responses)
    runtime = _runtime(tmp_path, artifacts, game, **options)
    runtime.initial_observation("session-1")
    return runtime, game


# ---- start, contract, identity ----------------------------------------------


def test_initial_observation_resets_and_publishes_the_first_frame(tmp_path, artifacts):
    game = FakeGame(RESET)
    runtime = _runtime(tmp_path, artifacts, game)
    observation = runtime.initial_observation("session-1")
    assert game.calls == [("reset", None, None)]
    content = _content(artifacts, observation.raw_record)
    assert content["outcome"] == "succeeded" and content["error"] is None
    assert content["actions_left"] == 50
    assert content["observation"]["legal_actions"] == [1, 2, 3, 4, 6]
    assert content["observation"]["layer_count"] == 1
    state = json.loads(observation.state.value)
    assert state["actions_taken"] == 0
    evidence = _evidence(tmp_path, state["evidence"])
    assert evidence["guid"] == "guid-1" and evidence["action_input"]["action"] == 0
    assert observation.state.digest == state["evidence"]


def test_contract_is_fixed_and_identity_pins_the_setup(tmp_path, artifacts):
    runtime, _ = _started(tmp_path, artifacts)
    contract = runtime.describe()
    assert contract.environment_id == f"arc-agi-3:{GAME_ID}"
    assert contract.initial_state.value == "initial"
    first = runtime.identity()
    assert first.component_id == "arc-agi-3-runtime" and first.version == "0.9.10"
    runtime.close()

    class OnlineGame(FakeGame):
        @property
        def info(self) -> ArcToolkitInfo:
            return replace(INFO, operation_mode="online")

    online = ArcRuntime(
        OnlineGame(), artifacts, tmp_path / "other.sqlite3", max_actions=50
    )
    assert online.identity().configuration_digest != first.configuration_digest
    assert online.describe() == contract
    limited = ArcRuntime(FakeGame(), artifacts, tmp_path / "x.sqlite3", max_actions=9)
    assert limited.identity().configuration_digest != first.configuration_digest


def test_first_reset_failure_raises_with_its_cause(tmp_path, artifacts):
    boom = OSError("network down")
    runtime = _runtime(tmp_path, artifacts, FakeGame(boom))
    with pytest.raises(
        ArcCallError, match="reset raised OSError: network down"
    ) as caught:
        runtime.initial_observation("session-1")
    assert caught.value.__cause__ is boom
    with pytest.raises(ArcCallError, match="no frame"):
        _runtime(tmp_path, artifacts, FakeGame(None)).initial_observation("s")


# ---- normal steps -------------------------------------------------------------


def test_step_succeeds_and_publishes_observation_and_evidence(tmp_path, artifacts):
    runtime, game = _started(tmp_path, artifacts, STEPS[0])
    receipt = runtime.submit(_proposal("p1", "step", action=4))
    assert receipt.status is ExecutionStatus.SUCCEEDED and receipt.error is None
    assert game.calls[-1] == ("step", 4, None)
    assert receipt.usage is not None and receipt.usage.actions == 1
    content = _content(artifacts, receipt.raw_artifact)
    assert content["outcome"] == "succeeded" and content["actions_left"] == 49
    assert content["observation"]["grid"] == list(
        RECORDING["steps"][0]["frame"]["grids"][-1]
    )
    assert receipt.state is not None and receipt.state.digest is not None
    state = json.loads(receipt.state.value)
    assert state["actions_taken"] == 1 and state["state"] == "NOT_FINISHED"
    assert len(receipt.state.value) < 300  # a cursor, not a grid
    assert runtime.frame == ArcFrame.from_toolkit(STEPS[0])


def test_evidence_artifact_keeps_every_layer_guid_and_echo(tmp_path, artifacts):
    animated = _toolkit(
        RECORDING["steps"][0]["frame"], frame=RESET.frame * 3, guid="guid-2"
    )
    runtime, _ = _started(tmp_path, artifacts, animated)
    receipt = runtime.submit(_proposal("p1", "step", action=4))
    content = _content(artifacts, receipt.raw_artifact)
    assert content["observation"]["layer_count"] == 3
    assert "grids" not in content["observation"]
    assert receipt.state is not None
    evidence = _evidence(tmp_path, json.loads(receipt.state.value)["evidence"])
    assert len(evidence["grids"]) == 3 and evidence["guid"] == "guid-2"
    assert evidence["action_input"]["action"] == 4


def test_click_sends_coordinates(tmp_path, artifacts):
    click = next(s for s in RECORDING["steps"] if s["action"] == 6)
    runtime, game = _started(tmp_path, artifacts, STEPS[0])
    receipt = runtime.submit(_proposal("p1", "step", action=6, **click["data"]))
    assert receipt.status is ExecutionStatus.SUCCEEDED
    assert game.calls[-1] == ("step", 6, click["data"])


# ---- refusals: nothing is sent ------------------------------------------------


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"action": 5}, "not legal now"),
        ({"action": 6, "x": 1}, "needs y"),
        ({"action": 1, "x": 1, "y": 1}, "takes no x or y"),
        ({"action": "1"}, "must be an integer"),
        ({"action": 1, "z": 0}, "unexpected arguments"),
    ],
)
def test_illegal_actions_are_rejected_without_a_call(
    tmp_path, artifacts, arguments, message
):
    runtime, game = _started(tmp_path, artifacts)
    receipt = runtime.submit(_proposal("p1", "step", **arguments))
    assert receipt.status is ExecutionStatus.REJECTED
    assert receipt.error is not None and message in receipt.error
    assert game.calls == [("reset", None, None)]
    assert runtime.actions_taken == 0
    content = _content(artifacts, receipt.raw_artifact)
    assert content["outcome"] == "rejected" and message in content["error"]
    assert content["observation"]["legal_actions"] == [1, 2, 3, 4, 6]


def test_unknown_action_name_is_rejected(tmp_path, artifacts):
    runtime, game = _started(tmp_path, artifacts)
    receipt = runtime.submit(_proposal("p1", "jump"))
    assert receipt.status is ExecutionStatus.REJECTED
    assert "unknown ARC action 'jump'" in str(receipt.error)
    assert len(game.calls) == 1


def test_game_over_refuses_step_but_allows_reset(tmp_path, artifacts):
    runtime, game = _started(tmp_path, artifacts, GAME_OVER, RESET)
    assert runtime.submit(_proposal("p1", "step", action=1)).status.value == "succeeded"
    refused = runtime.submit(_proposal("p2", "step", action=1))
    assert refused.status is ExecutionStatus.REJECTED
    assert "game is over (GAME_OVER); only reset" in str(refused.error)
    assert (
        _content(artifacts, refused.raw_artifact)["observation"]["legal_actions"] == []
    )
    reset = runtime.submit(_proposal("p3", "reset"))
    assert reset.status is ExecutionStatus.SUCCEEDED
    assert [c[0] for c in game.calls] == ["reset", "step", "reset"]


def test_action_limit_counts_steps_but_not_resets(tmp_path, artifacts):
    runtime, game = _started(
        tmp_path, artifacts, STEPS[0], RESET, STEPS[1], max_actions=2
    )
    assert runtime.submit(_proposal("p1", "step", action=4)).status.value == "succeeded"
    assert runtime.submit(_proposal("p2", "reset")).status.value == "succeeded"
    last = runtime.submit(_proposal("p3", "step", action=1))
    assert _content(artifacts, last.raw_artifact)["actions_left"] == 0
    over = runtime.submit(_proposal("p4", "step", action=1))
    assert over.status is ExecutionStatus.REJECTED
    assert "action limit of 2 is reached" in str(over.error)
    assert runtime.actions_taken == 2
    assert [c[0] for c in game.calls] == ["reset", "step", "reset", "step"]


# ---- unknown outcomes ---------------------------------------------------------


@pytest.mark.parametrize(
    "response, message",
    [
        (None, "answered step with no frame"),
        (
            ConnectionError("reset by peer"),
            "step raised ConnectionError: reset by peer",
        ),
        (_toolkit(RECORDING["reset"], levels_completed=-1), "malformed"),
        (_toolkit(RECORDING["reset"], game_id="other-game"), "not 'dc22-fdcac232'"),
    ],
)
def test_unknown_outcomes_are_indeterminate_and_force_a_reset(
    tmp_path, artifacts, response, message
):
    runtime, game = _started(tmp_path, artifacts, response, RESET)
    receipt = runtime.submit(_proposal("p1", "step", action=1))
    assert receipt.status is ExecutionStatus.INDETERMINATE
    assert message in str(receipt.error)
    assert "unknown" in str(receipt.error)
    assert receipt.state is not None and receipt.state.value == "unknown"
    content = _content(artifacts, receipt.raw_artifact)
    assert content["outcome"] == "unknown" and content["observation"] is None
    assert runtime.frame is None
    assert runtime.actions_taken == 1  # it may have reached the game

    # The same proposal is never resent; its stored receipt comes back.
    again = runtime.submit(_proposal("p1", "step", action=1))
    assert again == receipt and len(game.calls) == 2

    refused = runtime.submit(_proposal("p2", "step", action=1))
    assert refused.status is ExecutionStatus.REJECTED
    assert "current frame is unknown" in str(refused.error)
    assert len(game.calls) == 2

    assert runtime.submit(_proposal("p3", "reset")).status is ExecutionStatus.SUCCEEDED
    assert runtime.frame is not None


def test_timeout_is_indeterminate_and_blocks_overlapping_calls(tmp_path, artifacts):
    block = _Block(STEPS[0])
    runtime, game = _started(
        tmp_path, artifacts, block, RESET, call_timeout_seconds=0.05
    )
    timed_out = runtime.submit(_proposal("p1", "step", action=4))
    assert timed_out.status is ExecutionStatus.INDETERMINATE
    assert "did not answer within 0.05s" in str(timed_out.error)

    busy = runtime.submit(_proposal("p2", "reset"))
    assert busy.status is ExecutionStatus.REJECTED
    assert "still running; nothing was sent" in str(busy.error)
    assert [c[0] for c in game.calls] == ["reset", "step"]

    block.release.set()
    for _ in range(100):
        if not runtime._caller.busy:  # pylint: disable=protected-access
            break
        threading.Event().wait(0.01)
    assert runtime.submit(_proposal("p3", "reset")).status is ExecutionStatus.SUCCEEDED


# ---- durability: close, reopen, resume ----------------------------------------


def test_reopen_keeps_receipts_and_the_action_count(tmp_path, artifacts):
    runtime, game = _started(tmp_path, artifacts, STEPS[0], STEPS[1])
    first = runtime.submit(_proposal("p1", "step", action=4))
    runtime.submit(_proposal("p2", "step", action=1))
    runtime.close()
    assert game.closed
    with pytest.raises(ContractError, match="closed"):
        runtime.submit(_proposal("p3", "reset"))

    again = FakeGame(RESET, STEPS[2])
    reopened = _runtime(tmp_path, artifacts, again)
    assert reopened.identity() == runtime.identity()
    assert reopened.actions_taken == 2
    assert reopened.submit(_proposal("p1", "step", action=4)) == first
    assert reconciled(reopened, "p1") == first
    assert reopened.reconcile("never-sent").kind is ReconciliationKind.UNKNOWN

    refused = reopened.submit(_proposal("p3", "step", action=1))
    assert refused.status is ExecutionStatus.REJECTED
    assert "reset first" in str(refused.error)
    assert again.calls == []
    assert reopened.submit(_proposal("p4", "reset")).status is ExecutionStatus.SUCCEEDED
    resumed = reopened.submit(_proposal("p5", "step", action=1))
    assert resumed.status is ExecutionStatus.SUCCEEDED
    assert reopened.actions_taken == 3


def test_a_claim_without_a_receipt_is_indeterminate_and_not_sent(tmp_path, artifacts):
    runtime, game = _started(tmp_path, artifacts)
    proposal = _proposal("p1", "step", action=1)
    # A crash after the claim and before the receipt leaves only the claim.
    runtime._store.prepare(  # pylint: disable=protected-access
        "p1", proposal_fingerprint(proposal)
    )
    receipt = runtime.submit(proposal)
    assert receipt.status is ExecutionStatus.INDETERMINATE
    assert "claimed earlier without a receipt" in str(receipt.error)
    assert receipt.usage is None
    assert len(game.calls) == 1


def reconciled(runtime: ArcRuntime, operation_id: str) -> ExecutionReceipt | None:
    result = runtime.reconcile(operation_id)
    assert result.kind is ReconciliationKind.RECEIPT
    return result.receipt


def test_api_key_value_never_reaches_records(tmp_path, artifacts, monkeypatch):
    secret = "arc-secret-key-value-123"
    monkeypatch.setenv("ARC_API_KEY", secret)
    runtime, _ = _started(tmp_path, artifacts, None, RESET, STEPS[0])
    for index, arguments in enumerate(({"action": 1}, {}, {"action": 4})):
        name = "reset" if not arguments else "step"
        runtime.submit(_proposal(f"p{index}", name, **arguments))
    runtime.close()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
    assert secret not in json.dumps(runtime.identity().__repr__())


# ---- toolkit boundary ---------------------------------------------------------


def test_timed_caller_validates_and_keeps_causes():
    for bad in (0, -1, float("inf"), True):
        with pytest.raises(ContractError, match="finite and positive"):
            TimedCaller(bad)
    caller = TimedCaller(1)
    assert caller.call("ok", lambda: 7) == 7
    cause = KeyError("x")

    def fail() -> None:
        raise cause

    with pytest.raises(ArcCallError) as caught:
        caller.call("fail", fail)
    assert caught.value.__cause__ is cause

    release = threading.Event()
    slow = TimedCaller(0.02)
    with pytest.raises(ArcCallError, match="did not answer"):
        slow.call("slow", lambda: release.wait(5))
    with pytest.raises(ArcCallBusy, match="next was not sent"):
        slow.call("next", lambda: 1)
    release.set()


def test_toolkit_info_is_strict():
    with pytest.raises(ContractError, match="game_id"):
        replace(INFO, game_id="")
    with pytest.raises(ContractError, match="seed"):
        replace(INFO, seed=True)


def test_missing_toolkit_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "arc_agi", None)
    with pytest.raises(ContractError, match=r"install vharness\[arc\]"):
        require_arc_toolkit()
