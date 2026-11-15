"""Rebuildable working, knowledge, attempt, and lineage views over events."""

# pylint: disable=too-many-instance-attributes,duplicate-code,too-many-lines
# Working memory names each mandatory context field rather than a blob.
# Projection records mirror kernel write-side types without sharing a module.

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from .codec import canonical_json, parse_json
from .errors import ContractError, IntegrityError
from .models import (
    AttemptHistory,
    CommittedNode,
    EvaluationReceipt,
    Event,
    ExecutionReceipt,
    JsonValue,
    KnowledgeSource,
    SessionStatus,
    TaskSpec,
    frozen_mapping,
)
from .transitions import ActionOperation, EvaluationOperation, SessionState


class KnowledgeStatus(str, Enum):
    """Evidence-backed state of one knowledge item. Confidence cannot promote it."""

    HYPOTHESIS = "hypothesis"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    """One knowledge record with provenance back to a source event."""

    item_id: str
    statement: str
    scope: str
    status: KnowledgeStatus
    evidence_ids: tuple[str, ...]
    provenance: str
    source_event: int

    def __post_init__(self) -> None:
        for name, value in (
            ("item_id", self.item_id),
            ("statement", self.statement),
            ("scope", self.scope),
            ("provenance", self.provenance),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"knowledge {name} must be a non-empty string")
        if self.source_event < 1:
            raise ContractError("knowledge source_event must be positive")


@dataclass(frozen=True, slots=True)
class AttemptEvidenceSource:
    """Concise evidence excerpt anchored to one immutable journal event."""

    event_id: str
    sequence: int
    kind: str
    summary: str


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    """Bounded, source-linked view of one completed attempt."""

    attempt_id: str
    found: bool
    disposition: str | None
    base_node_id: str | None
    source_start: int
    source_end: int
    sources: tuple[AttemptEvidenceSource, ...]


_ATTEMPT_EVIDENCE_KINDS = {
    "action_intended",
    "receipt_received",
    "evaluation_intended",
    "evaluation_received",
    "completion_rejected",
}
_MAX_ATTEMPT_EVIDENCE_SOURCES = 24
_MAX_ATTEMPT_EVIDENCE_SUMMARY = 240


def missing_attempt_evidence(attempt_id: str) -> AttemptEvidence:
    """Return a durable empty result when a requested attempt does not exist."""
    return AttemptEvidence(attempt_id, False, None, None, 0, 0, ())


def attempt_evidence(
    history: AttemptHistory, events: tuple[Event, ...]
) -> AttemptEvidence:
    """Project action and evaluation evidence for a single attempt."""
    end = history.event_end_cursor
    if end is None:
        raise ContractError("attempt evidence is available only after closure")
    span = tuple(
        event
        for event in events
        if history.event_start_cursor <= event.sequence <= end
        and event.kind in _ATTEMPT_EVIDENCE_KINDS
    )
    sources = tuple(
        AttemptEvidenceSource(
            event_id=event.event_id,
            sequence=event.sequence,
            kind=event.kind,
            summary=_attempt_event_summary(event)[:_MAX_ATTEMPT_EVIDENCE_SUMMARY],
        )
        for event in span[-_MAX_ATTEMPT_EVIDENCE_SOURCES:]
    )
    return AttemptEvidence(
        attempt_id=history.attempt.attempt_id,
        found=True,
        disposition=history.attempt.disposition.value,
        base_node_id=history.attempt.base_node_id,
        source_start=history.event_start_cursor,
        source_end=end,
        sources=sources,
    )


def _attempt_event_summary(event: Event) -> str:
    """Render only decision-relevant fields from an attempt event."""
    payload = event.payload
    if event.kind == "action_intended":
        proposal = payload.get("proposal")
        return _summary_fields(
            "action",
            proposal,
            ("action_name", "proposal_id", "rationale", "arguments"),
        )
    if event.kind == "receipt_received":
        receipt = payload.get("receipt")
        return _summary_fields(
            "runtime receipt",
            receipt,
            ("receipt_id", "proposal_id", "status", "state", "error"),
        )
    if event.kind == "evaluation_intended":
        request = payload.get("request")
        return _summary_fields(
            "evaluation request",
            request,
            ("request_id", "attempt_id", "evaluated_state", "baseline_node_id"),
        )
    if event.kind == "evaluation_received":
        receipt = payload.get("receipt")
        return _summary_fields(
            "evaluation receipt",
            receipt,
            (
                "receipt_id",
                "request_id",
                "accepted",
                "comparison",
                "objectives",
                "evaluated_state",
            ),
        )
    return f"completion rejected reason={payload.get('reason', 'unknown')}"


def _summary_fields(label: str, value: object, fields: tuple[str, ...]) -> str:
    if not isinstance(value, Mapping):
        return label
    rendered = [f"{name}={value[name]}" for name in fields if name in value]
    return f"{label} " + " ".join(rendered)


@dataclass(frozen=True, slots=True)
class WorkingMemory:
    """Values the next decision is required to see before retrieval."""

    task: TaskSpec
    status: SessionStatus
    lineage_head: CommittedNode
    active_attempt_id: str | None
    pending_proposal_id: str | None
    wait_reason: str | None
    current_plan: str
    operator_direction: str | None
    latest_receipt_id: str | None
    latest_receipt: Mapping[str, JsonValue]
    observation: Mapping[str, JsonValue]
    remaining_budgets: Mapping[str, JsonValue]
    budgets: str


def working_memory(state: SessionState, events: tuple[Event, ...]) -> WorkingMemory:
    """Project mandatory working fields from scheduling state and the journal."""
    receipt = latest_receipt_payload(events)
    remaining = remaining_budgets(state, events)
    observation = observation_payload(state, events)
    return WorkingMemory(
        task=state.task,
        status=state.status,
        lineage_head=state.lineage_head,
        active_attempt_id=(
            None if state.active_attempt is None else state.active_attempt.attempt_id
        ),
        pending_proposal_id=state.pending_proposal_id,
        wait_reason=state.wait_reason,
        current_plan=current_plan_text(events, state.task.objective_version),
        operator_direction=_latest_operator_direction(events),
        latest_receipt_id=_latest_receipt_id(events),
        latest_receipt=receipt,
        observation=observation,
        remaining_budgets=remaining,
        budgets=_budget_text(remaining),
    )


def knowledge_items(
    state: SessionState, events: tuple[Event, ...]
) -> tuple[KnowledgeItem, ...]:
    """Rebuild knowledge from creation sources and explicit recorded transitions."""
    items = {
        source.source_id: _item_from_source(source, events)
        for source in state.knowledge_sources
    }
    for event in events:
        if event.kind != "knowledge_recorded":
            continue
        payload = event.payload.get("item")
        if not isinstance(payload, Mapping):
            continue
        item = _item_from_payload(payload, event.sequence)
        items[item.item_id] = item
    return tuple(
        sorted(items.values(), key=lambda item: (item.source_event, item.item_id))
    )


def attempt_views(state: SessionState) -> tuple[AttemptHistory, ...]:
    """Return every attempt, including unsuccessful trajectories."""
    return state.attempt_history


def lineage_nodes(state: SessionState) -> tuple[CommittedNode, ...]:
    """Return only externally accepted committed states."""
    return state.accepted_lineage


DETERMINISTIC_EXCERPT_MODEL = "deterministic-excerpt-v1"
DETERMINISTIC_EXCERPT_VERSION = "1"


@dataclass(frozen=True, slots=True)
class Episode:
    """A source-linked episodic summary. Source events remain durable."""

    episode_id: str
    source_start: int
    source_end: int
    summary: str
    evidence_ids: tuple[str, ...]
    model_id: str
    model_version: str
    source_event: int

    def __post_init__(self) -> None:
        for name, value in (
            ("episode_id", self.episode_id),
            ("summary", self.summary),
            ("model_id", self.model_id),
            ("model_version", self.model_version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"episode {name} must be a non-empty string")
        if self.source_start < 1 or self.source_end < self.source_start:
            raise ContractError("episode source span is invalid")
        if self.source_event < 1:
            raise ContractError("episode source_event must be positive")


def episodes(events: tuple[Event, ...]) -> tuple[Episode, ...]:
    """Rebuild episodic summaries from episode_recorded facts."""
    items: list[Episode] = []
    for event in events:
        if event.kind != "episode_recorded":
            continue
        payload = event.payload.get("episode")
        if not isinstance(payload, Mapping):
            continue
        items.append(_episode_from_payload(payload, event.sequence))
    return tuple(items)


def covering_checkpoint(events: tuple[Event, ...], source_end: int) -> Event | None:
    """Return a checkpoint after the span that also covers its cursor."""
    for event in events:
        if event.kind != "checkpoint" or event.sequence <= source_end:
            continue
        checkpoint = event.payload.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            continue
        cursor = checkpoint.get("cursor")
        if isinstance(cursor, bool) or not isinstance(cursor, int):
            continue
        if cursor >= source_end:
            return event
    return None


def excerpt_episode(objective: str, events: tuple[Event, ...]) -> str:
    """Deterministic source-linked excerpt. Not a learned or live-model summary."""
    attempts, actions, receipts, evaluations = _span_outcomes(events)
    evidence = episode_evidence_ids(events)
    parts = [f"objective {objective}"]
    if attempts:
        parts.append("attempts " + ",".join(attempts))
    if actions:
        parts.append("actions " + ",".join(actions))
    if receipts:
        parts.append("receipts " + ",".join(receipts))
    if evaluations:
        parts.append("evaluations " + ",".join(evaluations))
    if evidence:
        parts.append("evidence " + ",".join(evidence))
    return " | ".join(parts)


def _span_outcomes(
    events: tuple[Event, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    attempts: list[str] = []
    actions: list[str] = []
    receipts: list[str] = []
    evaluations: list[str] = []
    for event in events:
        if event.kind == "attempt_started":
            _append_mapping_text(attempts, event.payload.get("attempt"), "attempt_id")
        elif event.kind == "action_intended":
            _append_mapping_text(actions, event.payload.get("proposal"), "action_name")
        elif event.kind == "receipt_received":
            _append_mapping_text(receipts, event.payload.get("receipt"), "status")
        elif event.kind == "evaluation_received":
            receipt = event.payload.get("receipt")
            if isinstance(receipt, Mapping):
                evaluations.append(
                    "accepted" if receipt.get("accepted") else "rejected"
                )
    return tuple(attempts), tuple(actions), tuple(receipts), tuple(evaluations)


def _append_mapping_text(items: list[str], value: object, name: str) -> None:
    if not isinstance(value, Mapping):
        return
    field = value.get(name)
    if isinstance(field, str) and field.strip():
        items.append(field)


def episode_evidence_ids(events: tuple[Event, ...]) -> tuple[str, ...]:
    """Collect content-addressed evidence referenced in a source span."""
    evidence: list[str] = []
    seen: set[str] = set()
    for event in events:
        _collect_evidence(event.payload, evidence, seen)
    return tuple(evidence)


def _episode_from_payload(payload: Mapping[str, object], sequence: int) -> Episode:
    evidence = payload.get("evidence_ids", ())
    if not isinstance(evidence, (list, tuple)):
        raise ContractError("episode evidence_ids must be a sequence")
    start = payload.get("source_start")
    end = payload.get("source_end")
    source_event = payload.get("source_event", sequence)
    if isinstance(start, bool) or not isinstance(start, int):
        raise ContractError("episode source_start must be an integer")
    if isinstance(end, bool) or not isinstance(end, int):
        raise ContractError("episode source_end must be an integer")
    if isinstance(source_event, bool) or not isinstance(source_event, int):
        raise ContractError("episode source_event must be an integer")
    return Episode(
        episode_id=str(payload.get("episode_id") or ""),
        source_start=start,
        source_end=end,
        summary=str(payload.get("summary") or ""),
        evidence_ids=tuple(str(item) for item in evidence),
        model_id=str(payload.get("model_id") or ""),
        model_version=str(payload.get("model_version") or ""),
        source_event=source_event,
    )


def _collect_evidence(value: object, evidence: list[str], seen: set[str]) -> None:
    if isinstance(value, Mapping):
        digest = value.get("digest")
        if isinstance(digest, str) and _DIGEST.fullmatch(digest) and digest not in seen:
            seen.add(digest)
            evidence.append(digest)
        for item in value.values():
            _collect_evidence(item, evidence, seen)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_evidence(item, evidence, seen)


class InvestigationDisposition(str, Enum):
    """Organizational state of one overlapping investigation group."""

    ACTIVE = "active"
    PAUSED = "paused"
    CONCLUDED = "concluded"
    ABANDONED = "abandoned"


class ProgressCategory(str, Enum):
    """Whether a claim is about task outcome or knowledge."""

    OUTCOME = "outcome"
    KNOWLEDGE = "knowledge"


class ProgressStatus(str, Enum):
    """Assessor-facing claim status. Only supported counts as productive progress."""

    PENDING = "pending"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    """Claim-specific assessment retained alongside the claim transition."""

    target_id: str
    category: ProgressCategory
    before: str
    after: str
    scope: str
    assessor: str
    evidence_ids: tuple[str, ...]
    provenance: tuple[str, ...]
    status: ProgressStatus
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Investigation:
    """A lightweight group for a question and related evidence."""

    investigation_id: str
    question: str
    disposition: InvestigationDisposition
    source_event: int
    hypotheses: tuple[str, ...] = ()
    attempt_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    usage_ids: tuple[str, ...] = ()
    uncertainty: str = ""

    def __post_init__(self) -> None:
        if not self.investigation_id.strip() or not self.question.strip():
            raise ContractError("investigation requires an id and question")
        if self.source_event < 1:
            raise ContractError("investigation source_event must be positive")


@dataclass(frozen=True, slots=True)
class ProgressClaim:
    """An evidence-backed before/after claim. Pending is not progress."""

    claim_id: str
    category: ProgressCategory
    before: str
    after: str
    scope: str
    evidence_ids: tuple[str, ...]
    assessor: str
    status: ProgressStatus
    source_event: int

    def __post_init__(self) -> None:
        for name, value in (
            ("claim_id", self.claim_id),
            ("before", self.before),
            ("after", self.after),
            ("scope", self.scope),
            ("assessor", self.assessor),
        ):
            if not value.strip():
                raise ContractError(f"progress {name} must be non-empty")
        if self.source_event < 1:
            raise ContractError("progress source_event must be positive")


@dataclass(frozen=True, slots=True)
class RecoveryView:
    """Operational recovery from the journal. It is not productive progress."""

    operation_id: str
    result: str
    source_event: int


def investigations(events: tuple[Event, ...]) -> tuple[Investigation, ...]:
    """Rebuild investigations; last event for an id wins so reopen is allowed."""
    items: dict[str, Investigation] = {}
    for event in events:
        if event.kind != "investigation_recorded":
            continue
        payload = event.payload.get("investigation")
        if not isinstance(payload, Mapping):
            continue
        item = _investigation_from_payload(payload, event.sequence)
        items[item.investigation_id] = item
    return tuple(
        sorted(
            items.values(), key=lambda item: (item.source_event, item.investigation_id)
        )
    )


def unique_usage_ids(items: tuple[Investigation, ...]) -> tuple[str, ...]:
    """Charge shared usage once when investigations overlap."""
    seen: list[str] = []
    for item in items:
        for usage_id in item.usage_ids:
            if usage_id not in seen:
                seen.append(usage_id)
    return tuple(seen)


def progress_claims(events: tuple[Event, ...]) -> tuple[ProgressClaim, ...]:
    """Rebuild claims; last event for a fingerprint wins so pending can upgrade."""
    items: dict[tuple[str, str, str, str], ProgressClaim] = {}
    order: list[tuple[str, str, str, str]] = []
    for event in events:
        if event.kind not in {"progress_claim_recorded", "progress_claim_rejected"}:
            continue
        payload = event.payload.get("claim")
        if not isinstance(payload, Mapping):
            continue
        status = (
            ProgressStatus.REJECTED
            if event.kind == "progress_claim_rejected"
            else _progress_status_from_payload(payload)
        )
        claim = _claim_from_payload(payload, event.sequence, status)
        fingerprint = claim_fingerprint(claim)
        if event.kind == "progress_claim_rejected" and fingerprint in items:
            continue
        if fingerprint not in items:
            order.append(fingerprint)
        items[fingerprint] = claim
    return tuple(items[key] for key in order)


def productive_progress(claims: tuple[ProgressClaim, ...]) -> tuple[ProgressClaim, ...]:
    """Return only supported outcome/knowledge claims. Recovery never appears."""
    return tuple(claim for claim in claims if claim.status is ProgressStatus.SUPPORTED)


def supported_progress_digests(events: Sequence[Event]) -> tuple[str, ...]:
    """Return content-identity digests of supported progress, in journal order.

    Each supported ``progress_claim_recorded`` or ``knowledge_recorded``
    event contributes one digest of its claim identity (kind, before/after or
    statement, scope) — not the event's claim or item ID, so re-recording the
    same supported statement under a fresh ID yields the same digest. Pending,
    rejected, and stale claims contribute nothing, and neither do raw
    receipts, recovery, or waits: this projection is the P6 budget
    checkpoint's reset signal (ADR-0013), and only novel supported progress
    may reset that anchor.
    """
    digests: list[str] = []
    for event in events:
        if event.kind not in {"progress_claim_recorded", "knowledge_recorded"}:
            continue
        digest = _supported_progress_digest(event.kind, event.payload)
        if digest is not None:
            digests.append(digest)
    return tuple(digests)


def _supported_progress_digest(kind: str, payload: Mapping[str, object]) -> str | None:
    """Return one supported claim's or item's content-identity digest."""
    body = payload.get("claim") if "claim" in payload else payload.get("item")
    mapping = body if isinstance(body, Mapping) else None
    if not mapping or mapping.get("status") != "supported":
        return None
    identity = {
        "kind": kind,
        "before": mapping.get("before"),
        "after": mapping.get("after"),
        "statement": mapping.get("statement"),
        "scope": mapping.get("scope"),
    }
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return f"progress:{digest}"


def recovery_views(events: tuple[Event, ...]) -> tuple[RecoveryView, ...]:
    """Project operational recovery from reconciliation events."""
    views: list[RecoveryView] = []
    for event in events:
        if event.kind != "operation_reconciled":
            continue
        operation_id = event.payload.get("operation_id")
        result = event.payload.get("result")
        if isinstance(operation_id, str) and isinstance(result, str):
            views.append(RecoveryView(operation_id, result, event.sequence))
    return tuple(views)


def known_evidence_ids(
    state: SessionState, events: tuple[Event, ...]
) -> frozenset[str]:
    """IDs visible in the journal. Presence alone does not support a claim."""
    ids: set[str] = set()
    for item in knowledge_items(state, events):
        ids.add(item.item_id)
        ids.update(item.evidence_ids)
    for node in state.accepted_lineage:
        ids.add(node.node_id)
        if node.evaluation_receipt_id:
            ids.add(node.evaluation_receipt_id)
    for history in state.attempt_history:
        ids.add(history.attempt.attempt_id)
        ids.update(history.evaluation_receipt_ids)
    for event in events:
        ids.add(event.event_id)
        if event.kind == "receipt_received":
            receipt = event.payload.get("receipt")
            receipt_id = _string_field(receipt, "receipt_id")
            if receipt_id:
                ids.add(receipt_id)
    return frozenset(ids)


def validated_evidence_ids(
    state: SessionState, events: tuple[Event, ...]
) -> frozenset[str]:
    """IDs that can back a knowledge transition or knowledge-progress claim."""
    ids: set[str] = set()
    source_ids = {source.source_id for source in state.knowledge_sources}
    ids.update(source_ids)
    for source in state.knowledge_sources:
        if source.artifact is not None:
            ids.add(source.artifact.digest)
    changed = True
    items = knowledge_items(state, events)
    while changed:
        changed = False
        for item in items:
            if item.status is not KnowledgeStatus.SUPPORTED:
                continue
            if item.item_id in ids:
                continue
            if any(evidence_id in ids for evidence_id in item.evidence_ids):
                ids.add(item.item_id)
                changed = True
    return frozenset(ids)


# The evidence assessment API intentionally names every claim binding field.
# pylint: disable=too-many-arguments,too-many-locals
def assess_knowledge_transition(
    *,
    state: SessionState,
    events: tuple[Event, ...],
    target_id: str,
    statement: str,
    scope: str,
    evidence_ids: tuple[str, ...],
    assessor: str = "agent",
) -> EvidenceAssessment:
    """Assess a knowledge update against named, non-runtime evidence.

    Runtime receipts and evaluation IDs are intentionally not evidence for a
    knowledge fact.  A supported supplied source or supported knowledge item
    must be named explicitly; a contradictory item rejects the transition.
    """
    items = {item.item_id: item for item in knowledge_items(state, events)}
    sources = {source.source_id: source for source in state.knowledge_sources}
    provenance: list[str] = []
    reasons: list[str] = []
    if not assessor.strip():
        raise ContractError("evidence assessor must be non-empty")
    if not evidence_ids:
        reasons.append("no named evidence")
        status = ProgressStatus.PENDING
    else:
        supported = True
        rejected = False
        for evidence_id in evidence_ids:
            item = items.get(evidence_id)
            source = sources.get(evidence_id)
            if item is not None and item.status is KnowledgeStatus.SUPPORTED:
                provenance.append(item.provenance)
                continue
            if source is not None:
                provenance.append(source.provenance)
                continue
            if item is not None and item.status is KnowledgeStatus.CONTRADICTED:
                rejected = True
                reasons.append(f"evidence {evidence_id} contradicts the claim")
            else:
                reasons.append(f"evidence {evidence_id} is not validated")
            supported = False
        if rejected:
            status = ProgressStatus.REJECTED
        else:
            status = ProgressStatus.SUPPORTED if supported else ProgressStatus.PENDING
    return EvidenceAssessment(
        target_id=target_id,
        category=ProgressCategory.KNOWLEDGE,
        before="",
        after=statement,
        scope=scope,
        assessor=assessor,
        evidence_ids=evidence_ids,
        provenance=tuple(provenance),
        status=status,
        reasons=tuple(reasons),
    )


# The evidence assessment API intentionally names every claim binding field.
# pylint: disable=too-many-arguments,too-many-locals,too-many-branches
def assess_progress_claim(
    *,
    state: SessionState,
    events: tuple[Event, ...],
    claim_id: str,
    category: ProgressCategory,
    before: str,
    after: str,
    scope: str,
    evidence_ids: tuple[str, ...],
    assessor: str,
    prior_status: ProgressStatus | None = None,
) -> EvidenceAssessment:
    """Assess evidence against the complete before/after claim identity."""
    if not assessor.strip():
        raise ContractError("evidence assessor must be non-empty")
    if before == after or not evidence_ids:
        status = ProgressStatus.PENDING
        reasons = ("claim has no observable before/after evidence",)
        return _assessment(
            claim_id,
            category,
            before,
            after,
            scope,
            assessor,
            evidence_ids,
            (),
            status,
            reasons,
        )
    if prior_status is ProgressStatus.SUPPORTED:
        return _assessment(
            claim_id,
            category,
            before,
            after,
            scope,
            assessor,
            evidence_ids,
            (),
            ProgressStatus.REJECTED,
            ("claim fingerprint is already supported",),
        )
    items = {item.item_id: item for item in knowledge_items(state, events)}
    accepted = set(outcome_support_ids(state, events))
    rejected = _rejected_evaluation_ids(events)
    provenance: list[str] = []
    relevant = True
    contradictory = False
    for evidence_id in evidence_ids:
        if category is ProgressCategory.KNOWLEDGE:
            item = items.get(evidence_id)
            if item is not None and item.status is KnowledgeStatus.SUPPORTED:
                if item.statement != after:
                    relevant = False
                    continue
                provenance.append(item.provenance)
                continue
            if item is not None and item.status is KnowledgeStatus.CONTRADICTED:
                contradictory = True
            relevant = False
            continue
        if evidence_id in accepted:
            provenance.append("external evaluator")
            continue
        if evidence_id in rejected:
            contradictory = True
        relevant = False
    if contradictory:
        status = ProgressStatus.REJECTED
        reasons = ("named evidence rejects or contradicts the claim",)
    elif relevant:
        status = ProgressStatus.SUPPORTED
        reasons = ()
    else:
        status = ProgressStatus.PENDING
        reasons = ("named evidence is unrelated or not yet supported",)
    return _assessment(
        claim_id,
        category,
        before,
        after,
        scope,
        assessor,
        evidence_ids,
        tuple(provenance),
        status,
        reasons,
    )


# Keep the durable assessment constructor explicit for replay inspection.
# pylint: disable=too-many-arguments,too-many-positional-arguments
def _assessment(
    target_id: str,
    category: ProgressCategory,
    before: str,
    after: str,
    scope: str,
    assessor: str,
    evidence_ids: tuple[str, ...],
    provenance: tuple[str, ...],
    status: ProgressStatus,
    reasons: tuple[str, ...],
) -> EvidenceAssessment:
    return EvidenceAssessment(
        target_id=target_id,
        category=category,
        before=before,
        after=after,
        scope=scope,
        assessor=assessor,
        evidence_ids=evidence_ids,
        provenance=provenance,
        status=status,
        reasons=reasons,
    )


def _rejected_evaluation_ids(events: tuple[Event, ...]) -> frozenset[str]:
    return frozenset(
        receipt_id
        for event in events
        if event.kind == "evaluation_received"
        and event.payload.get("disposition") != "accepted"
        for receipt_id in [_string_field(event.payload.get("receipt"), "receipt_id")]
        if receipt_id is not None
    )


def outcome_support_ids(
    state: SessionState, events: tuple[Event, ...]
) -> frozenset[str]:
    """IDs that can back an outcome-progress claim: accepted external evaluation."""
    ids: set[str] = set()
    for node in state.accepted_lineage:
        if node.evaluation_receipt_id:
            ids.add(node.evaluation_receipt_id)
            ids.add(node.node_id)
    for event in events:
        if event.kind != "evaluation_received":
            continue
        if event.payload.get("disposition") != "accepted":
            continue
        receipt_id = _string_field(event.payload.get("receipt"), "receipt_id")
        if receipt_id:
            ids.add(receipt_id)
    return frozenset(ids)


def effective_knowledge_status(
    current: KnowledgeStatus | None,
    requested: KnowledgeStatus,
    evidence_ids: tuple[str, ...],
    validated: frozenset[str],
) -> KnowledgeStatus:
    """Apply one knowledge transition. Self-report cannot promote status."""
    evidenced = any(item in validated for item in evidence_ids)
    if requested is KnowledgeStatus.HYPOTHESIS:
        return KnowledgeStatus.HYPOTHESIS
    if requested is KnowledgeStatus.RETIRED:
        return (
            KnowledgeStatus.RETIRED
            if current is not None
            else KnowledgeStatus.HYPOTHESIS
        )
    if requested in {KnowledgeStatus.SUPPORTED, KnowledgeStatus.CONTRADICTED}:
        if evidenced:
            return requested
        return current if current is not None else KnowledgeStatus.HYPOTHESIS
    return current if current is not None else KnowledgeStatus.HYPOTHESIS


# Keyword-only fields name each promotion input rather than a packed record.
# pylint: disable=too-many-arguments
def progress_claim_decision(
    *,
    category: ProgressCategory,
    before: str,
    after: str,
    evidence_ids: tuple[str, ...],
    prior_status: ProgressStatus | None,
    validated: frozenset[str],
    outcome_ids: frozenset[str],
) -> tuple[ProgressStatus, str]:
    """Decide claim status. Pending may upgrade; ID presence is not enough."""
    if prior_status is ProgressStatus.SUPPORTED:
        return ProgressStatus.REJECTED, "progress_claim_rejected"
    if before == after or not evidence_ids:
        return ProgressStatus.PENDING, "progress_claim_recorded"
    if category is ProgressCategory.OUTCOME:
        supported = any(item in outcome_ids for item in evidence_ids)
    else:
        supported = any(item in validated for item in evidence_ids)
    if supported:
        return ProgressStatus.SUPPORTED, "progress_claim_recorded"
    return ProgressStatus.PENDING, "progress_claim_recorded"


def claim_fingerprint(claim: ProgressClaim) -> tuple[str, str, str, str]:
    """Identity used to reject duplicate progress reports."""
    return (claim.category.value, claim.before, claim.after, claim.scope)


@dataclass(frozen=True, slots=True)
class AlternativeRetention:
    """Byte-budget eviction that keeps protected evidence roots."""

    byte_budget: int
    bytes_used: int
    protected: tuple[str, ...]
    evicted: tuple[str, ...]
    unavailable: tuple[str, ...]
    reason: str = "byte_budget"


@dataclass(frozen=True, slots=True)
class RetentionRecord:
    """One journaled eviction outcome. Identity survives missing bytes."""

    digest: str
    reason: str
    available: bool
    size: int
    source_event: int

    def __post_init__(self) -> None:
        if not self.digest.strip() or not self.reason.strip():
            raise ContractError("retention record requires digest and reason")
        if self.size < 0 or self.source_event < 1:
            raise ContractError("retention size and source_event are invalid")


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


# pylint: disable=too-many-branches
def protected_digests(state: SessionState, events: tuple[Event, ...]) -> frozenset[str]:
    """Accepted, pending, and claim-required artifact identities.

    Failed-attempt payloads are not protected merely because they were journaled.
    Content-addressed digests are charged once; a shared object stays if any
    protected root still needs it. Non-digest evidence IDs are followed through
    receipts, sources, and the validated claim/knowledge graph.
    """
    ids: set[str] = set()
    index = _artifact_index(state, events)
    if state.initial_observation is not None and state.initial_observation.raw_record:
        ids.add(state.initial_observation.raw_record.digest)
    for source in state.knowledge_sources:
        if source.artifact is not None:
            ids.add(source.artifact.digest)
    for node in state.accepted_lineage:
        digest = node.state.digest
        if digest is not None and _DIGEST.fullmatch(digest):
            ids.add(digest)
    for item in knowledge_items(state, events):
        if item.status is KnowledgeStatus.RETIRED:
            continue
        _add_digest_ids(ids, item.evidence_ids)
        _follow_ids(ids, (item.item_id, *item.evidence_ids), index)
    for claim in progress_claims(events):
        if claim.status in {ProgressStatus.REJECTED, ProgressStatus.STALE}:
            continue
        _add_digest_ids(ids, claim.evidence_ids)
        _follow_ids(ids, claim.evidence_ids, index)
    for episode in episodes(events):
        _add_digest_ids(ids, episode.evidence_ids)
        _follow_ids(ids, episode.evidence_ids, index)
    for event in events:
        if event.kind != "evaluation_received":
            continue
        if event.payload.get("disposition") != "accepted":
            continue
        receipt = event.payload.get("receipt")
        if isinstance(receipt, Mapping):
            _walk_artifact_refs(receipt, ids)
    _protect_pending(state, ids)
    return frozenset(ids)


def select_evictions(
    inventory: tuple[tuple[str, int], ...],
    protected: frozenset[str],
    byte_budget: int,
) -> tuple[str, ...]:
    """Drop unprotected objects, largest first, until the store is within budget."""
    if byte_budget < 0:
        raise ContractError("byte_budget must not be negative")
    used = sum(size for _digest, size in inventory)
    if used <= byte_budget:
        return ()
    candidates = sorted(
        ((digest, size) for digest, size in inventory if digest not in protected),
        key=lambda item: (-item[1], item[0]),
    )
    evicted: list[str] = []
    remaining = used
    for digest, size in candidates:
        if remaining <= byte_budget:
            break
        evicted.append(digest)
        remaining -= size
    return tuple(evicted)


def _protect_pending(state: SessionState, ids: set[str]) -> None:
    pending = state.pending_proposal_id
    if pending is None:
        return
    for operation in state.operations:
        if isinstance(operation, ActionOperation) and operation.proposal_id == pending:
            for receipt in operation.receipts:
                _add_receipt_artifacts(receipt, ids)
        if (
            isinstance(operation, EvaluationOperation)
            and operation.request.request_id == pending
        ):
            if operation.raw_receipt is not None:
                try:
                    _walk_artifact_refs(parse_json(operation.raw_receipt), ids)
                except ContractError:
                    # Replay validates the canonical intake before this view;
                    # malformed legacy data cannot contribute a protection root.
                    pass
            if operation.reconciliation_receipt is not None:
                _add_evaluation_artifacts(operation.reconciliation_receipt, ids)


def _add_receipt_artifacts(receipt: ExecutionReceipt, ids: set[str]) -> None:
    if receipt.raw_artifact is not None:
        ids.add(receipt.raw_artifact.digest)


def _add_evaluation_artifacts(receipt: EvaluationReceipt, ids: set[str]) -> None:
    for reference in (*receipt.evidence, *receipt.completion_evidence):
        ids.add(reference.digest)
    if receipt.raw_receipt is not None:
        ids.add(receipt.raw_receipt.digest)


def _add_digest_ids(ids: set[str], values: tuple[str, ...]) -> None:
    for value in values:
        if _DIGEST.fullmatch(value):
            ids.add(value)


def _follow_ids(
    ids: set[str], keys: tuple[str, ...], index: Mapping[str, set[str]]
) -> None:
    for key in keys:
        ids.update(index.get(key, ()))


def _artifact_index(
    state: SessionState, events: tuple[Event, ...]
) -> dict[str, set[str]]:
    """Map source, receipt, item, and event IDs to reachable artifact digests."""
    index: dict[str, set[str]] = {}

    def add(key: str, digest: str) -> None:
        if _DIGEST.fullmatch(digest):
            index.setdefault(key, set()).add(digest)

    for source in state.knowledge_sources:
        if source.artifact is not None:
            add(source.source_id, source.artifact.digest)
    for event in events:
        refs: set[str] = set()
        _walk_artifact_refs(event.payload, refs)
        if refs:
            index.setdefault(event.event_id, set()).update(refs)
        if event.kind == "receipt_received":
            receipt_id = _string_field(event.payload.get("receipt"), "receipt_id")
            if receipt_id:
                index.setdefault(receipt_id, set()).update(refs)
        elif event.kind == "evaluation_received":
            receipt_id = _string_field(event.payload.get("receipt"), "receipt_id")
            if receipt_id:
                index.setdefault(receipt_id, set()).update(refs)
    changed = True
    items = knowledge_items(state, events)
    while changed:
        changed = False
        for item in items:
            before = set(index.get(item.item_id, ()))
            for evidence_id in item.evidence_ids:
                if _DIGEST.fullmatch(evidence_id):
                    add(item.item_id, evidence_id)
                index.setdefault(item.item_id, set()).update(index.get(evidence_id, ()))
            if index.get(item.item_id, set()) != before:
                changed = True
    return index


def _walk_artifact_refs(value: object, ids: set[str]) -> None:
    if isinstance(value, Mapping):
        digest = value.get("digest")
        if (
            isinstance(digest, str)
            and _DIGEST.fullmatch(digest)
            and isinstance(value.get("media_type"), str)
            and isinstance(value.get("provenance"), str)
        ):
            ids.add(digest)
        for item in value.values():
            _walk_artifact_refs(item, ids)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_artifact_refs(item, ids)


def retention_records(events: tuple[Event, ...]) -> tuple[RetentionRecord, ...]:
    """Rebuild eviction outcomes. Journal facts remain after bytes are gone."""
    records: list[RetentionRecord] = []
    for event in events:
        if event.kind != "artifact_evicted":
            continue
        digest = event.payload.get("digest")
        reason = event.payload.get("reason")
        available = event.payload.get("available")
        dropped = event.payload.get("dropped")
        size = event.payload.get("size")
        byte_budget = event.payload.get("byte_budget")
        if not isinstance(reason, str) or reason != "byte_budget":
            raise IntegrityError("malformed artifact eviction projection")
        if not _valid_eviction_identity(digest):
            raise IntegrityError("malformed artifact eviction projection")
        if not _valid_eviction_availability(available, dropped):
            raise IntegrityError("malformed artifact eviction projection")
        if not _valid_eviction_sizes(size, byte_budget):
            raise IntegrityError("malformed artifact eviction projection")
        assert isinstance(digest, str)
        assert isinstance(available, bool)
        assert isinstance(size, int) and not isinstance(size, bool)
        records.append(
            RetentionRecord(
                digest=digest,
                reason=reason,
                available=available,
                size=size,
                source_event=event.sequence,
            )
        )
    return tuple(records)


def _valid_eviction_identity(digest: object) -> bool:
    return isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None


def _valid_eviction_availability(available: object, dropped: object) -> bool:
    return (
        isinstance(available, bool)
        and isinstance(dropped, bool)
        and not (dropped and available)
    )


def _valid_eviction_sizes(size: object, byte_budget: object) -> bool:
    valid_size = isinstance(size, int) and not isinstance(size, bool) and size >= 0
    valid_budget = (
        isinstance(byte_budget, int)
        and not isinstance(byte_budget, bool)
        and byte_budget >= 0
    )
    return valid_size and valid_budget


def _item_from_source(
    source: KnowledgeSource, events: tuple[Event, ...]
) -> KnowledgeItem:
    source_event = 1
    if events:
        source_event = events[0].sequence
    evidence = () if source.artifact is None else (source.artifact.digest,)
    return KnowledgeItem(
        item_id=source.source_id,
        statement=source.provenance,
        scope="supplied knowledge",
        status=KnowledgeStatus.SUPPORTED,
        evidence_ids=evidence,
        provenance=source.provenance,
        source_event=source_event,
    )


def _investigation_from_payload(
    payload: Mapping[str, object], sequence: int
) -> Investigation:
    return Investigation(
        investigation_id=str(payload.get("investigation_id") or ""),
        question=str(payload.get("question") or ""),
        disposition=InvestigationDisposition(
            str(payload.get("disposition") or InvestigationDisposition.ACTIVE.value)
        ),
        source_event=_positive_int(payload.get("source_event"), sequence),
        hypotheses=_string_tuple(payload.get("hypotheses")),
        attempt_ids=_string_tuple(payload.get("attempt_ids")),
        evidence_ids=_string_tuple(payload.get("evidence_ids")),
        usage_ids=_string_tuple(payload.get("usage_ids")),
        uncertainty=str(payload.get("uncertainty") or ""),
    )


def _claim_from_payload(
    payload: Mapping[str, object], sequence: int, status: ProgressStatus
) -> ProgressClaim:
    return ProgressClaim(
        claim_id=str(payload.get("claim_id") or ""),
        category=ProgressCategory(
            str(payload.get("category") or ProgressCategory.KNOWLEDGE.value)
        ),
        before=str(payload.get("before") or ""),
        after=str(payload.get("after") or ""),
        scope=str(payload.get("scope") or ""),
        evidence_ids=_string_tuple(payload.get("evidence_ids")),
        assessor=str(payload.get("assessor") or "agent"),
        status=status,
        source_event=_positive_int(payload.get("source_event"), sequence),
    )


def _progress_status_from_payload(payload: Mapping[str, object]) -> ProgressStatus:
    raw = payload.get("status")
    try:
        return ProgressStatus(str(raw or ProgressStatus.PENDING.value))
    except ValueError:
        return ProgressStatus.PENDING


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if str(item))


def _item_from_payload(payload: Mapping[str, object], sequence: int) -> KnowledgeItem:
    status = payload.get("status")
    evidence = payload.get("evidence_ids", ())
    if not isinstance(evidence, (list, tuple)):
        raise ContractError("knowledge evidence_ids must be a sequence")
    return KnowledgeItem(
        item_id=str(payload.get("item_id") or ""),
        statement=str(payload.get("statement") or ""),
        scope=str(payload.get("scope") or ""),
        status=KnowledgeStatus(str(status or KnowledgeStatus.HYPOTHESIS.value)),
        evidence_ids=tuple(str(item) for item in evidence),
        provenance=str(payload.get("provenance") or "agent"),
        source_event=_positive_int(payload.get("source_event"), sequence),
    )


def current_plan_text(events: tuple[Event, ...], objective_version: int) -> str:
    """Latest plan recorded under the current objective, else none."""
    text = "none"
    for event in events:
        if event.kind != "plan_recorded":
            continue
        if event.objective_version != objective_version:
            continue
        recorded = event.payload.get("text")
        if isinstance(recorded, str) and recorded.strip():
            text = recorded
    return text


def objective_for_version(events: tuple[Event, ...], objective_version: int) -> str:
    """Return the durable objective text for one historical objective version."""
    objective: str | None = None
    for event in events:
        if event.kind == "session_created":
            task = event.payload.get("task")
            if (
                isinstance(task, Mapping)
                and task.get("objective_version") == objective_version
            ):
                candidate = task.get("objective")
                if isinstance(candidate, str) and candidate.strip():
                    objective = candidate
        elif (
            event.kind == "objective_steered"
            and event.objective_version == objective_version
        ):
            command = event.payload.get("command")
            if isinstance(command, Mapping):
                candidate = command.get("objective")
                if isinstance(candidate, str) and candidate.strip():
                    objective = candidate
    if objective is None:
        raise ContractError(
            f"objective version {objective_version} is missing from the journal"
        )
    return objective


def _latest_operator_direction(events: tuple[Event, ...]) -> str | None:
    text: str | None = None
    for event in events:
        if event.kind == "operator_message":
            command = event.payload.get("command")
            text = _string_field(command, "text") or text
        elif event.kind == "objective_steered":
            command = event.payload.get("command")
            steered = _string_field(command, "objective")
            if steered is not None:
                text = steered
    return text


def _latest_receipt_id(events: tuple[Event, ...]) -> str | None:
    receipt = latest_receipt_payload(events)
    if not receipt:
        return None
    receipt_id = receipt.get("receipt_id")
    return receipt_id if isinstance(receipt_id, str) else None


def latest_execution_receipt_payload(
    events: tuple[Event, ...],
) -> Mapping[str, JsonValue]:
    """Return the authoritative current runtime receipt, ignoring evaluations.

    A runtime may deliver status updates out of order.  Once an operation has
    produced a terminal receipt, later running or contradictory terminal
    receipts remain journal evidence but cannot replace that operation's
    authoritative state.  For an operation without a terminal receipt, the
    newest nonterminal update is authoritative.
    """
    candidates: dict[str, list[tuple[int, Mapping[str, JsonValue]]]] = {}
    for event in events:
        if event.kind != "receipt_received":
            continue
        payload = event.payload.get("receipt")
        if not isinstance(payload, Mapping):
            continue
        receipt = frozen_mapping(
            {str(key): _as_json_value(item) for key, item in payload.items()}
        )
        proposal_id = receipt.get("proposal_id")
        if not isinstance(proposal_id, str):
            continue
        candidates.setdefault(proposal_id, []).append((event.sequence, receipt))
    authoritative: list[tuple[int, Mapping[str, JsonValue]]] = []
    terminal_statuses = {
        "rejected",
        "succeeded",
        "failed",
        "indeterminate",
    }
    for receipts in candidates.values():
        terminal = [
            item for item in receipts if item[1].get("status") in terminal_statuses
        ]
        authoritative.append(terminal[0] if terminal else receipts[-1])
    if not authoritative:
        return frozen_mapping()
    return max(authoritative, key=lambda item: item[0])[1]


def latest_receipt_payload(events: tuple[Event, ...]) -> Mapping[str, JsonValue]:
    """Return the latest execution or evaluation receipt body, or empty."""
    receipt: Mapping[str, JsonValue] = frozen_mapping()
    for event in events:
        if event.kind not in {"receipt_received", "evaluation_received"}:
            continue
        payload = event.payload.get("receipt")
        if not isinstance(payload, Mapping):
            continue
        body = {str(key): _as_json_value(item) for key, item in payload.items()}
        if event.kind == "evaluation_received":
            disposition = event.payload.get("disposition")
            if isinstance(disposition, str):
                body["disposition"] = disposition
        receipt = frozen_mapping(body)
    return receipt


def remaining_budgets(
    state: SessionState, events: tuple[Event, ...]
) -> Mapping[str, JsonValue]:
    """Return used and remaining scheduling limits from ledger and journal."""
    used_calls = max(measured_model_calls(events), state.ledger.model_calls)
    return frozen_mapping(
        {
            "actions": _budget_remaining(
                state.task.budgets.actions, state.ledger.conservative_actions
            ),
            "model_calls": _budget_remaining(
                state.task.budgets.model_calls,
                max(used_calls, state.ledger.conservative_model_calls),
            ),
            "evaluations": _budget_remaining(
                state.task.budgets.evaluations,
                state.ledger.conservative_evaluations,
            ),
        }
    )


def measured_model_calls(events: tuple[Event, ...]) -> int:
    """Count measured model calls, including counted repairs in one decision."""
    total = 0
    for event in events:
        if event.kind not in {"model_decided", "model_malformed"}:
            continue
        usage = event.payload.get("usage")
        calls = usage.get("model_calls") if isinstance(usage, Mapping) else None
        if isinstance(calls, int) and not isinstance(calls, bool):
            total += calls
    return total


def observation_payload(
    state: SessionState,
    events: tuple[Event, ...],
    *,
    artifact_content: JsonValue | None = None,
) -> Mapping[str, JsonValue]:
    """Build observation from initial facts and the latest execution receipt.

    Evaluation receipts never supply observation state. Raw initial content is
    included when provided as artifact_content and no later execution artifact
    exists.
    """
    body: dict[str, JsonValue] = {}
    observation = state.initial_observation
    if observation is not None:
        body["observation_id"] = observation.observation_id
        body["initial_state"] = observation.state.value
        body["provenance"] = observation.provenance
        if observation.raw_record is not None:
            body["raw_record_digest"] = observation.raw_record.digest
        body["state"] = observation.state.value
        if observation.state.digest:
            body["digest"] = observation.state.digest
    receipt = latest_execution_receipt_payload(events)
    if receipt:
        body["receipt_id"] = receipt.get("receipt_id")
        body["status"] = receipt.get("status")
        state_ref = receipt.get("state")
        if isinstance(state_ref, Mapping):
            value = state_ref.get("value")
            parsed = _maybe_json(value)
            body["state"] = parsed if parsed is not None else value
            digest = state_ref.get("digest")
            if isinstance(digest, str):
                body["digest"] = digest
            revision = state_ref.get("revision")
            if isinstance(revision, str):
                body["revision"] = revision
            elif isinstance(state_ref.get("value"), str):
                body["revision"] = state_ref["value"]
    if artifact_content is not None:
        body["content"] = artifact_content
    return frozen_mapping(body)


def _string_field(value: object, name: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    field = value.get(name)
    return field if isinstance(field, str) else None


def _positive_int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _budget_text(remaining: Mapping[str, JsonValue]) -> str:
    parts = []
    for name in ("actions", "model_calls", "evaluations"):
        item = remaining.get(name)
        if not isinstance(item, Mapping):
            parts.append(f"{name}=unknown")
            continue
        limit = item.get("limit")
        used = item.get("used")
        left = item.get("remaining")
        if limit is None:
            parts.append(f"{name}=unbounded used={used}")
        else:
            parts.append(f"{name}={left}/{limit} used={used}")
    return " ".join(parts)


def _budget_remaining(limit: int | None, used: int) -> Mapping[str, JsonValue]:
    remaining: int | None = None if limit is None else max(0, limit - used)
    return frozen_mapping({"limit": limit, "used": used, "remaining": remaining})


def _as_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return frozen_mapping(
            {str(key): _as_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_as_json_value(item) for item in value)
    return str(value)


def _maybe_json(value: object) -> JsonValue | None:
    if not isinstance(value, str) or not value or value[0] not in "{[":
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return _as_json_value(parsed)
