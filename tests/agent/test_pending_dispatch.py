"""Deferred port dispatch: the coordinator persists intent, a worker calls out."""

from __future__ import annotations

import pytest

from vharness.agent import (
    ActionChoice,
    BudgetLimits,
    CompletionMode,
    ContractError,
    EvaluationChoice,
    EvaluationMode,
    ScriptedModel,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import Pause, Steer, UsageMeasurement
from vharness.agent.replay import replay
from vharness.agent.supervision import (
    Guidance,
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisionCapability,
    SupervisorResponse,
    active_guidance,
)

from test_session import FakeEnvironment, FakeEvaluator, FakeRuntime


def _dependencies(tmp_path, name: str):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    journal = SqliteJournal(root / "session.sqlite3")
    return journal, ArtifactStore(root / "artifacts"), FakeEvaluator()


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="task-pending-dispatch",
        objective="inspect the fixture",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=4, model_calls=4, evaluations=2),
        environment_id="fake",
        evaluation_contract_id="fake-evaluator-v1",
    )


def _session(tmp_path, name: str, *, mode=EvaluationMode.EXTERNAL_CHECK):
    journal, artifacts, evaluator = _dependencies(tmp_path, name)
    environment = FakeEnvironment()
    runtime = FakeRuntime()
    session = Session.create(
        f"session-{name}",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        evaluation_mode=mode,
    )
    return session, journal, runtime, evaluator


def test_deferred_action_dispatch_runs_only_when_taken(tmp_path):
    session, journal, runtime, _evaluator = _session(tmp_path, "deferred-action")
    try:
        session.begin_attempt()
        model = ScriptedModel((ActionChoice("inspect", "look at the state"),))
        handle = session.prepare_decision(model)
        session.apply_decision(handle, model.decide(handle.request), dispatch=False)
        assert runtime.proposals == []
        assert "action_intended" in [event.kind for event in session.events()]
        pending = session.take_pending_dispatch()
        assert pending is not None
        assert pending.kind == "action"
        assert session.take_pending_dispatch() is None
        assert pending.proposal is not None
        receipt = runtime.submit(pending.proposal)
        session.receive_receipt(receipt)
        assert [item.proposal_id for item in runtime.proposals] == [
            pending.proposal.proposal_id
        ]
        assert session.view().status is SessionStatus.RUNNING
        assert replay(session.events()) == session._state
    finally:
        journal.close()


def test_deferred_and_bundled_action_paths_record_the_same_events(tmp_path):
    deferred, deferred_journal, _runtime, _evaluator = _session(
        tmp_path, "compare-deferred"
    )
    bundled, bundled_journal, _runtime2, _evaluator2 = _session(
        tmp_path, "compare-bundled"
    )
    try:
        script = (ActionChoice("inspect", "look at the state"),)
        deferred.begin_attempt()
        handle = deferred.prepare_decision(ScriptedModel(script))
        deferred.apply_decision(
            handle, ScriptedModel(script).decide(handle.request), dispatch=False
        )
        pending = deferred.take_pending_dispatch()
        assert pending is not None and pending.proposal is not None
        deferred.receive_receipt(_runtime.submit(pending.proposal))
        bundled.begin_attempt()
        bundled_handle = bundled.prepare_decision(ScriptedModel(script))
        bundled.apply_decision(
            bundled_handle, ScriptedModel(script).decide(bundled_handle.request)
        )
        assert [event.kind for event in deferred.events()] == [
            event.kind for event in bundled.events()
        ]
    finally:
        deferred_journal.close()
        bundled_journal.close()


def test_deferred_evaluation_dispatch_promotes_through_receive(tmp_path):
    session, journal, _runtime, evaluator = _session(tmp_path, "deferred-evaluation")
    try:
        session.begin_attempt()
        model = ScriptedModel(
            (
                ActionChoice("inspect", "look at the state"),
                EvaluationChoice("score the attempt"),
            )
        )
        handle = session.prepare_decision(model)
        session.apply_decision(handle, model.decide(handle.request))
        handle = session.prepare_decision(model)
        session.apply_decision(handle, model.decide(handle.request), dispatch=False)
        assert evaluator.requests == []
        pending = session.take_pending_dispatch()
        assert pending is not None
        assert pending.kind == "evaluation"
        assert pending.request is not None
        receipt = evaluator.evaluate(pending.request)
        session.receive_evaluation(pending.request, receipt)
        assert session.view().lineage_head.evaluation_receipt_id == receipt.receipt_id
        assert replay(session.events()) == session._state
    finally:
        journal.close()


def test_operator_mode_evaluation_choice_defers_nothing(tmp_path):
    session, journal, _runtime, evaluator = _session(
        tmp_path, "operator-defer", mode=EvaluationMode.OPERATOR
    )
    try:
        session.begin_attempt()
        model = ScriptedModel((EvaluationChoice("score the attempt"),))
        handle = session.prepare_decision(model)
        session.apply_decision(handle, model.decide(handle.request), dispatch=False)
        assert session.take_pending_dispatch() is None
        assert session.view().status is SessionStatus.WAITING
        assert evaluator.requests == []
    finally:
        journal.close()


def _supervised_task() -> TaskSpec:
    return TaskSpec(
        task_id="task-supervised-dispatch",
        objective="inspect the fixture",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=8, model_calls=8, evaluations=2),
        environment_id="fake",
        evaluation_contract_id="fake-evaluator-v1",
    )


def _guidance() -> Guidance:
    return Guidance(
        diagnosis="repeated inspection without new information",
        constraints=("avoid rereading the fixture",),
        suggested_experiments=("read the receipt instead",),
        expires_after_call=20,
    )


def _supervisor_response() -> SupervisorResponse:
    return SupervisorResponse(
        _guidance(),
        UsageMeasurement("pending-supervisor-usage", model_calls=1),
        "scripted-supervisor",
    )


def _supervised_capability(supervisor: ScriptedSupervisor) -> SupervisionCapability:
    return SupervisionCapability(
        supervisor,
        MonitorPolicy(window_calls=4, repeat_threshold=3, cooldown_calls=1),
    )


def _supervised_session(tmp_path, name: str, *, supervisor: ScriptedSupervisor):
    journal, artifacts, evaluator = _dependencies(tmp_path, name)
    runtime = FakeRuntime()
    session = Session.create(
        f"session-{name}",
        _supervised_task(),
        journal=journal,
        artifacts=artifacts,
        environment=FakeEnvironment(),
        runtime=runtime,
        evaluator=evaluator,
        supervision=_supervised_capability(supervisor),
    )
    return session, journal, runtime, evaluator


def _resolve_pending(session, pending, runtime, evaluator, supervisor) -> None:
    """Run one deferred descriptor as a bounded worker would and apply it."""
    if pending.kind == "action":
        session.receive_receipt(runtime.submit(pending.proposal))
    elif pending.kind == "evaluation":
        session.receive_evaluation(pending.request, evaluator.evaluate(pending.request))
    elif pending.kind == "supervision":
        try:
            response = supervisor.advise(pending.digest)
        except Exception:  # pylint: disable=broad-exception-caught
            response = None
        session.receive_guidance(response)
    else:  # pragma: no cover - guarded by PendingDispatch validation
        raise AssertionError(pending.kind)


def _deferred_decision(session, model, runtime, evaluator, supervisor) -> bool:
    """Drive one model decision through the deferred dispatch protocol."""
    handle = session.prepare_decision(model, dispatch=False)
    while handle is None:
        pending = session.take_pending_dispatch()
        if pending is None:
            return False
        _resolve_pending(session, pending, runtime, evaluator, supervisor)
        handle = session.prepare_decision(model, dispatch=False)
    session.apply_decision(handle, model.decide(handle.request), dispatch=False)
    pending = session.take_pending_dispatch()
    if pending is not None:
        _resolve_pending(session, pending, runtime, evaluator, supervisor)
    return True


def _inspect_script(count: int) -> tuple[ActionChoice, ...]:
    return tuple(ActionChoice("inspect", f"repeat {index}") for index in range(count))


def _kinds(session) -> list[str]:
    return [event.kind for event in session.events()]


def _counter_tuple(counters) -> tuple:
    """Return the envelope-free usage totals for run-equivalence assertions."""
    return (
        counters.actions,
        counters.evaluations,
        counters.model_calls,
        counters.supervisor_calls,
        counters.conservative_supervisor_calls,
        counters.supervisor_input_tokens,
        counters.supervisor_output_tokens,
    )


def test_deferred_supervision_dispatch_runs_only_when_taken(tmp_path):
    supervisor = ScriptedSupervisor((_supervisor_response(),))
    session, journal, runtime, evaluator = _supervised_session(
        tmp_path, "deferred-supervision", supervisor=supervisor
    )
    try:
        model = ScriptedModel(_inspect_script(4))
        for _ in range(3):
            assert _deferred_decision(session, model, runtime, evaluator, supervisor)
        assert "supervision_triggered" not in _kinds(session)

        assert _deferred_decision(session, model, runtime, evaluator, supervisor)
        kinds = _kinds(session)
        assert kinds.count("supervision_triggered") == 1
        assert kinds.count("supervisor_intended") == 1
        assert kinds.count("guidance_received") == 1
        assert supervisor.calls == 1
        assert replay(session.events()) == session._state
    finally:
        journal.close()


def test_prepare_decision_waits_while_supervision_dispatch_is_pending(tmp_path):
    supervisor = ScriptedSupervisor((_supervisor_response(),))
    session, journal, runtime, evaluator = _supervised_session(
        tmp_path, "pending-supervision-guard", supervisor=supervisor
    )
    try:
        model = ScriptedModel(_inspect_script(5))
        for _ in range(3):
            _deferred_decision(session, model, runtime, evaluator, supervisor)

        # The fourth decision triggers and defers the consult without a main call.
        assert session.prepare_decision(model, dispatch=False) is None
        pending = session.take_pending_dispatch()
        assert pending is not None and pending.kind == "supervision"
        # Taken but unresolved, scheduling still waits for the guidance result.
        assert session.prepare_decision(model, dispatch=False) is None
        session.receive_guidance(supervisor.advise(pending.digest))
        assert session.prepare_decision(model, dispatch=False) is not None
    finally:
        journal.close()


def test_guidance_result_is_recorded_when_a_pause_is_admitted_mid_consult(tmp_path):
    """A control recorded mid-consult never discards the completed call."""
    supervisor = ScriptedSupervisor((_supervisor_response(),))
    session, journal, runtime, evaluator = _supervised_session(
        tmp_path, "pause-mid-consult", supervisor=supervisor
    )
    try:
        model = ScriptedModel(_inspect_script(4))
        for _ in range(3):
            _deferred_decision(session, model, runtime, evaluator, supervisor)

        # The fourth decision triggers and defers the consult.
        assert session.prepare_decision(model, dispatch=False) is None
        pending = session.take_pending_dispatch()
        assert pending is not None and pending.kind == "supervision"

        # The operator pauses while the supervisor call is in flight.
        session.enqueue(Pause("pause-1", session.session_id))
        session.advance()
        assert session.view().status is SessionStatus.PAUSED

        # The completed call is recorded with its usage, not rejected.
        session.receive_guidance(supervisor.advise(pending.digest))
        kinds = _kinds(session)
        assert kinds.count("guidance_received") == 1
        assert "guidance_suppressed" not in kinds
        assert session.counters().supervisor_calls == 1
        assert session.view().status is SessionStatus.PAUSED
        assert active_guidance(session.events()) is not None
        assert replay(session.events()) == session._state
    finally:
        journal.close()


def test_guidance_result_is_recorded_and_suppressed_after_a_steer(tmp_path):
    """A consult returned after a steer is accounted for but not delivered."""
    supervisor = ScriptedSupervisor((_supervisor_response(),))
    session, journal, runtime, evaluator = _supervised_session(
        tmp_path, "steer-mid-consult", supervisor=supervisor
    )
    try:
        model = ScriptedModel(_inspect_script(4))
        for _ in range(3):
            _deferred_decision(session, model, runtime, evaluator, supervisor)

        assert session.prepare_decision(model, dispatch=False) is None
        pending = session.take_pending_dispatch()
        assert pending is not None and pending.kind == "supervision"

        session.enqueue(
            Steer("steer-1", session.session_id, "new objective", "new evidence")
        )
        session.advance()

        session.receive_guidance(supervisor.advise(pending.digest))
        kinds = _kinds(session)
        assert kinds.count("guidance_received") == 1
        assert kinds.count("guidance_suppressed") == 1
        assert session.counters().supervisor_calls == 1
        assert active_guidance(session.events()) is None
        assert replay(session.events()) == session._state
    finally:
        journal.close()


def test_guidance_result_is_recorded_exactly_once(tmp_path):
    """A completed consult settles once; a duplicate delivery is refused."""
    supervisor = ScriptedSupervisor((_supervisor_response(),))
    session, journal, runtime, evaluator = _supervised_session(
        tmp_path, "guidance-once", supervisor=supervisor
    )
    try:
        model = ScriptedModel(_inspect_script(4))
        for _ in range(3):
            _deferred_decision(session, model, runtime, evaluator, supervisor)
        assert session.prepare_decision(model, dispatch=False) is None
        pending = session.take_pending_dispatch()
        assert pending is not None and pending.kind == "supervision"

        response = supervisor.advise(pending.digest)
        session.receive_guidance(response)
        with pytest.raises(ContractError, match="no supervisor consult"):
            session.receive_guidance(response)
        assert session.counters().supervisor_calls == 1
        assert replay(session.events()) == session._state
    finally:
        journal.close()


def test_steer_closes_active_guidance_without_losing_its_usage(tmp_path):
    """A steer closes stale guidance before the next request is assembled."""
    supervisor = ScriptedSupervisor((_supervisor_response(),))
    session, journal, runtime, evaluator = _supervised_session(
        tmp_path, "steer-closes-guidance", supervisor=supervisor
    )
    try:
        model = ScriptedModel(_inspect_script(4))
        for _ in range(4):
            _deferred_decision(session, model, runtime, evaluator, supervisor)
        assert active_guidance(session.events()) is not None

        session.enqueue(
            Steer("steer-1", session.session_id, "new objective", "new evidence")
        )
        session.advance()

        kinds = _kinds(session)
        assert kinds.count("guidance_received") == 1
        assert kinds.count("guidance_suppressed") == 1
        assert active_guidance(session.events()) is None
        # The completed supervisor call keeps its recorded usage.
        assert session.counters().supervisor_calls == 1
        assert replay(session.events()) == session._state

        # The next request is assembled without the superseded guidance.
        assert (
            session.prepare_decision(ScriptedModel(_inspect_script(1)), dispatch=False)
            is not None
        )
        manifest = [
            event.payload["manifest"]
            for event in session.events()
            if event.kind == "context_assembled"
        ][-1]
        assert all(pair[0] != "supervision" for pair in manifest["included"])
    finally:
        journal.close()


def test_deferred_and_inline_supervision_failure_record_the_same_events(tmp_path):
    deferred_supervisor = ScriptedSupervisor(())  # an empty script fails the consult
    deferred, deferred_journal, deferred_runtime, deferred_evaluator = (
        _supervised_session(
            tmp_path, "compare-failure-deferred", supervisor=deferred_supervisor
        )
    )
    inline_supervisor = ScriptedSupervisor(())
    inline, inline_journal, inline_runtime, inline_evaluator = _supervised_session(
        tmp_path, "compare-failure-inline", supervisor=inline_supervisor
    )
    try:
        deferred_model = ScriptedModel(_inspect_script(4))
        for _ in range(4):
            _deferred_decision(
                deferred,
                deferred_model,
                deferred_runtime,
                deferred_evaluator,
                deferred_supervisor,
            )
        inline_model = ScriptedModel(_inspect_script(4))
        for _ in range(4):
            inline.step(inline_model)

        assert _kinds(deferred) == _kinds(inline)
        assert _kinds(deferred).count("supervisor_failed") == 1
        assert "guidance_received" not in _kinds(deferred)
        assert _counter_tuple(deferred.counters()) == _counter_tuple(inline.counters())
        assert deferred_supervisor.calls == inline_supervisor.calls == 1
        assert replay(deferred.events()) == deferred._state
    finally:
        deferred_journal.close()
        inline_journal.close()


def test_deferred_action_is_settled_not_submitted_after_a_steer(tmp_path):
    """A steer applied before the take settles the deferred action as unsent."""
    session, journal, runtime, _evaluator = _session(tmp_path, "steer-deferred")
    try:
        session.begin_attempt()
        model = ScriptedModel((ActionChoice("inspect", "look at the state"),))
        handle = session.prepare_decision(model, dispatch=False)
        session.apply_decision(handle, model.decide(handle.request), dispatch=False)

        session.enqueue(
            Steer("steer-1", session.session_id, "new objective", "new evidence")
        )
        session.advance()

        assert session.take_pending_dispatch() is None
        assert runtime.proposals == []
        reconciled = [
            event for event in session.events() if event.kind == "operation_reconciled"
        ]
        assert reconciled
        assert reconciled[-1].payload["result"] == "not_submitted"
        assert replay(session.events()) == session._state
    finally:
        journal.close()


def test_deferred_evaluation_is_settled_not_submitted_after_a_steer(tmp_path):
    """A stale deferred evaluation is settled as unsent without running it."""
    session, journal, _runtime, evaluator = _session(
        tmp_path, "steer-deferred-evaluation"
    )
    try:
        session.begin_attempt()
        model = ScriptedModel((EvaluationChoice("score the attempt"),))
        handle = session.prepare_decision(model, dispatch=False)
        session.apply_decision(handle, model.decide(handle.request), dispatch=False)

        session.enqueue(
            Steer("steer-2", session.session_id, "new objective", "new evidence")
        )
        session.advance()

        assert session.take_pending_dispatch() is None
        assert evaluator.requests == []
        reconciled = [
            event for event in session.events() if event.kind == "operation_reconciled"
        ]
        assert reconciled
        assert reconciled[-1].payload["result"] == "not_submitted"
        assert replay(session.events()) == session._state
    finally:
        journal.close()


def test_deferred_and_inline_supervision_record_the_same_events(tmp_path):
    deferred_supervisor = ScriptedSupervisor((_supervisor_response(),))
    deferred, deferred_journal, deferred_runtime, deferred_evaluator = (
        _supervised_session(
            tmp_path, "compare-supervision-deferred", supervisor=deferred_supervisor
        )
    )
    inline_supervisor = ScriptedSupervisor((_supervisor_response(),))
    inline, inline_journal, inline_runtime, inline_evaluator = _supervised_session(
        tmp_path, "compare-supervision-inline", supervisor=inline_supervisor
    )
    try:
        deferred_model = ScriptedModel(_inspect_script(4))
        for _ in range(4):
            _deferred_decision(
                deferred,
                deferred_model,
                deferred_runtime,
                deferred_evaluator,
                deferred_supervisor,
            )
        inline_model = ScriptedModel(_inspect_script(4))
        for _ in range(4):
            inline.step(inline_model)

        assert _kinds(deferred) == _kinds(inline)
        assert deferred_supervisor.calls == inline_supervisor.calls == 1
        assert replay(deferred.events()) == deferred._state
        assert replay(inline.events()) == inline._state
    finally:
        deferred_journal.close()
        inline_journal.close()
