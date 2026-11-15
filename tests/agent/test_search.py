"""FTS5 journal search and BM25-backed context ranking."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vharness.agent import (
    BudgetLimits,
    CompletionMode,
    KnowledgeSource,
    MemoryUpdate,
    RecordEnvelope,
    ScriptedModel,
    Session,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.context import COEFFICIENTS, assemble_context
from vharness.agent.journal import SearchFilter, SearchHit, SqliteJournal
from vharness.agent.memory import KnowledgeStatus
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


def _event(event_id: str, sequence: int, kind: str, payload: dict) -> Event:
    return Event(
        event_id,
        "session-search",
        sequence,
        kind,
        1,
        payload,
        "2026-09-12T00:00:00+00:00",
    )


def test_fts5_ranks_matching_payload_and_filters_by_kind(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.sqlite3")
    first = _event(
        "event-1",
        1,
        "knowledge_recorded",
        {"item": {"statement": "uniquealpha kernel tiling"}},
    )
    second = _event(
        "event-2",
        2,
        "receipt_received",
        {"receipt": {"note": "unrelated beta observation"}},
    )
    third = _event(
        "event-3",
        3,
        "knowledge_recorded",
        {"item": {"statement": "uniquealpha register pressure"}},
    )
    journal.append((first, second, third), expected_cursor=0)
    hits = journal.search("session-search", "uniquealpha")
    assert [hit.event_id for hit in hits] == ["event-1", "event-3"]
    assert hits[0].relevance >= hits[1].relevance
    assert hits[0].excerpt
    filtered = journal.search(
        "session-search",
        "uniquealpha",
        filters=SearchFilter(kinds=("receipt_received",)),
    )
    assert filtered == ()
    assert journal.search("session-search", "uniquealpha") == hits
    journal.close()


def test_empty_query_returns_no_hits(tmp_path):
    journal = SqliteJournal(tmp_path / "journal.sqlite3")
    journal.append(
        (_event("event-1", 1, "knowledge_recorded", {"item": {"statement": "alpha"}}),),
        expected_cursor=0,
    )
    assert journal.search("session-search", "") == ()
    assert journal.search("session-search", "   ") == ()
    journal.close()


def test_search_index_rebuilds_for_existing_events(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal = SqliteJournal(path)
    journal.append(
        (
            _event(
                "event-1",
                1,
                "knowledge_recorded",
                {"item": {"statement": "reindexme token"}},
            ),
        ),
        expected_cursor=0,
    )
    journal.close()
    reopened = SqliteJournal(path)
    hits = reopened.search("session-search", "reindexme")
    assert [hit.event_id for hit in hits] == ["event-1"]
    reopened.close()


def test_bm25_maps_to_nonnegative_relevance_using_overlap_coefficient():
    hit = SearchHit(
        "event-hot",
        "session-search",
        4,
        "knowledge_recorded",
        "uniqueomega evidence",
        -3.0,
    )
    assert COEFFICIENTS["overlap"] == 1.0
    assert hit.relevance == 3.0
    assert hit.relevance * COEFFICIENTS["overlap"] == 3.0


@pytest.fixture
def session(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    observed = datetime.now(timezone.utc).isoformat()
    created = Session.create(
        "session-search",
        TaskSpec(
            "task-1",
            "uniqueomega kernel work",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=4, evaluations=2, model_calls=8),
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
                    "kb-docs", "session-search", 1, observed, "hardware notes"
                ),
            ),
        ),
    )
    yield created
    journal.close()


def test_session_step_feeds_fts_hits_into_context(session):
    model = ScriptedModel(
        (
            MemoryUpdate(
                "h1",
                "uniqueomega tiling discovered",
                "kernel",
                KnowledgeStatus.HYPOTHESIS,
                ("kb-docs",),
            ),
        )
    )
    session.step(model)
    hits = session._journal.search(  # noqa: SLF001
        "session-search",
        "uniqueomega",
        filters=SearchFilter(kinds=("knowledge_recorded",)),
    )
    assert hits
    assert any("uniqueomega" in hit.excerpt.lower() for hit in hits)
    bundle = assemble_context(
        session._state,  # noqa: SLF001
        session.events(limit=1000),
        FakeEnvironment().describe(),
        token_budget=256,
        hits=hits,
    )
    assert bundle.manifest.coefficients["overlap"] == 1.0
    included = [item_id for item_id, _band in bundle.manifest.included]
    assert any(
        item_id.startswith("knowledge:") or item_id.startswith("fts:")
        for item_id in included
    )
