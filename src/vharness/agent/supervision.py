"""Deterministic progress monitor for PHASE-0003 supervision.

ARCH-0001 (section "Progress, supervision, and variation") fixes the monitor
signals and escalation path but intentionally chooses no numerical trigger
defaults. The shipped default is the ADR-0014 adoption of the EXP-0003
selection, ``monitor.p6.v1``: the EXP-0001 repetition policy (window 8,
repetition threshold 3, cooldown 4) over action, failure, and evaluation
fingerprints, extended with the R1/R2 evidence-repeat fingerprints, with the
B2 claim-free budget-burn trigger replaced by the ADR-0013 supported-progress
budget checkpoint. :func:`monitor_policy_p5` retains the ADR-0011 selection
and :func:`monitor_policy_p1` retains the EXP-0001 selection verbatim for
retained-evidence recomputation: historical journals keep their recorded
policy semantics and are never rewritten as P6 outcomes. Policies are
versioned run policy, not architecture constants.

R1/R2 fingerprint the stdout payload the agent received in a successful
receipt of an evidence-bearing action (``exec``/``job_read``): R1 hashes the
payload exactly as received, R2 hashes it after stripping transport headers
and echo markers per the frozen EXP-0002 normalization rule. The session
computes both at receipt time and journals them on the ``receipt_received``
event, so the monitor stays a pure function over the journal;
:func:`evaluate_supervision` also accepts an artifact reader to compute the
fingerprints for journals recorded before that field existed (for example
retained acceptance-run replays). The B2 burn trigger fires at
``ceil(burn_fraction * model_call_budget)`` consumed calls without a
first-seen evidence fingerprint and shares the repetition cooldown.

The ADR-0013 budget checkpoint replaces that evidence-novelty reset under the
shipped default: a new evidence digest is evidence arrival, not by itself
supported progress. The checkpoint is due when the settled ledger consumption
charged to the run's model-call budget exceeds the consumed-call anchor by
``ceil(burn_fraction * model_call_budget)``. Only novel supported
outcome/knowledge progress resets the anchor; duplicate support, pending or
self-reported claims, raw receipts, novel evidence digests, recovery, and
waits never do. The trigger journals its consumed calls, threshold, and
progress anchor alongside the policy version and cause.

The monitor is a pure function over the journal: it maintains rolling signals
instead of asking a model whether it is stuck. Recovery events, pending claims,
and raw activity stay separate operational signals and never reset stagnation.

The advisory supervisor receives only a bounded trajectory digest and returns
typed :class:`Guidance`. It has no runtime port, journal writer, or direct
state mutation; the session applies guidance through the ordinary event path.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, AbstractSet, Protocol, Sequence
from uuid import uuid4

from .board_moves import BoardLevel, BoardStep, MoveLabel, current_level
from .codec import MAX_JSON_BYTES, canonical_json, parse_json_object
from .errors import ContractError
from .memory import (
    InvestigationDisposition,
    investigations,
    supported_progress_digests,
)
from .models import ArtifactRef, Event, JsonValue, UsageMeasurement
from .pricing import RateCard
from .transitions import SessionState

# The supervision module carries the monitor, the advisory supervisor
# boundary, and their payload contracts in one kernel module.
# pylint: disable=too-many-lines

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a kernel import cycle
    from .kernel import TextGenerator

MONITOR_POLICY_VERSION = "monitor.p6.v1"
MONITOR_POLICY_P5_VERSION = "monitor.p5.v1"
MONITOR_POLICY_P1_VERSION = "monitor.p1.v1"
#: ADR-0023: the p6 composition with board-move labels, for board games (ARC).
MONITOR_POLICY_P7_VERSION = "monitor.p7.v1"

SUPERVISOR_SYSTEM_PROMPT = (
    "You are an advisory progress supervisor for an autonomous agent. You "
    "receive a bounded trajectory digest and return one JSON Guidance object. "
    "You cannot invoke tools or change state; your output is advisory only. "
    "Use only facts present in the digest. Listed receipted effects are "
    "confirmed facts; an effect missing from the digest is not evidence that it "
    "did not happen. Do not claim that files, fixes, tests, or reports exist or "
    "are complete unless the digest contains evidence for that claim. State "
    "uncertainty and suggest a way to verify assumptions."
)

SUPERVISION_TRIGGERED = "supervision_triggered"

ACTION_FINGERPRINT_PREFIX = "action"
RECEIPT_FINGERPRINT_PREFIX = "receipt"
EVALUATION_FINGERPRINT_PREFIX = "evaluation"
STDOUT_STRICT_PREFIX = "stdout-strict"
STDOUT_NORMALIZED_PREFIX = "stdout-normalized"

BURN_CAUSE = "budget-burn-evidence"
CHECKPOINT_CAUSE = "budget-checkpoint"
# The trigger causes the shipped policies emit; the replay validator accepts
# exactly this set, so a future policy cause must extend it deliberately.
TRIGGER_CAUSES = frozenset({"repetition", BURN_CAUSE, CHECKPOINT_CAUSE})
DEFAULT_BURN_FRACTION = 0.6
EVIDENCE_ACTIONS = ("exec", "job_read")

_SUCCESSFUL_RECEIPT_STATUSES = ("succeeded", "accepted")


class EvidenceArtifacts(Protocol):
    """The single artifact-store operation evidence fingerprints require."""

    # pylint: disable=unnecessary-ellipsis,too-few-public-methods
    def read(self, reference: ArtifactRef) -> bytes:
        """Return one referenced artifact's content bytes."""
        ...


@dataclass(frozen=True, slots=True)
# The versioned policy names every run-tunable trigger input deliberately.
# pylint: disable=too-many-instance-attributes
class MonitorPolicy:
    """Versioned stagnation-trigger policy.

    ``window_calls`` is the rolling decision window, ``repeat_threshold`` is
    the number of repetitions of one normalized fingerprint that fires a
    trigger, and ``cooldown_calls`` is the minimum number of decisions between
    triggers. ``evidence_fingerprints`` counts the R1/R2 stdout digests in the
    same window. ``burn_trigger`` adds the B2 claim-free burn trigger and
    ``checkpoint_trigger`` the ADR-0013 supported-progress budget checkpoint,
    both due at ``burn_fraction`` of the run's main model-call budget; the two
    are exclusive compositions, and the checkpoint replaces the burn trigger
    under the shipped default. These values are run policy, not architecture
    defaults; the shipped default is the ADR-0014 adoption of the EXP-0003
    selection.
    """

    version: str = MONITOR_POLICY_VERSION
    window_calls: int = 8
    repeat_threshold: int = 3
    cooldown_calls: int = 4
    evidence_fingerprints: bool = True
    burn_trigger: bool = False
    burn_fraction: float = DEFAULT_BURN_FRACTION
    checkpoint_trigger: bool = True

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ContractError("monitor policy version must be non-empty")
        if (
            self.window_calls < 1
            or self.repeat_threshold < 1
            or self.cooldown_calls < 1
        ):
            raise ContractError(
                "monitor policy windows and thresholds must be positive"
            )
        if not (
            isinstance(self.evidence_fingerprints, bool)
            and isinstance(self.burn_trigger, bool)
            and isinstance(self.checkpoint_trigger, bool)
        ):
            raise ContractError("monitor policy trigger flags must be booleans")
        if isinstance(self.burn_fraction, bool) or not isinstance(
            self.burn_fraction, (int, float)
        ):
            raise ContractError("monitor policy burn fraction must be a number")
        if not 0.0 < float(self.burn_fraction) <= 1.0:
            raise ContractError("monitor policy burn fraction must be in (0, 1]")
        if self.burn_trigger and self.checkpoint_trigger:
            raise ContractError(
                "the burn and checkpoint triggers are exclusive compositions"
            )
        if self.burn_trigger and not self.evidence_fingerprints:
            raise ContractError(
                "the claim-free burn trigger requires evidence fingerprints"
            )


def monitor_policy_p5() -> MonitorPolicy:
    """Return the ADR-0011 selection, retained for P5 journal recomputation.

    Retained acceptance replays and evidence tools that recompute the
    shipped-P5 semantics pin this policy explicitly: P5 journals keep P5
    semantics and are never rewritten as P6 outcomes (ADR-0014 decision 4).
    """
    return MonitorPolicy(
        version=MONITOR_POLICY_P5_VERSION,
        window_calls=8,
        repeat_threshold=3,
        cooldown_calls=4,
        evidence_fingerprints=True,
        burn_trigger=True,
        checkpoint_trigger=False,
    )


def monitor_policy_p7() -> MonitorPolicy:
    """Return the ADR-0023 board-game policy: p6 plus board-move labels.

    The triggers and their numbers are p6's. What differs is what counts as a
    repeat on a board: a move that changed the board is not a repeat of its
    action, a move back to an earlier board is, and a weak move still counts
    as a repeat of its action. Runs without board observations behave as p6.
    """
    return MonitorPolicy(version=MONITOR_POLICY_P7_VERSION)


def monitor_policy_p1() -> MonitorPolicy:
    """Return the EXP-0001 selection, retained for evidence recomputation.

    Retained PHASE-0003 evidence tools pin this policy explicitly: their
    bundles record ``monitor.p1.v1`` semantics and must keep reproducing them
    after the ADR-0011 and ADR-0014 switches.
    """
    return MonitorPolicy(
        version=MONITOR_POLICY_P1_VERSION,
        window_calls=8,
        repeat_threshold=3,
        cooldown_calls=4,
        evidence_fingerprints=False,
        burn_trigger=False,
        checkpoint_trigger=False,
    )


@dataclass(frozen=True, slots=True)
# The facts record names every monitor signal input deliberately.
# pylint: disable=too-many-instance-attributes
class DecisionFacts:
    """Journal-derived facts for one completed model decision.

    ``fingerprints`` are normalized action, failure, and evaluation
    fingerprints. ``evidence_fingerprints`` are the R1/R2 stdout digests of
    this decision's successful evidence-bearing receipts; they are empty for
    policies that do not fingerprint evidence. ``supported_progress`` counts
    supported outcome/knowledge claims only; pending claims and raw activity
    never count as progress. ``progress_digests`` are those claims' content
    identities (kind, before/after or statement, scope), so duplicate support
    under a fresh identity is visible as a repeat. ``consumed_calls`` is the
    settled ledger consumption charged to the run's model-call budget for
    this decision: the main call including retries and repairs, plus
    supervisor consults settled in the same span; terminals with unknown
    usage add nothing, mirroring the ledger's measured consumption.
    """

    call: int
    fingerprints: tuple[str, ...]
    actions: int
    failed_receipts: int
    input_tokens: int
    supported_progress: int
    evidence_fingerprints: tuple[str, ...] = ()
    progress_digests: tuple[str, ...] = ()
    consumed_calls: int = 1


@dataclass(frozen=True, slots=True)
# The trigger retains every policy input for BENCH-0001 false-intervention audits.
# pylint: disable=too-many-instance-attributes
class SupervisionTrigger:
    """One monitor trigger with its recorded inputs.

    ``input_event_ids`` retains the journal events that carried the repeated
    fingerprint so BENCH-0001 can measure false intervention and cost.
    ``consumed_calls``, ``threshold``, and ``progress_anchor`` are journal-
    recorded only by the ADR-0013 budget checkpoint; the repetition and burn
    causes leave them unset.
    """

    policy_version: str
    call: int
    cause: str
    fingerprint: str | None
    repeat_count: int | None
    actions_used: int
    tokens_used: int
    input_event_ids: tuple[str, ...]
    consumed_calls: int | None = None
    threshold: int | None = None
    progress_anchor: int | None = None


@dataclass(frozen=True, slots=True)
class CheckpointSuppression:
    """One due-but-cooldown-suppressed budget checkpoint disposition.

    ADR-0013 journals the cooldown/suppression disposition of every due
    checkpoint: the checkpoint was due at this boundary on the settled
    ledger, and the shared repetition cooldown — not the absence of a due
    condition — kept it from firing. Replay reproduces the disposition from
    the recorded ledger state and cooldown window.
    """

    policy_version: str
    call: int
    cause: str
    consumed_calls: int
    threshold: int
    progress_anchor: int
    previous_trigger_call: int
    cooldown_calls: int
    input_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MonitorDiagnostics:
    """Bounded diagnostics distinguishing repeated cycles from plateaus.

    A high ``repetition_ratio`` with few distinct fingerprints indicates
    repeated cycles; a low ratio with ``supported_progress`` indicates a
    productive plateau. The two are never conflated.
    """

    decisions: int
    total_fingerprints: int
    distinct_fingerprints: int
    repetition_ratio: float
    supported_progress: int
    last_trigger_call: int | None


def normalize_text(text: str) -> str:
    """Lowercase and collapse whitespace for stable fingerprint identity."""
    return " ".join(text.lower().split())


def action_fingerprint(name: str, arguments: object) -> str:
    """Return the normalized fingerprint for one proposed action."""
    args = arguments if isinstance(arguments, Mapping) else {}
    canonical = canonical_json({key: args[key] for key in sorted(args)})
    normalized = normalize_text(canonical)
    return f"{ACTION_FINGERPRINT_PREFIX}:{normalize_text(name)}:{normalized}"


def receipt_fingerprint(status: object, error: object) -> str:
    """Return the normalized fingerprint for one failed runtime receipt."""
    status_text = status if isinstance(status, str) else ""
    error_text = error if isinstance(error, str) else ""
    return (
        f"{RECEIPT_FINGERPRINT_PREFIX}:{normalize_text(status_text)}"
        f":{normalize_text(error_text)}"
    )


def evaluation_fingerprint(disposition: object) -> str:
    """Return the normalized fingerprint for one rejected evaluation."""
    text = disposition if isinstance(disposition, str) else ""
    return f"{EVALUATION_FINGERPRINT_PREFIX}:{normalize_text(text)}"


def normalize_stdout(text: str) -> str:
    """Normalize one stdout payload per the frozen EXP-0002 rule.

    An ``HTTP/`` transport header block is stripped, dash-only echo marker
    lines are dropped, and the remainder is lowercased with whitespace
    collapsed. The rule is pinned by EXP-0002's signal definitions.
    """
    lines = text.splitlines()
    kept: list[str] = []
    index = 0
    if lines and lines[0].startswith("HTTP/"):
        index = 1
        while index < len(lines) and lines[index].strip():
            index += 1
        if index < len(lines):
            index += 1
    for line in lines[index:]:
        if line.strip() and set(line.strip()) == {"-"}:
            continue
        kept.append(line)
    return normalize_text("\n".join(kept))


def _artifact_ref(payload: object) -> ArtifactRef | None:
    """Parse one serialized artifact reference from a receipt envelope."""
    if not isinstance(payload, Mapping):
        return None
    digest = payload.get("digest")
    if not isinstance(digest, str) or len(digest) != 64:
        return None
    size = payload.get("size")
    return ArtifactRef(
        digest=digest,
        size=size if isinstance(size, int) and not isinstance(size, bool) else 0,
        media_type=(
            media
            if isinstance((media := payload.get("media_type")), str)
            else "application/octet-stream"
        ),
        provenance=(
            provenance
            if isinstance((provenance := payload.get("provenance")), str)
            else "unknown"
        ),
    )


def _envelope_stdout(payload: bytes, artifacts: EvidenceArtifacts) -> bytes | None:
    """Extract the stdout payload from a receipt artifact when it is one.

    A result envelope lists per-stream output artifacts by digest or carries
    an inline ``stdout`` field; anything else is itself the stdout payload
    (the fixture shape), reported as ``None`` so the caller falls back to the
    raw bytes.
    """
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    entries = envelope.get("output_artifacts")
    if isinstance(entries, list):
        chunks: list[tuple[int, bytes]] = []
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("stream") != "stdout":
                continue
            reference = _artifact_ref(entry.get("artifact"))
            if reference is not None:
                offset = entry.get("offset")
                chunks.append(
                    (
                        (
                            offset
                            if isinstance(offset, int) and not isinstance(offset, bool)
                            else 0
                        ),
                        artifacts.read(reference),
                    )
                )
        if chunks:
            chunks.sort(key=lambda item: item[0])
            return b"".join(chunk for _, chunk in chunks)
    inline = envelope.get("stdout")
    return inline.encode("utf-8") if isinstance(inline, str) else b""


def stdout_fingerprints(
    payload: bytes, artifacts: EvidenceArtifacts
) -> tuple[str, ...]:
    """Return the R1/R2 fingerprints for one receipt artifact payload."""
    stdout = _envelope_stdout(payload, artifacts)
    if stdout is None:
        stdout = payload
    strict = hashlib.sha256(stdout).hexdigest()
    text = stdout.decode("utf-8", errors="replace")
    normalized = hashlib.sha256(normalize_stdout(text).encode("utf-8")).hexdigest()
    return (
        f"{STDOUT_STRICT_PREFIX}:{strict}",
        f"{STDOUT_NORMALIZED_PREFIX}:{normalized}",
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _budget_rejected_facts(
    event: Event,
) -> tuple[tuple[str, ...], int, int, int, int] | None:
    """Fingerprint rejected dispatch decisions without counting progress."""
    if event.kind == "model_decided":
        result_kind = event.payload.get("result_kind")
        if result_kind == "EvaluationChoice":
            return (evaluation_fingerprint("budget exhausted"),), 0, 0, 0, 0
        if result_kind == "ActionChoice":
            output = event.payload.get("output")
            if isinstance(output, str):
                stripped = output.strip()
                start, end = stripped.find("{"), stripped.rfind("}")
                if 0 <= start < end:
                    try:
                        result = parse_json_object(stripped[start : end + 1])
                    except ContractError:
                        result = {}
                    name = result.get("action_name")
                    if isinstance(name, str):
                        return (
                            (action_fingerprint(name, result.get("arguments")),),
                            0,
                            0,
                            0,
                            0,
                        )
    return None


# One branch per monitored event kind; the dispatch is the responsibility.
# pylint: disable=too-many-return-statements,too-many-branches
def _event_facts(
    event: Event, *, budget_rejected: bool = False
) -> tuple[tuple[str, ...], int, int, int, int]:
    """Return one event's (fingerprints, actions, failed, tokens, progress)."""
    if budget_rejected:
        facts = _budget_rejected_facts(event)
        if facts is not None:
            return facts
    if event.kind == "action_intended":
        proposal = event.payload.get("proposal")
        if not isinstance(proposal, Mapping):
            return (), 0, 0, 0, 0
        name = proposal.get("action_name")
        if not isinstance(name, str):
            return (), 0, 0, 0, 0
        return (action_fingerprint(name, proposal.get("arguments")),), 1, 0, 0, 0
    if event.kind == "receipt_received":
        receipt = event.payload.get("receipt")
        if not isinstance(receipt, Mapping):
            return (), 0, 0, 0, 0
        status = receipt.get("status")
        if status in _SUCCESSFUL_RECEIPT_STATUSES:
            return (), 0, 0, 0, 0
        fingerprint = receipt_fingerprint(status, receipt.get("error"))
        return (fingerprint,), 0, 1, 0, 0
    if event.kind == "evaluation_received":
        disposition = event.payload.get("disposition")
        if disposition == "accepted":
            return (), 0, 0, 0, 0
        return (evaluation_fingerprint(disposition),), 0, 0, 0, 0
    if event.kind == "context_assembled":
        manifest = event.payload.get("manifest")
        if not isinstance(manifest, Mapping):
            return (), 0, 0, 0, 0
        serialized = manifest.get("serialized_input_tokens")
        tokens = (
            _as_int(serialized)
            if isinstance(serialized, int)
            else _as_int(manifest.get("tokens_used"))
        )
        return (), 0, 0, tokens, 0
    if event.kind == "progress_claim_recorded":
        claim = event.payload.get("claim")
        if isinstance(claim, Mapping) and claim.get("status") == "supported":
            return (), 0, 0, 0, 1
        return (), 0, 0, 0, 0
    if event.kind == "knowledge_recorded":
        item = event.payload.get("item")
        if isinstance(item, Mapping) and item.get("status") == "supported":
            return (), 0, 0, 0, 1
        return (), 0, 0, 0, 0
    return (), 0, 0, 0, 0


def intended_actions(events: Sequence[Event]) -> dict[str, str]:
    """Map proposal id to action name over the given event span.

    The session reuses this map to scope receipt-time evidence
    fingerprinting; the monitor uses it for the reader fallback.
    """
    intended: dict[str, str] = {}
    for event in events:
        if event.kind != "action_intended":
            continue
        proposal = event.payload.get("proposal")
        if not isinstance(proposal, Mapping):
            continue
        proposal_id = proposal.get("proposal_id")
        name = proposal.get("action_name")
        if isinstance(proposal_id, str) and isinstance(name, str):
            intended[proposal_id] = name
    return intended


def _receipt_evidence(
    event: Event,
    intended: Mapping[str, str],
    artifacts: EvidenceArtifacts | None,
) -> tuple[str, ...]:
    """Return one receipt's R1/R2 fingerprints when it carried stdout.

    Journaled ``evidence_fingerprints`` (the session's receipt-time
    computation) win; the artifact-reader fallback serves journals recorded
    before that field existed. Only successful receipts of evidence-bearing
    actions fingerprint, and the reader is never consulted without them.
    """
    receipt = event.payload.get("receipt")
    if not isinstance(receipt, Mapping):
        return ()
    if receipt.get("status") not in _SUCCESSFUL_RECEIPT_STATUSES:
        return ()
    journaled = event.payload.get("evidence_fingerprints")
    if isinstance(journaled, (list, tuple)):
        fingerprints = tuple(item for item in journaled if isinstance(item, str))
        if len(fingerprints) == len(journaled):
            return fingerprints
    if artifacts is None:
        return ()
    proposal_id = receipt.get("proposal_id")
    if (
        not isinstance(proposal_id, str)
        or intended.get(proposal_id) not in EVIDENCE_ACTIONS
    ):
        return ()
    reference = _artifact_ref(receipt.get("raw_artifact"))
    if reference is None:
        return ()
    return stdout_fingerprints(artifacts.read(reference), artifacts)


def _carried_facts(
    events: Sequence[Event], rejected_budget_requests: AbstractSet[str]
) -> tuple[tuple[str, ...], int, int, int, int]:
    """Return one segment's carried fingerprints and counters."""
    fingerprints: list[str] = []
    actions = 0
    failed = 0
    tokens = 0
    progress = 0
    for event in events:
        (
            event_fingerprints,
            event_actions,
            event_failed,
            event_tokens,
            event_progress,
        ) = _event_facts(
            event,
            budget_rejected=(
                event.kind == "model_decided"
                and event.payload.get("request_id") in rejected_budget_requests
            ),
        )
        fingerprints.extend(event_fingerprints)
        actions += event_actions
        failed += event_failed
        tokens += event_tokens
        progress += event_progress
    return tuple(fingerprints), actions, failed, tokens, progress


def _evidence_facts(
    events: Sequence[Event],
    intended: Mapping[str, str],
    artifacts: EvidenceArtifacts | None,
) -> tuple[str, ...]:
    """Return one segment's R1/R2 evidence fingerprints."""
    evidence: list[str] = []
    for event in events:
        if event.kind == "receipt_received":
            evidence.extend(_receipt_evidence(event, intended, artifacts))
    return tuple(evidence)


_SETTLED_MODEL_TERMINALS = (
    "model_decided",
    "model_malformed",
    "guidance_received",
    "supervisor_failed",
)


def _consumed_calls(events: Sequence[Event]) -> int:
    """Sum one segment's settled ledger consumption charged to the call budget.

    Every settled model terminal journals its usage: main terminals carry the
    full dispatched-call count including retries and repairs, and supervisor
    consults are charged to the same budget. A terminal settled with unknown
    usage adds nothing, mirroring the ledger's measured consumption.
    """
    consumed = 0
    for event in events:
        if event.kind not in _SETTLED_MODEL_TERMINALS:
            continue
        usage = event.payload.get("usage")
        calls = usage.get("model_calls") if isinstance(usage, Mapping) else None
        if isinstance(calls, int) and not isinstance(calls, bool) and calls > 0:
            consumed += calls
    return consumed


def decision_facts(
    events: Sequence[Event],
    call: int,
    artifacts: EvidenceArtifacts | None = None,
) -> DecisionFacts:
    """Derive the facts for one decision from its journal events.

    ``artifacts`` enables the evidence-fingerprint reader fallback for
    journals that predate the receipt-time journaled field; the session's own
    runs carry ``evidence_fingerprints`` on the receipt events themselves.
    """
    rejected_budget_requests = {
        request_id
        for event in events
        if event.kind == "model_application_rejected"
        and event.payload.get("reason") == "resource budget exhausted"
        for request_id in (event.payload.get("request_id"),)
        if isinstance(request_id, str)
    }
    intended = intended_actions(events)
    fingerprints, actions, failed, tokens, progress = _carried_facts(
        events, rejected_budget_requests
    )
    evidence = _evidence_facts(events, intended, artifacts)
    return DecisionFacts(
        call=call,
        fingerprints=tuple(fingerprints),
        actions=actions,
        failed_receipts=failed,
        input_tokens=tokens,
        supported_progress=progress,
        evidence_fingerprints=tuple(evidence),
        progress_digests=supported_progress_digests(events),
        consumed_calls=_consumed_calls(events),
    )


def decision_segments(events: Sequence[Event]) -> tuple[tuple[Event, ...], ...]:
    """Split the journal into per-decision segments.

    Each segment starts at a ``model_intended`` event and holds that decision's
    model call and its consequences. Events before the first model call (for
    example session creation) belong to no decision.
    """
    segments: list[tuple[Event, ...]] = []
    current: list[Event] | None = None
    for event in events:
        if event.kind == "model_intended":
            if current is not None:
                segments.append(tuple(current))
            current = [event]
        elif current is not None:
            current.append(event)
    if current is not None:
        segments.append(tuple(current))
    return tuple(segments)


def facts_history(
    events: Sequence[Event],
    artifacts: EvidenceArtifacts | None = None,
) -> tuple[DecisionFacts, ...]:
    """Derive one facts record per completed decision, in journal order."""
    return tuple(
        decision_facts(segment, index, artifacts)
        for index, segment in enumerate(decision_segments(events), start=1)
    )


def last_trigger_call(events: Sequence[Event]) -> int | None:
    """Return the most recent recorded trigger call, if any."""
    last: int | None = None
    for event in events:
        if event.kind != SUPERVISION_TRIGGERED:
            continue
        call = event.payload.get("call")
        if isinstance(call, int) and call >= 1 and (last is None or call > last):
            last = call
    return last


_STEP_FINGERPRINT_PREFIX = f"{ACTION_FINGERPRINT_PREFIX}:step:"
_BOARD_PROVENANCE = "arc-agi-3-result"
_BOARD_CACHE_LIMIT = 4096
#: Parsed observations by artifact digest. Artifacts are content-addressed, so
#: a digest always names the same observation.
_board_cache: dict[str, Mapping[str, object] | None] = {}


def _board_observation(
    receipt: Mapping[str, object], artifacts: EvidenceArtifacts
) -> Mapping[str, object] | None:
    """Return a successful board receipt's observation, cached by digest."""
    ref = _artifact_ref(receipt.get("raw_artifact"))
    if ref is None or ref.provenance != _BOARD_PROVENANCE:
        return None
    if ref.digest not in _board_cache:
        if len(_board_cache) >= _BOARD_CACHE_LIMIT:
            _board_cache.clear()
        observation = parse_json_object(artifacts.read(ref)).get("observation")
        _board_cache[ref.digest] = (
            observation if isinstance(observation, Mapping) else None
        )
    return _board_cache[ref.digest]


def _board_step(
    sequence: int,
    arguments: Mapping[str, JsonValue],
    observation: Mapping[str, object],
) -> BoardStep | None:
    """Build one board step from an observation, or None if it has no board."""
    grid = observation.get("grid")
    if not isinstance(grid, (list, tuple)) or not grid:
        return None
    rows = tuple(row for row in grid if isinstance(row, str))
    if len(rows) != len(grid):
        return None
    levels = observation.get("levels_completed")
    legal = observation.get("legal_actions")
    return BoardStep(
        sequence=sequence,
        arguments=arguments,
        board=rows,
        levels_completed=(
            levels if isinstance(levels, int) and not isinstance(levels, bool) else None
        ),
        full_reset=observation.get("full_reset") is True,
        legal_actions=tuple(
            item
            for item in (legal if isinstance(legal, (list, tuple)) else ())
            if isinstance(item, int) and not isinstance(item, bool)
        ),
    )


def board_level(
    events: Sequence[Event], artifacts: EvidenceArtifacts
) -> BoardLevel | None:
    """Return the labelled moves of the current board level (ADR-0023).

    Only successful ``step`` receipts that carry a board observation count.
    None when the journal holds no board observation, which is how runs of
    other environments keep p6 behavior under p7.
    """
    arguments: dict[str, Mapping[str, JsonValue]] = {}
    steps: list[BoardStep] = []
    for event in events:
        if event.kind == "action_intended":
            proposal = event.payload.get("proposal")
            if isinstance(proposal, Mapping) and proposal.get("action_name") == "step":
                proposal_id = proposal.get("proposal_id")
                args = proposal.get("arguments")
                if isinstance(proposal_id, str) and isinstance(args, Mapping):
                    arguments[proposal_id] = args
            continue
        if event.kind != "receipt_received":
            continue
        receipt = event.payload.get("receipt")
        if not isinstance(receipt, Mapping) or receipt.get("status") != "succeeded":
            continue
        proposal_id = receipt.get("proposal_id")
        if not isinstance(proposal_id, str) or proposal_id not in arguments:
            continue
        observation = _board_observation(receipt, artifacts)
        if observation is None:
            continue
        step = _board_step(event.sequence, arguments[proposal_id], observation)
        if step is not None:
            steps.append(step)
    return current_level(steps)


def board_fingerprints(level: BoardLevel, since_sequence: int) -> tuple[str, ...]:
    """Return the repeat fingerprints of the level's moves in the window.

    A weak move repeats its action; a return repeats one shared fingerprint
    that names the untried legal actions; forward and first moves repeat
    nothing.
    """
    returns = (
        "board:return-to-an-earlier-board; "
        f"Untried legal actions: {list(level.untried_actions)}"
    )
    fingerprints: list[str] = []
    for move in level.moves:
        if move.step.sequence < since_sequence:
            continue
        if move.label is MoveLabel.WEAK:
            fingerprints.append(action_fingerprint("step", move.step.arguments))
        elif move.label is MoveLabel.RETURN:
            fingerprints.append(returns)
    return tuple(fingerprints)


def _window_counts(
    events: Sequence[Event],
    window: Sequence[DecisionFacts],
    policy: MonitorPolicy,
    artifacts: EvidenceArtifacts | None,
) -> Counter[str]:
    """Count the window's repeat fingerprints under ``policy``.

    Under ``monitor.p7.v1`` with board observations, ``step`` action
    fingerprints give way to the board-move fingerprints (ADR-0023).
    """
    level = (
        board_level(events, artifacts)
        if policy.version == MONITOR_POLICY_P7_VERSION and artifacts is not None
        else None
    )
    counts: Counter[str] = Counter()
    for fact in window:
        counts.update(
            fingerprint
            for fingerprint in fact.fingerprints
            if level is None or not fingerprint.startswith(_STEP_FINGERPRINT_PREFIX)
        )
        if policy.evidence_fingerprints:
            counts.update(fact.evidence_fingerprints)
    if level is not None:
        segments = decision_segments(events)[-policy.window_calls :]
        since = segments[0][0].sequence if segments and segments[0] else 0
        counts.update(board_fingerprints(level, since))
    return counts


def evaluate_supervision(
    events: Sequence[Event],
    call: int,
    policy: MonitorPolicy = MonitorPolicy(),
    *,
    artifacts: EvidenceArtifacts | None = None,
    model_call_budget: int | None = None,
) -> SupervisionTrigger | None:
    """Evaluate the monitor policy at one decision boundary.

    ``call`` is the upcoming decision ordinal and ``events`` must contain the
    completed decisions ``1..call-1``. The monitor returns ``None`` when the
    policy does not fire (including during cooldown) and a
    :class:`SupervisionTrigger` with its recorded inputs when it does.

    ``artifacts`` enables the evidence-fingerprint reader fallback for
    journals recorded before receipt-time fingerprinting. A burn- or
    checkpoint-enabled policy requires ``model_call_budget`` (the run's main
    model-call budget) and refuses to evaluate without one, so the trigger
    never degrades silently.

    Under ``monitor.p7.v1``, ``artifacts`` also supplies the board
    observations that label moves (ADR-0023).
    """
    if call < 1:
        raise ContractError("supervision call must be positive")
    history = facts_history(events, artifacts)
    if call == 1:
        if history:
            raise ContractError("first decision has no preceding model calls")
    elif not history or history[-1].call != call - 1:
        raise ContractError("journal does not contain the decisions preceding the call")
    previous_trigger = last_trigger_call(events)
    if previous_trigger is not None and call - previous_trigger < policy.cooldown_calls:
        return None
    window = history[-policy.window_calls :]
    counts = _window_counts(events, window, policy, artifacts)
    if counts:
        fingerprint, count = sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )[0]
        if count >= policy.repeat_threshold:
            window_event_ids = tuple(
                event.event_id
                for segment in decision_segments(events)[-policy.window_calls :]
                for event in segment
            )
            return SupervisionTrigger(
                policy_version=policy.version,
                call=call,
                cause="repetition",
                fingerprint=fingerprint,
                repeat_count=count,
                actions_used=sum(fact.actions for fact in window),
                tokens_used=sum(fact.input_tokens for fact in window),
                input_event_ids=window_event_ids,
            )
    if policy.burn_trigger:
        return _burn_trigger(events, history, call, policy, model_call_budget)
    if policy.checkpoint_trigger:
        return _checkpoint_trigger(events, history, call, policy, model_call_budget)
    return None


def _burn_trigger(
    events: Sequence[Event],
    history: tuple[DecisionFacts, ...],
    call: int,
    policy: MonitorPolicy,
    model_call_budget: int | None,
) -> SupervisionTrigger | None:
    """Evaluate the B2 claim-free burn trigger at one boundary.

    The trigger fires at ``ceil(burn_fraction * model_call_budget)``
    consumed calls without a first-seen evidence fingerprint: repeated
    evidence never counts as progress, the first occurrence of each R1/R2
    digest does. It shares the repetition cooldown and defers to a
    repetition hit on the same boundary. The trigger records every event
    since the last progress-bearing call so BENCH-0001 can audit the burn.
    """
    threshold = _consumed_call_threshold(policy, model_call_budget)
    last_progress = _last_evidence_progress(history)
    if (call - 1) - last_progress < threshold:
        return None
    return SupervisionTrigger(
        policy_version=policy.version,
        call=call,
        cause=BURN_CAUSE,
        fingerprint=None,
        repeat_count=None,
        actions_used=sum(fact.actions for fact in history),
        tokens_used=sum(fact.input_tokens for fact in history),
        input_event_ids=tuple(
            event.event_id
            for segment in decision_segments(events)[last_progress:]
            for event in segment
        ),
    )


def _consumed_call_threshold(
    policy: MonitorPolicy, model_call_budget: int | None
) -> int:
    """Validate the run's main model-call budget and return the trigger threshold.

    A consumed-call trigger never degrades silently: without a positive
    budget it raises instead of evaluating.
    """
    if (
        isinstance(model_call_budget, bool)
        or not isinstance(model_call_budget, int)
        or model_call_budget < 1
    ):
        raise ContractError(
            "a consumed-call trigger requires a positive model-call budget"
        )
    return math.ceil(policy.burn_fraction * model_call_budget)


def _checkpoint_trigger(
    events: Sequence[Event],
    history: tuple[DecisionFacts, ...],
    call: int,
    policy: MonitorPolicy,
    model_call_budget: int | None,
) -> SupervisionTrigger | None:
    """Evaluate the ADR-0013 supported-progress budget checkpoint.

    The checkpoint is due when the settled ledger consumption since the last
    novel supported progress reaches ``ceil(burn_fraction *
    model_call_budget)``. Novel supported outcome/knowledge progress resets
    the consumed-call anchor; duplicate support, pending or self-reported
    claims, raw receipts, novel evidence digests, recovery, and waits never
    do. It shares the repetition cooldown and defers to a repetition hit on
    the same boundary, and journals its consumed calls, threshold, and
    progress anchor with every event since the anchor decision.
    """
    threshold = _consumed_call_threshold(policy, model_call_budget)
    consumed, anchor, anchor_index = _progress_anchor(history)
    if consumed - anchor < threshold:
        return None
    return SupervisionTrigger(
        policy_version=policy.version,
        call=call,
        cause=CHECKPOINT_CAUSE,
        fingerprint=None,
        repeat_count=None,
        actions_used=sum(fact.actions for fact in history),
        tokens_used=sum(fact.input_tokens for fact in history),
        input_event_ids=tuple(
            event.event_id
            for segment in decision_segments(events)[anchor_index:]
            for event in segment
        ),
        consumed_calls=consumed,
        threshold=threshold,
        progress_anchor=anchor,
    )


def _progress_anchor(history: tuple[DecisionFacts, ...]) -> tuple[int, int, int]:
    """Return (consumed, anchor, anchor_index) over the decision history.

    The anchor is the cumulative settled consumption at the last novel
    supported progress identity: identities already supported earlier in the
    run never reset it (duplicate support), and pending or self-reported
    claims never touch it at all (ADR-0013). ``anchor_index`` is the 0-based
    decision index that set the anchor, so the trigger can retain the events
    the checkpoint reviewed.
    """
    consumed = 0
    anchor = 0
    anchor_index = 0
    seen: set[str] = set()
    for index, fact in enumerate(history):
        consumed += fact.consumed_calls
        novel = [digest for digest in fact.progress_digests if digest not in seen]
        seen.update(fact.progress_digests)
        if novel:
            anchor = consumed
            anchor_index = index
    return consumed, anchor, anchor_index


def _last_evidence_progress(history: tuple[DecisionFacts, ...]) -> int:
    """Return the last call that received a first-seen evidence fingerprint."""
    seen: set[str] = set()
    last = 0
    for fact in history:
        fresh = [
            fingerprint
            for fingerprint in fact.evidence_fingerprints
            if fingerprint not in seen
        ]
        if fresh:
            seen.update(fresh)
            last = fact.call
    return last


def checkpoint_suppression(
    events: Sequence[Event],
    call: int,
    policy: MonitorPolicy = MonitorPolicy(),
    *,
    artifacts: EvidenceArtifacts | None = None,
    model_call_budget: int | None = None,
) -> CheckpointSuppression | None:
    """Return the due-but-cooldown-suppressed checkpoint disposition, if any.

    ``evaluate_supervision`` short-circuits on the shared repetition cooldown
    before any consumed-call trigger is considered, so a due checkpoint
    suppressed by the cooldown leaves no trigger and no journal trace. This
    predicate reports exactly that case: the checkpoint is due at ``call``
    on the settled ledger and only the cooldown keeps it from firing. The
    journal and history validation matches :func:`evaluate_supervision`.

    The retained P5 burn and P1 compositions return ``None``: their
    suppression stays unjournaled, so their journals keep their recorded
    semantics (ADR-0014 decision 4).
    """
    if policy.burn_trigger or not policy.checkpoint_trigger:
        return None
    if call < 1:
        raise ContractError("supervision call must be positive")
    history = facts_history(events, artifacts)
    if call == 1:
        if history:
            raise ContractError("first decision has no preceding model calls")
    elif not history or history[-1].call != call - 1:
        raise ContractError("journal does not contain the decisions preceding the call")
    previous_trigger = last_trigger_call(events)
    if previous_trigger is None or call - previous_trigger >= policy.cooldown_calls:
        return None
    threshold = _consumed_call_threshold(policy, model_call_budget)
    consumed, anchor, anchor_index = _progress_anchor(history)
    if consumed - anchor < threshold:
        return None
    return CheckpointSuppression(
        policy_version=policy.version,
        call=call,
        cause=CHECKPOINT_CAUSE,
        consumed_calls=consumed,
        threshold=threshold,
        progress_anchor=anchor,
        previous_trigger_call=previous_trigger,
        cooldown_calls=policy.cooldown_calls,
        input_event_ids=tuple(
            event.event_id
            for segment in decision_segments(events)[anchor_index:]
            for event in segment
        ),
    )


def checkpoint_suppression_payload(
    suppression: CheckpointSuppression,
) -> Mapping[str, JsonValue]:
    """Return the durable payload for one ``checkpoint_suppressed`` event."""
    return {
        "policy_version": suppression.policy_version,
        "call": suppression.call,
        "cause": suppression.cause,
        "consumed_calls": suppression.consumed_calls,
        "threshold": suppression.threshold,
        "progress_anchor": suppression.progress_anchor,
        "previous_trigger_call": suppression.previous_trigger_call,
        "cooldown_calls": suppression.cooldown_calls,
        "input_event_ids": tuple(suppression.input_event_ids),
    }


def monitor_diagnostics(events: Sequence[Event]) -> MonitorDiagnostics:
    """Summarize rolling progress signals without evaluating the policy."""
    history = facts_history(events)
    all_fingerprints = [
        fingerprint for fact in history for fingerprint in fact.fingerprints
    ]
    total = len(all_fingerprints)
    distinct = len(set(all_fingerprints))
    ratio = (total - distinct) / total if total else 0.0
    return MonitorDiagnostics(
        decisions=len(history),
        total_fingerprints=total,
        distinct_fingerprints=distinct,
        repetition_ratio=ratio,
        supported_progress=sum(fact.supported_progress for fact in history),
        last_trigger_call=last_trigger_call(events),
    )


def trigger_payload(trigger: SupervisionTrigger) -> Mapping[str, JsonValue]:
    """Return the durable payload for one ``supervision_triggered`` event."""
    return {
        "policy_version": trigger.policy_version,
        "call": trigger.call,
        "cause": trigger.cause,
        "fingerprint": trigger.fingerprint,
        "repeat_count": trigger.repeat_count,
        "actions_used": trigger.actions_used,
        "tokens_used": trigger.tokens_used,
        "input_event_ids": tuple(trigger.input_event_ids),
        "consumed_calls": trigger.consumed_calls,
        "threshold": trigger.threshold,
        "progress_anchor": trigger.progress_anchor,
    }


# --- Advisory supervisor boundary ---

GUIDANCE_SCHEMA: Mapping[str, JsonValue] = {
    "type": "object",
    "required": ("kind", "diagnosis", "constraints", "suggested_experiments"),
    "properties": {
        "kind": {"const": "Guidance"},
        "diagnosis": {"type": "string"},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "suggested_experiments": {"type": "array", "items": {"type": "string"}},
        "expires_after_call": {"type": "integer"},
    },
}

SUPERVISION_EVENT_KINDS = (
    "supervision_enabled",
    "supervision_triggered",
    "checkpoint_suppressed",
    "supervisor_intended",
    "guidance_received",
    "guidance_accepted",
    "guidance_rejected",
    "guidance_expired",
    "guidance_suppressed",
    "supervisor_failed",
    "guidance_response_rejected",
    "operator_escalated",
)


@dataclass(frozen=True, slots=True)
class Guidance:
    """Advisory supervisor output. No tools and no execution access.

    ``expires_after_call`` is the decision call after which the guidance no
    longer applies; the session records an explicit expiry event.
    """

    diagnosis: str
    constraints: tuple[str, ...]
    suggested_experiments: tuple[str, ...]
    expires_after_call: int

    def __post_init__(self) -> None:
        if not self.diagnosis.strip():
            raise ContractError("guidance diagnosis must be non-empty")
        if self.expires_after_call < 1:
            raise ContractError("guidance expiry must be a positive call")


@dataclass(frozen=True, slots=True)
class SupervisorDigest:
    """Bounded trajectory digest visible to the advisory supervisor.

    The digest carries attempts, committed objective vectors, recent failures,
    receipted effects, and still-unexplored directions. It never exposes the
    full journal, action content, command output, or any runtime control.
    """

    objective: str
    attempt_count: int
    committed_objectives: tuple[Mapping[str, JsonValue], ...]
    recent_failures: tuple[str, ...]
    recent_effects: tuple[str, ...]
    unexplored_directions: tuple[str, ...]
    trigger: SupervisionTrigger

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ContractError("supervisor digest objective must be non-empty")
        if self.attempt_count < 0:
            raise ContractError("supervisor digest attempt count must not be negative")


@dataclass(frozen=True, slots=True)
class SupervisorResponse:
    """One advisory supervisor call result with usage.

    The session settles the supervisor reservation with ``usage`` through the
    same path as the main model call, so supervisor cost stays accountable.
    """

    guidance: Guidance
    usage: UsageMeasurement | None
    model_id: str
    finish_reason: str = "stop"


class Supervisor(Protocol):
    """Advisory supervisor: bounded digest in, typed response out."""

    # pylint: disable=unnecessary-ellipsis,too-few-public-methods
    def advise(self, digest: SupervisorDigest) -> SupervisorResponse:
        """Return advisory guidance. Must not invoke tools or mutate state."""
        ...


class ScriptedSupervisor:
    """Deterministic supervisor yielding pre-typed responses in order."""

    # pylint: disable=too-few-public-methods

    def __init__(self, responses: tuple[SupervisorResponse, ...]) -> None:
        self._remaining = list(responses)
        self.calls = 0
        self.digests: list[SupervisorDigest] = []

    def advise(self, digest: SupervisorDigest) -> SupervisorResponse:
        """Pop the next scripted response or fail when the script is exhausted."""
        self.calls += 1
        self.digests.append(digest)
        if not self._remaining:
            raise ContractError("scripted supervisor has no remaining guidance")
        return self._remaining.pop(0)


class ModelSupervisor:
    """Advisory supervisor backed by the run's OpenAI-compatible connection.

    One ``advise`` call renders the bounded digest, performs exactly one
    provider call, parses the visible response into typed :class:`Guidance`, and
    returns it with the measured usage. A transport error or malformed response
    raises; the session settles the consult as ``supervisor_failed`` and
    continues without guidance (ARCH-0001: bounded, never fatal).
    """

    # The supervisor exposes one protocol method by design.
    # pylint: disable=too-few-public-methods

    def __init__(
        self,
        generator: "TextGenerator",
        *,
        model_id: str,
        rate_card: RateCard | None = None,
    ) -> None:
        if not model_id.strip():
            raise ContractError("supervisor model id must be non-empty")
        self._generator = generator
        self._model_id = model_id
        #: COD-86: prices each consult when the run has a cost limit.
        self.rate_card = rate_card

    @property
    def max_output_tokens(self) -> int | None:
        """The supervisor call's answer limit, when its generator states one."""
        limit = getattr(self._generator, "max_tokens", None)
        return limit if isinstance(limit, int) and not isinstance(limit, bool) else None

    def _usage(
        self, generation: object, duration_ms: int, *, failed: bool
    ) -> UsageMeasurement:
        """Measure one consult, priced at the rate card when there is one."""
        input_tokens = _generation_token(
            getattr(generation, "prompt_tokens", None), unknown_zero=failed
        )
        output_tokens = _generation_token(
            getattr(generation, "completion_tokens", None), unknown_zero=failed
        )
        cached = _optional_token(getattr(generation, "cached_prompt_tokens", None))
        if cached is not None and (input_tokens is None or cached > input_tokens):
            cached = None
        return UsageMeasurement(
            measurement_id=f"supervisor-usage-{uuid4().hex[:12]}",
            model_calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            cost=(
                None
                if self.rate_card is None
                else self.rate_card.cost(input_tokens, output_tokens, cached)
            ),
        )

    def advise(self, digest: SupervisorDigest) -> SupervisorResponse:
        """Run one advisory provider call for the given trajectory digest."""
        prompt = render_digest(digest)
        started = time.monotonic()
        generation = self._generator.generate(SUPERVISOR_SYSTEM_PROMPT, prompt)
        duration_ms = max(0, int(round((time.monotonic() - started) * 1000)))
        failed = bool(getattr(generation, "error", None))
        usage = self._usage(generation, duration_ms, failed=failed)
        # Import locally to avoid the kernel's module-level guidance import
        # forming a cycle with this supervisor module.
        from .kernel import ModelCallError  # pylint: disable=import-outside-toplevel

        if failed:
            raise ModelCallError("supervisor transport failure", usage=usage)
        text = getattr(generation, "text", "")
        finish_reason = getattr(generation, "finish_reason", "") or "stop"
        if not isinstance(text, str) or not text.strip():
            raise ModelCallError("supervisor provider output exhausted", usage=usage)
        try:
            guidance = parse_guidance(text)
        except ContractError as exc:
            if str(finish_reason).lower() in {
                "length",
                "max_tokens",
                "max_output_tokens",
                "token_limit",
            }:
                raise ModelCallError(
                    "supervisor provider output exhausted", usage=usage
                ) from exc
            raise ModelCallError("malformed supervisor output", usage=usage) from exc
        model_id = getattr(generation, "model", "") or self._model_id
        return SupervisorResponse(
            guidance=guidance,
            usage=usage,
            model_id=model_id,
            finish_reason=finish_reason,
        )


def _optional_token(value: object) -> int | None:
    """Return a bounded non-negative token count, or ``None`` when unknown."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _generation_token(value: object, *, unknown_zero: bool) -> int | None:
    """Treat Generation's default zero as unknown when a transport failed."""
    token = _optional_token(value)
    if unknown_zero and token == 0:
        return None
    return token


_EFFECT_ARGUMENT_FIELDS = ("path", "command", "job_id")
_EFFECT_VALUE_LIMIT = 120


def _bounded_text(value: str, limit: int = _EFFECT_VALUE_LIMIT) -> str:
    """Return one single-line excerpt so a digest band stays bounded."""
    collapsed = " ".join(value.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "..."


def _effect_targets(arguments: Mapping[str, object]) -> str:
    """Summarize the addressed targets of one action without its content."""
    parts: list[str] = []
    for field in _EFFECT_ARGUMENT_FIELDS:
        value = arguments.get(field)
        if isinstance(value, str) and value:
            parts.append(f"{field}={_bounded_text(value)}")
    return " ".join(parts)


def _effect_summaries(events: Sequence[Event]) -> tuple[str, ...]:
    """Summarize successful receipted actions as bounded, content-free facts.

    The supervisor must be able to see that durable work happened without
    receiving the journal: each entry names the action and the target it
    addressed, never the written content or the command output.
    """
    intended: dict[str, tuple[str, Mapping[str, object]]] = {}
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
        intended[proposal_id] = (
            name,
            arguments if isinstance(arguments, Mapping) else {},
        )
    effects: list[str] = []
    for event in events:
        if event.kind != "receipt_received":
            continue
        receipt = event.payload.get("receipt")
        if not isinstance(receipt, Mapping):
            continue
        if receipt.get("status") not in _SUCCESSFUL_RECEIPT_STATUSES:
            continue
        proposal_id = receipt.get("proposal_id")
        entry = intended.get(proposal_id) if isinstance(proposal_id, str) else None
        if entry is None:
            continue
        name, arguments = entry
        targets = _effect_targets(arguments)
        effects.append(f"{name} {targets}".strip())
    return tuple(effects)


def build_digest(  # pylint: disable=too-many-arguments
    state: SessionState,
    events: Sequence[Event],
    trigger: SupervisionTrigger,
    *,
    max_failures: int = 10,
    max_effects: int = 8,
    max_directions: int = 5,
) -> SupervisorDigest:
    """Build the bounded trajectory digest for one supervision request."""
    if max_failures < 1 or max_effects < 1 or max_directions < 1:
        raise ContractError("digest bounds must be positive")
    history = facts_history(events)
    recent_failures = tuple(
        fingerprint
        for fact in history
        for fingerprint in fact.fingerprints
        if not fingerprint.startswith(ACTION_FINGERPRINT_PREFIX)
    )[-max_failures:]
    committed_objectives = tuple(node.objectives for node in state.accepted_lineage)
    directions = tuple(
        investigation.question
        for investigation in investigations(tuple(events))
        if investigation.disposition is InvestigationDisposition.ACTIVE
    )[:max_directions]
    return SupervisorDigest(
        objective=state.task.objective,
        attempt_count=len(state.attempt_history),
        committed_objectives=committed_objectives,
        recent_failures=recent_failures,
        recent_effects=_effect_summaries(events)[-max_effects:],
        unexplored_directions=directions,
        trigger=trigger,
    )


def render_digest(digest: SupervisorDigest) -> str:
    """Render the bounded digest as the supervisor prompt."""
    lines = [
        "You are an advisory progress supervisor for an autonomous agent.",
        "The agent may be stagnating. Diagnose the observed pattern, name "
        "assumptions to challenge, and suggest one small discriminating "
        "experiment or replan. Use only evidence in this digest; do not claim "
        "that unshown files, fixes, tests, or reports exist or are complete. "
        "Do not invoke tools or mutate state.",
        "",
        f"Objective: {digest.objective}",
        f"Attempts: {digest.attempt_count}",
        "Committed objective vectors:",
    ]
    lines.extend(
        f"- {canonical_json(vector)}" for vector in digest.committed_objectives
    )
    lines.append("Recent failures:")
    lines.extend(f"- {failure}" for failure in digest.recent_failures)
    lines.append("Receipted durable effects:")
    lines.extend(f"- {effect}" for effect in digest.recent_effects)
    lines.append("Still-unexplored directions:")
    lines.extend(f"- {direction}" for direction in digest.unexplored_directions)
    lines.append(f"Trigger: {digest.trigger.cause} at call {digest.trigger.call}")
    if digest.trigger.fingerprint is not None:
        lines.append(f"Repeated fingerprint: {digest.trigger.fingerprint}")
    lines.append("")
    lines.append(
        "Return one JSON object with kind 'Guidance', a diagnosis, constraints, "
        "suggested_experiments, and expires_after_call."
    )
    return "\n".join(lines)


def _extract_json_object(text: str) -> str:
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ContractError("supervisor output exceeds the supported byte bound")
    stripped = text.strip()
    if not stripped:
        raise ContractError("supervisor output is empty")
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ContractError("supervisor output does not contain a JSON object")
    return stripped[start : end + 1]


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def parse_guidance(text: str) -> Guidance:
    """Parse one visible supervisor response into typed Guidance."""
    if not isinstance(text, str):
        raise ContractError("supervisor output must be text")
    payload = parse_json_object(_extract_json_object(text))
    if payload.get("kind") != "Guidance":
        raise ContractError("supervisor output must have kind 'Guidance'")
    diagnosis = payload.get("diagnosis")
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        raise ContractError("guidance diagnosis must be non-empty")
    expiry = payload.get("expires_after_call")
    if isinstance(expiry, bool) or not isinstance(expiry, int) or expiry < 1:
        raise ContractError("guidance expires_after_call must be a positive integer")
    return Guidance(
        diagnosis=diagnosis,
        constraints=_string_tuple(payload.get("constraints")),
        suggested_experiments=_string_tuple(payload.get("suggested_experiments")),
        expires_after_call=expiry,
    )


class GuidanceDecision(str, Enum):
    """The main agent's recorded response to active supervisor guidance."""

    ACCEPT = "accept"
    REJECT = "reject"
    REPLAN = "replan"


@dataclass(frozen=True, slots=True)
class SupervisionCapability:
    """One run-global kernel capability flag pairing policy with an advisor.

    The session records ``policy`` durably when the run is created, so reopening
    verifies the same run policy; ``supervisor`` is a connector re-supplied on
    reopen like the runtime and evaluator. The flag is global to a run and is
    never selected by benchmark identity.
    """

    supervisor: Supervisor
    policy: MonitorPolicy = MonitorPolicy()


def policy_payload(policy: MonitorPolicy) -> dict[str, JsonValue]:
    """Return the durable capability payload for one monitor policy."""
    return {
        "version": policy.version,
        "window_calls": policy.window_calls,
        "repeat_threshold": policy.repeat_threshold,
        "cooldown_calls": policy.cooldown_calls,
        "evidence_fingerprints": policy.evidence_fingerprints,
        "burn_trigger": policy.burn_trigger,
        "burn_fraction": policy.burn_fraction,
        "checkpoint_trigger": policy.checkpoint_trigger,
    }


def policy_from_payload(payload: Mapping[str, object]) -> MonitorPolicy:
    """Parse one durable monitor-policy payload, failing closed on bad input.

    Payloads recorded before the ADR-0011 switch carry no trigger-flag
    fields; they are the EXP-0001 composition and default the flags off.
    Payloads recorded before the ADR-0014 switch carry no checkpoint flag;
    they default it off, so P5 journals reconstruct their recorded
    composition.
    """
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ContractError("monitor policy version must be a non-empty string")

    def _positive(name: str) -> int:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ContractError(f"monitor policy {name} must be a positive integer")
        return value

    def _flag(name: str) -> bool:
        value = payload.get(name, False)
        if not isinstance(value, bool):
            raise ContractError(f"monitor policy {name} must be a boolean")
        return value

    fraction = payload.get("burn_fraction", DEFAULT_BURN_FRACTION)
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        raise ContractError("monitor policy burn_fraction must be a number")
    return MonitorPolicy(
        version=version,
        window_calls=_positive("window_calls"),
        repeat_threshold=_positive("repeat_threshold"),
        cooldown_calls=_positive("cooldown_calls"),
        evidence_fingerprints=_flag("evidence_fingerprints"),
        burn_trigger=_flag("burn_trigger"),
        burn_fraction=float(fraction),
        checkpoint_trigger=_flag("checkpoint_trigger"),
    )


def guidance_payload(guidance: Guidance) -> dict[str, JsonValue]:
    """Return the durable payload for one guidance body."""
    return {
        "diagnosis": guidance.diagnosis,
        "constraints": guidance.constraints,
        "suggested_experiments": guidance.suggested_experiments,
        "expires_after_call": guidance.expires_after_call,
    }


def guidance_from_payload(payload: Mapping[str, object]) -> Guidance:
    """Parse one durable guidance payload, failing closed on bad input."""
    diagnosis = payload.get("diagnosis")
    if not isinstance(diagnosis, str):
        raise ContractError("guidance diagnosis must be text")
    expiry = payload.get("expires_after_call")
    if isinstance(expiry, bool) or not isinstance(expiry, int):
        raise ContractError("guidance expiry must be an integer")

    def _strings(name: str) -> tuple[str, ...]:
        value = payload.get(name)
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value
        ):
            raise ContractError(f"guidance {name} must be a list of strings")
        return tuple(item for item in value)

    return Guidance(
        diagnosis=diagnosis,
        constraints=_strings("constraints"),
        suggested_experiments=_strings("suggested_experiments"),
        expires_after_call=expiry,
    )


def active_guidance(
    events: Sequence[Event],
) -> tuple[str, Guidance] | None:
    """Return the event id and body of the latest unanswered, current guidance.

    A recorded accept/reject response, an explicit expiry, or a suppression
    closes the active guidance; a later ``guidance_received`` supersedes an
    earlier one. Guidance recorded under a superseded objective version (for
    example before an operator steer) is never returned, so a stale diagnosis
    cannot steer work under a newer objective.
    """
    source: str | None = None
    body: Guidance | None = None
    recorded_version: int | None = None
    current_version = 1
    for event in events:
        current_version = event.objective_version
        if event.kind == "guidance_received":
            payload = event.payload.get("guidance")
            if not isinstance(payload, Mapping):
                raise ContractError("guidance_received payload lacks guidance")
            source = event.event_id
            body = guidance_from_payload(payload)
            recorded_version = event.objective_version
        elif source is not None and event.kind in (
            "guidance_accepted",
            "guidance_rejected",
            "guidance_expired",
            "guidance_suppressed",
        ):
            source, body, recorded_version = None, None, None
    if source is None or body is None or recorded_version != current_version:
        return None
    return source, body


def escalation_stage(events: Sequence[Event]) -> int:
    """Return the ladder stage the next trigger takes in its stagnation episode.

    ARCH-0001 fixes the ladder: stage 1 delivers guidance, stage 2 checkpoints
    and replans into a fresh attempt, and stage 3 escalates to an operator
    question. Supported outcome or knowledge progress ends an episode; pending
    claims and raw activity never do.
    """
    last_progress = 0
    for event in events:
        if event.kind == "progress_claim_recorded":
            claim = event.payload.get("claim")
            if isinstance(claim, Mapping) and claim.get("status") == "supported":
                last_progress = event.sequence
        elif event.kind == "knowledge_recorded":
            item = event.payload.get("item")
            if isinstance(item, Mapping) and item.get("status") == "supported":
                last_progress = event.sequence
    return 1 + sum(
        1
        for event in events
        if event.kind == SUPERVISION_TRIGGERED and event.sequence > last_progress
    )
