"""Source-linked episodic compaction after a covering checkpoint."""

from __future__ import annotations

from dataclasses import replace

import pytest

from vharness.agent import (
    BudgetLimits,
    CompletionMode,
    LineageQuery,
    PlanUpdate,
    ScriptedModel,
    Session,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.context import assemble_context
from vharness.agent.errors import ContractError, IntegrityError, TransitionError
from vharness.agent.journal import SearchHit, SqliteJournal
from vharness.agent.memory import (
    DETERMINISTIC_EXCERPT_MODEL,
    covering_checkpoint,
    episodes,
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
    Steer,
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
    created = Session.create(
        "session-compact",
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
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    yield created
    journal.close()


def test_compact_after_covering_checkpoint_keeps_source_events(session):
    session.step(ScriptedModel((LineageQuery(),)))
    before = session.events(limit=1000)
    source_end = session.view().cursor
    session.checkpoint()
    episode = session.compact_episodes(1, source_end)
    assert episode.model_id == DETERMINISTIC_EXCERPT_MODEL
    assert episode.source_start == 1
    assert episode.source_end == source_end
    assert "objective inspect the fixture" in episode.summary
    after = session.events(limit=1000)
    sources = [event.event_id for event in after if event.sequence <= source_end]
    assert sources == [event.event_id for event in before]
    replayed = replay(after)
    assert replayed.lineage_head == session.view().lineage_head
    assert [item.episode_id for item in episodes(after)] == [episode.episode_id]


def test_missing_covering_checkpoint_is_rejected(session):
    session.step(ScriptedModel((LineageQuery(),)))
    with pytest.raises(TransitionError, match="covering checkpoint"):
        session.compact_episodes(1, session.view().cursor)


def test_checkpoint_at_or_before_source_end_does_not_cover(session):
    session.checkpoint()
    session.step(ScriptedModel((LineageQuery(),)))
    source_end = session.view().cursor
    covering = covering_checkpoint(session.events(limit=1000), source_end)
    assert covering is None
    with pytest.raises(TransitionError, match="covering checkpoint"):
        session.compact_episodes(1, source_end)


def test_duplicate_span_is_rejected(session):
    session.step(ScriptedModel((LineageQuery(),)))
    source_end = session.view().cursor
    session.checkpoint()
    session.compact_episodes(1, source_end)
    with pytest.raises(TransitionError, match="already compacted"):
        session.compact_episodes(1, source_end)


def test_invalid_span_is_a_contract_error(session):
    with pytest.raises(ContractError, match="source span"):
        session.compact_episodes(2, 1)


def test_reopen_rebuilds_episodes(session):
    session.step(ScriptedModel((LineageQuery(),)))
    source_end = session.view().cursor
    session.checkpoint()
    created = session.compact_episodes(1, source_end)
    reopened = Session.open(
        "session-compact",
        journal=session._journal,  # noqa: SLF001
        artifacts=session._artifacts,  # noqa: SLF001
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    rebuilt = episodes(reopened.events(limit=1000))
    assert rebuilt == (created,)


def test_compaction_uses_source_objective_after_steer(session):
    session.step(ScriptedModel((PlanUpdate("finish the original investigation"),)))
    source_end = session.view().cursor
    session.enqueue(
        Steer(
            "steer-compaction",
            "session-compact",
            "new objective",
            "new evidence",
        )
    )
    session.advance()
    session.checkpoint()

    episode = session.compact_episodes(1, source_end)

    assert "objective inspect the fixture" in episode.summary
    assert "objective new objective" not in episode.summary


def test_replay_validates_plan_recorded_payload(session):
    session.step(ScriptedModel((PlanUpdate("inspect then evaluate"),)))
    events = list(session.events(limit=1000))
    index = next(
        index for index, event in enumerate(events) if event.kind == "plan_recorded"
    )
    payload = dict(events[index].payload)
    payload.pop("source_event")
    events[index] = replace(events[index], payload=payload)

    with pytest.raises(IntegrityError, match="unexpected or missing"):
        replay(events)


def test_replay_validates_episode_recorded_payload(session):
    session.step(ScriptedModel((LineageQuery(),)))
    source_end = session.view().cursor
    session.checkpoint()
    session.compact_episodes(1, source_end)
    events = list(session.events(limit=1000))
    index = next(
        index for index, event in enumerate(events) if event.kind == "episode_recorded"
    )
    payload = dict(events[index].payload)
    episode = dict(payload["episode"])
    episode["source_event"] = events[index].sequence - 1
    payload["episode"] = episode
    events[index] = replace(events[index], payload=payload)

    with pytest.raises(IntegrityError, match="source span or source_event"):
        replay(events)


def _environment() -> EnvironmentContract:
    return FakeEnvironment().describe()


def test_context_prefers_source_excerpts_to_episode_summaries():
    from vharness.agent.transitions import SessionState
    from vharness.agent.models import CommittedNode, SessionStatus

    node = CommittedNode("root-1", 1, StateRef("fake", "initial", digest="0" * 64))
    state = SessionState(
        task=TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(),
            "fake",
            "fake-evaluator-v1",
        ),
        status=SessionStatus.RUNNING,
        lineage_head=node,
        accepted_lineage=(node,),
    )
    source = Event(
        "event-1",
        "session-1",
        1,
        "receipt_received",
        1,
        {"note": "inspect the fixture distinctive zebra"},
        "2026-09-14T00:00:00+00:00",
    )
    recorded = Event(
        "event-2",
        "session-1",
        2,
        "episode_recorded",
        1,
        {
            "episode": {
                "episode_id": "episode-1-1",
                "source_start": 1,
                "source_end": 1,
                "summary": "receipts succeeded in source span",
                "evidence_ids": (),
                "model_id": DETERMINISTIC_EXCERPT_MODEL,
                "model_version": "1",
                "source_event": 2,
            }
        },
        "2026-09-14T00:00:01+00:00",
    )
    hits = (
        SearchHit(
            "event-1",
            "session-1",
            1,
            "receipt_received",
            "inspect the fixture distinctive zebra",
            -3.0,
        ),
    )
    assembled = assemble_context(
        state,
        (source, recorded),
        _environment(),
        token_budget=400,
        hits=hits,
    )
    assert hasattr(assembled, "items")
    included = [item.item_id for item in assembled.items]
    assert "fts:event-1" in included
    assert "episode:episode-1-1" not in included
    assert ("episode:episode-1-1", "source_preferred") in assembled.manifest.omitted


def test_tight_retrieved_budget_may_include_episode_summary():
    from vharness.agent.transitions import SessionState
    from vharness.agent.models import CommittedNode, SessionStatus

    node = CommittedNode("root-1", 1, StateRef("fake", "initial", digest="0" * 64))
    state = SessionState(
        task=TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(),
            "fake",
            "fake-evaluator-v1",
        ),
        status=SessionStatus.RUNNING,
        lineage_head=node,
        accepted_lineage=(node,),
    )
    source = Event(
        "event-1",
        "session-1",
        1,
        "receipt_received",
        1,
        {"note": "inspect the fixture " + ("zebra " * 80)},
        "2026-09-14T00:00:00+00:00",
    )
    recorded = Event(
        "event-2",
        "session-1",
        2,
        "episode_recorded",
        1,
        {
            "episode": {
                "episode_id": "episode-1-1",
                "source_start": 1,
                "source_end": 1,
                "summary": "objective inspect the fixture",
                "evidence_ids": (),
                "model_id": DETERMINISTIC_EXCERPT_MODEL,
                "model_version": "1",
                "source_event": 2,
            }
        },
        "2026-09-14T00:00:01+00:00",
    )
    hits = (
        SearchHit(
            "event-1",
            "session-1",
            1,
            "receipt_received",
            "inspect the fixture " + ("zebra " * 80),
            -3.0,
        ),
    )
    assembled = assemble_context(
        state,
        (source, recorded),
        _environment(),
        token_budget=120,
        hits=hits,
    )
    included = [item.item_id for item in assembled.items]
    assert "episode:episode-1-1" in included
    assert "fts:event-1" not in included
    assert "sources=1-1" in next(
        item.text for item in assembled.items if item.item_id == "episode:episode-1-1"
    )
