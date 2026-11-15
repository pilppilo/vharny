"""Official FrozenLake checkpoint and CartPole adapter diagnostic tests."""

from __future__ import annotations

import json

import pytest

from vharness.agent import (
    ActionChoice,
    BudgetLimits,
    CompletionMode,
    EvaluationChoice,
    ScriptedModel,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.adapters.gymnasium import (
    CARTPOLE_ENV_ID,
    FROZENLAKE_ENV_ID,
    FROZENLAKE_GOAL,
    OFFICIAL_EVALUATOR_CONTRACT,
    OFFICIAL_EVALUATOR_ID,
    OFFICIAL_RUNTIME_ID,
    OfficialGymnasiumEvaluator,
    OfficialGymnasiumRuntime,
    discrete_action_schema,
    json_safe_gym_value,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.checkpoint_policy import frozenlake_diagnostic_checkpoint_results
from vharness.agent.environments import NativeGoal
from vharness.agent.errors import ContractError
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import ActionProposal, ArtifactRef, ResourceReservation
from vharness.agent.ports import ReconciliationKind


@pytest.fixture
def stores(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    yield journal, ArtifactStore(tmp_path / "artifacts")
    journal.close()


@pytest.fixture
def official_runtime(stores):
    pytest.importorskip("gymnasium")
    _journal, artifacts = stores
    runtime = OfficialGymnasiumRuntime(artifacts, seed=0)
    try:
        yield runtime, artifacts
    finally:
        runtime.close()


def _official_session(
    stores,
    *,
    env_id=FROZENLAKE_ENV_ID,
    goal=FROZENLAKE_GOAL,
    objective=None,
):
    pytest.importorskip("gymnasium")
    journal, artifacts = stores
    runtime = OfficialGymnasiumRuntime(artifacts, env_id=env_id, seed=0)
    evaluator = OfficialGymnasiumEvaluator(artifacts, goal)
    session_id = f"session-official-{env_id.lower()}"
    session = Session.create(
        session_id,
        TaskSpec(
            task_id=f"task-{env_id.lower()}",
            objective=objective or f"complete the native {env_id} control goal",
            completion_mode=CompletionMode.FINITE,
            success_evidence="external evaluator acceptance",
            budgets=BudgetLimits(actions=8, evaluations=2, model_calls=10),
            environment_id=runtime.environment_id,
            evaluation_contract_id=OFFICIAL_EVALUATOR_CONTRACT,
        ),
        journal=journal,
        artifacts=artifacts,
        environment=runtime,
        runtime=runtime,
        evaluator=evaluator,
        initial_observation=runtime.initial_observation(session_id),
    )
    return session, runtime, artifacts


def test_cartpole_control_diagnostic_uses_native_state_and_promotes(stores):
    session, runtime, artifacts = _official_session(
        stores,
        env_id=CARTPOLE_ENV_ID,
        goal=NativeGoal(min_return=3.0),
        objective="exercise the bounded three-step CartPole adapter diagnostic",
    )
    try:
        model = ScriptedModel(
            (
                ActionChoice("reset", "official reset"),
                ActionChoice("step", "apply left control", {"action": 0}),
                ActionChoice("step", "apply left control", {"action": 0}),
                ActionChoice("step", "apply left control", {"action": 0}),
                EvaluationChoice("report the predeclared three-step return goal"),
            )
        )
        for _ in range(5):
            session.step(model)

        assert session.view().status is SessionStatus.COMPLETED
        assert runtime.environment_id == CARTPOLE_ENV_ID
        schema = runtime.describe().actions[1].argument_schema["properties"]["action"]
        assert schema["type"] == "integer"
        assert schema["minimum"] == 0
        assert schema["maximum"] == 1
        node = session.accepted_lineage()[-1]
        assert node.objectives["return"] == 3.0
        assert node.objectives["episode_length"] == 3
        assert node.objectives["terminated"] is False
        assert node.objectives["truncated"] is False

        request = next(
            event.payload["request"]
            for event in session.events(limit=100)
            if event.kind == "evaluation_intended"
        )
        receipt = next(
            event.payload["receipt"]
            for event in session.events(limit=100)
            if event.kind == "evaluation_received"
        )
        assert request["evaluated_state"] == receipt["evaluated_state"]
        assert receipt["accepted"] is True

        terminal = next(
            event.payload["receipt"]
            for event in reversed(session.events(limit=100))
            if event.kind == "receipt_received"
        )
        raw = terminal["raw_artifact"]
        payload = json.loads(
            artifacts.read(
                ArtifactRef(
                    raw["digest"],
                    raw["size"],
                    raw["media_type"],
                    raw["provenance"],
                )
            ).decode("utf-8")
        )
        assert payload["episode_length"] == 3
        assert payload["return"] == 3.0
        assert len(payload["observation"]) == 4
    finally:
        runtime.close()


def test_json_safe_gym_value_converts_plain_python():
    assert json_safe_gym_value(3) == 3
    assert json_safe_gym_value(1.5) == 1.5
    assert json_safe_gym_value({"prob": 1}) == {"prob": 1}
    assert json_safe_gym_value((0, 1)) == (0, 1)


def test_json_safe_gym_value_converts_numpy_scalars():
    numpy = pytest.importorskip("numpy")
    assert json_safe_gym_value(numpy.int64(3)) == 3
    assert json_safe_gym_value(numpy.float64(1.0)) == 1.0


def test_discrete_action_schema_publishes_bounds():
    schema = discrete_action_schema(minimum=0, maximum=3)
    action = schema["properties"]["action"]
    assert action["type"] == "integer"
    assert action["minimum"] == 0
    assert action["maximum"] == 3


def test_official_runtime_resets_before_describe(official_runtime):
    runtime, artifacts = official_runtime
    gymnasium = pytest.importorskip("gymnasium")
    contract = runtime.describe()
    payload = json.loads(contract.initial_state.value)
    assert payload["observation"] == 0
    assert payload["episode_length"] == 0
    observation = runtime.initial_observation("session-obs")
    assert observation.raw_record is not None
    body = json.loads(artifacts.read(observation.raw_record).decode("utf-8"))
    assert body["observation"] == 0
    assert body["terminated"] is False
    step_schema = contract.actions[1].argument_schema["properties"]["action"]
    assert step_schema["minimum"] == 0
    assert step_schema["maximum"] == 3
    identity = runtime.identity()
    assert identity.component_id == OFFICIAL_RUNTIME_ID
    assert identity.version == gymnasium.__version__
    assert identity.configuration_digest is not None
    runtime.close()
    assert runtime.closed


def test_official_contract_initial_state_stays_at_construction_reset(stores):
    session, runtime, _artifacts = _official_session(stores)
    try:
        session.step(
            ScriptedModel((ActionChoice("step", "take one step", {"action": 1}),))
        )
        payload = json.loads(runtime.describe().initial_state.value)
        assert payload["observation"] == 0
        assert payload["episode_length"] == 0
    finally:
        runtime.close()


def test_official_frozenlake_scripted_path_promotes_lineage(stores):
    session, runtime, artifacts = _official_session(stores)
    try:
        model = ScriptedModel(frozenlake_diagnostic_checkpoint_results())
        for _ in range(8):
            session.step(model)
        assert session.view().status is SessionStatus.COMPLETED
        node = session.accepted_lineage()[-1]
        assert node.objectives["terminated"] is True
        assert node.objectives["truncated"] is False
        assert node.objectives["return"] == 1.0
        assert node.objectives["episode_length"] == 6
        receipts = [
            event.payload["receipt"]
            for event in session.events(limit=100)
            if event.kind == "receipt_received"
        ]
        raw = receipts[-1]["raw_artifact"]
        payload = json.loads(
            artifacts.read(
                ArtifactRef(
                    raw["digest"], raw["size"], raw["media_type"], raw["provenance"]
                )
            ).decode("utf-8")
        )
        assert payload["observation"] == 15
        assert payload["return"] == 1.0
        assert runtime.identity().component_id == OFFICIAL_RUNTIME_ID
        assert session.view().task.evaluation_contract_id == OFFICIAL_EVALUATOR_CONTRACT
        assert runtime.identity().component_id != "gymnasium-fixture-runtime"
    finally:
        runtime.close()


def test_official_evaluator_rejects_requested_state_before_goal(stores):
    session, runtime, _artifacts = _official_session(stores)
    try:
        model = ScriptedModel(
            (
                ActionChoice("reset", "official reset"),
                ActionChoice("step", "one step", {"action": 1}),
                EvaluationChoice("report native FrozenLake goal"),
            )
        )
        session.step(model)
        session.step(model)
        session.step(model)
        assert session.view().status is SessionStatus.RUNNING
        assert len(session.accepted_lineage()) == 1
        assert runtime.terminated is False
    finally:
        runtime.close()


def test_official_out_of_bounds_action_is_contract_error(stores):
    session, runtime, _artifacts = _official_session(stores)
    try:
        with pytest.raises(ContractError, match="must be <= 3"):
            session.step(
                ScriptedModel((ActionChoice("step", "illegal action", {"action": 4}),))
            )
    finally:
        runtime.close()


def test_native_goal_does_not_live_on_the_runtime():
    assert not hasattr(OfficialGymnasiumRuntime, "native_accepts")
    assert not hasattr(OfficialGymnasiumEvaluator, "native_accepts")
    goal = NativeGoal(require_terminated=True, reject_truncated=True, min_return=1.0)
    assert goal.accepts(
        {
            "terminated": True,
            "truncated": False,
            "return": 1.0,
            "observation": 15,
        }
    )
    assert not goal.accepts(
        {
            "terminated": True,
            "truncated": False,
            "return": 0.0,
            "observation": 12,
        }
    )


def test_official_identities_are_not_fixture_identities(official_runtime):
    runtime, artifacts = official_runtime
    evaluator = OfficialGymnasiumEvaluator(artifacts)
    assert runtime.identity().component_id == OFFICIAL_RUNTIME_ID
    assert evaluator.identity().component_id == OFFICIAL_EVALUATOR_ID
    assert runtime.environment_id == FROZENLAKE_ENV_ID
    assert evaluator.identity().configuration_digest is not None


def test_official_runtime_advertises_unsupported_duplicate_recovery(official_runtime):
    runtime, _artifacts = official_runtime
    capabilities = runtime.describe().capabilities
    assert not capabilities.idempotency
    assert not capabilities.reconciliation
    proposal = ActionProposal(
        proposal_id="proposal-duplicate",
        session_id="session-duplicate",
        objective_version=1,
        attempt_id="attempt-duplicate",
        action_name="step",
        arguments={"action": 1},
        expected_state=runtime.describe().initial_state,
        reservation=ResourceReservation(
            reservation_id="reservation-duplicate",
            operation_id="proposal-duplicate",
            objective_version=1,
            actions=1,
        ),
        rationale="exercise duplicate proposal semantics",
    )
    runtime.submit(proposal)
    runtime.submit(proposal)
    assert runtime.episode_length == 2
    assert (
        runtime.reconcile(proposal.proposal_id).kind is ReconciliationKind.UNSUPPORTED
    )
