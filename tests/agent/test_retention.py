"""Selective artifact retention with protected roots and journaled eviction."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vharness.agent import (
    BudgetLimits,
    CompletionMode,
    KnowledgeSource,
    RecordEnvelope,
    Session,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import ContractError, IntegrityError
from vharness.agent.journal import SqliteJournal
from vharness.agent.memory import (
    ProgressCategory,
    ProgressStatus,
    protected_digests,
    retention_records,
    select_evictions,
)
from vharness.agent.models import (
    ActionDefinition,
    ArtifactRef,
    ComponentIdentity,
    CommittedNode,
    EnvironmentContract,
    EvaluationReceipt,
    Event,
    ExecutionReceipt,
    ExecutionStatus,
    RuntimeCapabilities,
    SessionStatus,
    StateRef,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.transitions import SessionState


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


def test_select_evictions_drops_largest_unprotected_first():
    protected = frozenset({"p" * 64})
    inventory = (
        ("p" * 64, 50),
        ("b" * 64, 40),
        ("a" * 64, 40),
        ("c" * 64, 10),
    )
    assert select_evictions(inventory, protected, 55) == (
        "a" * 64,
        "b" * 64,
        "c" * 64,
    )
    assert select_evictions(inventory, protected, 100) == ("a" * 64,)
    assert select_evictions(inventory, protected, 140) == ()
    assert select_evictions(inventory, protected, 0) == (
        "a" * 64,
        "b" * 64,
        "c" * 64,
    )
    assert select_evictions((("p" * 64, 100),), protected, 0) == ()
    with pytest.raises(ContractError, match="byte_budget"):
        select_evictions(inventory, protected, -1)


def test_failed_receipt_artifact_is_not_protected_without_a_claim():
    optional = ArtifactRef("a" * 64, 8, "text/plain", "failed-attempt")
    required = ArtifactRef("b" * 64, 4, "text/plain", "knowledge")
    node = CommittedNode("root-1", 1, StateRef("fake", "initial", digest="0" * 64))
    source = KnowledgeSource(
        "kb-docs",
        "notes",
        "2026-09-12T00:00:00+00:00",
        RecordEnvelope("kb-docs", "session-1", 1, "2026-09-12T00:00:00+00:00", "notes"),
        artifact=required,
    )
    state = SessionState(
        task=TaskSpec(
            "task-1",
            "inspect",
            CompletionMode.FINITE,
            "evidence",
            BudgetLimits(),
            "fake",
            "eval",
        ),
        status=SessionStatus.RUNNING,
        lineage_head=node,
        knowledge_sources=(source,),
        accepted_lineage=(node,),
    )
    events = (
        Event(
            "event-1",
            "session-1",
            1,
            "receipt_received",
            1,
            {
                "receipt": {
                    "raw_artifact": {
                        "digest": optional.digest,
                        "size": optional.size,
                        "media_type": optional.media_type,
                        "provenance": optional.provenance,
                    }
                }
            },
            "2026-09-12T00:00:00+00:00",
        ),
        Event(
            "event-2",
            "session-1",
            2,
            "progress_claim_recorded",
            1,
            {
                "claim": {
                    "claim_id": "c1",
                    "category": ProgressCategory.KNOWLEDGE.value,
                    "before": "unknown",
                    "after": "known",
                    "scope": "task",
                    "evidence_ids": (required.digest,),
                    "assessor": "agent",
                    "status": ProgressStatus.SUPPORTED.value,
                    "source_event": 2,
                }
            },
            "2026-09-12T00:00:00+00:00",
        ),
    )
    protected = protected_digests(state, events)
    assert required.digest in protected
    assert optional.digest not in protected
    rejected_event = Event(
        "event-3",
        "session-1",
        3,
        "progress_claim_rejected",
        1,
        {
            "claim": {
                "claim_id": "c2",
                "category": ProgressCategory.KNOWLEDGE.value,
                "before": "a",
                "after": "b",
                "scope": "scope",
                "evidence_ids": (optional.digest,),
                "assessor": "agent",
                "status": ProgressStatus.REJECTED.value,
                "source_event": 3,
            }
        },
        "2026-09-12T00:00:00+00:00",
    )
    assert optional.digest not in protected_digests(state, events + (rejected_event,))


def test_claim_citing_receipt_id_protects_receipt_artifact():
    optional = ArtifactRef("a" * 64, 8, "text/plain", "failed-attempt")
    node = CommittedNode("root-1", 1, StateRef("fake", "initial", digest="0" * 64))
    state = SessionState(
        task=TaskSpec(
            "task-1",
            "inspect",
            CompletionMode.FINITE,
            "evidence",
            BudgetLimits(),
            "fake",
            "eval",
        ),
        status=SessionStatus.RUNNING,
        lineage_head=node,
        accepted_lineage=(node,),
    )
    events = (
        Event(
            "event-1",
            "session-1",
            1,
            "receipt_received",
            1,
            {
                "receipt": {
                    "receipt_id": "receipt-1",
                    "raw_artifact": {
                        "digest": optional.digest,
                        "size": optional.size,
                        "media_type": optional.media_type,
                        "provenance": optional.provenance,
                    },
                }
            },
            "2026-09-12T00:00:00+00:00",
        ),
        Event(
            "event-2",
            "session-1",
            2,
            "progress_claim_recorded",
            1,
            {
                "claim": {
                    "claim_id": "c1",
                    "category": ProgressCategory.KNOWLEDGE.value,
                    "before": "unknown",
                    "after": "observed",
                    "scope": "task",
                    "evidence_ids": ("receipt-1",),
                    "assessor": "agent",
                    "status": ProgressStatus.PENDING.value,
                    "source_event": 2,
                }
            },
            "2026-09-12T00:00:00+00:00",
        ),
    )
    assert optional.digest in protected_digests(state, events)


def test_session_evicts_optional_bytes_and_keeps_protected_lesson(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    store = ArtifactStore(tmp_path / "artifacts")
    observed = datetime.now(timezone.utc).isoformat()
    required = store.put(b"keep-protected", media_type="text/plain", provenance="kb")
    optional = store.put(b"x" * 4000, media_type="text/plain", provenance="failed")
    session = Session.create(
        "session-retain",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=4, evaluations=2, model_calls=8),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=store,
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
        knowledge_sources=(
            KnowledgeSource(
                "kb-docs",
                "hardware notes",
                observed,
                RecordEnvelope(
                    "kb-docs", "session-retain", 1, observed, "hardware notes"
                ),
                artifact=required,
            ),
        ),
    )
    result = session.enforce_retention(required.size + 16)
    assert optional.digest in result.evicted
    assert required.digest in result.protected
    assert optional.digest not in dict(store.inventory())
    assert store.read(required) == b"keep-protected"
    with pytest.raises(IntegrityError, match="missing"):
        store.read(optional)
    kinds = [event.kind for event in session.events(limit=1000)]
    assert kinds.count("artifact_eviction_intended") == 1
    assert kinds.count("artifact_evicted") == 1
    records = retention_records(session.events(limit=1000))
    assert records[-1].digest == optional.digest
    assert records[-1].available is False
    reopened = Session.open(
        "session-retain",
        journal=journal,
        artifacts=store,
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    assert reopened.view().task.task_id == "task-1"
    assert any(
        event.kind == "artifact_evicted" for event in reopened.events(limit=1000)
    )
    journal.close()


def test_interrupted_eviction_intent_is_reconciled(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    store = ArtifactStore(tmp_path / "artifacts")
    optional = store.put(b"y" * 200, media_type="text/plain", provenance="failed")
    session = Session.create(
        "session-retain",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=store,
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
        knowledge_sources=(),
    )
    session._persist(  # noqa: SLF001
        "artifact_eviction_intended",
        session._state,  # noqa: SLF001
        {
            "digest": optional.digest,
            "size": optional.size,
            "reason": "byte_budget",
            "byte_budget": 0,
        },
    )
    assert store.read(optional) == b"y" * 200
    result = session.enforce_retention(10_000)
    assert optional.digest not in dict(store.inventory())
    assert any(
        event.kind == "artifact_evicted"
        and event.payload.get("digest") == optional.digest
        for event in session.events(limit=1000)
    )
    assert optional.digest not in result.evicted
    journal.close()


def test_two_sessions_share_one_required_artifact(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    store = ArtifactStore(tmp_path / "artifacts")
    observed = datetime.now(timezone.utc).isoformat()
    required = store.put(b"shared-keep", media_type="text/plain", provenance="kb")
    optional = store.put(b"z" * 4000, media_type="text/plain", provenance="failed")
    source = KnowledgeSource(
        "kb-docs",
        "hardware notes",
        observed,
        RecordEnvelope("kb-docs", "session-b", 1, observed, "hardware notes"),
        artifact=required,
    )
    session_b = Session.create(
        "session-b",
        TaskSpec(
            "task-b",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=store,
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
        knowledge_sources=(source,),
    )
    session_a = Session.create(
        "session-a",
        TaskSpec(
            "task-a",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=store,
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
        knowledge_sources=(),
    )
    result = session_a.enforce_retention(0)
    assert required.digest in result.protected
    assert required.digest not in result.evicted
    assert optional.digest in result.evicted
    assert required.digest in dict(store.inventory())
    assert store.read(required) == b"shared-keep"
    assert session_b.view().task.task_id == "task-b"
    with pytest.raises(IntegrityError, match="missing"):
        store.read(optional)
    journal.close()


def test_two_journals_share_one_required_artifact(tmp_path):
    journal_a = SqliteJournal(tmp_path / "a.sqlite3")
    journal_b = SqliteJournal(tmp_path / "b.sqlite3")
    store = ArtifactStore(tmp_path / "artifacts")
    observed = datetime.now(timezone.utc).isoformat()
    required = store.put(b"shared-keep", media_type="text/plain", provenance="kb")
    optional = store.put(b"z" * 4000, media_type="text/plain", provenance="failed")
    source = KnowledgeSource(
        "kb-docs",
        "hardware notes",
        observed,
        RecordEnvelope("kb-docs", "session-b", 1, observed, "hardware notes"),
        artifact=required,
    )
    session_b = Session.create(
        "session-b",
        TaskSpec(
            "task-b",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal_b,
        artifacts=store,
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
        knowledge_sources=(source,),
    )
    session_a = Session.create(
        "session-a",
        TaskSpec(
            "task-a",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal_a,
        artifacts=store,
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
        knowledge_sources=(),
    )
    result = session_a.enforce_retention(0)
    assert required.digest in result.protected
    assert required.digest not in result.evicted
    assert store.read(required) == b"shared-keep"
    assert session_b.view().task.task_id == "task-b"
    with pytest.raises(IntegrityError, match="missing"):
        store.read(optional)
    journal_a.close()
    journal_b.close()


@pytest.mark.parametrize(
    "field, value",
    [
        ("digest", "not-a-digest"),
        ("size", True),
        ("available", "false"),
    ],
)
def test_retention_projection_rejects_malformed_canonical_outcomes(field, value):
    payload = {
        "digest": "a" * 64,
        "size": 10,
        "reason": "byte_budget",
        "byte_budget": 0,
        "available": False,
        "dropped": True,
    }
    payload[field] = value
    event = Event(
        "retention-event",
        "session-retention",
        1,
        "artifact_evicted",
        1,
        payload,
        "2026-01-01T00:00:00+00:00",
    )

    with pytest.raises(IntegrityError, match="malformed artifact eviction"):
        retention_records((event,))
