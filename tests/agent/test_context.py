"""Deterministic context ranking with FTS5 hits and unchanged coefficients."""

from __future__ import annotations

from vharness.agent.context import (
    COEFFICIENTS,
    RECENT_BAND_MAX_TOKENS,
    RECENT_ITEM_MAX_TOKENS,
    ContextBundle,
    ContextOverflow,
    ReceiptExcerpt,
    assemble_context,
    count_tokens,
    estimate_serialized_tokens,
    _salience_query,
    TokenAccounting,
)
from vharness.agent.journal import SearchHit
from vharness.agent.models import (
    ActionDefinition,
    BudgetLimits,
    CommittedNode,
    CompletionMode,
    EnvironmentContract,
    Event,
    RuntimeCapabilities,
    SessionStatus,
    StateRef,
    TaskSpec,
)
from vharness.agent.memory import working_memory
from vharness.agent.transitions import SessionState


def _environment() -> EnvironmentContract:
    return EnvironmentContract(
        environment_id="fake",
        version="1",
        initial_state=StateRef("fake", "initial", digest="0" * 64),
        actions=(ActionDefinition("inspect", "Inspect current state"),),
        capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
    )


def _state() -> SessionState:
    node = CommittedNode("root-1", 1, StateRef("fake", "initial", digest="0" * 64))
    return SessionState(
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


def _event(event_id: str, sequence: int, kind: str) -> Event:
    return Event(
        event_id=event_id,
        session_id="session-1",
        sequence=sequence,
        kind=kind,
        objective_version=1,
        payload={"note": kind},
        recorded_at="2026-09-12T00:00:00+00:00",
    )


def _hit(
    event_id: str, sequence: int, excerpt: str, bm25: float, kind: str
) -> SearchHit:
    return SearchHit(
        event_id=event_id,
        session_id="session-1",
        sequence=sequence,
        kind=kind,
        excerpt=excerpt,
        bm25=bm25,
    )


def test_fts_hits_enter_retrieved_band_in_relevance_order():
    events = (
        _event("event-1", 1, "session_created"),
        _event("event-2", 2, "question_recorded"),
        _event("event-3", 3, "progress_claim_recorded"),
    )
    hits = (
        _hit(
            "event-2",
            2,
            "distinctive zebra question",
            -8.0,
            "question_recorded",
        ),
        _hit(
            "event-3",
            3,
            "weaker mention",
            -3.0,
            "progress_claim_recorded",
        ),
    )
    assembled = assemble_context(
        _state(), events, _environment(), token_budget=256, hits=hits
    )
    assert isinstance(assembled, ContextBundle)
    retrieved = [item.item_id for item in assembled.items if item.band == "retrieved"]
    assert retrieved.index("fts:event-2") < retrieved.index("fts:event-3")
    assert assembled.manifest.coefficients == COEFFICIENTS
    recent_ids = [item.item_id for item in assembled.items if item.band == "recent"]
    assert "event:event-2" not in recent_ids
    assert ("event:event-2", "duplicate") in assembled.manifest.omitted


def test_tiny_bm25_still_outranks_recency_for_better_match():
    events = (
        _event("event-1", 1, "session_created"),
        _event("event-2", 2, "question_recorded"),
        _event("event-3", 3, "progress_claim_recorded"),
    )
    hits = (
        _hit(
            "event-2",
            2,
            "older distinctive inspect fixture evidence",
            -3e-6,
            "question_recorded",
        ),
        _hit(
            "event-3",
            3,
            "newer weaker mention",
            -1e-6,
            "progress_claim_recorded",
        ),
    )
    assembled = assemble_context(
        _state(), events, _environment(), token_budget=256, hits=hits
    )
    assert isinstance(assembled, ContextBundle)
    retrieved = [item.item_id for item in assembled.items if item.band == "retrieved"]
    assert retrieved.index("fts:event-2") < retrieved.index("fts:event-3")
    assert dict(assembled.manifest.coefficients) == dict(COEFFICIENTS)


def test_identical_inputs_yield_identical_manifests_with_hits():
    events = (_event("event-1", 1, "session_created"),)
    hits = (
        _hit(
            "event-1",
            1,
            "session created inspect fixture",
            -1.0,
            "session_created",
        ),
    )
    first = assemble_context(
        _state(), events, _environment(), token_budget=128, hits=hits
    )
    second = assemble_context(
        _state(), events, _environment(), token_budget=128, hits=hits
    )
    assert isinstance(first, ContextBundle)
    assert first.manifest == second.manifest
    assert first.manifest.coefficients["overlap"] == 1.0
    assert first.manifest.coefficients["mmr"] == 8.0
    assert count_tokens(first.text) == first.manifest.tokens_used


def test_recent_completion_rejection_exposes_reason_without_payload_dump():
    events = (
        _event("event-1", 1, "session_created"),
        Event(
            event_id="event-2",
            session_id="session-1",
            sequence=2,
            kind="completion_rejected",
            objective_version=1,
            payload={
                "reason": "no current external evidence",
                "rationale": "private model rationale",
                "untrusted": "must not enter context",
            },
            recorded_at="2026-09-12T00:00:00+00:00",
        ),
    )

    first = assemble_context(_state(), events, _environment(), token_budget=16384)
    second = assemble_context(_state(), events, _environment(), token_budget=16384)

    assert isinstance(first, ContextBundle)
    assert first.manifest == second.manifest
    assert "event 2 completion_rejected reason=no current external evidence" in (
        first.text
    )
    assert "private model rationale" not in first.text
    assert "must not enter context" not in first.text


def test_greedy_mmr_repicks_after_each_selection():
    events = (
        _event("event-1", 1, "session_created"),
        _event("event-2", 2, "question_recorded"),
        _event("event-3", 3, "progress_claim_recorded"),
        _event("event-4", 4, "operator_message"),
    )
    duplicate = "inspect fixture evidence inspect fixture evidence"
    hits = (
        _hit("event-2", 2, duplicate, -10.0, "question_recorded"),
        _hit("event-3", 3, f"{duplicate} again", -9.0, "progress_claim_recorded"),
        _hit("event-4", 4, "zebra unique operator note", -2.0, "operator_message"),
    )
    assembled = assemble_context(
        _state(), events, _environment(), token_budget=16384, hits=hits
    )
    assert isinstance(assembled, ContextBundle)
    retrieved = [
        item.item_id
        for item in assembled.items
        if item.band == "retrieved" and item.item_id.startswith("fts:")
    ]
    assert retrieved[0] in {"fts:event-2", "fts:event-3"}
    assert retrieved[1] == "fts:event-4"


def test_manifest_records_overhead_and_item_token_allocation():
    events = (_event("event-1", 1, "session_created"),)
    assembled = assemble_context(
        _state(),
        events,
        _environment(),
        token_budget=16384,
        overhead_tokens=40,
        reserved_output_tokens=256,
    )
    assert isinstance(assembled, ContextBundle)
    allocation = dict(assembled.manifest.allocation)
    assert allocation["overhead"] == 40
    assert allocation["reserved_output"] == 256
    assert allocation["mandatory"] > 0
    assert allocation["unused"] >= 0
    assert sum(tokens for _item_id, tokens in assembled.manifest.item_tokens) == (
        allocation["mandatory"] + allocation["retrieved"] + allocation["recent"]
    )
    assert assembled.manifest.coefficients["salience"] == 1.0
    assert assembled.manifest.coefficients["causal_proximity"] == 1.0
    assert assembled.manifest.coefficients["overlap"] == 1.0


def test_reserved_output_tokens_participate_in_context_overflow():
    events = (_event("event-1", 1, "session_created"),)
    without_reservation = assemble_context(
        _state(), events, _environment(), token_budget=1
    )
    with_reservation = assemble_context(
        _state(),
        events,
        _environment(),
        token_budget=1,
        reserved_output_tokens=1024,
    )

    assert isinstance(without_reservation, ContextOverflow)
    assert isinstance(with_reservation, ContextOverflow)
    assert with_reservation.tokens_required == (
        without_reservation.tokens_required + 1024
    )


def test_current_plan_contributes_to_salience_query():
    events = (
        _event("event-1", 1, "session_created"),
        Event(
            event_id="event-2",
            session_id="session-1",
            sequence=2,
            kind="plan_recorded",
            objective_version=1,
            payload={"text": "investigate zebra evidence", "source_event": 2},
            recorded_at="2026-09-12T00:00:00+00:00",
        ),
    )

    query = _salience_query(working_memory(_state(), events))

    assert "investigate" in query
    assert "zebra" in query


def test_serialized_input_estimate_bounds_the_run6_dense_request():
    dense = "x" * 205_811
    assert estimate_serialized_tokens("") == 0
    assert estimate_serialized_tokens("abc") == 3  # 1.25 chars per token prior
    assert estimate_serialized_tokens(dense) == 164649
    assert estimate_serialized_tokens(dense) > 24_000


def test_calibrated_estimate_adds_a_margin_over_the_observed_ratio():
    text = "a" * 1_000
    assert estimate_serialized_tokens(text, 0.5) == 550  # 0.5 x 1.1 margin
    assert estimate_serialized_tokens(text, 0.25) == 275
    assert estimate_serialized_tokens("", 0.5) == 0


def test_prior_and_calibration_bound_the_cod38_smoke_requests():
    """COD-73: every recorded smoke request, before and after calibration."""
    import json
    import sqlite3
    from pathlib import Path as _Path

    journal = (
        _Path(__file__).parents[2]
        / "evidence/phase-0004-arc-agi-3-smoke/attempt-4-scorecard/session.sqlite3"
    )
    db = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT kind, payload FROM events WHERE kind IN "
        "('model_intended', 'model_decided') ORDER BY sequence"
    ).fetchall()
    db.close()
    inputs, worst = {}, None
    checked = 0
    for kind, raw in rows:
        payload = json.loads(raw)
        if kind == "model_intended":
            inputs[payload["request_id"]] = payload["input"]
            continue
        text = inputs[payload["request_id"]]
        reported = payload["usage"]["input_tokens"]
        assert estimate_serialized_tokens(text, worst) >= reported
        ratio = reported / len(text)
        worst = ratio if worst is None else max(worst, ratio)
        checked += 1
    assert checked >= 14


def test_exact_fit_and_one_token_overflow_use_recorded_counter():
    events = (_event("event-1", 1, "session_created"),)

    def counter(items, text, manifest):
        del items, text, manifest
        return TokenAccounting(1, 2, 2, 0)

    exact = assemble_context(
        _state(),
        events,
        _environment(),
        token_budget=5,
        request_token_counter=counter,
    )
    assert isinstance(exact, ContextBundle)
    assert exact.manifest.allocation["unused"] == 0
    assert exact.manifest.serialized_input_tokens == 1

    overflow = assemble_context(
        _state(),
        events,
        _environment(),
        token_budget=4,
        request_token_counter=counter,
    )
    assert isinstance(overflow, ContextOverflow)
    assert overflow.tokens_required == 5


def test_causal_proximity_prefers_lineage_head_over_distant_hit():
    events = (
        _event("event-1", 1, "session_created"),
        _event("event-9", 9, "question_recorded"),
    )
    hits = (
        _hit(
            "event-1",
            1,
            "older session created inspect fixture",
            -10.0,
            "session_created",
        ),
        _hit(
            "event-9",
            9,
            "distant question without inspect terms",
            -0.1,
            "question_recorded",
        ),
    )
    assembled = assemble_context(
        _state(), events, _environment(), token_budget=16384, hits=hits
    )
    assert isinstance(assembled, ContextBundle)
    retrieved = [item.item_id for item in assembled.items if item.band == "retrieved"]
    assert retrieved[0] == "node:root-1"


def _intended_event(
    event_id: str, sequence: int, proposal_id: str, command: str
) -> Event:
    return Event(
        event_id=event_id,
        session_id="session-1",
        sequence=sequence,
        kind="action_intended",
        objective_version=1,
        payload={
            "proposal": {
                "proposal_id": proposal_id,
                "action_name": "exec",
                "arguments": {"command": command, "timeout_seconds": 20},
            }
        },
        recorded_at="2026-11-02T00:00:00+00:00",
    )


def _receipt_event(
    event_id: str, sequence: int, receipt_id: str, proposal_id: str, digest: str
) -> Event:
    return Event(
        event_id=event_id,
        session_id="session-1",
        sequence=sequence,
        kind="receipt_received",
        objective_version=1,
        payload={
            "receipt": {
                "receipt_id": receipt_id,
                "proposal_id": proposal_id,
                "status": "succeeded",
                "raw_artifact": {
                    "digest": digest,
                    "size": 128,
                    "media_type": "application/json",
                    "provenance": "process-runtime-result",
                },
            }
        },
        recorded_at="2026-11-02T00:00:00+00:00",
    )


def _knowledge_event(event_id: str, sequence: int, item_id: str) -> Event:
    return Event(
        event_id=event_id,
        session_id="session-1",
        sequence=sequence,
        kind="knowledge_recorded",
        objective_version=1,
        payload={
            "item": {
                "item_id": item_id,
                "statement": "port 8080 serves the timing application",
                "status": "supported",
                "scope": "target",
            }
        },
        recorded_at="2026-11-02T00:00:00+00:00",
    )


def _investigation_event(event_id: str, sequence: int, investigation_id: str) -> Event:
    return Event(
        event_id=event_id,
        session_id="session-1",
        sequence=sequence,
        kind="investigation_recorded",
        objective_version=1,
        payload={
            "investigation": {
                "investigation_id": investigation_id,
                "question": "which service listens on port 8080?",
                "disposition": "active",
            }
        },
        recorded_at="2026-11-02T00:00:00+00:00",
    )


def _recent_text(assembled: ContextBundle, event_id: str) -> str:
    texts = {
        item.source_id: item.text for item in assembled.items if item.band == "recent"
    }
    return texts[event_id]


def test_receipts_render_command_status_excerpt_and_digest_link():
    events = (
        _intended_event("event-2", 2, "proposal-1", "nmap -sV -p- 127.0.0.1"),
        _receipt_event("event-3", 3, "receipt-1", "proposal-1", "a" * 64),
    )
    content = {"receipt-1": ReceiptExcerpt("total 4 drwxr-xr-x share", None)}
    assembled = assemble_context(
        _state(), events, _environment(), token_budget=16384, receipt_content=content
    )
    assert isinstance(assembled, ContextBundle)
    text = _recent_text(assembled, "event-3")
    assert "exec" in text
    assert "status=succeeded" in text
    assert "command=nmap -sV -p- 127.0.0.1" in text
    assert "output=`total 4 drwxr-xr-x share`" in text
    assert "artifact=aaaaaaaaaaaa" in text
    item = next(i for i in assembled.items if i.source_id == "event-3")
    assert item.source_event == 3
    assert count_tokens(item.text) <= RECENT_ITEM_MAX_TOKENS


def test_oversized_receipt_excerpts_are_bounded_to_the_item_cap():
    events = (_receipt_event("event-1", 1, "receipt-1", "proposal-1", "a" * 64),)
    content = {"receipt-1": ReceiptExcerpt(" ".join(["word"] * 500), None)}
    assembled = assemble_context(
        _state(), events, _environment(), token_budget=16384, receipt_content=content
    )
    assert isinstance(assembled, ContextBundle)
    item = next(i for i in assembled.items if i.source_id == "event-1")
    assert count_tokens(item.text) <= RECENT_ITEM_MAX_TOKENS
    assert "output=`" in item.text


def test_identical_receipt_outputs_collapse_to_a_counted_line():
    events = (
        _intended_event("event-2", 2, "proposal-1", "pwd && ls -la"),
        _receipt_event("event-3", 3, "receipt-1", "proposal-1", "a" * 64),
        _intended_event("event-5", 5, "proposal-2", "pwd && ls -la"),
        _receipt_event("event-6", 6, "receipt-2", "proposal-2", "b" * 64),
    )
    content = {
        "receipt-1": ReceiptExcerpt("total 4 share", "stdout-normalized:abc"),
        "receipt-2": ReceiptExcerpt("total 4 share", "stdout-normalized:abc"),
    }
    assembled = assemble_context(
        _state(), events, _environment(), token_budget=16384, receipt_content=content
    )
    assert isinstance(assembled, ContextBundle)
    assert "output=`total 4 share`" in _recent_text(assembled, "event-3")
    repeat = _recent_text(assembled, "event-6")
    assert "identical output to event 3 occurrence 2" in repeat
    assert "output=" not in repeat
    again = assemble_context(
        _state(), events, _environment(), token_budget=16384, receipt_content=content
    )
    assert isinstance(again, ContextBundle)
    assert again.manifest == assembled.manifest


def test_recent_band_stops_at_the_recorded_cap_with_typed_omissions():
    events = tuple(
        _receipt_event(
            f"event-{n}", n, f"receipt-{n}", f"proposal-{n}", hex(n)[2:] * 64
        )
        for n in range(1, 61)
    )
    content = {
        f"receipt-{n}": ReceiptExcerpt(" ".join(["out"] * 40), None)
        for n in range(1, 61)
    }
    assembled = assemble_context(
        _state(), events, _environment(), token_budget=24000, receipt_content=content
    )
    assert isinstance(assembled, ContextBundle)
    assert assembled.manifest.allocation["recent"] <= RECENT_BAND_MAX_TOKENS
    reasons = {item_id: reason for item_id, reason in assembled.manifest.omitted}
    assert "band_cap" in reasons.values()
    assert all(
        reason != "token_budget"
        for item_id, reason in assembled.manifest.omitted
        if item_id.startswith("event:")
    )


def test_self_state_block_lists_open_investigations_and_knowledge():
    events = (
        _knowledge_event("event-2", 2, "know-1"),
        _investigation_event("event-3", 3, "inv-1"),
    )
    assembled = assemble_context(_state(), events, _environment(), token_budget=16384)
    assert isinstance(assembled, ContextBundle)
    self_state = next(item for item in assembled.items if item.item_id == "self_state")
    assert "knowledge know-1@2 supported" in self_state.text
    assert "investigation inv-1@3" in self_state.text


def test_self_state_block_reports_empty_state_without_directives():
    assembled = assemble_context(_state(), (), _environment(), token_budget=16384)
    assert isinstance(assembled, ContextBundle)
    self_state = next(item for item in assembled.items if item.item_id == "self_state")
    assert self_state.text == "self_state investigations=none knowledge=none"


def test_unmapped_kinds_keep_the_bare_prefix_and_policy_version_is_2():
    events = (
        _event("event-1", 1, "artifact_read"),
        _event("event-2", 2, "context_assembled"),
    )
    assembled = assemble_context(_state(), events, _environment(), token_budget=16384)
    assert isinstance(assembled, ContextBundle)
    assert _recent_text(assembled, "event-1") == "event 1 artifact_read"
    assert _recent_text(assembled, "event-2") == "event 2 context_assembled"
    assert assembled.manifest.policy_version == 2
