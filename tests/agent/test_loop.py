"""AVO vertical-slice checks: model-chosen order, lineage, context, pause."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from vharness.agent import (
    ActionChoice,
    AttemptQuery,
    BudgetLimits,
    CompletionMode,
    EvaluationChoice,
    KnowledgeQuery,
    KnowledgeSource,
    LineageQuery,
    MemoryUpdate,
    OperatorQuestion,
    PlanUpdate,
    RecordEnvelope,
    ScriptedModel,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.context import RETRIEVAL_EVENT_KINDS, assemble_context
from vharness.agent.errors import TransitionError
from vharness.agent.journal import SearchFilter, SqliteJournal
from vharness.agent.memory import (
    KnowledgeStatus,
    knowledge_items,
    observation_payload,
)
from vharness.agent.models import (
    ActionDefinition,
    ComponentIdentity,
    EnvironmentContract,
    EvaluationReceipt,
    ExecutionReceipt,
    ExecutionStatus,
    Message,
    Pause,
    Resume,
    RuntimeCapabilities,
    StateRef,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)


class FakeEnvironment:
    """Deterministic contract used by the variation-loop tests."""

    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="fake",
            version="1",
            initial_state=StateRef("fake", "initial", digest="0" * 64),
            actions=(
                ActionDefinition("inspect", "Inspect current state"),
                ActionDefinition("debug", "Debug current state"),
            ),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class FakeRuntime:
    """Records proposals and returns a successful receipt."""

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

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.requests = []

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
            accepted=self.accepted,
            comparison="improved" if self.accepted else "regressed",
            objectives={"score": 1},
        )

    def reconcile(self, request_id):
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


@pytest.fixture
def dependencies(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    yield journal, ArtifactStore(
        tmp_path / "artifacts"
    ), FakeEnvironment(), FakeRuntime(), FakeEvaluator()
    journal.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task(**overrides) -> TaskSpec:
    values = {
        "task_id": "task-1",
        "objective": "inspect the fixture",
        "completion_mode": CompletionMode.FINITE,
        "success_evidence": "external evaluator acceptance",
        "budgets": BudgetLimits(actions=4, evaluations=2, model_calls=8),
        "environment_id": "fake",
        "evaluation_contract_id": "fake-evaluator-v1",
    }
    values.update(overrides)
    return TaskSpec(**values)


def _source(session_id: str) -> KnowledgeSource:
    observed = _now()
    return KnowledgeSource(
        "kb-docs",
        "hardware notes",
        observed,
        RecordEnvelope("kb-docs", session_id, 1, observed, "hardware notes"),
    )


def _session(dependencies, *, evaluator=None, knowledge=(), task=None):
    journal, artifacts, environment, runtime, default_evaluator = dependencies
    session = Session.create(
        "session-loop",
        task or _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator or default_evaluator,
        knowledge_sources=knowledge,
    )
    return session, runtime, evaluator or default_evaluator


def _kinds(session: Session) -> list[str]:
    return [event.kind for event in session.events(limit=1000)]


def _manifests(session: Session) -> list[object]:
    return [
        event.payload["manifest"]
        for event in session.events(limit=1000)
        if event.kind == "context_assembled"
    ]


def test_authoritative_runtime_state_survives_late_receipts_and_reopen(
    dependencies,
):
    session, runtime, _evaluator = _session(dependencies)
    session.step(ScriptedModel((ActionChoice("inspect", "inspect"),)))
    proposal = runtime.proposals[0]
    terminal = session.events(limit=1000)[-1].payload["receipt"]
    assert terminal["receipt_id"] == f"receipt-{proposal.proposal_id}"
    session.request_evaluation()
    session.receive_receipt(
        ExecutionReceipt(
            "late-running",
            proposal.proposal_id,
            proposal.objective_version,
            ExecutionStatus.RUNNING,
            state=StateRef("fake", "late", digest="3" * 64, revision="3"),
        )
    )
    observation = observation_payload(session._state, session.events())  # noqa: SLF001
    assert observation["state"] == f"after-{proposal.proposal_id}"
    assert observation["receipt_id"] == terminal["receipt_id"]

    reopened = Session.open(
        "session-loop",
        journal=session._journal,  # noqa: SLF001
        artifacts=session._artifacts,  # noqa: SLF001
        environment=FakeEnvironment(),
        runtime=runtime,
        evaluator=_evaluator,
    )
    reopened_observation = observation_payload(
        reopened._state, reopened.events()  # noqa: SLF001
    )
    assert reopened_observation == observation


def test_model_chooses_inspect_act_evaluate_order(dependencies):
    session, runtime, _evaluator = _session(
        dependencies, knowledge=(_source("session-loop"),)
    )
    model = ScriptedModel(
        (
            LineageQuery(),
            KnowledgeQuery(),
            ActionChoice("inspect", "look at the state"),
            EvaluationChoice("score the attempt"),
        )
    )
    for _ in range(4):
        session.step(model)

    kinds = _kinds(session)
    agent_kinds = [
        kind
        for kind in kinds
        if kind
        in {
            "lineage_inspected",
            "knowledge_inspected",
            "action_intended",
            "evaluation_intended",
            "lineage_promoted",
        }
    ]
    assert agent_kinds == [
        "lineage_inspected",
        "knowledge_inspected",
        "action_intended",
        "evaluation_intended",
        "lineage_promoted",
    ]
    assert len(runtime.proposals) == 1
    assert session.accepted_lineage()[-1].parent_id == "root-session-loop"
    assert model.calls == 4
    inspected = next(
        event
        for event in session.events(limit=1000)
        if event.kind == "knowledge_inspected"
    )
    assert inspected.payload["item_ids"] == ("kb-docs",)


def test_alternate_act_then_evaluate_order_still_commits(dependencies):
    session, runtime, _evaluator = _session(dependencies)
    model = ScriptedModel(
        (
            ActionChoice("inspect", "act first"),
            EvaluationChoice("evaluate second"),
            LineageQuery(),
        )
    )
    for _ in range(3):
        session.step(model)
    kinds = _kinds(session)
    assert kinds.index("action_intended") < kinds.index("evaluation_intended")
    assert kinds.index("evaluation_intended") < kinds.index("lineage_inspected")
    assert len(runtime.proposals) == 1
    assert len(session.accepted_lineage()) == 2


def test_model_chooses_debug_as_second_action_before_evaluation(dependencies):
    session, runtime, _evaluator = _session(dependencies)
    model = ScriptedModel(
        (
            ActionChoice("inspect", "inspect before debugging"),
            ActionChoice("debug", "debug based on inspection"),
            EvaluationChoice("evaluate after debugging"),
        )
    )
    for _ in range(3):
        session.step(model)

    assert [proposal.action_name for proposal in runtime.proposals] == [
        "inspect",
        "debug",
    ]
    kinds = _kinds(session)
    action_positions = [
        index for index, kind in enumerate(kinds) if kind == "action_intended"
    ]
    evaluation_position = kinds.index("evaluation_intended")
    assert len(action_positions) == 2
    assert action_positions[0] < action_positions[1] < evaluation_position
    assert len(session.accepted_lineage()) == 2


def test_rejected_attempt_stays_outside_lineage_and_remains_queryable(dependencies):
    evaluator = FakeEvaluator(accepted=False)
    session, _runtime, _evaluator = _session(dependencies, evaluator=evaluator)
    session.begin_attempt("rejected-attempt")
    model = ScriptedModel(
        (
            ActionChoice("inspect", "try a change"),
            EvaluationChoice("check it"),
            AttemptQuery("rejected-attempt"),
            LineageQuery(),
        )
    )
    session.step(model)
    session.step(model)
    session.step(model)
    assert len(session.accepted_lineage()) == 1
    assert session.accepted_lineage()[0].parent_id is None
    rejected = next(
        item
        for item in session.attempts()
        if item.attempt.attempt_id == "rejected-attempt"
    )
    assert rejected.attempt.disposition.value == "rejected"
    inspected = next(
        event
        for event in session.events(limit=1000)
        if event.kind == "attempt_inspected"
    )
    evidence = inspected.payload["attempt"]
    assert evidence["attempt_id"] == rejected.attempt.attempt_id
    assert evidence["disposition"] == "rejected"
    sources = evidence["sources"]
    assert all(len(source["summary"]) <= 240 for source in sources)
    assert any(
        source["kind"] == "action_intended"
        and source["sequence"] >= evidence["source_start"]
        and "try a change" in source["summary"]
        for source in sources
    )
    assert any(
        source["kind"] == "evaluation_received"
        and source["event_id"]
        and "regressed" in source["summary"]
        for source in sources
    )
    session.step(model)
    visible = model.requests[-1].context.text
    assert evidence["attempt_id"] in visible
    assert "try a change" in visible
    assert "regressed" in visible
    assert all(source["event_id"] in visible for source in sources)
    assert "root-session-loop" in next(
        event.payload["node_ids"]
        for event in session.events(limit=1000)
        if event.kind == "lineage_inspected"
    )
    journal, artifacts, environment, runtime, _ = dependencies
    reopened = Session.open(
        "session-loop",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    replayed = next(
        event
        for event in reopened.events(limit=1000)
        if event.kind == "attempt_inspected"
    )
    assert replayed.payload == inspected.payload
    assert reopened.attempts()[0].attempt.disposition.value == "rejected"


def test_unknown_attempt_query_is_a_durable_empty_result(dependencies):
    session, _runtime, _evaluator = _session(dependencies)
    model = ScriptedModel((AttemptQuery("missing-attempt"), LineageQuery()))

    session.step(model)
    inspected = next(
        event
        for event in session.events(limit=1000)
        if event.kind == "attempt_inspected"
    )
    assert {
        key: value
        for key, value in inspected.payload["attempt"].items()
        if key != "__type__"
    } == {
        "attempt_id": "missing-attempt",
        "found": False,
        "disposition": None,
        "base_node_id": None,
        "source_start": 0,
        "source_end": 0,
        "sources": (),
    }
    session.step(model)
    assert model.calls == 2


def test_incomparable_attempt_can_be_inspected_after_reopen(dependencies):
    class IncomparableEvaluator(FakeEvaluator):
        def evaluate(self, request):
            return replace(
                super().evaluate(request),
                comparison="incomparable",
            )

    evaluator = IncomparableEvaluator(accepted=True)
    session, runtime, _evaluator = _session(dependencies, evaluator=evaluator)
    session.begin_attempt("incomparable-attempt")
    model = ScriptedModel(
        (
            ActionChoice("inspect", "collect incomparable evidence"),
            EvaluationChoice("compare the attempt"),
            AttemptQuery("incomparable-attempt"),
            LineageQuery(),
        )
    )
    session.step(model)
    session.step(model)
    session.step(model)

    assert session.attempts()[0].attempt.disposition.value == "incomparable"
    inspected = next(
        event
        for event in session.events(limit=1000)
        if event.kind == "attempt_inspected"
    )
    assert inspected.payload["attempt"]["disposition"] == "incomparable"
    reopened = Session.open(
        "session-loop",
        journal=session._journal,  # noqa: SLF001
        artifacts=session._artifacts,  # noqa: SLF001
        environment=FakeEnvironment(),
        runtime=runtime,
        evaluator=evaluator,
    )
    replayed = next(
        event
        for event in reopened.events(limit=1000)
        if event.kind == "attempt_inspected"
    )
    assert replayed.payload == inspected.payload
    assert reopened.attempts()[0].attempt.disposition.value == "incomparable"


def test_identical_journal_and_budget_yield_identical_manifests(dependencies):
    session, _runtime, _evaluator = _session(
        dependencies, knowledge=(_source("session-loop"),)
    )
    model = ScriptedModel((LineageQuery(), LineageQuery()))
    session.step(model, token_budget=16384)
    session.step(model, token_budget=16384)
    manifests = _manifests(session)
    assert len(manifests) == 2
    events = session.events(limit=1000)
    hits = session._journal.search(  # noqa: SLF001
        "session-loop",
        session._state.task.objective,  # noqa: SLF001
        filters=SearchFilter(kinds=RETRIEVAL_EVENT_KINDS),
    )
    first = assemble_context(
        session._state,  # noqa: SLF001
        events,
        FakeEnvironment().describe(),
        token_budget=256,
        hits=hits,
    )
    second = assemble_context(
        session._state,  # noqa: SLF001
        events,
        FakeEnvironment().describe(),
        token_budget=256,
        hits=hits,
    )
    assert first.manifest == second.manifest
    assert manifests[0]["policy_version"] == 2
    assert manifests[0]["token_counter"] == "whitespace-v1"
    assert manifests[0]["token_estimator"] == "conservative-chars-v1"


def test_current_plan_contributes_to_journal_retrieval_query(dependencies, monkeypatch):
    session, _runtime, _evaluator = _session(dependencies)
    session.step(ScriptedModel((PlanUpdate("investigate zebra evidence"),)))
    queries = []
    original_search = session._journal.search  # noqa: SLF001

    def search(session_id, query, *, filters=()):
        queries.append(query)
        return original_search(session_id, query, filters=filters)

    monkeypatch.setattr(session._journal, "search", search)  # noqa: SLF001
    session.step(ScriptedModel((LineageQuery(),)))

    assert any("investigate zebra evidence" in query for query in queries)


def test_pause_makes_no_model_call_or_proposal(dependencies):
    session, runtime, _evaluator = _session(dependencies)
    model = ScriptedModel(
        (
            ActionChoice("inspect", "before pause"),
            ActionChoice("inspect", "must not run"),
        )
    )
    session.step(model)
    assert model.calls == 1
    assert len(runtime.proposals) == 1
    session.enqueue(Pause("pause-1", "session-loop"))
    session.advance()
    assert session.view().status is SessionStatus.PAUSED
    session.step(model)
    assert model.calls == 1
    assert len(runtime.proposals) == 1


def test_resume_continues_without_duplicate_effect(dependencies):
    session, runtime, _evaluator = _session(dependencies)
    model = ScriptedModel(
        (
            ActionChoice("inspect", "first"),
            EvaluationChoice("after resume"),
        )
    )
    session.step(model)
    session.enqueue(Pause("pause-1", "session-loop"))
    session.advance()
    session.enqueue(Resume("resume-1", "session-loop"))
    session.advance()
    assert session.view().status is SessionStatus.RUNNING
    session.step(model)
    assert model.calls == 2
    assert len(runtime.proposals) == 1
    assert len(session.accepted_lineage()) == 2
    reopened = Session.open(
        "session-loop",
        journal=session._journal,  # noqa: SLF001
        artifacts=session._artifacts,  # noqa: SLF001
        environment=session._environment,  # noqa: SLF001
        runtime=runtime,
        evaluator=session._evaluator,  # noqa: SLF001
    )
    assert reopened.view() == session.view()
    assert [item.attempt.disposition.value for item in reopened.attempts()] == [
        history.attempt.disposition.value for history in session.attempts()
    ]


def test_mandatory_overflow_does_not_call_the_model(dependencies):
    session, runtime, _evaluator = _session(dependencies)
    model = ScriptedModel((LineageQuery(),))
    try:
        session.step(model, token_budget=1)
    except TransitionError as exc:
        assert "mandatory context" in str(exc)
    else:
        raise AssertionError("expected overflow")
    assert model.calls == 0
    assert len(runtime.proposals) == 0
    assert "context_overflow" in _kinds(session)


def test_memory_update_is_queryable_with_source_event(dependencies):
    session, _runtime, _evaluator = _session(dependencies)
    model = ScriptedModel(
        (
            MemoryUpdate(
                "h1",
                "the fixture is inspectable",
                "task",
                KnowledgeStatus.HYPOTHESIS,
                ("event-1",),
            ),
            KnowledgeQuery("h1"),
        )
    )
    session.step(model)
    session.step(model)
    items = knowledge_items(session._state, session.events(limit=1000))  # noqa: SLF001
    recorded = next(item for item in items if item.item_id == "h1")
    assert recorded.status is KnowledgeStatus.HYPOTHESIS
    assert recorded.source_event >= 1
    inspected = next(
        event
        for event in session.events(limit=1000)
        if event.kind == "knowledge_inspected"
    )
    assert inspected.payload["item_ids"] == ("h1",)


def test_fts_question_excerpt_enters_retrieved_band(dependencies):
    session, _runtime, _evaluator = _session(dependencies)
    model = ScriptedModel(
        (
            OperatorQuestion("inspect the fixture distinctive zebra question"),
            LineageQuery(),
        )
    )
    session.step(model)
    session.enqueue(Message("msg-answer", "session-loop", "continue inspecting"))
    session.advance()
    session.step(model)
    included = _manifests(session)[-1]["included"]
    assert any(
        item_id.startswith("fts:") and band == "retrieved" for item_id, band in included
    )


def test_operator_message_is_mandatory_context(dependencies):
    session, _runtime, _evaluator = _session(dependencies)
    session.enqueue(Message("msg-1", "session-loop", "steer toward inspect"))
    session.advance()
    model = ScriptedModel((LineageQuery(),))
    session.step(model)
    text = model.requests[0].context.text
    assert "steer toward inspect" in text
