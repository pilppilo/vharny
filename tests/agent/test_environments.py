"""Mechanical software and Gymnasium fixtures use the same session kernel."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace

import pytest

from vharness.agent import (
    ActionChoice,
    BudgetLimits,
    CompletionMode,
    EvaluationChoice,
    LineageQuery,
    ScriptedModel,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from gymnasium_fixtures import (
    continuous_gymnasium_fixture,
    discrete_gymnasium_fixture,
)
from vharness.agent.errors import ContractError, PersistenceError
from vharness.agent.environments import (
    CONTINUOUS_DRIVE_GOAL,
    DISCRETE_REACH_GOAL,
    GymnasiumEvaluator,
    SoftwareEvaluator,
    SoftwareFixture,
)
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import ArtifactRef, ExecutionStatus, StateRef
from vharness.agent.models import ActionProposal, ResourceReservation
from vharness.agent.ports import ReconciliationKind


@pytest.fixture
def stores(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    yield journal, ArtifactStore(tmp_path / "artifacts")
    journal.close()


def _software_session(stores):
    journal, artifacts = stores
    fixture = SoftwareFixture(artifacts)
    session = Session.create(
        "session-env",
        TaskSpec(
            task_id="task-1",
            objective="keep value equals 1",
            completion_mode=CompletionMode.FINITE,
            success_evidence="external evaluator acceptance",
            budgets=BudgetLimits(actions=6, evaluations=2, model_calls=8),
            environment_id=fixture.environment_id,
            evaluation_contract_id="software-fixture-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=fixture,
        runtime=fixture,
        evaluator=SoftwareEvaluator(fixture, artifacts),
    )
    return session, fixture


def _gym_session(stores, fixture, *, objective: str, goal):
    journal, artifacts = stores
    session = Session.create(
        "session-env",
        TaskSpec(
            task_id="task-1",
            objective=objective,
            completion_mode=CompletionMode.FINITE,
            success_evidence="external evaluator acceptance",
            budgets=BudgetLimits(actions=6, evaluations=2, model_calls=8),
            environment_id=fixture.environment_id,
            evaluation_contract_id="gymnasium-fixture-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=fixture,
        runtime=fixture,
        evaluator=GymnasiumEvaluator(artifacts, goal),
    )
    return session


def test_software_fixture_publishes_workspace_files_on_write(stores):
    _journal, artifacts = stores
    session, _fixture = _software_session(stores)
    session.step(
        ScriptedModel(
            (
                ActionChoice(
                    "write",
                    "fix the source",
                    {"path": "main.py", "content": "value = 1\n"},
                ),
            )
        )
    )
    receipts = [
        event.payload["receipt"]
        for event in session.events(limit=100)
        if event.kind == "receipt_received"
    ]
    raw = receipts[0]["raw_artifact"]
    assert raw["provenance"] == "software-fixture-write"
    payload = artifacts.read(
        ArtifactRef(raw["digest"], raw["size"], raw["media_type"], raw["provenance"])
    )
    assert b'"main.py"' in payload
    assert b"value = 1" in payload


def test_software_fixture_contract_is_stable_and_recovery_is_durable(
    stores,
):
    _journal, artifacts = stores
    fixture = SoftwareFixture(artifacts)
    initial = fixture.describe()

    proposal = ActionProposal(
        proposal_id="duplicate-proposal",
        session_id="contract-test",
        objective_version=1,
        attempt_id="attempt-1",
        action_name="write",
        arguments={"path": "main.py", "content": "value = 1\n"},
        expected_state=initial.initial_state,
        reservation=ResourceReservation(
            "reservation-1", "duplicate-proposal", 1, actions=1
        ),
        rationale="change the workspace",
    )
    receipt = fixture.submit(proposal)
    restarted = SoftwareFixture(artifacts)

    assert restarted.describe().initial_state == restarted._state()  # noqa: SLF001
    assert restarted.describe().capabilities.idempotency is True
    recovered = restarted.reconcile(proposal.proposal_id)
    assert recovered.kind is ReconciliationKind.RECEIPT
    assert recovered.receipt == receipt
    assert restarted.submit(proposal) == receipt
    with pytest.raises(ContractError, match="different proposal"):
        restarted.submit(
            replace(
                proposal,
                arguments={"path": "main.py", "content": "value = 2\n"},
            )
        )
    assert (
        restarted.reconcile("missing-proposal").kind is ReconciliationKind.NOT_SUBMITTED
    )
    assert restarted.describe().initial_state == restarted._state()  # noqa: SLF001


def test_software_session_reopens_with_stable_contract_and_restored_state(stores):
    journal, artifacts = stores
    fixture = SoftwareFixture(artifacts)
    fixture.workspace_record(
        {"main.py": "value = 1\n"}, revision="1", tested=True, check_passed=True
    )
    session = Session.create(
        "session-env",
        TaskSpec(
            task_id="task-1",
            objective="keep value equals 1",
            completion_mode=CompletionMode.FINITE,
            success_evidence="external evaluator acceptance",
            budgets=BudgetLimits(actions=6, evaluations=2, model_calls=8),
            environment_id=fixture.environment_id,
            evaluation_contract_id="software-fixture-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=fixture,
        runtime=fixture,
        evaluator=SoftwareEvaluator(fixture, artifacts),
    )

    restarted = SoftwareFixture(artifacts)
    reopened = Session.open(
        "session-env",
        journal=journal,
        artifacts=artifacts,
        environment=restarted,
        runtime=restarted,
        evaluator=SoftwareEvaluator(restarted, artifacts),
    )

    assert reopened.view().pending_proposal_id is None
    assert restarted.files["main.py"] == "value = 1\n"
    assert restarted._state().revision == "1"  # noqa: SLF001


def test_software_fixture_fresh_process_ignores_pending_effect(stores):
    _journal, artifacts = stores
    fixture = SoftwareFixture(artifacts)
    initial = fixture.describe()
    proposal = ActionProposal(
        proposal_id="fresh-process-proposal",
        session_id="fresh-process",
        objective_version=1,
        attempt_id="attempt-1",
        action_name="write",
        arguments={"path": "main.py", "content": "value = 1\n"},
        expected_state=initial.initial_state,
        reservation=ResourceReservation(
            "reservation-1", "fresh-process-proposal", 1, actions=1
        ),
        rationale="change the workspace",
    )
    fixture.submit(proposal)

    pending = artifacts.root / ".software-fixture-state.pending"
    pending.write_bytes(b"partial crash write")
    script = """
import sys

from vharness.agent.artifacts import ArtifactStore
from vharness.agent.environments import SoftwareFixture

fixture = SoftwareFixture(ArtifactStore(sys.argv[1]))
result = fixture.reconcile(sys.argv[2])
print(fixture.files["main.py"], end="")
print(result.kind.value)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(artifacts.root), proposal.proposal_id],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == ["value = 1", "receipt"]
    assert pending.is_file()


def test_software_fixture_write_failure_does_not_expose_candidate_state(stores):
    _journal, artifacts = stores
    fixture = SoftwareFixture(artifacts)
    initial_files = dict(fixture.files)
    initial_state = fixture._state()  # noqa: SLF001

    def fail_checkpoint(**_kwargs):
        raise PersistenceError("injected checkpoint failure")

    fixture._persist_state = fail_checkpoint  # type: ignore[method-assign]
    proposal = ActionProposal(
        proposal_id="checkpoint-failure",
        session_id="fixture-test",
        objective_version=1,
        attempt_id="attempt-1",
        action_name="write",
        arguments={"path": "main.py", "content": "value = 1\n"},
        expected_state=initial_state,
        reservation=ResourceReservation(
            "reservation-1", "checkpoint-failure", 1, actions=1
        ),
        rationale="inject checkpoint failure",
    )

    with pytest.raises(PersistenceError, match="injected checkpoint failure"):
        fixture.submit(proposal)
    assert fixture.files == initial_files
    assert fixture._state() == initial_state  # noqa: SLF001
    assert (
        fixture.reconcile(proposal.proposal_id).kind is ReconciliationKind.NOT_SUBMITTED
    )


def test_software_fixture_workspace_publication_failure_does_not_expose_candidate_state(
    stores,
):
    _journal, artifacts = stores
    fixture = SoftwareFixture(artifacts)
    initial_files = dict(fixture.files)
    initial_state = fixture._state()  # noqa: SLF001

    def fail_workspace(**_kwargs):
        raise PersistenceError("injected workspace publication failure")

    fixture.workspace_record = fail_workspace  # type: ignore[method-assign]
    proposal = ActionProposal(
        proposal_id="workspace-failure",
        session_id="fixture-test",
        objective_version=1,
        attempt_id="attempt-1",
        action_name="write",
        arguments={"path": "main.py", "content": "value = 1\n"},
        expected_state=initial_state,
        reservation=ResourceReservation(
            "reservation-1", "workspace-failure", 1, actions=1
        ),
        rationale="inject workspace failure",
    )

    with pytest.raises(
        PersistenceError, match="injected workspace publication failure"
    ):
        fixture.submit(proposal)
    assert fixture.files == initial_files
    assert fixture._state() == initial_state  # noqa: SLF001
    assert (
        fixture.reconcile(proposal.proposal_id).kind is ReconciliationKind.NOT_SUBMITTED
    )


def test_software_fixture_rejects_oversized_candidate_without_effect(stores):
    _journal, artifacts = stores
    fixture = SoftwareFixture(artifacts)
    initial_files = dict(fixture.files)
    initial_state = fixture._state()  # noqa: SLF001
    proposal = ActionProposal(
        proposal_id="oversized-write",
        session_id="fixture-test",
        objective_version=1,
        attempt_id="attempt-1",
        action_name="write",
        arguments={"path": "main.py", "content": "x" * 1_000_001},
        expected_state=initial_state,
        reservation=ResourceReservation(
            "reservation-1", "oversized-write", 1, actions=1
        ),
        rationale="oversized candidate",
    )

    with pytest.raises(ContractError, match="byte bound"):
        fixture.submit(proposal)
    assert fixture.files == initial_files
    assert fixture._state() == initial_state  # noqa: SLF001


def test_software_initial_observation_lists_workspace_files(stores):
    _journal, artifacts = stores
    fixture = SoftwareFixture(artifacts)
    observation = fixture.initial_observation("session-obs")
    assert observation.raw_record is not None
    payload = artifacts.read(observation.raw_record)
    assert b'"main.py"' in payload
    assert b"value = 0" in payload


def test_software_fixture_write_then_test_completes_with_session_step(stores):
    session, fixture = _software_session(stores)
    model = ScriptedModel(
        (
            ActionChoice("read", "inspect workspace", {"path": "main.py"}),
            ActionChoice(
                "write",
                "fix the source",
                {"path": "main.py", "content": "value = 1\n"},
            ),
            ActionChoice("test", "run the check"),
            EvaluationChoice("ask the workspace evaluator"),
        )
    )
    for _ in range(4):
        session.step(model)

    assert fixture.files["main.py"] == "value = 1\n"
    assert fixture.last_check_passed is True
    assert session.view().status is SessionStatus.COMPLETED
    assert len(session.accepted_lineage()) == 2
    kinds = [event.kind for event in session.events(limit=100)]
    assert kinds.count("receipt_received") == 3
    assert "lineage_promoted" in kinds


def test_software_failed_check_stays_outside_lineage(stores):
    session, fixture = _software_session(stores)
    model = ScriptedModel(
        (
            ActionChoice("test", "run the check"),
            EvaluationChoice("ask the workspace evaluator"),
        )
    )
    session.step(model)
    session.step(model)
    assert fixture.last_check_passed is False
    assert session.view().status is SessionStatus.RUNNING
    assert len(session.accepted_lineage()) == 1
    receipts = [
        event.payload["receipt"]
        for event in session.events(limit=100)
        if event.kind == "receipt_received"
    ]
    assert receipts[0]["status"] == ExecutionStatus.FAILED.value


def test_rejected_evaluation_receipt_is_mandatory_on_next_decision(stores):
    session, _fixture = _software_session(stores)
    model = ScriptedModel(
        (
            ActionChoice("test", "run the check"),
            EvaluationChoice("ask the workspace evaluator"),
            LineageQuery(),
        )
    )
    session.step(model)
    session.step(model)
    session.step(model)
    receipt = model.requests[2].latest_receipt
    assert receipt["accepted"] is False
    assert receipt["disposition"] == "rejected"


def test_observation_after_evaluation_keeps_execution_state(stores):
    session, fixture = _software_session(stores)
    model = ScriptedModel(
        (
            ActionChoice("test", "run the check"),
            EvaluationChoice("ask the workspace evaluator"),
            LineageQuery(),
        )
    )
    session.step(model)
    session.step(model)
    session.step(model)
    observation = model.requests[2].observation
    assert observation["status"] == ExecutionStatus.FAILED.value
    assert "accepted" not in observation
    assert observation["revision"] == fixture._state().revision  # noqa: SLF001
    receipt = model.requests[2].latest_receipt
    assert receipt["accepted"] is False


def test_software_read_returns_file_content_on_the_next_decision(stores):
    session, fixture = _software_session(stores)
    model = ScriptedModel(
        (
            ActionChoice("read", "inspect workspace", {"path": "main.py"}),
            LineageQuery(),
        )
    )
    session.step(model)
    session.step(model)
    content = model.requests[1].observation["content"]
    assert content["path"] == "main.py"
    assert content["content"] == fixture.files["main.py"]
    schemas = {
        action.name: dict(action.argument_schema)
        for action in model.requests[1].actions
    }
    assert schemas["read"]["required"] == ("path",)
    assert model.requests[1].result_schemas["ActionChoice"]
    assert model.requests[1].remaining_budgets["actions"]["used"] == 1
    assert model.requests[1].request_id
    assert model.requests[1].policy_version == 2


def test_software_evaluator_uses_requested_state_not_stale_check_flag(stores):
    session, fixture = _software_session(stores)
    model = ScriptedModel(
        (
            ActionChoice(
                "write",
                "fix the source",
                {"path": "main.py", "content": "value = 1\n"},
            ),
            ActionChoice("test", "run the check"),
            ActionChoice(
                "write",
                "break the source",
                {"path": "main.py", "content": "value = 0\n"},
            ),
            EvaluationChoice("ask the workspace evaluator"),
        )
    )
    for _ in range(4):
        session.step(model)
    assert fixture.last_check_passed is False
    assert session.view().status is SessionStatus.RUNNING
    assert len(session.accepted_lineage()) == 1


def test_discrete_gymnasium_reset_and_step_use_official_values(stores):
    fixture = discrete_gymnasium_fixture()
    session = _gym_session(
        stores, fixture, objective="reach observation 3", goal=DISCRETE_REACH_GOAL
    )
    model = ScriptedModel(
        (
            ActionChoice("reset", "official reset"),
            ActionChoice("step", "official step", {"action": 1}),
            ActionChoice("step", "official step", {"action": 1}),
            ActionChoice("step", "official step", {"action": 1}),
            EvaluationChoice("report native return"),
        )
    )
    for _ in range(5):
        session.step(model)

    assert fixture.observation == 3
    assert fixture.terminated is True
    assert fixture.truncated is False
    assert fixture.reward == 1.0
    assert fixture.episode_return == 1.0
    assert fixture.episode_length == 3
    assert session.view().status is SessionStatus.COMPLETED
    node = session.accepted_lineage()[-1]
    assert node.objectives["return"] == 1.0
    assert node.objectives["episode_length"] == 3
    assert node.objectives["terminated"] is True
    assert node.objectives["observation"] == 3


def test_gymnasium_evaluator_rejects_requested_state_before_native_goal(stores):
    fixture = discrete_gymnasium_fixture()
    session = _gym_session(
        stores, fixture, objective="reach observation 3", goal=DISCRETE_REACH_GOAL
    )
    model = ScriptedModel(
        (
            ActionChoice("reset", "official reset"),
            ActionChoice("step", "official step", {"action": 1}),
            EvaluationChoice("report native return"),
        )
    )
    session.step(model)
    session.step(model)
    session.step(model)
    assert fixture.observation == 1
    assert fixture.terminated is False
    assert session.view().status is SessionStatus.RUNNING
    assert len(session.accepted_lineage()) == 1


def test_continuous_gymnasium_reset_and_step_use_official_values(stores):
    fixture = continuous_gymnasium_fixture()
    session = _gym_session(
        stores,
        fixture,
        objective="drive position to zero",
        goal=CONTINUOUS_DRIVE_GOAL,
    )
    model = ScriptedModel(
        (
            ActionChoice("reset", "official reset"),
            ActionChoice("step", "official step", {"action": "-1.0"}),
            EvaluationChoice("report native return"),
        )
    )
    session.step(model)
    session.step(model)
    session.step(model)

    assert fixture.observation == 0.0
    assert fixture.terminated is True
    assert fixture.truncated is False
    assert fixture.reward == 0.0
    assert fixture.episode_return == 0.0
    assert fixture.episode_length == 1
    assert session.view().status is SessionStatus.COMPLETED
    node = session.accepted_lineage()[-1]
    assert node.objectives["return"] == 0.0
    assert node.objectives["episode_length"] == 1


def test_software_fixture_initial_state_reflects_restored_workspace(stores):
    _journal, artifacts = stores
    fixture = SoftwareFixture(artifacts)
    fixture.workspace_record(
        {"main.py": "value = 1\n"}, revision="1", tested=True, check_passed=True
    )

    restarted = SoftwareFixture(artifacts)
    assert restarted.describe().initial_state == restarted._state()  # noqa: SLF001
    assert restarted.describe().initial_state.revision == "1"
    observation = restarted.initial_observation("new-session-from-restored-state")
    assert observation.state.revision == "1"


def test_software_fixture_keeps_session_creation_state_after_workspace_change(stores):
    _journal, artifacts = stores
    fixture = SoftwareFixture(artifacts)
    fixture.initial_observation("initial-state-bound")
    creation_state = fixture.describe().initial_state

    fixture.workspace_record(
        {"main.py": "value = 1\n"}, revision="1", tested=True, check_passed=True
    )

    assert fixture.describe().initial_state == creation_state
    assert fixture._state().revision == "1"  # noqa: SLF001
    assert creation_state.revision == "0"


def test_software_fixture_accepts_durable_creation_state(stores):
    _journal, artifacts = stores
    original = SoftwareFixture(artifacts)
    creation_state = original.describe().initial_state
    original.workspace_record(
        {"main.py": "value = 1\n"}, revision="1", tested=True, check_passed=True
    )

    fixture = SoftwareFixture(artifacts, initial_state=creation_state)

    assert fixture.describe().initial_state == creation_state
    assert fixture._state().revision == "1"  # noqa: SLF001
    with pytest.raises(ContractError, match="cannot seed another stateful session"):
        fixture.initial_observation("new-session-with-old-binding")
    with pytest.raises(ContractError, match="initial state owner is invalid"):
        SoftwareFixture(artifacts, initial_state=StateRef("another-environment", "x"))
