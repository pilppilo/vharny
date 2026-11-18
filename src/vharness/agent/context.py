"""Deterministic context bands, ranking, and inspectable manifests."""

# pylint: disable=too-many-instance-attributes,too-many-lines
# Manifest and item records name every inspectable field; bands, ranking,
# rendering, and the manifest stay in this one module by contract
# (PHASE-0002 context.py responsibilities).

from __future__ import annotations

from collections.abc import Mapping

import json
import math
from dataclasses import dataclass, field, replace
from functools import partial
from types import MappingProxyType
from typing import Callable

from .codec import canonical_json
from .errors import ContractError
from .journal import SearchHit
from .memory import (
    InvestigationDisposition,
    WorkingMemory,
    investigations,
    knowledge_items,
    episodes,
    working_memory,
)
from .models import EnvironmentContract, Event
from .supervision import Guidance, active_guidance, guidance_payload
from .transitions import SessionState

POLICY_VERSION = 2
# The recorded serialized-window count stays the selection heuristic; the
# admission bound is a separate conservative estimate (ADR-0016).
TOKEN_COUNTER = "whitespace-v1"
# COD-73: the estimate is calibrated from provider-reported usage. Before the
# first report it assumes 1.25 characters per token, below every ratio observed
# so far (run6: 1.28-2.95; ARC-AGI-3 hex grids: about 1.75).
TOKEN_ESTIMATOR = "calibrated-chars-v2"
PRIOR_CHARS_PER_TOKEN = 1.25
#: Margin over the worst observed tokens-per-character ratio.
CALIBRATION_MARGIN = 1.1
#: ADR-0020: in calibrated mode, retrieved notes may use at most this share of
#: the space left after must-have context and reserved output; recent history
#: gets the rest, including any share retrieved notes leave unused.
RETRIEVED_SHARE = 0.25
#: COD-89: skip probes that a lower bound proves would fail. Results are
#: identical either way; tests turn it off to compare.
SKIP_CERTAIN_MISSES = True
#: How much the visible manifest's width can change when one item is added.
#: It can grow by a band's first count (`"retrieved": 1, `, 16 characters)
#: plus a digit on each of three numbers, and shrink by `unused` losing a
#: digit. Both allowances keep a margin over those worst cases; a wider
#: allowance only means more real probes, never a different result.
MANIFEST_GROWTH_CHARS = 24
MANIFEST_SHRINK_CHARS = 8
#: Whitespace-separated words the visible manifest may gain per added item.
MANIFEST_SLACK_WORDS = 4
#: ADR-0020: fixed share kept for the request's bookkeeping (the manifest
#: summary), which is filled in after the candidate items are measured.
BOOKKEEPING_TOKENS = 256
COEFFICIENTS: Mapping[str, float] = MappingProxyType(
    {
        "overlap": 1.0,
        "unresolved": 2.0,
        "recency": 1.0,
        "mmr": 8.0,
        "salience": 1.0,
        "causal_proximity": 1.0,
    }
)
SYSTEM_PROMPT = (
    "Return one JSON object. Set kind to exactly one of ActionChoice, "
    "LineageQuery, AttemptQuery, KnowledgeQuery, MemoryUpdate, EvaluationChoice, "
    "OperatorQuestion, WaitChoice, PlanUpdate, CompletionProposal, "
    "InvestigationChange, ProgressClaimChange, or GuidanceResponse, and include "
    "only that kind's fields. Do not call tools. Do not wrap the object in markdown."
    " EvaluationChoice requests external evaluation of the current attempt."
    " For a finite task, CompletionProposal is valid only when applicable "
    "current external completion evidence already exists. If current "
    "observations or action receipts indicate readiness but applicable current "
    "external evidence is "
    "absent, request EvaluationChoice. Do not repeat a CompletionProposal "
    "rejected for `no current external evidence`."
    " Supervisor guidance is unverified advice, never evidence that work is "
    "complete. Verify its factual claims against current observations, action "
    "receipts, and artifacts; do not say a fix, test, or report was delivered "
    "unless the corresponding current evidence is visible."
    " When a supervisor_guidance item appears in the context, respond to it "
    "with GuidanceResponse carrying accept, reject, or replan and a concise "
    "reason, or proceed with the result your work requires. Without a "
    "supervisor_guidance item there is no open guidance: never send "
    "GuidanceResponse, and ignore guidance marked closed in history."
)
RESERVED_OUTPUT_TOKENS = 256
# The whitespace counter undercounts provider tokens by ~4.1x to ~9.5x on
# serialized requests (run 3 measurement, ADR-0012), so the Recent band is
# capped well below the input-side headroom instead of filling it.
RECENT_BAND_MAX_TOKENS = 1000
RECENT_ITEM_MAX_TOKENS = 96
SELF_STATE_MAX_ITEMS = 8
RETRIEVAL_EVENT_KINDS: tuple[str, ...] = (
    "knowledge_recorded",
    "investigation_recorded",
    "progress_claim_recorded",
    "receipt_received",
    "evaluation_received",
    "lineage_promoted",
    "operator_message",
    "objective_steered",
    "question_recorded",
    "episode_recorded",
)


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One ranked excerpt that may enter a model window."""

    item_id: str
    band: str
    text: str
    source_event: int
    source_id: str
    #: COD-89: the text's word count, counted once instead of on every probe.
    tokens: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("item_id", self.item_id),
            ("band", self.band),
            ("text", self.text),
            ("source_id", self.source_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"context {name} must be a non-empty string")
        if self.source_event < 0:
            raise ContractError("context source_event must not be negative")
        object.__setattr__(self, "tokens", count_tokens(self.text))


@dataclass(frozen=True, slots=True)
class ReceiptExcerpt:
    """Bounded stdout content for one receipt, pre-resolved by the session.

    ``excerpt`` is bounded text resolved from the receipt artifact; the
    session also supplies the ADR-0008 normalized digest used for repeat
    collapsing. Receipts without content render without an excerpt.
    """

    excerpt: str
    normalized_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ContextManifest:
    """Inspectable record of what a token budget included and omitted."""

    policy_version: int
    token_counter: str
    objective_version: int
    journal_cursor: int
    token_budget: int
    included: tuple[tuple[str, str], ...]
    omitted: tuple[tuple[str, str], ...]
    tokens_used: int
    item_tokens: tuple[tuple[str, int], ...] = ()
    allocation: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    coefficients: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType(dict(COEFFICIENTS))
    )
    serialized_input_tokens: int | None = None
    system_prompt_tokens: int | None = None
    reserved_output_tokens: int | None = None
    serialization_overhead_tokens: int | None = None
    estimated_input_tokens: int | None = None
    token_estimator: str = TOKEN_ESTIMATOR
    #: Worst provider tokens-per-character ratio the estimate used; None = prior.
    estimate_tokens_per_char: float | None = None
    #: ADR-0020: the measure fit was decided in (TOKEN_COUNTER or TOKEN_ESTIMATOR).
    fit_basis: str = TOKEN_COUNTER
    #: ADR-0020: band limits in the fit basis; empty under TOKEN_COUNTER.
    share_limits: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "coefficients", MappingProxyType(dict(self.coefficients))
        )
        object.__setattr__(self, "allocation", MappingProxyType(dict(self.allocation)))
        object.__setattr__(
            self, "share_limits", MappingProxyType(dict(self.share_limits))
        )


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """Selected items plus the concatenated visible model input."""

    items: tuple[ContextItem, ...]
    manifest: ContextManifest
    text: str


@dataclass(frozen=True, slots=True)
class ContextOverflow:
    """Mandatory content that cannot fit without dropping required identity."""

    missing_item_ids: tuple[str, ...]
    token_budget: int
    tokens_required: int


@dataclass(frozen=True, slots=True)
class TokenAccounting:
    """Exact request-window accounting from the configured token counter."""

    input_tokens: int
    system_prompt_tokens: int
    reserved_output_tokens: int
    serialization_overhead_tokens: int
    estimated_input_tokens: int = 0
    estimated_system_prompt_tokens: int = 0
    #: ADR-0020: TOKEN_ESTIMATOR once a provider has reported usage.
    fit_basis: str = TOKEN_COUNTER
    #: Tokens per character behind ``estimated_input_tokens`` (COD-89 bounds).
    estimate_rate: float = 0.0

    @property
    def total_tokens(self) -> int:
        """Return the full system, request, and reserved-output total.

        Under ADR-0020 the total is in the calibrated estimate once a provider
        has reported usage, and in the recorded word count before that.
        """
        if self.fit_basis == TOKEN_ESTIMATOR:
            return (
                self.estimated_input_tokens
                + self.estimated_system_prompt_tokens
                + self.reserved_output_tokens
            )
        return (
            self.input_tokens + self.system_prompt_tokens + self.reserved_output_tokens
        )


RequestTokenCounter = Callable[
    [tuple[ContextItem, ...], str, ContextManifest], TokenAccounting
]


def count_tokens(text: str) -> int:
    """Count whitespace-delimited tokens. Empty text costs nothing."""
    stripped = text.strip()
    return 0 if not stripped else len(stripped.split())


def estimate_serialized_tokens(text: str, tokens_per_char: float | None = None) -> int:
    """Estimate serialized-window tokens as an upper bound (ADR-0016, COD-73).

    With no provider report yet, assume ``PRIOR_CHARS_PER_TOKEN``, which is
    below every ratio observed so far. Once a provider has reported usage,
    ``tokens_per_char`` is the worst ratio it showed, and the estimate adds
    ``CALIBRATION_MARGIN`` to it. The provider's own count is still recorded
    after each call and reconciled; this estimate only bounds it beforehand.
    """
    return math.ceil(len(text) * estimate_rate(tokens_per_char))


def estimate_rate(tokens_per_char: float | None) -> float:
    """Tokens per character the estimate charges: prior, or calibrated + margin."""
    if tokens_per_char is None:
        return 1 / PRIOR_CHARS_PER_TOKEN
    return tokens_per_char * CALIBRATION_MARGIN


# pylint: disable=too-many-locals,too-many-arguments,too-many-statements
def assemble_context(
    state: SessionState,
    events: tuple[Event, ...],
    environment: EnvironmentContract,
    *,
    token_budget: int,
    hits: tuple[SearchHit, ...] = (),
    overhead_tokens: int = 0,
    reserved_output_tokens: int = 0,
    receipt_content: Mapping[str, ReceiptExcerpt] | None = None,
    request_token_counter: RequestTokenCounter | None = None,
) -> ContextBundle | ContextOverflow:
    """Build mandatory/retrieved/recent bands for one decision.

    The same journal, environment contract, policy, hits, overhead, budget,
    and receipt content produce the same manifest. Coefficients are recorded;
    they are not claimed from AVO. FTS/BM25 relevance uses the existing
    overlap coefficient.
    """
    if token_budget < 1:
        raise ContractError("token_budget must be positive")
    if overhead_tokens < 0 or reserved_output_tokens < 0:
        raise ContractError("overhead and reserved output must not be negative")
    content_map = (
        receipt_content if receipt_content is not None else MappingProxyType({})
    )
    working = working_memory(state, events)
    cursor = events[-1].sequence if events else 0
    overhead = overhead_tokens + reserved_output_tokens
    mandatory = _mandatory_items(
        environment,
        working,
        cursor,
        active_guidance(events),
        state=state,
        events=events,
    )
    required = sum(item.tokens for item in mandatory) + overhead
    if request_token_counter is None and required > token_budget:
        return ContextOverflow(
            missing_item_ids=tuple(item.item_id for item in mandatory),
            token_budget=token_budget,
            tokens_required=required,
        )
    remaining = token_budget - required
    exact_accounting: TokenAccounting | None = None

    # COD-89: exact totals from real probes, and proven upper bounds from
    # skipped ones, keyed by the candidate's item ids.
    exact: dict[tuple[str, ...], TokenAccounting] = {}
    upper: dict[tuple[str, ...], int] = {}

    def settled(candidate: tuple[ContextItem, ...], ceiling: int) -> bool | None:
        """Decide fit from bounds when they are conclusive, else None."""
        if not SKIP_CERTAIN_MISSES or not exact:
            return None
        key = tuple(item.item_id for item in candidate)
        base = exact.get(key[:-1])
        reference = base or next(iter(exact.values()))
        if base is not None and (
            base.total_tokens + _min_growth(candidate[-1], base) > ceiling
        ):
            return False
        base_upper = base.total_tokens if base is not None else upper.get(key[:-1])
        if base_upper is None:
            return None
        bound = base_upper + _max_growth(candidate[-1], reference)
        if bound <= ceiling:
            upper[key] = bound
            return True
        return None

    def fits(candidate: tuple[ContextItem, ...], ceiling: int = token_budget) -> bool:
        """Ask the caller's serializer whether this exact candidate fits."""
        decided = settled(candidate, ceiling)
        if decided is None and SKIP_CERTAIN_MISSES and len(candidate) > 1:
            base_key = tuple(item.item_id for item in candidate[:-1])
            if base_key not in exact:
                # Bounds were inconclusive on an inexact base: measure the base
                # once, so later candidates on it can be settled exactly.
                measure(candidate[:-1])
                decided = settled(candidate, ceiling)
        if decided is not None:
            return decided
        return measure(candidate).total_tokens <= ceiling

    def measure(candidate: tuple[ContextItem, ...]) -> TokenAccounting:
        """Render and count one candidate request, and remember the result."""
        text = "\n".join(item.text for item in candidate)
        provisional = _manifest(
            state,
            cursor,
            token_budget,
            candidate,
            (),
            overhead_tokens=0,
            reserved_output_tokens=0,
            serialized_input_tokens=None,
            system_prompt_tokens=None,
            serialization_overhead_tokens=None,
        )
        counter = request_token_counter
        if counter is None:  # pragma: no cover - only called in exact mode
            raise ContractError("measuring a candidate needs a request token counter")
        accounting = counter(candidate, text, provisional)
        exact[tuple(item.item_id for item in candidate)] = accounting
        return accounting

    share_limits: Mapping[str, int] = MappingProxyType({})
    retrieved_fits: Callable[[tuple[ContextItem, ...]], bool] | None = None
    recent_fits: Callable[[tuple[ContextItem, ...]], bool] | None = None
    if request_token_counter is not None:
        mandatory_accounting = _required_exact_accounting(
            request_token_counter,
            state,
            cursor,
            token_budget,
            mandatory,
        )
        required = mandatory_accounting.total_tokens
        if required > token_budget:
            return ContextOverflow(
                missing_item_ids=tuple(item.item_id for item in mandatory),
                token_budget=token_budget,
                tokens_required=required,
            )
        remaining = token_budget
        retrieved_ceiling, recent_ceiling, share_limits = _shares(
            mandatory_accounting.fit_basis, required, token_budget
        )
        retrieved_fits = partial(fits, ceiling=retrieved_ceiling)
        recent_fits = partial(fits, ceiling=recent_ceiling)
    retrieved, omitted_retrieved = _select_retrieved(
        state,
        events,
        working,
        remaining,
        cursor,
        hits,
        prefix=mandatory if request_token_counter is not None else (),
        fits=retrieved_fits,
    )
    retrieved_tokens = sum(item.tokens for item in retrieved)
    remaining -= retrieved_tokens
    recent, omitted_recent = _select_recent(
        events,
        remaining,
        retrieved,
        prefix=(*mandatory, *retrieved) if request_token_counter is not None else (),
        fits=recent_fits,
        receipt_content=content_map,
    )
    recent_tokens = sum(item.tokens for item in recent)
    remaining -= recent_tokens
    items = (*mandatory, *retrieved, *recent)
    filtered, preferred = _prefer_sources_to_summaries(items, (), events)
    dropped = {item.item_id for item in items} - {item.item_id for item in filtered}
    remaining += sum(item.tokens for item in items if item.item_id in dropped)
    items = filtered
    omitted = (*omitted_retrieved, *omitted_recent, *preferred)
    retrieved_tokens = sum(item.tokens for item in items if item.band == "retrieved")
    recent_tokens = sum(item.tokens for item in items if item.band == "recent")
    text = "\n".join(item.text for item in items)
    mandatory_tokens = sum(item.tokens for item in mandatory)
    manifest = _manifest(
        state,
        cursor,
        token_budget,
        items,
        omitted,
        overhead_tokens=overhead_tokens,
        reserved_output_tokens=reserved_output_tokens,
        serialized_input_tokens=None,
        system_prompt_tokens=None,
        serialization_overhead_tokens=None,
        mandatory_tokens=mandatory_tokens,
        retrieved_tokens=retrieved_tokens,
        recent_tokens=recent_tokens,
        unused=remaining,
    )
    if request_token_counter is not None:
        exact_accounting = request_token_counter(items, text, manifest)
        manifest = _manifest(
            state,
            cursor,
            token_budget,
            items,
            omitted,
            overhead_tokens=exact_accounting.serialization_overhead_tokens,
            reserved_output_tokens=exact_accounting.reserved_output_tokens,
            serialized_input_tokens=exact_accounting.input_tokens,
            system_prompt_tokens=exact_accounting.system_prompt_tokens,
            serialization_overhead_tokens=(
                exact_accounting.serialization_overhead_tokens
            ),
            estimated_input_tokens=exact_accounting.estimated_input_tokens,
            mandatory_tokens=mandatory_tokens,
            retrieved_tokens=retrieved_tokens,
            recent_tokens=recent_tokens,
            unused=token_budget - exact_accounting.total_tokens,
        )
        manifest = replace(
            manifest,
            fit_basis=exact_accounting.fit_basis,
            share_limits=share_limits,
        )
    return ContextBundle(items, manifest, text)


def _mandatory_items(
    environment: EnvironmentContract,
    working: WorkingMemory,
    cursor: int,
    guidance: tuple[str, Guidance] | None,
    *,
    state: SessionState,
    events: tuple[Event, ...],
) -> tuple[ContextItem, ...]:
    actions = canonical_json(
        [
            {
                "name": action.name,
                "description": action.description,
                "argument_schema": dict(action.argument_schema),
            }
            for action in environment.actions
        ]
    )
    attempt = working.active_attempt_id or "none"
    pending = working.pending_proposal_id or "none"
    direction = working.operator_direction or "none"
    wait = working.wait_reason or "none"
    plan = working.current_plan
    receipt = (
        canonical_json(working.latest_receipt) if working.latest_receipt else "none"
    )
    observation = canonical_json(working.observation) if working.observation else "none"
    texts = (
        (
            "objective",
            (
                f"objective_v{working.task.objective_version} "
                f"{working.task.objective} evidence={working.task.success_evidence}"
            ),
        ),
        (
            "environment",
            f"environment {environment.environment_id} actions={actions}",
        ),
        ("operator", f"operator_direction {direction}"),
        (
            "evaluation",
            f"evaluation {working.task.evaluation_contract_id}",
        ),
        (
            "lineage_head",
            (
                f"lineage_head {working.lineage_head.node_id} "
                f"parent={working.lineage_head.parent_id or 'none'}"
            ),
        ),
        ("attempt", f"active_attempt {attempt} pending={pending} wait={wait}"),
        ("plan", f"current_plan {plan}"),
        ("self_state", _self_state_text(state, events)),
        ("budgets", f"budgets remaining {working.budgets}"),
        ("receipt", f"latest_receipt {receipt}"),
        ("observation", f"observation {observation}"),
    )
    items = [
        ContextItem(item_id, "mandatory", text, cursor, item_id)
        for item_id, text in texts
    ]
    if guidance is not None:
        source_id, body = guidance
        items.append(
            ContextItem(
                "supervision",
                "mandatory",
                f"supervisor_guidance {canonical_json(dict(guidance_payload(body)))}",
                cursor,
                source_id,
            )
        )
    return tuple(items)


# pylint: disable=too-many-arguments,too-many-positional-arguments
def _self_state_text(state: SessionState, events: tuple[Event, ...]) -> str:
    """Bounded derived self-state for the mandatory band (ADR-0012)."""
    lines: list[str] = []
    active = tuple(
        investigation
        for investigation in investigations(events)
        if investigation.disposition is InvestigationDisposition.ACTIVE
    )
    for investigation in active[:SELF_STATE_MAX_ITEMS]:
        lines.append(
            f"investigation {investigation.investigation_id}"
            f"@{investigation.source_event} "
            f"{_bounded_words(investigation.question, 16)}"
        )
    if len(active) > SELF_STATE_MAX_ITEMS:
        lines.append(f"+{len(active) - SELF_STATE_MAX_ITEMS} investigations")
    knowledge = knowledge_items(state, events)
    for item in knowledge[:SELF_STATE_MAX_ITEMS]:
        lines.append(
            f"knowledge {item.item_id}@{item.source_event} "
            f"{item.status.value} {_bounded_words(item.statement, 16)}"
        )
    if len(knowledge) > SELF_STATE_MAX_ITEMS:
        lines.append(f"+{len(knowledge) - SELF_STATE_MAX_ITEMS} knowledge")
    if not lines:
        return "self_state investigations=none knowledge=none"
    return "self_state " + "; ".join(lines)


# pylint: disable=too-many-arguments,too-many-positional-arguments
def _select_retrieved(
    state: SessionState,
    events: tuple[Event, ...],
    working: WorkingMemory,
    remaining: int,
    cursor: int,
    hits: tuple[SearchHit, ...],
    *,
    prefix: tuple[ContextItem, ...] = (),
    fits: Callable[[tuple[ContextItem, ...]], bool] | None = None,
) -> tuple[tuple[ContextItem, ...], tuple[tuple[str, str], ...]]:
    """Greedy MMR: re-score remaining candidates after every selection."""
    remaining_items = list(_retrieved_candidates(state, events, cursor, hits))
    query = tuple(working.task.objective.lower().split())
    salience_query = _salience_query(working)
    relevance = _relevance_by_event(hits)
    selected: list[ContextItem] = []
    omitted: list[tuple[str, str]] = []
    budget = remaining
    while remaining_items:
        best = min(
            remaining_items,
            key=lambda item: (
                -_retrieved_score(
                    item,
                    query,
                    cursor,
                    tuple(selected),
                    relevance,
                    salience_query,
                    state,
                ),
                item.source_event,
                item.item_id,
            ),
        )
        remaining_items.remove(best)
        score = _retrieved_score(
            best, query, cursor, tuple(selected), relevance, salience_query, state
        )
        cost = count_tokens(best.text)
        candidate = (*prefix, *selected, best)
        if (fits is not None and not fits(candidate)) or (
            fits is None and cost > budget
        ):
            omitted.append((best.item_id, "token_budget"))
            continue
        if score <= 0 and selected:
            omitted.append((best.item_id, "low_relevance"))
            continue
        selected.append(best)
        if fits is None:
            budget -= cost
    return tuple(selected), tuple(omitted)


def _retrieved_candidates(
    state: SessionState,
    events: tuple[Event, ...],
    cursor: int,
    hits: tuple[SearchHit, ...],
) -> tuple[ContextItem, ...]:
    items: list[ContextItem] = []
    for item in knowledge_items(state, events):
        items.append(
            ContextItem(
                f"knowledge:{item.item_id}",
                "retrieved",
                f"knowledge {item.status.value} {item.statement} scope={item.scope}",
                item.source_event,
                item.item_id,
            )
        )
    for node in state.accepted_lineage:
        items.append(
            ContextItem(
                f"node:{node.node_id}",
                "retrieved",
                (
                    f"committed {node.node_id} parent={node.parent_id or 'none'} "
                    f"eval={node.evaluation_receipt_id or 'none'}"
                ),
                cursor,
                node.node_id,
            )
        )
    for history in state.attempt_history:
        attempt = history.attempt
        if attempt.disposition.value == "accepted":
            continue
        items.append(
            ContextItem(
                f"attempt:{attempt.attempt_id}",
                "retrieved",
                (
                    f"attempt {attempt.attempt_id} "
                    f"disposition={attempt.disposition.value} "
                    f"base={attempt.base_node_id}"
                ),
                history.event_start_cursor,
                attempt.attempt_id,
            )
        )
    for episode in episodes(events):
        items.append(
            ContextItem(
                f"episode:{episode.episode_id}",
                "retrieved",
                (
                    f"episode {episode.episode_id} "
                    f"sources={episode.source_start}-{episode.source_end} "
                    f"{episode.summary}"
                ),
                episode.source_event,
                episode.episode_id,
            )
        )
    for investigation in investigations(events):
        if investigation.disposition is not InvestigationDisposition.ACTIVE:
            continue
        items.append(
            ContextItem(
                f"investigation:{investigation.investigation_id}",
                "retrieved",
                (
                    f"investigation {investigation.investigation_id} "
                    f"{investigation.question} "
                    f"disposition={investigation.disposition.value}"
                ),
                investigation.source_event,
                investigation.investigation_id,
            )
        )
    covered = {
        item.source_event
        for item in items
        if not item.item_id.startswith("node:")
        and not item.item_id.startswith("episode:")
    }
    for hit in hits:
        if hit.sequence in covered:
            continue
        items.append(
            ContextItem(
                f"fts:{hit.event_id}",
                "retrieved",
                f"event {hit.sequence} {hit.kind} {hit.excerpt}",
                hit.sequence,
                hit.event_id,
            )
        )
        covered.add(hit.sequence)
    return tuple(items)


def _retrieved_score(
    item: ContextItem,
    query: tuple[str, ...],
    cursor: int,
    selected: tuple[ContextItem, ...],
    relevance: Mapping[int, float],
    salience_query: tuple[str, ...],
    state: SessionState,
) -> float:
    overlap = _item_overlap(item, query, relevance) * COEFFICIENTS["overlap"]
    unresolved = (
        COEFFICIENTS["unresolved"] if "hypothesis" in item.text.split() else 0.0
    )
    age = max(cursor - item.source_event, 0)
    recency = COEFFICIENTS["recency"] / (1.0 + age)
    salience = _overlap(salience_query, item.text) * COEFFICIENTS["salience"]
    causal = _causal_proximity(item, state, cursor) * COEFFICIENTS["causal_proximity"]
    penalty = 0.0
    if selected:
        penalty = COEFFICIENTS["mmr"] * max(
            _jaccard(item.text, other.text) for other in selected
        )
    return overlap + unresolved + recency + salience + causal - penalty


def _prefer_sources_to_summaries(
    selected: tuple[ContextItem, ...],
    omitted: tuple[tuple[str, str], ...],
    events: tuple[Event, ...],
) -> tuple[tuple[ContextItem, ...], tuple[tuple[str, str], ...]]:
    """Keep source excerpts when both a span and its episode summary fit."""
    by_id = {episode.episode_id: episode for episode in episodes(events)}
    kept: list[ContextItem] = []
    extra: list[tuple[str, str]] = []
    for item in selected:
        if not item.item_id.startswith("episode:"):
            kept.append(item)
            continue
        episode = by_id.get(item.source_id)
        if episode is None:
            kept.append(item)
            continue
        if any(
            other.band == "retrieved"
            and not other.item_id.startswith("episode:")
            and episode.source_start <= other.source_event <= episode.source_end
            for other in selected
        ):
            extra.append((item.item_id, "source_preferred"))
            continue
        kept.append(item)
    return tuple(kept), (*omitted, *extra)


def _salience_query(working: WorkingMemory) -> tuple[str, ...]:
    blob = (
        f"{working.current_plan} {working.operator_direction or ''} "
        f"{canonical_json(working.observation)}"
    )
    return tuple(blob.lower().split())


def _causal_proximity(item: ContextItem, state: SessionState, cursor: int) -> float:
    head = state.lineage_head.node_id
    parent = state.lineage_head.parent_id
    attempt_id = (
        None if state.active_attempt is None else state.active_attempt.attempt_id
    )
    if item.source_id in {head, attempt_id} or (
        parent is not None and item.source_id == parent
    ):
        return 1.0
    if attempt_id is None:
        return 0.0
    start: int | None = None
    for history in state.attempt_history:
        if history.attempt.attempt_id == attempt_id:
            start = history.event_start_cursor
            break
    if start is not None and start <= item.source_event <= cursor:
        return 0.5
    return 0.0


def _item_overlap(
    item: ContextItem, query: tuple[str, ...], relevance: Mapping[int, float]
) -> float:
    if not item.item_id.startswith("node:") and item.source_event in relevance:
        return relevance[item.source_event]
    return _overlap(query, item.text)


def _relevance_by_event(hits: tuple[SearchHit, ...]) -> Mapping[int, float]:
    """Scale BM25 onto 0..1 so the overlap coefficient stays 1.0."""
    scores: dict[int, float] = {}
    for hit in hits:
        current = scores.get(hit.sequence)
        score = hit.relevance
        if current is None or score > current:
            scores[hit.sequence] = score
    if not scores:
        return scores
    lowest = min(scores.values())
    highest = max(scores.values())
    if highest == lowest:
        return {sequence: 1.0 for sequence in scores}
    span = highest - lowest
    return {sequence: (value - lowest) / span for sequence, value in scores.items()}


@dataclass(frozen=True, slots=True)
class _RecentRenderContext:
    """Per-assembly render inputs derived once from the event tuple."""

    proposals: Mapping[str, tuple[str, Mapping[str, object]]]
    receipt_content: Mapping[str, ReceiptExcerpt]
    occurrences: Mapping[str, tuple[int, int]]
    #: COD-76: the event id of the one open guidance, if any. Every other
    #: guidance_received event renders as closed, without its advice, so the
    #: model is never invited to answer guidance that no longer applies.
    open_guidance_id: str | None

    @classmethod
    def build(
        cls,
        events: tuple[Event, ...],
        receipt_content: Mapping[str, ReceiptExcerpt],
    ) -> _RecentRenderContext:
        """Pair action intents with their proposals and precompute repeats."""

        # pylint: disable=duplicate-code
        # The payload-shape guards mirror supervision's effect summaries:
        # both walk action intents defensively, but the render context needs
        # arguments and supervision's map is deliberately name-only.
        proposals: dict[str, tuple[str, Mapping[str, object]]] = {}
        for event in events:
            if event.kind != "action_intended":
                continue
            proposal = event.payload.get("proposal")
            if not isinstance(proposal, Mapping):
                continue
            proposal_id = proposal.get("proposal_id")
            name = proposal.get("action_name")
            if not isinstance(proposal_id, str) or not isinstance(name, str):
                continue
            arguments = proposal.get("arguments")
            proposals[proposal_id] = (
                name,
                arguments if isinstance(arguments, Mapping) else MappingProxyType({}),
            )
        active = active_guidance(events)
        return cls(
            proposals,
            receipt_content,
            _receipt_occurrences(events, receipt_content),
            active[0] if active is not None else None,
        )


def _receipt_occurrences(
    events: tuple[Event, ...],
    receipt_content: Mapping[str, ReceiptExcerpt],
) -> Mapping[str, tuple[int, int]]:
    """Map receipt event ids to (first sequence, occurrence) by ADR-0008 digest."""

    # pylint: disable=duplicate-code
    # Receipt-payload shape guards are shared with the supervision walk; this
    # pass derives occurrence counts for rendering instead of effects.
    first: dict[str, int] = {}
    counts: dict[str, int] = {}
    occurrences: dict[str, tuple[int, int]] = {}
    for event in events:
        if event.kind != "receipt_received":
            continue
        receipt = event.payload.get("receipt")
        if not isinstance(receipt, Mapping):
            continue
        content = receipt_content.get(str(receipt.get("receipt_id")))
        digest = content.normalized_fingerprint if content is not None else None
        if not digest:
            continue
        counts[digest] = counts.get(digest, 0) + 1
        first.setdefault(digest, event.sequence)
        occurrences[event.event_id] = (first[digest], counts[digest])
    return occurrences


def _select_recent(
    events: tuple[Event, ...],
    remaining: int,
    retrieved: tuple[ContextItem, ...],
    *,
    prefix: tuple[ContextItem, ...] = (),
    fits: Callable[[tuple[ContextItem, ...]], bool] | None = None,
    receipt_content: Mapping[str, ReceiptExcerpt] = MappingProxyType({}),
) -> tuple[tuple[ContextItem, ...], tuple[tuple[str, str], ...]]:
    retrieved_sources = {item.source_id for item in retrieved}
    context = _RecentRenderContext.build(events, receipt_content)
    selected: list[ContextItem] = []
    omitted: list[tuple[str, str]] = []
    budget = remaining
    band_used = 0
    for event in reversed(events):
        item = ContextItem(
            f"event:{event.event_id}",
            "recent",
            _recent_event_text(event, context),
            event.sequence,
            event.event_id,
        )
        if event.event_id in retrieved_sources:
            omitted.append((item.item_id, "duplicate"))
            continue
        cost = item.tokens
        if band_used + cost > RECENT_BAND_MAX_TOKENS:
            omitted.append((item.item_id, "band_cap"))
            continue
        candidate = (*prefix, *selected, item)
        if (fits is not None and not fits(candidate)) or (
            fits is None and cost > budget
        ):
            omitted.append((item.item_id, "token_budget"))
            continue
        selected.append(item)
        band_used += cost
        if fits is None:
            budget -= cost
    selected.reverse()
    return tuple(selected), tuple(omitted)


_GUIDANCE_CLOSING_KINDS = frozenset(
    {
        "guidance_accepted",
        "guidance_rejected",
        "guidance_expired",
        "guidance_suppressed",
        "guidance_response_rejected",
    }
)


def _recent_event_text(event: Event, context: _RecentRenderContext) -> str:
    """Render bounded per-kind content; unmapped kinds keep the bare prefix."""
    # pylint: disable=too-many-return-statements
    # One guarded branch per rendered kind; a dispatch table would need a
    # uniform renderer signature and shadow the payload guards.
    prefix = f"event {event.sequence} {event.kind}"
    if event.kind == "receipt_received":
        return _render_receipt(event, prefix, context)
    if event.kind == "action_intended":
        return _render_action_intended(event, prefix)
    if event.kind in ("model_decided", "model_malformed"):
        return _render_model_decided(event, prefix)
    if event.kind in {"knowledge_recorded", "investigation_recorded"}:
        return _render_record(event, prefix)
    if event.kind in {"progress_claim_recorded", "progress_claim_rejected"}:
        return _render_claim(event, prefix)
    if event.kind == "operator_message":
        return _render_command_field(event, prefix, "text")
    if event.kind == "objective_steered":
        return _render_command_field(event, prefix, "objective")
    if event.kind == "evaluation_received":
        return _render_evaluation(event, prefix)
    if event.kind == "guidance_received" or event.kind in _GUIDANCE_CLOSING_KINDS:
        return _render_guidance(event, prefix, context.open_guidance_id)
    if event.kind == "attempt_started":
        return _render_attempt_started(event, prefix)
    if event.kind == "attempt_inspected":
        return _render_attempt_inspected(event, prefix)
    if event.kind == "completion_rejected":
        return _render_completion_rejected(event, prefix)
    return prefix


def _render_receipt(event: Event, prefix: str, context: _RecentRenderContext) -> str:
    """Render one receipt: command, status, bounded excerpt, digest link."""
    receipt = event.payload.get("receipt")
    if not isinstance(receipt, Mapping):
        return prefix
    proposal_id = receipt.get("proposal_id")
    entry = context.proposals.get(proposal_id) if isinstance(proposal_id, str) else None
    name = entry[0] if entry else "unknown"
    arguments = entry[1] if entry else MappingProxyType({})
    parts = [prefix, name, f"status={receipt.get('status')}"]
    occurrence = context.occurrences.get(event.event_id)
    if occurrence is not None and occurrence[1] > 1:
        parts.append(
            f"identical output to event {occurrence[0]} occurrence {occurrence[1]}"
        )
        return " ".join(parts)
    command = _action_command(arguments)
    if command:
        parts.append(f"command={_bounded_words(command, 24)}")
    content = context.receipt_content.get(str(receipt.get("receipt_id")))
    if content is not None and content.excerpt:
        used = count_tokens(" ".join(parts)) + 3
        limit = max(RECENT_ITEM_MAX_TOKENS - used, 0)
        if limit:
            parts.append(f"output=`{_bounded_words(content.excerpt, limit)}`")
    raw = receipt.get("raw_artifact")
    if isinstance(raw, Mapping):
        digest = raw.get("digest")
        if isinstance(digest, str):
            parts.append(f"artifact={digest[:12]}")
    return " ".join(parts)


def _render_action_intended(event: Event, prefix: str) -> str:
    proposal = event.payload.get("proposal")
    if not isinstance(proposal, Mapping):
        return prefix
    name = proposal.get("action_name")
    if not isinstance(name, str) or not name:
        return prefix
    arguments = proposal.get("arguments")
    parts = [prefix, name]
    command = _action_command(
        arguments if isinstance(arguments, Mapping) else MappingProxyType({})
    )
    if command:
        parts.append(f"command={_bounded_words(command, 24)}")
    return " ".join(parts)


def _render_model_decided(event: Event, prefix: str) -> str:
    """Render a decision's result kind, or a failed decision's class (ADR-0022)."""
    if event.kind == "model_malformed":
        failure_class = event.payload.get("failure_class")
        if not isinstance(failure_class, str) or not failure_class:
            return prefix
        return f"{prefix} failure_class={failure_class}"
    result_kind = event.payload.get("result_kind")
    if not isinstance(result_kind, str) or not result_kind:
        return prefix
    return f"{prefix} result_kind={result_kind}"


def _render_record(event: Event, prefix: str) -> str:
    if event.kind == "knowledge_recorded":
        payload = event.payload.get("item")
        label = "knowledge"
    else:
        payload = event.payload.get("investigation")
        label = "investigation"
    if not isinstance(payload, Mapping):
        return prefix
    statement_field = "statement" if label == "knowledge" else "question"
    status_field = "status" if label == "knowledge" else "disposition"
    statement = payload.get(statement_field)
    if not isinstance(statement, str) or not statement.strip():
        return prefix
    parts = [prefix]
    status = payload.get(status_field)
    if isinstance(status, str) and status:
        parts.append(f"{label}={status}")
    parts.append(_bounded_words(statement, 24))
    return " ".join(parts)


def _render_claim(event: Event, prefix: str) -> str:
    claim = event.payload.get("claim")
    if not isinstance(claim, Mapping):
        return prefix
    parts = [prefix]
    before = claim.get("before")
    after = claim.get("after")
    if isinstance(before, str) and before.strip():
        parts.append(f"{_bounded_words(before, 12)} ->")
    if isinstance(after, str) and after.strip():
        parts.append(_bounded_words(after, 12))
    return " ".join(parts) if len(parts) > 1 else prefix


def _render_command_field(event: Event, prefix: str, name: str) -> str:
    command = event.payload.get("command")
    if not isinstance(command, Mapping):
        return prefix
    value = command.get(name)
    if not isinstance(value, str) or not value.strip():
        return prefix
    return f"{prefix} {_bounded_words(value, 24)}"


def _render_evaluation(event: Event, prefix: str) -> str:
    parts = [prefix]
    disposition = event.payload.get("disposition")
    if isinstance(disposition, str) and disposition:
        parts.append(f"disposition={disposition}")
    receipt = event.payload.get("receipt")
    if (
        isinstance(receipt, Mapping)
        and isinstance(receipt.get("status"), str)
        and receipt["status"]
    ):
        parts.append(f"status={receipt['status']}")
    return " ".join(parts) if len(parts) > 1 else prefix


def _render_guidance(event: Event, prefix: str, open_guidance_id: str | None) -> str:
    """Render open guidance with its advice; closed guidance only as closed."""
    if event.kind != "guidance_received":
        return f"{prefix}; guidance closed"
    if event.event_id != open_guidance_id:
        return f"{prefix} closed; needs no response"
    guidance = event.payload.get("guidance")
    if not isinstance(guidance, Mapping):
        return prefix
    parts = [prefix]
    diagnosis = guidance.get("diagnosis")
    if isinstance(diagnosis, str) and diagnosis.strip():
        parts.append(_bounded_words(diagnosis, 24))
    expires = guidance.get("expires_after_call")
    if isinstance(expires, int) and not isinstance(expires, bool):
        parts.append(f"expires_after_call={expires}")
    return " ".join(parts) if len(parts) > 1 else prefix


def _render_attempt_started(event: Event, prefix: str) -> str:
    attempt = event.payload.get("attempt")
    if not isinstance(attempt, Mapping):
        return prefix
    attempt_id = attempt.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return prefix
    return f"{prefix} attempt={attempt_id}"


def _render_attempt_inspected(event: Event, prefix: str) -> str:
    attempt = event.payload.get("attempt")
    if not isinstance(attempt, Mapping):
        return prefix
    attempt_id = attempt.get("attempt_id")
    found = attempt.get("found")
    disposition = attempt.get("disposition")
    sources = attempt.get("sources")
    details = []
    if isinstance(sources, (tuple, list)):
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            sequence = source.get("sequence")
            source_id = source.get("event_id")
            summary = source.get("summary")
            if (
                isinstance(sequence, int)
                and isinstance(source_id, str)
                and isinstance(summary, str)
            ):
                details.append(f"{sequence}:{source_id} {_bounded_words(summary, 16)}")
    return (
        f"{prefix} attempt={attempt_id} found={found} "
        f"disposition={disposition} " + " | ".join(details)
    ).strip()


def _render_completion_rejected(event: Event, prefix: str) -> str:
    reason = event.payload.get("reason")
    if not isinstance(reason, str):
        return prefix
    normalized = " ".join(reason.split())
    return f"{prefix} reason={normalized}" if normalized else prefix


def _action_command(arguments: Mapping[str, object]) -> str | None:
    """Return the first target-bearing string argument of one action."""
    for name in ("command", "path", "job_id"):
        value = arguments.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _bounded_words(text: str, limit: int) -> str:
    """Bound text to at most ``limit`` whitespace tokens, ellipsized."""
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]) + " ..."


def _overlap(query: tuple[str, ...], text: str) -> float:
    if not query:
        return 0.0
    terms = set(text.lower().split())
    return sum(1 for token in query if token in terms) / len(query)


def _jaccard(left: str, right: str) -> float:
    left_terms = set(left.lower().split())
    right_terms = set(right.lower().split())
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def _manifest(
    state: SessionState,
    cursor: int,
    token_budget: int,
    items: tuple[ContextItem, ...],
    omitted: tuple[tuple[str, str], ...],
    *,
    overhead_tokens: int,
    reserved_output_tokens: int,
    serialized_input_tokens: int | None,
    system_prompt_tokens: int | None,
    serialization_overhead_tokens: int | None,
    estimated_input_tokens: int | None = None,
    mandatory_tokens: int | None = None,
    retrieved_tokens: int | None = None,
    recent_tokens: int | None = None,
    unused: int | None = None,
) -> ContextManifest:
    """Build one manifest shape for fit probes and the returned bundle."""
    mandatory = (
        sum(item.tokens for item in items if item.band == "mandatory")
        if mandatory_tokens is None
        else mandatory_tokens
    )
    retrieved = (
        sum(item.tokens for item in items if item.band == "retrieved")
        if retrieved_tokens is None
        else retrieved_tokens
    )
    recent = (
        sum(item.tokens for item in items if item.band == "recent")
        if recent_tokens is None
        else recent_tokens
    )
    context_tokens = count_tokens("\n".join(item.text for item in items))
    return ContextManifest(
        policy_version=POLICY_VERSION,
        token_counter=TOKEN_COUNTER,
        objective_version=state.task.objective_version,
        journal_cursor=cursor,
        token_budget=token_budget,
        included=tuple((item.item_id, item.band) for item in items),
        omitted=omitted,
        tokens_used=context_tokens,
        item_tokens=tuple((item.item_id, item.tokens) for item in items),
        allocation=MappingProxyType(
            {
                "overhead": overhead_tokens,
                "reserved_output": reserved_output_tokens,
                "mandatory": mandatory,
                "retrieved": retrieved,
                "recent": recent,
                "unused": (
                    token_budget
                    - overhead_tokens
                    - reserved_output_tokens
                    - mandatory
                    - retrieved
                    - recent
                    if unused is None
                    else unused
                ),
            }
        ),
        serialized_input_tokens=serialized_input_tokens,
        system_prompt_tokens=system_prompt_tokens,
        reserved_output_tokens=reserved_output_tokens,
        serialization_overhead_tokens=serialization_overhead_tokens,
        estimated_input_tokens=estimated_input_tokens,
    )


def _min_growth(item: ContextItem, before: TokenAccounting) -> int:
    """A lower bound on how much adding ``item`` grows the measured total.

    The request is ``json.dumps`` output, and context items are joined with a
    newline, which serializes without whitespace. So in the word count the
    item adds at least its own words minus one (a merged boundary word). In
    the calibrated estimate it adds at least its escaped length, less
    ``MANIFEST_SHRINK_CHARS`` for manifest numbers losing width, at the
    estimate's rate.
    """
    if before.fit_basis == TOKEN_ESTIMATOR:
        escaped = len(json.dumps(item.text)) - 2
        return max(
            0,
            math.floor((escaped - MANIFEST_SHRINK_CHARS) * before.estimate_rate) - 1,
        )
    return max(0, item.tokens - 1)


def _max_growth(item: ContextItem, reference: TokenAccounting) -> int:
    """An upper bound on how much adding ``item`` grows the measured total.

    The mirror of ``_min_growth``: the item's escaped length plus its newline
    separator and ``MANIFEST_GROWTH_CHARS`` at the estimate's rate, or its words plus
    the manifest's word slack.
    """
    if reference.fit_basis == TOKEN_ESTIMATOR:
        escaped = len(json.dumps(item.text)) - 2
        return (
            math.ceil((escaped + 2 + MANIFEST_GROWTH_CHARS) * reference.estimate_rate)
            + 1
        )
    return item.tokens + MANIFEST_SLACK_WORDS


def _shares(
    fit_basis: str, required: int, token_budget: int
) -> tuple[int, int, Mapping[str, int]]:
    """ADR-0020: the retrieved and recent ceilings, and band limits.

    Under the word count the whole budget stays open to every band, as before.
    In the calibrated estimate, ``BOOKKEEPING_TOKENS`` are kept for the
    manifest summary; retrieved notes may use ``RETRIEVED_SHARE`` of what is
    left after must-have context and reserved output; recent history may use
    everything else, including any share retrieved notes leave unused.
    """
    if fit_basis != TOKEN_ESTIMATOR:
        return token_budget, token_budget, MappingProxyType({})
    open_space = max(0, token_budget - required - BOOKKEEPING_TOKENS)
    retrieved_ceiling = required + math.floor(RETRIEVED_SHARE * open_space)
    recent_ceiling = required + open_space
    return (
        retrieved_ceiling,
        recent_ceiling,
        MappingProxyType(
            {
                "mandatory": required,
                "retrieved": retrieved_ceiling - required,
                "recent": recent_ceiling - retrieved_ceiling,
                "bookkeeping": token_budget - recent_ceiling,
            }
        ),
    )


def _required_exact_accounting(
    counter: RequestTokenCounter,
    state: SessionState,
    cursor: int,
    token_budget: int,
    mandatory: tuple[ContextItem, ...],
) -> TokenAccounting:
    """Return the accounting for mandatory context alone."""
    text = "\n".join(item.text for item in mandatory)
    manifest = _manifest(
        state,
        cursor,
        token_budget,
        mandatory,
        (),
        overhead_tokens=0,
        reserved_output_tokens=0,
        serialized_input_tokens=None,
        system_prompt_tokens=None,
        serialization_overhead_tokens=None,
    )
    return counter(mandatory, text, manifest)
