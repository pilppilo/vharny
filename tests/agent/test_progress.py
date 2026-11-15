"""Investigation grouping and progress-claim projections."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Mapping, cast

import pytest

from vharness.agent import (
    ActionChoice,
    BudgetLimits,
    CompletionMode,
    InvestigationChange,
    KnowledgeQuery,
    LineageQuery,
    KnowledgeSource,
    MemoryUpdate,
    ProgressClaimChange,
    RecordEnvelope,
    ScriptedModel,
    Session,
    TaskSpec,
)
from vharness.agent.errors import ContractError
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import IntegrityError
from vharness.agent.journal import SqliteJournal
from vharness.agent.memory import (
    InvestigationDisposition,
    KnowledgeStatus,
    ProgressCategory,
    ProgressStatus,
    investigations,
    knowledge_items,
    productive_progress,
    progress_claims,
    recovery_views,
    unique_usage_ids,
)
from vharness.agent.models import (
    ActionDefinition,
    ComponentIdentity,
    EnvironmentContract,
    EvaluationReceipt,
    Event,
    ExecutionReceipt,
    ExecutionStatus,
    RuntimeCapabilities,
    StateRef,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.replay import replay


class FakeEnvironment:
    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="fake",
            version="1",
            initial_state=StateRef("fake", "initial", digest="0" * 64),
            actions=(ActionDefinition("inspect", "Inspect current state"),),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class FakeRuntime:
    def identity(self):
        return ComponentIdentity("fake-runtime", "v1")

    def submit(self, proposal):
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef("fake", "after", digest="1" * 64),
        )

    def reconcile(self, operation_id):
        del operation_id
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class FakeEvaluator:
    def identity(self):
        return ComponentIdentity("fake-evaluator", "v1")

    def evaluate(self, request):
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
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


@pytest.fixture
def session(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    observed = datetime.now(timezone.utc).isoformat()
    created = Session.create(
        "session-progress",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=4, evaluations=2, model_calls=12),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
        knowledge_sources=(
            KnowledgeSource(
                "kb-docs",
                "hardware notes",
                observed,
                RecordEnvelope(
                    "kb-docs", "session-progress", 1, observed, "hardware notes"
                ),
            ),
        ),
    )
    yield created
    journal.close()


def test_overlapping_investigations_dedupe_shared_usage(session):
    model = ScriptedModel(
        (
            InvestigationChange(
                "inv-a",
                "how does inspect work",
                usage_ids=("op-1", "op-2"),
            ),
            InvestigationChange(
                "inv-b",
                "what does inspect return",
                usage_ids=("op-2", "op-3"),
            ),
        )
    )
    session.step(model)
    session.step(model)
    groups = investigations(session.events(limit=1000))
    assert {item.investigation_id for item in groups} == {"inv-a", "inv-b"}
    assert unique_usage_ids(groups) == ("op-1", "op-2", "op-3")


def test_replay_rejects_malformed_projection_identity_payloads(session):
    model = ScriptedModel(
        (
            MemoryUpdate("h1", "inspect is legal", "task", KnowledgeStatus.HYPOTHESIS),
            KnowledgeQuery(),
            InvestigationChange("inv-a", "how does inspect work"),
            ProgressClaimChange(
                "claim-a", ProgressCategory.OUTCOME, "before", "after", "task"
            ),
            LineageQuery(),
        )
    )
    for _ in range(5):
        session.step(model)
    events = list(session.events(limit=1000))
    cases = {
        "knowledge_recorded": lambda payload: _with_nested_field(
            payload, "item", "source_event", 0
        ),
        "knowledge_inspected": lambda payload: _with_nested_item_field(
            payload, "status", "invalid"
        ),
        "investigation_recorded": lambda payload: _with_nested_field(
            payload, "investigation", "source_event", 0
        ),
        "progress_claim_recorded": lambda payload: _with_nested_field(
            payload, "claim", "source_event", 0
        ),
        "lineage_inspected": lambda payload: {**payload, "node_ids": ()},
    }
    for kind, mutate in cases.items():
        index = next(index for index, event in enumerate(events) if event.kind == kind)
        event = events[index]
        corrupted = replace(event, payload=mutate(dict(event.payload)))
        candidate = [*events]
        candidate[index] = corrupted
        with pytest.raises(IntegrityError):
            replay(candidate)


def _with_nested_field(
    payload: dict[str, object], parent: str, field: str, value: object
) -> dict[str, object]:
    nested = dict(cast(Mapping[str, object], payload[parent]))
    nested[field] = value
    payload[parent] = nested
    return payload


def _with_nested_item_field(
    payload: dict[str, object], field: str, value: object
) -> dict[str, object]:
    items = [
        dict(cast(Mapping[str, object], item))
        for item in cast(tuple[object, ...], payload["items"])
    ]
    items[0][field] = value
    payload["items"] = items
    return payload


def test_investigation_can_be_abandoned_and_reopened(session):
    model = ScriptedModel(
        (
            InvestigationChange("inv-a", "open question"),
            InvestigationChange(
                "inv-a",
                "open question",
                InvestigationDisposition.ABANDONED,
            ),
            InvestigationChange(
                "inv-a",
                "open question again",
                InvestigationDisposition.ACTIVE,
                hypotheses=("maybe inspect is a no-op",),
            ),
        )
    )
    session.step(model)
    session.step(model)
    session.step(model)
    groups = investigations(session.events(limit=1000))
    assert len(groups) == 1
    assert groups[0].disposition is InvestigationDisposition.ACTIVE
    assert groups[0].hypotheses == ("maybe inspect is a no-op",)
    assert groups[0].question == "open question again"


def test_unevidenced_and_duplicate_claims_are_not_productive(session):
    model = ScriptedModel(
        (
            ProgressClaimChange(
                "c1",
                ProgressCategory.KNOWLEDGE,
                "unknown",
                "unknown",
                "rules",
            ),
            ProgressClaimChange(
                "c2",
                ProgressCategory.KNOWLEDGE,
                "unknown",
                "unknown",
                "rules",
                evidence_ids=("kb-docs",),
            ),
        )
    )
    session.step(model)
    session.step(model)
    claims = progress_claims(session.events(limit=1000))
    assert len(claims) == 1
    assert claims[0].status is ProgressStatus.PENDING
    assert productive_progress(claims) == ()


def test_memory_update_cannot_self_declare_supported(session):
    model = ScriptedModel(
        (
            MemoryUpdate(
                "h1",
                "inspect is legal",
                "task",
                KnowledgeStatus.SUPPORTED,
            ),
        )
    )
    session.step(model)
    items = knowledge_items(session._state, session.events(limit=1000))  # noqa: SLF001
    recorded = next(item for item in items if item.item_id == "h1")
    assert recorded.status is KnowledgeStatus.HYPOTHESIS


def test_hypothesis_is_not_validated_evidence_for_a_claim(session):
    model = ScriptedModel(
        (
            MemoryUpdate(
                "h1",
                "inspect is legal",
                "task",
                KnowledgeStatus.HYPOTHESIS,
                ("kb-docs",),
            ),
            ProgressClaimChange(
                "c1",
                ProgressCategory.KNOWLEDGE,
                "unknown action",
                "inspect is legal",
                "actions",
                evidence_ids=("h1",),
            ),
        )
    )
    session.step(model)
    session.step(model)
    claims = progress_claims(session.events(limit=1000))
    assert len(claims) == 1
    assert claims[0].status is ProgressStatus.PENDING
    assert productive_progress(claims) == ()


def test_pending_claim_upgrades_when_validated_evidence_arrives(session):
    model = ScriptedModel(
        (
            ProgressClaimChange(
                "c1",
                ProgressCategory.KNOWLEDGE,
                "unknown action",
                "inspect is legal",
                "actions",
            ),
            MemoryUpdate(
                "h1",
                "inspect is legal",
                "task",
                KnowledgeStatus.SUPPORTED,
                ("kb-docs",),
            ),
            ProgressClaimChange(
                "c1",
                ProgressCategory.KNOWLEDGE,
                "unknown action",
                "inspect is legal",
                "actions",
                evidence_ids=("h1",),
            ),
        )
    )
    session.step(model)
    session.step(model)
    session.step(model)
    items = knowledge_items(session._state, session.events(limit=1000))  # noqa: SLF001
    assert next(item for item in items if item.item_id == "h1").status is (
        KnowledgeStatus.SUPPORTED
    )
    claims = progress_claims(session.events(limit=1000))
    assert len(claims) == 1
    assert claims[0].status is ProgressStatus.SUPPORTED
    assert productive_progress(claims) == claims


def test_supported_knowledge_claim_requires_real_evidence_and_change(session):
    model = ScriptedModel(
        (
            MemoryUpdate(
                "h1",
                "inspect is legal",
                "task",
                KnowledgeStatus.SUPPORTED,
                ("kb-docs",),
            ),
            ProgressClaimChange(
                "c1",
                ProgressCategory.KNOWLEDGE,
                "unknown action",
                "inspect is legal",
                "actions",
                evidence_ids=("h1",),
            ),
        )
    )
    session.step(model)
    session.step(model)
    claims = progress_claims(session.events(limit=1000))
    assert len(claims) == 1
    assert claims[0].status is ProgressStatus.SUPPORTED
    assert productive_progress(claims) == claims


def test_unrelated_supported_knowledge_item_does_not_support_claim(session):
    model = ScriptedModel(
        (
            MemoryUpdate(
                "unrelated",
                "the fixture is blue",
                "appearance",
                KnowledgeStatus.SUPPORTED,
                ("kb-docs",),
            ),
            ProgressClaimChange(
                "c1",
                ProgressCategory.KNOWLEDGE,
                "unknown action",
                "inspect is legal",
                "actions",
                evidence_ids=("unrelated",),
            ),
        )
    )
    session.step(model)
    session.step(model)
    claim = progress_claims(session.events(limit=1000))[0]
    assert claim.status is ProgressStatus.PENDING
    assessment = next(
        event.payload["assessment"]
        for event in session.events(limit=1000)
        if event.kind == "progress_claim_recorded"
    )
    assert assessment["before"] == "unknown action"
    assert assessment["after"] == "inspect is legal"
    assert assessment["scope"] == "actions"
    assert assessment["assessor"] == "agent"
    assert assessment["status"] == "pending"


def test_successful_runtime_receipt_does_not_support_knowledge_claim(
    session,
):
    model = ScriptedModel(
        (
            # The receipt ID is known after this action, but it is not
            # knowledge evidence even when the action succeeds.
            ActionChoice("inspect", "inspect"),
            ProgressClaimChange(
                "c1",
                ProgressCategory.KNOWLEDGE,
                "unknown action",
                "inspect is legal",
                "actions",
                evidence_ids=("receipt-placeholder",),
            ),
        )
    )
    session.step(model)
    receipt_id = session.events(limit=1000)[-1].payload["receipt"]["receipt_id"]
    proposal_id = session._state.action_operations[0].proposal_id  # noqa: SLF001
    session.receive_receipt(
        ExecutionReceipt(
            "failed-receipt",
            proposal_id,
            1,
            ExecutionStatus.FAILED,
            error="late contradictory failure",
        )
    )
    events = session.events(limit=1000)
    claim_model = ScriptedModel(
        (
            ProgressClaimChange(
                "c1",
                ProgressCategory.KNOWLEDGE,
                "unknown action",
                "inspect is legal",
                "actions",
                evidence_ids=(receipt_id, "failed-receipt"),
            ),
        )
    )
    # Use the same durable session; the first model's unused result is
    # intentionally irrelevant to the evidence assessment.
    del events
    session.step(claim_model)
    claim = progress_claims(session.events(limit=1000))[-1]
    assert claim.status is ProgressStatus.PENDING


def test_outcome_claim_is_not_supported_by_knowledge_source_id(session):
    model = ScriptedModel(
        (
            ProgressClaimChange(
                "c1",
                ProgressCategory.OUTCOME,
                "failing check",
                "passing check",
                "workspace",
                evidence_ids=("kb-docs",),
            ),
        )
    )
    session.step(model)
    claims = progress_claims(session.events(limit=1000))
    assert claims[0].status is ProgressStatus.PENDING
    assert productive_progress(claims) == ()


def test_duplicate_supported_claim_is_rejected(session):
    model = ScriptedModel(
        (
            MemoryUpdate(
                "h1",
                "inspect is legal",
                "task",
                KnowledgeStatus.SUPPORTED,
                ("kb-docs",),
            ),
            ProgressClaimChange(
                "c1",
                ProgressCategory.KNOWLEDGE,
                "unknown action",
                "inspect is legal",
                "actions",
                evidence_ids=("h1",),
            ),
            ProgressClaimChange(
                "c2",
                ProgressCategory.KNOWLEDGE,
                "unknown action",
                "inspect is legal",
                "actions",
                evidence_ids=("h1",),
            ),
        )
    )
    session.step(model)
    session.step(model)
    session.step(model)
    claims = progress_claims(session.events(limit=1000))
    assert len(claims) == 1
    assert claims[0].status is ProgressStatus.SUPPORTED
    kinds = [event.kind for event in session.events(limit=1000)]
    assert "progress_claim_rejected" in kinds


def test_recovery_views_are_not_productive_progress():
    event = Event(
        "event-1",
        "session-progress",
        1,
        "operation_reconciled",
        1,
        {
            "operation_id": "proposal-1",
            "operation_kind": "action",
            "result": "unknown",
            "receipt": None,
        },
        "2026-09-12T00:00:00+00:00",
    )
    views = recovery_views((event,))
    assert len(views) == 1
    assert views[0].operation_id == "proposal-1"


def test_empty_assessor_is_rejected_before_evidence_evaluation(session):
    with pytest.raises(ContractError, match="assessor must be non-empty"):
        ProgressClaimChange(
            "c1",
            ProgressCategory.KNOWLEDGE,
            "unknown action",
            "inspect is legal",
            "actions",
            evidence_ids=(),
            assessor="",
        )
    assert productive_progress(()) == ()
