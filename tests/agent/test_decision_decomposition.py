"""Crash-window and live-control tests for the decision decomposition."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from typing import Iterator

import pytest

from vharness.agent import (
    ActionChoice,
    BudgetLimits,
    CompletionMode,
    ScriptedModel,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import ContractError, IntegrityError, TransitionError
from vharness.agent.journal import SqliteJournal
from vharness.agent.kernel import ModelResponse
from vharness.agent.models import (
    ActionDefinition,
    ComponentIdentity,
    EnvironmentContract,
    EvaluationReceipt,
    ExecutionReceipt,
    ExecutionStatus,
    OperatorCommand,
    Pause,
    RuntimeCapabilities,
    StateRef,
    Steer,
    Stop,
    UsageMeasurement,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.replay import replay
from vharness.agent.session_state import evaluation_request_from_payload


class FakeEnvironment:
    """Deterministic contract with one inspectable action."""

    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="fake",
            version="1",
            initial_state=StateRef("fake", "initial", digest="0" * 64),
            actions=(ActionDefinition("inspect", "Inspect current state"),),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class FakeRuntime:
    """Records submitted proposals and returns a successful receipt."""

    def __init__(self) -> None:
        self.proposals = []

    def identity(self):
        return ComponentIdentity("fake-runtime", "v1")

    def submit(self, proposal):
        self.proposals.append(proposal)
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef("fake", f"after-{proposal.proposal_id}", digest="1" * 64),
        )

    def reconcile(self, operation_id):
        del operation_id
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class FakeEvaluator:
    """Returns configured external acceptance."""

    def __init__(self) -> None:
        self.requests = []
        self.reconciliation_kind = ReconciliationKind.RECEIPT

    def identity(self):
        return ComponentIdentity("fake-evaluator", "v1")

    def evaluate(self, request):
        self.requests.append(request)
        return EvaluationReceipt(
            receipt_id=f"evaluation-{request.request_id}",
            request_id=request.request_id,
            objective_version=request.objective_version,
            evaluated_state=request.evaluated_state,
            baseline_node_id=request.baseline_node_id,
            evaluator_version=request.evaluator_version,
            accepted=True,
            comparison="improved",
        )

    def reconcile(self, request_id):
        del request_id
        return EvaluationReconciliationResult(self.reconciliation_kind)


class ControlDuringDecide:
    """Enqueues one durable control from inside the blocking model call."""

    def __init__(
        self, session: Session, command: OperatorCommand, result: ActionChoice
    ) -> None:
        self._session = session
        self._command = command
        self._result = result
        self.model_id = "control-during-decide"
        self.provider_id = "test"
        self.repair_budget = 0

    def decide(self, request) -> ModelResponse:
        self._session.enqueue(self._command)
        return ModelResponse(
            result=self._result,
            output=type(self._result).__name__,
            model_id=request.model_id,
            finish_reason="stop",
            usage=UsageMeasurement(
                measurement_id=f"{request.request_id}-usage",
                model_calls=1,
                duration_ms=0,
            ),
            duration_ms=0,
        )


@pytest.fixture
def dependencies(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    yield journal, ArtifactStore(
        tmp_path / "artifacts"
    ), FakeEnvironment(), FakeRuntime(), FakeEvaluator()
    journal.close()


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="task-1",
        objective="inspect the fixture",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=4, evaluations=2, model_calls=8),
        environment_id="fake",
        evaluation_contract_id="fake-evaluator-v1",
    )


def _create(dependencies, session_id: str) -> Session:
    journal, artifacts, environment, runtime, evaluator = dependencies
    return Session.create(
        session_id,
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )


def _open(dependencies, session_id: str) -> Session:
    journal, artifacts, environment, runtime, evaluator = dependencies
    return Session.open(
        session_id,
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )


def _kinds(session: Session) -> list[str]:
    return [event.kind for event in session.events(limit=1000)]


@contextmanager
def _crash_on_event(journal: SqliteJournal, kind: str) -> Iterator[None]:
    """Abort any insert of ``kind``, simulating a crash at that fact."""
    trigger = f"abort_{kind}"
    connection = journal._connection  # pylint: disable=protected-access
    connection.execute(f"""
        CREATE TRIGGER {trigger}
        BEFORE INSERT ON events
        WHEN NEW.kind = '{kind}'
        BEGIN
            SELECT RAISE(ABORT, 'injected {kind} crash');
        END
        """)
    try:
        yield
    finally:
        connection.execute(f"DROP TRIGGER {trigger}")


def test_result_and_action_intent_commit_atomically(dependencies):
    """A crash between the decision and its intent records neither fact."""
    journal = dependencies[0]
    runtime = dependencies[3]
    session = _create(dependencies, "session-atomic")

    with _crash_on_event(journal, "action_intended"):
        with pytest.raises(IntegrityError, match="injected action_intended crash"):
            session.step(ScriptedModel((ActionChoice("inspect", "inspect"),)))

    kinds = _kinds(session)
    assert "action_intended" not in kinds
    assert "model_decided" not in kinds
    assert runtime.proposals == []

    reopened = _open(dependencies, "session-atomic")
    assert replay(reopened.events()) == reopened._state
    reopened.step(ScriptedModel((ActionChoice("inspect", "inspect"),)))
    assert _kinds(reopened).count("action_intended") == 1
    assert len(runtime.proposals) == 1


def test_pause_during_blocking_decision_suppresses_the_action(dependencies):
    """A pause admitted during the model call is recorded and never dispatched."""
    runtime = dependencies[3]
    session = _create(dependencies, "session-pause-block")
    model = ControlDuringDecide(
        session, Pause("pause-block", session.session_id), ActionChoice("inspect", "go")
    )
    returned = session.step(model)

    kinds = _kinds(session)
    assert "session_paused" in kinds
    assert "command_applied" in kinds
    assert "model_result_suppressed" in kinds
    assert "action_intended" not in kinds
    assert runtime.proposals == []
    assert session.view().status is SessionStatus.PAUSED
    assert returned.status is session.view().status
    assert returned.cursor == session.view().cursor

    reopened = _open(dependencies, "session-pause-block")
    assert replay(reopened.events()) == reopened._state
    assert reopened.view().status is SessionStatus.PAUSED
    assert returned.status is reopened.view().status
    assert returned.cursor == reopened.view().cursor


def test_stop_during_blocking_decision_suppresses_the_action(dependencies):
    """A stop admitted during the model call prevents the pending dispatch."""
    runtime = dependencies[3]
    session = _create(dependencies, "session-stop-block")
    model = ControlDuringDecide(
        session, Stop("stop-block", session.session_id), ActionChoice("inspect", "go")
    )
    returned = session.step(model)

    kinds = _kinds(session)
    assert "session_stopped" in kinds
    assert "command_applied" in kinds
    assert "model_result_suppressed" in kinds
    assert "action_intended" not in kinds
    assert runtime.proposals == []
    assert session.view().status is SessionStatus.STOPPED
    assert returned.status is session.view().status
    assert returned.cursor == session.view().cursor
    reopened = _open(dependencies, "session-stop-block")
    assert returned.status is reopened.view().status
    assert returned.cursor == reopened.view().cursor


def test_apply_decision_is_idempotent_per_request(dependencies):
    """Re-applying one decision is a no-op; conflicting content is an error."""
    runtime = dependencies[3]
    session = _create(dependencies, "session-idempotent")
    model = ScriptedModel((ActionChoice("inspect", "inspect"),))
    handle = session.prepare_decision(model)
    assert handle is not None
    response = model.decide(handle.request)

    session.apply_decision(handle, response)
    session.apply_decision(handle, response)
    assert _kinds(session).count("action_intended") == 1
    assert len(runtime.proposals) == 1

    reopened = _open(dependencies, "session-idempotent")
    event_count = len(reopened.events(limit=1000))
    reopened.apply_decision(handle, response)
    assert len(reopened.events(limit=1000)) == event_count

    conflicting = ModelResponse(
        result=response.result,
        output="different output",
        model_id=response.model_id,
        finish_reason="stop",
        usage=response.usage,
    )
    with pytest.raises(ContractError, match="already applied"):
        session.apply_decision(handle, conflicting)


def test_changed_result_and_usage_conflict_for_same_request(dependencies):
    session = _create(dependencies, "session-result-identity")
    model = ScriptedModel((ActionChoice("inspect", "inspect"),))
    handle = session.prepare_decision(model)
    assert handle is not None
    response = model.decide(handle.request)
    session.apply_decision(handle, response)

    changed = replace(
        response,
        result=replace(response.result, rationale="different rationale"),
        usage=UsageMeasurement(
            f"{handle.request_id}-changed-usage",
            model_calls=1,
            input_tokens=999,
        ),
    )
    with pytest.raises(ContractError, match="already applied"):
        session.apply_decision(handle, changed)


def test_changed_typed_result_conflicts_with_same_output_and_usage(dependencies):
    session = _create(dependencies, "session-result-only-conflict")
    model = ScriptedModel((ActionChoice("inspect", "inspect"),))
    handle = session.prepare_decision(model)
    assert handle is not None
    response = model.decide(handle.request)
    session.apply_decision(handle, response)

    changed = replace(
        response, result=replace(response.result, rationale="different rationale")
    )
    with pytest.raises(ContractError, match="already applied"):
        session.apply_decision(handle, changed)


def test_changed_usage_conflicts_with_same_output_and_result(dependencies):
    session = _create(dependencies, "session-usage-only-conflict")
    model = ScriptedModel((ActionChoice("inspect", "inspect"),))
    handle = session.prepare_decision(model)
    assert handle is not None
    response = model.decide(handle.request)
    session.apply_decision(handle, response)

    changed = replace(
        response,
        usage=UsageMeasurement(
            f"{handle.request_id}-different-usage", model_calls=1, input_tokens=999
        ),
    )
    with pytest.raises(ContractError, match="already applied"):
        session.apply_decision(handle, changed)


def test_output_suffix_beyond_durable_excerpt_conflicts(dependencies):
    session = _create(dependencies, "session-output-suffix-conflict")
    model = ScriptedModel((ActionChoice("inspect", "inspect"),))
    handle = session.prepare_decision(model)
    assert handle is not None
    common_prefix = "x" * (64 * 1024)
    response = replace(model.decide(handle.request), output=common_prefix + "first")
    session.apply_decision(handle, response)

    changed = replace(response, output=common_prefix + "second")
    with pytest.raises(ContractError, match="already applied"):
        session.apply_decision(handle, changed)


def test_legacy_terminal_replays_but_cannot_authenticate_a_duplicate(
    dependencies, monkeypatch
):
    session = _create(dependencies, "session-legacy-terminal")
    model = ScriptedModel((ActionChoice("inspect", "inspect"),))
    handle = session.prepare_decision(model)
    assert handle is not None
    response = model.decide(handle.request)
    session.apply_decision(handle, response)
    events = session.events(limit=1000)
    terminal = next(
        event
        for event in events
        if event.kind == "model_decided"
        and event.payload.get("request_id") == handle.request_id
    )
    legacy_payload = dict(terminal.payload)
    legacy_payload.pop("response_identity")
    legacy_terminal = replace(terminal, payload=legacy_payload)
    legacy_events = tuple(
        legacy_terminal if event is terminal else event for event in events
    )
    assert replay(legacy_events) == session._state

    monkeypatch.setattr(
        session, "_model_terminal_record", lambda _request_id: legacy_payload
    )
    with pytest.raises(ContractError, match="already applied"):
        session.apply_decision(handle, response)


def test_prepare_decision_refuses_a_second_unsettled_main_call(dependencies):
    session = _create(dependencies, "session-one-main-call")
    model = ScriptedModel(
        (ActionChoice("inspect", "first"), ActionChoice("inspect", "second"))
    )

    first = session.prepare_decision(model)
    second = session.prepare_decision(model)

    assert first is not None
    assert second is None
    assert _kinds(session).count("model_intended") == 1
    session.apply_decision(first, model.decide(first.request))
    assert len(dependencies[3].proposals) == 1


def test_waiting_pending_operation_suppresses_retained_decision(dependencies):
    runtime = dependencies[3]
    session = _create(dependencies, "session-pending-evaluation")
    handle = session.prepare_decision(
        ScriptedModel((ActionChoice("inspect", "late action"),))
    )
    assert handle is not None
    response = ScriptedModel((ActionChoice("inspect", "unused"),)).decide(
        handle.request
    )

    request, reservation, pending = session._prepare_evaluation(session._state)
    session._persist(
        "evaluation_intended",
        pending,
        {"request": request, "reservation": reservation},
    )
    assert session.view().status is SessionStatus.WAITING
    assert session.view().pending_proposal_id == request.request_id

    session.apply_decision(handle, response)

    assert session.view().status is SessionStatus.WAITING
    assert "model_result_suppressed" in _kinds(session)
    assert "action_intended" not in _kinds(session)
    assert runtime.proposals == []
    assert session.counters().model_calls == 1
    reopened = _open(dependencies, "session-pending-evaluation")
    assert replay(reopened.events()) == reopened._state
    assert reopened.view().status is SessionStatus.WAITING


def test_applied_steer_suppresses_original_handle_without_retagging(dependencies):
    runtime = dependencies[3]
    session = _create(dependencies, "session-stale-steer")
    model = ScriptedModel((ActionChoice("inspect", "stale action"),))
    handle = session.prepare_decision(model)
    assert handle is not None
    response = model.decide(handle.request)
    original_version = handle.objective_version

    session.enqueue(
        Steer(
            "steer-before-apply",
            session.session_id,
            "new objective",
            "external acceptance",
        )
    )
    session.advance()
    session.apply_decision(handle, response)

    events = session.events(limit=1000)
    intent = next(
        event
        for event in events
        if event.kind == "model_intended"
        and event.payload.get("request_id") == handle.request_id
    )
    terminal = next(
        event
        for event in events
        if event.kind == "model_decided"
        and event.payload.get("request_id") == handle.request_id
    )
    assert intent.objective_version == original_version
    assert terminal.payload["cursor"] == handle.cursor_at_intent + 1
    assert "model_result_suppressed" in [event.kind for event in events]
    assert "action_intended" not in [event.kind for event in events]
    assert runtime.proposals == []
    assert replay(events) == session._state


def _assert_rejected_model_application(
    dependencies,
    session_id,
    session,
    handle,
    response,
    error_type,
    environment=None,
    error_match=None,
    retry_is_identifiable=True,
):
    previous_terminal_count = _kinds(session).count("model_decided")
    previous_action_intents = _kinds(session).count("action_intended")
    previous_model_calls = session.counters().model_calls
    with pytest.raises(error_type) as raised:
        session.apply_decision(handle, response)
    if error_match is not None:
        assert error_match in str(raised.value)

    events = session.events(limit=1000)
    kinds = [event.kind for event in events]
    assert kinds.count("model_decided") == previous_terminal_count + 1
    assert kinds.count("model_application_rejected") == 1
    assert kinds.count("action_intended") == previous_action_intents
    operation = next(
        item
        for item in session._state.model_operations
        if item.request_id == handle.request_id
    )
    assert operation.settled
    assert session.counters().model_calls == previous_model_calls + 1
    terminal = next(
        event
        for event in events
        if event.kind == "model_decided"
        and event.payload.get("request_id") == handle.request_id
    )
    assert terminal.payload["usage"]["input_tokens"] == response.usage.input_tokens
    assert terminal.payload["usage"]["output_tokens"] == response.usage.output_tokens
    rejection = next(
        event
        for event in events
        if event.kind == "model_application_rejected"
        and event.payload.get("request_id") == handle.request_id
    )
    assert rejection.payload["reason"]
    assert replay(events) == session._state

    journal, artifacts, default_environment, runtime, evaluator = dependencies
    reopened = Session.open(
        session_id,
        journal=journal,
        artifacts=artifacts,
        environment=environment or default_environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    assert replay(reopened.events()) == reopened._state
    assert reopened.counters().model_calls == previous_model_calls + 1
    reopened_operation = next(
        item
        for item in reopened._state.model_operations
        if item.request_id == handle.request_id
    )
    assert reopened_operation.settled
    event_count = len(reopened.events(limit=1000))
    if retry_is_identifiable:
        reopened.apply_decision(handle, response)
    else:
        with pytest.raises(ContractError, match="already applied"):
            reopened.apply_decision(handle, response)
    assert len(reopened.events(limit=1000)) == event_count


def test_unknown_action_application_error_settles_and_replays(dependencies):
    session_id = "session-invalid-action"
    session = _create(dependencies, session_id)
    model = ScriptedModel((ActionChoice("missing", "inspect"),))
    handle = session.prepare_decision(model)
    assert handle is not None
    response = replace(
        model.decide(handle.request),
        usage=UsageMeasurement(
            f"{handle.request_id}-measured",
            model_calls=1,
            input_tokens=111,
            output_tokens=22,
        ),
    )

    _assert_rejected_model_application(
        dependencies,
        session_id,
        session,
        handle,
        response,
        ContractError,
        error_match="unknown environment action",
    )


def test_invalid_action_schema_application_error_settles_and_replays(dependencies):
    journal, artifacts, _environment, runtime, evaluator = dependencies

    class StrictEnvironment:
        """Environment whose inspect action rejects undeclared arguments."""

        def describe(self) -> EnvironmentContract:
            return EnvironmentContract(
                environment_id="fake",
                version="1",
                initial_state=StateRef("fake", "initial", digest="0" * 64),
                actions=(
                    ActionDefinition(
                        "inspect",
                        "Inspect current state",
                        {"type": "object", "additionalProperties": False},
                    ),
                ),
                capabilities=RuntimeCapabilities(),
            )

    session_id = "session-invalid-schema"
    session = Session.create(
        session_id,
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=StrictEnvironment(),
        runtime=runtime,
        evaluator=evaluator,
    )
    model = ScriptedModel((ActionChoice("inspect", "inspect", {"unexpected": True}),))
    handle = session.prepare_decision(model)
    assert handle is not None
    response = model.decide(handle.request)

    _assert_rejected_model_application(
        dependencies,
        session_id,
        session,
        handle,
        response,
        ContractError,
        environment=StrictEnvironment(),
        error_match="undeclared",
    )


def test_unfingerprintable_typed_result_still_settles_known_usage(dependencies):
    session_id = "session-unfingerprintable-result"
    session = _create(dependencies, session_id)
    model = ScriptedModel((ActionChoice("inspect", "inspect"),))
    handle = session.prepare_decision(model)
    assert handle is not None
    response = replace(
        model.decide(handle.request),
        result=ActionChoice("inspect", "r" * 1_000_100),
        output="bounded visible output",
        usage=UsageMeasurement(
            f"{handle.request_id}-measured",
            model_calls=1,
            input_tokens=111,
        ),
    )

    _assert_rejected_model_application(
        dependencies,
        session_id,
        session,
        handle,
        response,
        ContractError,
        error_match="JSON payload exceeds",
        retry_is_identifiable=False,
    )


def test_action_budget_application_error_settles_fifth_model_call(dependencies):
    session_id = "session-action-budget-rejection"
    session = _create(dependencies, session_id)
    model = ScriptedModel(tuple(ActionChoice("inspect", "inspect") for _ in range(5)))
    for _ in range(4):
        session.step(model)
    handle = session.prepare_decision(model)
    assert handle is not None
    response = replace(
        model.decide(handle.request),
        usage=UsageMeasurement(
            f"{handle.request_id}-measured",
            model_calls=1,
            input_tokens=111,
            output_tokens=22,
        ),
    )

    _assert_rejected_model_application(
        dependencies,
        session_id,
        session,
        handle,
        response,
        TransitionError,
        error_match="action budget exhausted",
    )
    assert session.counters().model_calls == 5


def test_paused_session_prepares_no_decision(dependencies):
    """A paused session returns no handle and records no model intent."""
    session = _create(dependencies, "session-paused")
    session.enqueue(Pause("pause-1", session.session_id))
    handle = session.prepare_decision(ScriptedModel((ActionChoice("inspect", "x"),)))
    assert handle is None
    assert "model_intended" not in _kinds(session)
    assert session.view().status is SessionStatus.PAUSED


def _evaluation_request(session: Session, request_id: str):
    for event in session.events(limit=1000):
        if event.kind == "evaluation_intended":
            request = evaluation_request_from_payload(event.payload["request"])
            if request.request_id == request_id:
                return request
    raise AssertionError("no durable evaluation intent")


def _evaluated_session(dependencies, session_id: str):
    session = _create(dependencies, session_id)
    session.begin_attempt("attempt-1")
    receipt = session.submit_action("inspect", {}, rationale="go")
    evaluation = session.request_evaluation(receipt.state)
    return session, evaluation, _evaluation_request(session, evaluation.request_id)


def test_consumed_evaluation_replay_is_a_noop(dependencies):
    """Re-applying a consumed evaluation returns its recorded receipt unchanged."""
    session, evaluation, request = _evaluated_session(
        dependencies, "session-eval-dedup"
    )
    before = _kinds(session)
    # pylint: disable-next=protected-access
    again = session._receive_evaluation_receipt(request, evaluation)
    assert again == evaluation
    assert _kinds(session) == before


def test_conflicting_evaluation_receipt_is_rejected(dependencies):
    """Different content under one request ID is a contract error, not a no-op."""
    session, evaluation, request = _evaluated_session(
        dependencies, "session-eval-conflict"
    )
    conflicting = replace(evaluation, receipt_id="evaluation-other")
    with pytest.raises(ContractError, match="conflicts with durable intake"):
        # pylint: disable-next=protected-access
        session._receive_evaluation_receipt(request, conflicting)


def test_receipt_contradicts_not_submitted_reconciliation(dependencies):
    """A receipt cannot override a proved not-submitted evaluation."""
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = _create(dependencies, "session-eval-notsub")
    session.begin_attempt("attempt-1")
    receipt = session.submit_action("inspect", {}, rationale="go")
    # pylint: disable=protected-access
    request, reservation, pending = session._prepare_evaluation(
        session._state, receipt.state
    )
    session._persist(
        "evaluation_intended",
        pending,
        {"request": request, "reservation": reservation},
    )
    evaluator.reconciliation_kind = ReconciliationKind.NOT_SUBMITTED
    operation = session._state.evaluation_operations[0]
    session._reconcile_evaluation(operation)
    arriving = evaluator.evaluate(request)
    with pytest.raises(ContractError, match="not-submitted"):
        session._receive_evaluation_receipt(request, arriving)
    # pylint: enable=protected-access
