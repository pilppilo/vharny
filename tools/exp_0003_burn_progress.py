"""Run, replay, aggregate, and verify the EXP-0003 burn-progress experiment.

Experiment-local scaffolding over public kernel and journal APIs, mirroring
the EXP-0002 apparatus. The EXP-0001 scripted fixtures are reused unchanged;
five explicitly labeled paired classes are added here; and the candidate
monitor P6 (``monitor.p6.v1``) implements the ADR-0013 supported-progress
budget checkpoint exactly as the accepted decision and the approved EXP-0003
predeclaration describe: ledger-based consumed-call counting, anchor resets
only on novel supported progress, the shared four-call cooldown with
repetition precedence, advisory-only checkpoints, and operator escalation
when no supervisor capacity fits the budget. The shipped supervision default
stays on ``monitor.p5.v1`` (ADR-0011); nothing here changes product code.

The aggregate reports sensitivity and intervention cost together and selects
no policy: adoption is the operator's separate decision. The pre-registration
covers the new mechanics (run2 checkpoint boundaries, the paired classes, the
40-call threshold check); the carried EXP-0001/0002 classes are reported as
measured comparisons, not invented fire counts.

Usage::

    uv run python tools/exp_0003_burn_progress.py manifest
    uv run python tools/exp_0003_burn_progress.py run
    uv run python tools/exp_0003_burn_progress.py replay --run2-dir <dir> \
        [--run4-dir <dir>]
    uv run python tools/exp_0003_burn_progress.py aggregate
    uv run python tools/exp_0003_burn_progress.py verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from vharness.agent import (
    BudgetLimits,
    CompletionMode,
    ComponentIdentity,
    ContractError,
    KnowledgeSource,
    RecordEnvelope,
    Session,
    SessionStatus,
    TaskSpec,
    TransitionError,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import canonical_json
from vharness.agent.evidence import counters as derive_counters
from vharness.agent.journal import SqliteJournal
from vharness.agent.kernel import (
    MemoryUpdate,
    ModelCallError,
    ProgressClaimChange,
)
from vharness.agent.models import Event
from vharness.agent.supervision import (
    MonitorPolicy,
    decision_segments,
    evaluate_supervision,
)

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

# astroid does not resolve the repository's implicit tools namespace package,
# so the source root is added to sys.path above; the imports are exercised by
# the unit tests and every fixture run.
from tools.exp_0001_supervisor_policy import (  # pylint: disable=import-error,wrong-import-position
    FIXTURES,
    FIXTURE_CLASSES,
    RECOVERY_SCRIPT,
    STAGNATION_CLASSES,
    SEEDS,
    Decision,
    FixtureModel,
    FixtureSpec,
    TriggerReport,
    _deliver_operator_cue,
    _probe,
)
from tools.exp_0002_signal_coverage import (  # pylint: disable=import-error,wrong-import-position
    ACTION_BUDGET,
    BURN_FRACTION,
    CONTEXT_TOKEN_BUDGET,
    COOLDOWN_CALLS,
    EVALUATION_BUDGET,
    MAIN_CALL_BUDGET,
    OPERATOR_CUE_CALL,
    REPEAT_THRESHOLD,
    RUN2_JOURNAL_SHA256,
    SUPERVISOR_CALL_ALLOCATION,
    SUPERVISOR_TOKENS_PER_CALL,
    SUPERVISOR_TOKEN_ALLOCATION,
    WINDOW_CALLS,
    CandidateMonitor,
    OutputFixtureEnvironment,
    OutputFixtureEvaluator,
    OutputFixtureRuntime,
    StepFacts,
    _as_int,
    _as_list,
    _as_mapping,
    _as_str,
    _bundle_label,
    _decision_json,
    _decision_prefixes,
    _event_stream_bytes,
    _exec_output,
    _git_revision,
    _load_oracle,
    _load_run2_events,
    _read_events,
    _run2_call_budget,
    _sha256_file,
    _step_facts,
    _write_boundary_csv,
    _write_fingerprint_csv,
    _now,
    verify_artifact_store_copy,
)

# This is a bounded, auditable evidence harness, not a product module.
# pylint: disable=too-many-arguments,too-many-instance-attributes
# pylint: disable=too-many-lines,too-many-locals,too-many-statements,too-many-branches
# pylint: disable=too-few-public-methods

BUNDLE_DIR = SOURCE_ROOT / "evidence" / "exp-0003-burn-progress"
TOOL_RELPATH = "tools/exp_0003_burn_progress.py"
EVENT_STREAM_NAME = "events.jsonl.gz"
RUN2_ORACLE_RELPATH = (
    "coordination/diagnostics/acceptance-20261028/review-data/"
    "counterfactual_a_fingerprints.csv"
)

POLICIES = ("P4", "P5", "P6")
CANDIDATES = ("P4", "P5", "P6")
CHECKPOINT_POLICY = "P6"
CHECKPOINT_CAUSE = "budget-checkpoint"

PAIRED_CLASSES = (
    "novel-stdout-no-progress",
    "novel-stdout-with-progress",
    "pending-claims-only",
    "duplicate-progress",
    "retry-heavy-burn",
)
ALL_CLASSES = (*FIXTURE_CLASSES, *PAIRED_CLASSES)
# Classes where a burn/checkpoint trigger is the correct observation; the
# carried stagnation classes are among them (EXP-0001's defining behavior).
BURN_SENSITIVITY_CLASSES = (
    "novel-stdout-no-progress",
    "pending-claims-only",
    "duplicate-progress",
    "retry-heavy-burn",
)
# Classes where any trigger is a false intervention on exploration or on
# truthful progress (the intervention cost the predeclaration measures).
INTERVENTION_COST_CLASSES = (
    "productive",
    "plateaued-exploring",
    "novel-stdout-with-progress",
)

PAIRED_CLASS_NOTE = {
    "novel-stdout-no-progress": (
        "novel stdout on every call with no supported progress: the run4 "
        "shape at fixture scale. P5's evidence-novelty reset stays silent; "
        "P4 and P6 must checkpoint."
    ),
    "novel-stdout-with-progress": (
        "equally novel evidence WITH valid novel supported progress every "
        "other call: truthful resets must keep every policy silent."
    ),
    "pending-claims-only": (
        "self-reported claims that never assess to supported progress, plus "
        "novel probes: pending noise must not reset any anchor."
    ),
    "duplicate-progress": (
        "the same supported knowledge statement re-recorded every call "
        "under fresh item IDs: duplicate support resets P4's claim-based "
        "anchor but must not reset P6's, which dedups supported-progress "
        "content identity."
    ),
    "retry-heavy-burn": (
        "two labeled synthetic transport retries per decision (consumed "
        "calls = 3 x decision index; the underlying session ledger still "
        "sees one call per decision): P6's ledger counting must checkpoint "
        "at consumed 9 while decision-counted policies stay silent."
    ),
}

CHECKPOINT_SEMANTICS = {
    "formula": (
        "checkpoint due at decision boundary c when consumed_calls(c-1) - "
        "consumed_at_last_novel_supported_progress >= ceil(0.6 * "
        "model_call_budget); consumed calls are the settled ledger "
        "consumption per decision (1 + the labeled retry overlay in the "
        "retry-heavy class), not the count of decision manifests"
    ),
    "anchor_resets": (
        "only novel supported progress resets the anchor: a supported "
        "progress_claim_recorded or knowledge_recorded event whose claim/"
        "item identity (kind, id, before/after or statement, scope) has not "
        "been supported before in the run. Duplicate support, pending or "
        "self-reported claims, raw receipts, novel evidence digests, "
        "recovery, and waits never reset it"
    ),
    "evidence_novelty": (
        "R1/R2 evidence fingerprints feed repetition detection unchanged; "
        "evidence novelty is diagnostic input and never P6's reset signal"
    ),
    "cooldown": (
        "one shared four-call cooldown across repetition and checkpoint "
        "triggers; repetition has precedence at a boundary where both are "
        "due; a due-but-suppressed checkpoint is recorded, never lost"
    ),
    "advisory": (
        "a checkpoint is an advisory spending review: it never declares "
        "task failure, acceptance, or completion; when no supervisor "
        "capacity fits the remaining budget the harness records a durable "
        "operator escalation with the trigger evidence instead"
    ),
}

POLICY_PARAMETERS: Mapping[str, Mapping[str, object]] = {
    "P4": {
        "kind": "carried-families + stdout-evidence + claim-based-burn",
        "version": "monitor.p4.v1",
        "window_calls": WINDOW_CALLS,
        "repeat_threshold": REPEAT_THRESHOLD,
        "cooldown_calls": COOLDOWN_CALLS,
        "burn_fraction": BURN_FRACTION,
    },
    "P5": {
        "kind": "carried-families + stdout-evidence + claim-free-burn",
        "version": "monitor.p5.v1",
        "window_calls": WINDOW_CALLS,
        "repeat_threshold": REPEAT_THRESHOLD,
        "cooldown_calls": COOLDOWN_CALLS,
        "burn_fraction": BURN_FRACTION,
    },
    "P6": {
        "kind": (
            "carried-families + stdout-evidence + ledger-based "
            "supported-progress checkpoints"
        ),
        "version": "monitor.p6.v1",
        "window_calls": WINDOW_CALLS,
        "repeat_threshold": REPEAT_THRESHOLD,
        "cooldown_calls": COOLDOWN_CALLS,
        "burn_fraction": BURN_FRACTION,
    },
}

# Pre-registered run2 expectations, frozen before execution. The run2 budget
# is 40 model calls and run2 recorded no supported progress, so P6's
# checkpoint fires exactly where P4's claim-based burn fired in EXP-0002
# (consumed 24/28/32/36 at boundaries 25/29/33/37) and P5 must stay silent.
RUN2_PRE_REGISTRATION: Mapping[str, Mapping[str, object]] = {
    "P4": {"repetition": [12, 17], "burn_consumed": [24, 28, 32, 36]},
    "P5": {"repetition": [12, 17], "burn_consumed": []},
    "P6": {"repetition": [12, 17], "checkpoint_consumed": [24, 28, 32, 36]},
}

# Pre-registered paired-class expectations, frozen before execution from the
# CHECKPOINT_SEMANTICS formula (12-call fixture budgets; consumed calls equal
# the decision index except in retry-heavy-burn where consumed = 3 x index;
# the per-seed fire counts are the number of registered trigger boundaries).
PAIRED_PRE_REGISTRATION: Mapping[str, Mapping[str, Mapping[str, object]]] = {
    "novel-stdout-no-progress": {
        "P4": {"repetition": [], "burn_consumed": [8]},
        "P5": {"repetition": [], "burn_consumed": []},
        "P6": {"repetition": [], "checkpoint_consumed": [8]},
    },
    "novel-stdout-with-progress": {
        "P4": {"repetition": [], "burn_consumed": []},
        "P5": {"repetition": [], "burn_consumed": []},
        "P6": {"repetition": [], "checkpoint_consumed": []},
    },
    "pending-claims-only": {
        "P4": {"repetition": [], "burn_consumed": [8]},
        "P5": {"repetition": [], "burn_consumed": []},
        "P6": {"repetition": [], "checkpoint_consumed": [8]},
    },
    "duplicate-progress": {
        "P4": {"repetition": [], "burn_consumed": []},
        "P5": {"repetition": [], "burn_consumed": [8]},
        "P6": {"repetition": [], "checkpoint_consumed": [9]},
    },
    "retry-heavy-burn": {
        "P4": {"repetition": [], "burn_consumed": []},
        "P5": {"repetition": [], "burn_consumed": []},
        "P6": {"repetition": [], "checkpoint_consumed": [9]},
    },
}

THRESHOLD_CHECK = {
    "budget": 40,
    "expected_first_checkpoint_consumed": 24,
    "expected_first_checkpoint_boundary": 25,
    "novel_stdout_calls": (
        "every decision carries a distinct R1/R2 digest; the checkpoint is "
        "due exactly the same despite evidence novelty"
    ),
    "provider_calls": 0,
}


# --- Paired fixture classes ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryHeavySpec(FixtureSpec):
    """A fixture spec with a labeled synthetic transport-retry overlay.

    ``retries_per_call`` charges extra consumed calls to every decision in
    the harness's ledger counting. The underlying session still records one
    model call per decision; the overlay is the experiment's labeled model
    of retry-heavy consumption and is documented in the manifest.
    """

    retries_per_call: int = 0


def _item(statement: str) -> Decision:
    return Decision("knowledge-item", claim_after=statement)


def _pending_claim(after: str) -> Decision:
    return Decision("knowledge-progress", claim_after=after)


PAIRED_FIXTURES: Mapping[str, FixtureSpec] = {
    "novel-stdout-no-progress": FixtureSpec(
        name="novel-stdout-no-progress",
        plateau=tuple(_probe(f"t{index}") for index in range(1, 13)),
        recovery=RECOVERY_SCRIPT,
    ),
    "novel-stdout-with-progress": FixtureSpec(
        name="novel-stdout-with-progress",
        plateau=tuple(
            decision
            for index in range(1, 7)
            for decision in (
                _probe(f"t{index}"),
                _item(
                    f"finding {index}: the fixture goal requires focus, "
                    "repair, and verify"
                ),
            )
        ),
        recovery=RECOVERY_SCRIPT,
    ),
    "pending-claims-only": FixtureSpec(
        name="pending-claims-only",
        plateau=tuple(
            decision
            for index in range(1, 7)
            for decision in (
                _probe(f"t{index}"),
                _pending_claim(f"self-reported guess {index}: the goal seems near"),
            )
        ),
        recovery=RECOVERY_SCRIPT,
    ),
    "duplicate-progress": FixtureSpec(
        name="duplicate-progress",
        plateau=(_item("the fixture goal requires focus, repair, and verify"),),
        recovery=RECOVERY_SCRIPT,
    ),
    # astroid does not merge the dataclass-inherited fields of the spec.
    # pylint: disable=unexpected-keyword-arg
    "retry-heavy-burn": RetryHeavySpec(
        name="retry-heavy-burn",
        plateau=tuple(_probe(f"t{index}") for index in range(1, 13)),
        recovery=RECOVERY_SCRIPT,
        retries_per_call=2,
    ),
}


class PendingClaimModel(FixtureModel):
    """Scripted model whose self-reported claims never carry evidence."""

    def _result_for(self, decision: Decision):
        result = super()._result_for(decision)
        if isinstance(result, ProgressClaimChange):
            return replace(result, evidence_ids=(), assessor="self-reported")
        return result


class DuplicateKnowledgeModel(FixtureModel):
    """Scripted model that re-supports one knowledge identity per call.

    Every decision re-records the same supported statement under a fresh
    item ID, so the session supports it again and the shipped claim-based
    progress definition resets every call while the content-level dedup in
    P6 does not.
    """

    def __init__(self, spec: FixtureSpec, seed: int, source_id: str) -> None:
        super().__init__(spec, seed, source_id)
        self._items = 0

    def _result_for(self, decision: Decision):
        result = super()._result_for(decision)
        if isinstance(result, MemoryUpdate):
            self._items += 1
            return replace(result, item_id=f"{result.item_id}-{self._items}")
        return result


def _model_for(spec: FixtureSpec, seed: int, source_id: str) -> FixtureModel:
    """Return the scripted model variant a paired class needs."""
    if spec.name == "pending-claims-only":
        return PendingClaimModel(spec, seed, source_id)
    if spec.name == "duplicate-progress":
        return DuplicateKnowledgeModel(spec, seed, source_id)
    return FixtureModel(spec, seed, source_id)


def _retries_for(spec: FixtureSpec) -> int:
    """Return the labeled retry overlay a fixture spec declares."""
    if isinstance(spec, RetryHeavySpec):
        return max(0, spec.retries_per_call)
    return 0


# --- Per-decision facts -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpFacts:
    """Journal-derived facts for one completed decision, EXP-0003 shape.

    ``consumed_calls`` is the settled ledger consumption charged to the call
    budget (1 + the labeled retry overlay); ``progress_digests`` are the
    identity digests of supported progress claims and knowledge items
    recorded in this decision; ``pending_claims`` counts claim events that
    did not assess to supported.
    """

    call: int
    consumed_calls: int
    carried_fingerprints: tuple[str, ...]
    evidence_fingerprints: tuple[str, ...]
    progress_digests: tuple[str, ...]
    pending_claims: int
    actions: int
    failed_receipts: int
    input_tokens: int
    progress_signals: int

    @property
    def fingerprints(self) -> tuple[str, ...]:
        """Return every repetition fingerprint this decision contributed."""
        return self.carried_fingerprints + self.evidence_fingerprints


def _claim_digest(kind: str, payload: Mapping[str, object]) -> str | None:
    """Return the identity digest of one supported claim or knowledge item."""
    body = payload.get("claim") if "claim" in payload else payload.get("item")
    mapping = _as_mapping(body)
    if not mapping:
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


def _exp_facts(
    events: Sequence[Event],
    call: int,
    store: ArtifactStore,
    *,
    retries: int = 0,
) -> ExpFacts:
    """Derive one decision's facts, including progress identities."""
    base = _step_facts(events, call, store, evidence_actions=None)
    digests: list[str] = []
    pending = 0
    for event in events:
        if event.kind not in {"progress_claim_recorded", "knowledge_recorded"}:
            continue
        mapping = _as_mapping(event.payload.get("claim", event.payload.get("item")))
        status = _as_str(mapping.get("status"))
        if status == "supported":
            digest = _claim_digest(event.kind, event.payload)
            if digest is not None:
                digests.append(digest)
        elif status == "pending":
            pending += 1
    return ExpFacts(
        call=call,
        consumed_calls=1 + max(0, retries),
        carried_fingerprints=base.carried_fingerprints,
        evidence_fingerprints=base.evidence_fingerprints,
        progress_digests=tuple(digests),
        pending_claims=pending,
        actions=base.actions,
        failed_receipts=base.failed_receipts,
        input_tokens=base.input_tokens,
        progress_signals=base.progress_signals,
    )


def _facts_json(facts: Sequence[ExpFacts]) -> list[dict[str, object]]:
    return [
        {
            "call": fact.call,
            "consumed_calls": fact.consumed_calls,
            "carried_fingerprints": list(fact.carried_fingerprints),
            "evidence_fingerprints": list(fact.evidence_fingerprints),
            "progress_digests": list(fact.progress_digests),
            "pending_claims": fact.pending_claims,
            "actions": fact.actions,
            "failed_receipts": fact.failed_receipts,
            "input_tokens": fact.input_tokens,
            "progress_signals": fact.progress_signals,
        }
        for fact in facts
    ]


# --- Candidate monitors -------------------------------------------------------


class Exp3Runtime(OutputFixtureRuntime):
    """Scripted fixture runtime scoped to the EXP-0003 experiment IDs."""

    def identity(self) -> ComponentIdentity:
        """Return the experiment-scoped runtime identity."""
        return ComponentIdentity(f"exp0003-{self._spec.name}-runtime", "v1")


class Exp3Environment(OutputFixtureEnvironment):
    """Fixture environment scoped to the EXP-0003 experiment IDs."""

    @property
    def environment_id(self) -> str:
        """Return the experiment-scoped environment ID."""
        return f"exp0003-{self._spec.name}"


class Exp3Evaluator(OutputFixtureEvaluator):
    """Fixture evaluator scoped to the EXP-0003 experiment IDs."""

    def identity(self) -> ComponentIdentity:
        """Return the experiment-scoped evaluator identity."""
        return ComponentIdentity(f"exp0003-{self._spec.name}-evaluator", "v1")


def _exp_repetition_hit(
    facts: Sequence[ExpFacts],
) -> tuple[str, int] | None:
    """Return the most-repeated fingerprint in the window past the threshold."""
    counts: Counter[str] = Counter()
    for fact in facts[-WINDOW_CALLS:]:
        counts.update(fact.fingerprints)
    if not counts:
        return None
    fingerprint, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    if count >= REPEAT_THRESHOLD:
        return fingerprint, count
    return None


def _progress_anchor(facts: Sequence[ExpFacts]) -> tuple[int, int, int]:
    """Return (consumed_total, anchor_consumed, pending_claims) over the facts.

    The anchor is the consumed-call total at the last novel supported
    progress identity: identities already supported earlier in the run never
    reset it (duplicate support), and pending or self-reported claims never
    touch it at all.
    """
    consumed = 0
    anchor = 0
    seen: set[str] = set()
    pending = 0
    for fact in facts:
        consumed += fact.consumed_calls
        pending += fact.pending_claims
        novel = [digest for digest in fact.progress_digests if digest not in seen]
        seen.update(fact.progress_digests)
        if novel:
            anchor = consumed
    return consumed, anchor, pending


@dataclass(frozen=True, slots=True)
class CheckpointReport:
    """One P6 checkpoint trigger with its full recorded decision inputs."""

    policy: str
    call: int
    reason: str
    fingerprint: str | None
    repeat_count: int | None
    actions_used: int
    tokens_used: int
    consumed_calls: int
    threshold: int
    progress_anchor: int

    def to_json(self) -> dict[str, object]:
        """Return the trigger-input log entry with checkpoint fields."""
        return {
            "policy": self.policy,
            "call": self.call,
            "reason": self.reason,
            "fingerprint": self.fingerprint,
            "repeat_count": self.repeat_count,
            "actions_used": self.actions_used,
            "tokens_used": self.tokens_used,
            "consumed_calls": self.consumed_calls,
            "threshold": self.threshold,
            "progress_anchor": self.progress_anchor,
        }


class CheckpointMonitor:
    """Experiment-local P6: ledger-based supported-progress budget checkpoints.

    Repetition detection carries the shipped families plus the R1/R2
    evidence digests unchanged. The checkpoint is due when the settled
    ledger consumption since the last novel supported progress reaches
    ``ceil(0.6 * budget)``; the shared four-call cooldown and repetition
    precedence match the shipped policies; the checkpoint is advisory and
    never declares failure, acceptance, or completion.
    """

    version = "monitor.p6.v1"

    def __init__(self, call_budget: int) -> None:
        self._call_budget = call_budget
        self._last_fire: int | None = None

    @property
    def policy(self) -> str:
        """Return the policy identifier."""
        return CHECKPOINT_POLICY

    def threshold(self) -> int:
        """Return the checkpoint's consumed-call threshold."""
        return math.ceil(BURN_FRACTION * self._call_budget)

    def due(self, facts: Sequence[ExpFacts]) -> tuple[int, int] | None:
        """Return (consumed, anchor) when a checkpoint is due, else None."""
        consumed, anchor, _pending = _progress_anchor(facts)
        if consumed - anchor >= self.threshold():
            return consumed, anchor
        return None

    def observe(
        self, facts: Sequence[ExpFacts], call: int
    ) -> TriggerReport | CheckpointReport | None:
        """Return a trigger report when the policy fires before this call."""
        if self._last_fire is not None and call - self._last_fire < COOLDOWN_CALLS:
            return None
        hit = _exp_repetition_hit(facts)
        if hit is not None:
            self._last_fire = call
            counted = facts[-WINDOW_CALLS:]
            return TriggerReport(
                policy=CHECKPOINT_POLICY,
                call=call,
                reason="repetition",
                fingerprint=hit[0],
                repeat_count=hit[1],
                actions_used=sum(fact.actions for fact in counted),
                tokens_used=sum(fact.input_tokens for fact in counted),
            )
        due = self.due(facts)
        if due is None:
            return None
        self._last_fire = call
        consumed, anchor = due
        return CheckpointReport(
            policy=CHECKPOINT_POLICY,
            call=call,
            reason=CHECKPOINT_CAUSE,
            fingerprint=None,
            repeat_count=None,
            actions_used=sum(fact.actions for fact in facts),
            tokens_used=sum(fact.input_tokens for fact in facts),
            consumed_calls=consumed,
            threshold=self.threshold(),
            progress_anchor=anchor,
        )


# --- Fixture task and runner ---------------------------------------------------


def _knowledge_source(spec: FixtureSpec, session_id: str) -> KnowledgeSource:
    source_id = f"exp0003-{spec.name}-brief"
    observed_at = _now()
    return KnowledgeSource(
        source_id=source_id,
        provenance="fixture brief",
        observed_at=observed_at,
        envelope=RecordEnvelope(source_id, session_id, 1, observed_at, "fixture brief"),
    )


def _task(spec: FixtureSpec, seed: int) -> TaskSpec:
    return TaskSpec(
        task_id=f"exp0003-{spec.name}-s{seed}",
        objective=f"Reach the fixture goal for {spec.name} (seed {seed}).",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external fixture-evaluator acceptance",
        budgets=BudgetLimits(
            actions=ACTION_BUDGET,
            model_calls=MAIN_CALL_BUDGET,
            evaluations=EVALUATION_BUDGET,
        ),
        environment_id=f"exp0003-{spec.name}",
        evaluation_contract_id=f"exp0003-{spec.name}-evaluator:v1",
    )


def _run_once(  # pylint: disable=too-many-branches
    spec: FixtureSpec,
    seed: int,
    policy: str,
    run_dir: Path,
    *,
    repeat: bool = False,
) -> dict[str, object]:
    run_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-repeat" if repeat else ""
    session_id = f"exp0003-{spec.name}-s{seed}-{policy.lower()}{suffix}"
    run_id = f"{spec.name}/s{seed}/{policy}{suffix}"
    retries = _retries_for(spec)

    journal = SqliteJournal(run_dir / "session.sqlite3")
    session: Session | None = None
    events: tuple[Event, ...] = ()
    counters = None
    facts: list[ExpFacts] = []
    triggers: list[dict[str, object]] = []
    suppressions: list[dict[str, object]] = []
    escalations: list[dict[str, object]] = []
    operator_cues: list[dict[str, object]] = []
    supervisor_calls = 0
    supervisor_tokens = 0
    accepted_at: int | None = None
    terminal = "model_budget_exhausted"
    error: str | None = None
    cursor = 0
    steps = 0
    consumed = 0
    try:
        artifacts = ArtifactStore(run_dir / "artifacts")
        runtime = Exp3Runtime(spec, artifacts)
        evaluator = Exp3Evaluator(spec, runtime)
        environment = Exp3Environment(spec)
        source = _knowledge_source(spec, session_id)
        model = _model_for(spec, seed, source.source_id)
        monitor = None
        if policy == "P6":
            monitor = CheckpointMonitor(MAIN_CALL_BUDGET)
        elif policy in ("P4", "P5"):
            monitor = CandidateMonitor(policy, MAIN_CALL_BUDGET)
        session = Session.create(
            session_id,
            _task(spec, seed),
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            knowledge_sources=(source,),
        )
        for call in range(1, MAIN_CALL_BUDGET + 1):
            if (
                policy == "B0"
                and spec.name in STAGNATION_CLASSES
                and call == OPERATOR_CUE_CALL
            ):
                operator_cues.append(_deliver_operator_cue(session, session_id, call))
                model.intervene()
            if monitor is not None:
                report = monitor.observe(tuple(facts), call)
                due = (
                    monitor.due(tuple(facts))
                    if isinstance(monitor, CheckpointMonitor)
                    else None
                )
                if report is not None:
                    row = report.to_json()
                    triggers.append(row)
                    if supervisor_calls < SUPERVISOR_CALL_ALLOCATION:
                        supervisor_calls += 1
                        supervisor_tokens += SUPERVISOR_TOKENS_PER_CALL
                        model.intervene()
                    else:
                        escalations.append(
                            {
                                "call": call,
                                "reason": _as_str(row.get("reason")),
                                "detail": (
                                    "no supervisor capacity fits the "
                                    "remaining budget; the trigger evidence "
                                    "is escalated to the operator"
                                ),
                            }
                        )
                elif due is not None:
                    suppressions.append(
                        {
                            "call": call,
                            "consumed_calls": due[0],
                            "progress_anchor": due[1],
                            "threshold": monitor.threshold(),
                            "disposition": "cooldown-suppressed",
                        }
                    )
            view = session.step(model, token_budget=CONTEXT_TOKEN_BUDGET)
            steps = call
            consumed += 1 + retries
            if view.pending_proposal_id is not None:
                session.reconcile_pending()
            new_events = session.events(after_sequence=cursor, limit=10000)
            if new_events:
                cursor = new_events[-1].sequence
            facts.append(_exp_facts(new_events, call, artifacts, retries=retries))
            if len(session.accepted_lineage()) > 1:
                accepted_at = call
                terminal = "accepted"
                break
            if view.status in {
                SessionStatus.COMPLETED,
                SessionStatus.STOPPED,
                SessionStatus.FAILED,
            }:
                terminal = f"status:{view.status.value}"
                break
            if consumed >= MAIN_CALL_BUDGET:
                break
    except ModelCallError as exc:
        terminal = "model_error"
        error = f"{type(exc).__name__}: {exc}"
    except (TransitionError, ContractError) as exc:
        terminal = "error"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            events = session.events(limit=10000)
            counters = derive_counters(events)
        journal.close()
    if session is None or counters is None:
        raise RuntimeError(f"run {run_id} did not create a session: {error}")

    journal_path = run_dir / "session.sqlite3"
    source_journal_sha256 = _sha256_file(journal_path)
    wal_sidecars = [
        sidecar
        for sidecar in (
            journal_path.with_name(journal_path.name + "-wal"),
            journal_path.with_name(journal_path.name + "-shm"),
        )
        if sidecar.exists()
    ]
    stream, uncompressed_bytes = _event_stream_bytes(events)
    (run_dir / EVENT_STREAM_NAME).write_bytes(stream)
    journal_path.unlink()
    for sidecar in wal_sidecars:
        sidecar.unlink()
    summary: dict[str, object] = {
        "run_id": run_id,
        "session_id": session_id,
        "fixture": spec.name,
        "seed": seed,
        "policy": policy,
        "repeat": repeat,
        "onset_call": spec.onset_call,
        "steps": steps,
        "consumed_calls": consumed,
        "retries_per_call": retries,
        "terminal": terminal,
        "error": error,
        "accepted": accepted_at is not None,
        "accepted_at_call": accepted_at,
        "calls_to_recovery": (
            accepted_at - spec.onset_call if accepted_at is not None else None
        ),
        "triggers": triggers,
        "checkpoint_suppressions": suppressions,
        "operator_escalations": escalations,
        "operator_cues": operator_cues,
        "supervisor_calls": supervisor_calls,
        "supervisor_tokens": supervisor_tokens,
        "supervisor_allocation_exceeded": (
            supervisor_calls > SUPERVISOR_CALL_ALLOCATION
            or supervisor_tokens > SUPERVISOR_TOKEN_ALLOCATION
        ),
        "facts": _facts_json(facts),
        "counters": {
            "actions": counters.actions,
            "evaluations": counters.evaluations,
            "model_calls": counters.model_calls,
            "commits": counters.commits,
            "attempts": counters.attempts,
            "reconciliations": counters.reconciliations,
        },
        "journal": {
            "path": EVENT_STREAM_NAME,
            "format": "canonical-event-stream-gzip-jsonl",
            "sha256": hashlib.sha256(stream).hexdigest(),
            "uncompressed_bytes": uncompressed_bytes,
            "source_journal_sha256": source_journal_sha256,
            "event_count": len(events),
            "wal_sidecars": [sidecar.name for sidecar in wal_sidecars],
        },
        "artifacts": {
            "path": "artifacts",
            "retained": True,
            "file_count": sum(
                1 for path in artifacts.root.rglob("*") if path.is_file()
            ),
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (run_dir / "triggers.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "policy_parameters": POLICY_PARAMETERS[policy],
                "triggers": triggers,
                "checkpoint_suppressions": suppressions,
                "operator_escalations": escalations,
                "operator_cues": operator_cues,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


# --- Run2/run4 replay ----------------------------------------------------------


def _carried_only_facts(fact: StepFacts, call: int) -> ExpFacts:
    """Convert an EXP-0002 facts record into the EXP-0003 shape for run2.

    Run2's journal records no transport retries and its supervision history
    is claim-free, so every decision consumes exactly one settled call and
    P6's anchor never moves: cumulative consumption c-1 with a zero anchor
    reproduces the checkpoint boundaries from the declared formula.
    """
    return ExpFacts(
        call=call,
        consumed_calls=1,
        carried_fingerprints=fact.carried_fingerprints,
        evidence_fingerprints=fact.evidence_fingerprints,
        progress_digests=(),
        pending_claims=0,
        actions=fact.actions,
        failed_receipts=fact.failed_receipts,
        input_tokens=fact.input_tokens,
        progress_signals=fact.progress_signals,
    )


def replay_run2(run2_dir: Path, bundle: Path) -> dict[str, object]:
    """Replay P4/P5/P6 over the verified run2 journal and artifact copies."""
    journal_path = run2_dir / "session.sqlite3"
    if not journal_path.exists():
        raise SystemExit(f"{journal_path} does not exist")
    observed_sha256 = _sha256_file(journal_path)
    if observed_sha256 != RUN2_JOURNAL_SHA256:
        raise SystemExit(
            f"run2 journal copy sha256 {observed_sha256} does not verify "
            f"against the pre-registered {RUN2_JOURNAL_SHA256}; stop the "
            "replay slice per the EXP-0003 predeclaration"
        )
    store_dir = run2_dir / "artifacts"
    store_problems = (
        verify_artifact_store_copy(store_dir) if store_dir.exists() else ["missing"]
    )
    if store_problems:
        raise SystemExit(
            f"run2 artifact-store copy did not verify ({len(store_problems)} "
            "problems, first: " + store_problems[0] + "); stop the replay "
            "slice per the EXP-0003 predeclaration"
        )
    store = ArtifactStore(store_dir)
    events = _load_run2_events(journal_path)
    budget = _run2_call_budget(events)
    segments = decision_segments(events)
    prefixes = _decision_prefixes(events)
    decision_facts_list = [
        _step_facts(segment, call, store, evidence_actions=frozenset({"exec"}))
        for call, segment in enumerate(segments, start=1)
    ]

    monitors = {
        "P4": CandidateMonitor("P4", budget),
        "P5": CandidateMonitor("P5", budget),
        "P6": CheckpointMonitor(budget),
    }
    replay_rows: dict[str, list[dict[str, object]]] = {
        policy: [] for policy in POLICIES
    }
    fingerprint_rows: list[dict[str, object]] = []
    oracle = _load_oracle()
    for boundary in range(2, len(prefixes) + 2):
        prefix = prefixes[boundary - 2]
        shipped_facts = decision_facts_list[: boundary - 1]
        # The shipped default is monitor.p5.v1 (ADR-0011): cross-check the
        # local P5 arm against it over each prefix.
        shipped = evaluate_supervision(
            prefix,
            boundary,
            MonitorPolicy(),
            artifacts=store,
            model_call_budget=budget,
        )
        if shipped is not None:
            replay_rows["P5"].append(
                {
                    "call": boundary,
                    "consumed_calls": boundary - 1,
                    "reason": shipped.cause,
                    "fingerprint": shipped.fingerprint,
                    "repeat_count": shipped.repeat_count,
                    "source": "shipped-evaluate_supervision",
                }
            )
        for policy in ("P4", "P5"):
            report = monitors[policy].observe(tuple(shipped_facts), boundary)
            if report is not None:
                replay_rows[policy].append(report.to_json())
        checkpoint_facts = [
            _carried_only_facts(fact, call)
            for call, fact in enumerate(shipped_facts, start=1)
        ]
        p6_report = monitors["P6"].observe(checkpoint_facts, boundary)
        if p6_report is not None:
            replay_rows["P6"].append(p6_report.to_json())
        if boundary - 1 <= len(decision_facts_list):
            latest = decision_facts_list[boundary - 2]
            if latest.evidence_fingerprints:
                strict = next(
                    (
                        fingerprint
                        for fingerprint in latest.evidence_fingerprints
                        if fingerprint.startswith("stdout-strict")
                    ),
                    "",
                )
                normalized = next(
                    (
                        fingerprint
                        for fingerprint in latest.evidence_fingerprints
                        if fingerprint.startswith("stdout-normalized")
                    ),
                    "",
                )
                row = oracle.get(boundary - 1, {})
                fingerprint_rows.append(
                    {
                        "call": boundary - 1,
                        "stdout_strict": strict,
                        "stdout_normalized": normalized,
                        "oracle_strict": row.get("stdout_strict_sha256", ""),
                        "oracle_normalized": row.get("stdout_normalized_sha256", ""),
                        "strict_matches": bool(row)
                        and strict.endswith(row.get("stdout_strict_sha256", "")),
                        "normalized_matches": bool(row)
                        and normalized.endswith(
                            row.get("stdout_normalized_sha256", "")
                        ),
                    }
                )

    summary = _replay_summary(replay_rows, fingerprint_rows, budget, len(prefixes))
    summary["journal"] = {
        "path": str(journal_path),
        "sha256": observed_sha256,
        "expected_sha256": RUN2_JOURNAL_SHA256,
        "verified": True,
        "event_count": len(events),
    }
    summary["artifact_store"] = {
        "path": str(store_dir),
        "files_verified": sum(1 for path in store_dir.rglob("*") if path.is_file()),
        "problems": store_problems,
    }
    _write_replay_bundle(bundle, "run2", replay_rows, fingerprint_rows, summary)
    return summary


def replay_run4(run4_dir: Path | None, bundle: Path) -> dict[str, object]:
    """Replay the run4 journal copy, or record the input as missing.

    The predeclaration freezes the run4 input SHA-256 when a verified copy
    is supplied. With no copy available this slice records the missing input
    and stops, exactly as EXP-0003's Inputs and procedure requires; the
    fixture executions and the run2 replay proceed independently.
    """
    record: dict[str, object] = {
        "slice": "run4-counterfactual-replay",
        "executed": False,
    }
    if run4_dir is None or not (run4_dir / "session.sqlite3").exists():
        record["status"] = "missing-input"
        record["detail"] = (
            "no sha256-verified run4 journal copy was supplied; per the "
            "EXP-0003 predeclaration the replay is recorded as missing and "
            "stopped, and the fixture executions and run2 replay stand "
            "independently"
        )
    else:
        record["status"] = "supplied-but-unpinned"
        record["detail"] = (
            "a run4 copy exists but this predeclaration pinned no expected "
            "SHA-256 for it; verify the copy, record its digest in a "
            "governed addendum, then rerun the slice"
        )
        record["observed_sha256"] = _sha256_file(run4_dir / "session.sqlite3")
    replay_dir = bundle / "replay"
    replay_dir.mkdir(parents=True, exist_ok=True)
    (replay_dir / "run4-replay.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return record


def _write_replay_bundle(
    bundle: Path,
    name: str,
    replay_rows: Mapping[str, Sequence[Mapping[str, object]]],
    fingerprint_rows: Sequence[Mapping[str, object]],
    summary: Mapping[str, object],
) -> None:
    """Write one replay slice's CSV tables and summary."""
    replay_dir = bundle / "replay"
    replay_dir.mkdir(parents=True, exist_ok=True)
    for policy in POLICIES:
        _write_boundary_csv(
            replay_dir / f"{name}-{policy.lower()}-boundaries.csv",
            replay_rows[policy],
        )
    _write_fingerprint_csv(
        replay_dir / f"{name}-stdout-fingerprints.csv", fingerprint_rows
    )
    (replay_dir / f"{name}-replay-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _replay_summary(
    replay_rows: Mapping[str, Sequence[Mapping[str, object]]],
    fingerprint_rows: Sequence[Mapping[str, object]],
    budget: int,
    decisions: int,
) -> dict[str, object]:
    """Compare the run2 replay tables against the pre-registration."""
    policies: dict[str, dict[str, object]] = {}
    for policy in POLICIES:
        expected = RUN2_PRE_REGISTRATION[policy]
        expected_repetition = [
            _as_int(value) for value in _as_list(expected.get("repetition"))
        ]
        registered = _as_list(
            expected.get("burn_consumed", expected.get("checkpoint_consumed", []))
        )
        expected_consumed = [_as_int(value) + 1 for value in registered]
        observed_repetition = [
            _as_int(row.get("call"))
            for row in replay_rows[policy]
            if row.get("reason") == "repetition"
            and _as_str(row.get("source")) != "shipped-evaluate_supervision"
        ]
        observed_triggers = [
            _as_int(row.get("call"))
            for row in replay_rows[policy]
            if _as_str(row.get("reason")).startswith("budget-")
        ]
        missing = sorted(set(expected_repetition) - set(observed_repetition)) + sorted(
            set(expected_consumed) - set(observed_triggers)
        )
        extra = sorted(
            set(observed_repetition) - set(expected_repetition) - set(expected_consumed)
        ) + sorted(set(observed_triggers) - set(expected_consumed))
        entry: dict[str, object] = {
            "expected_repetition_boundaries": expected_repetition,
            "expected_trigger_boundaries": expected_consumed,
            "expected_trigger_consumed": [_as_int(value) for value in registered],
            "observed_repetition_boundaries": observed_repetition,
            "observed_trigger_boundaries": observed_triggers,
            "observed_trigger_consumed": [
                boundary - 1 for boundary in observed_triggers
            ],
            "observed_triggers": [
                row
                for row in replay_rows[policy]
                if _as_str(row.get("source")) != "shipped-evaluate_supervision"
            ],
            "missing_pre_registered_fires": missing,
            "extra_fires": extra,
        }
        if policy == "P5":
            entry["shipped_cross_check_triggers"] = [
                row
                for row in replay_rows[policy]
                if _as_str(row.get("source")) == "shipped-evaluate_supervision"
            ]
        policies[policy] = entry
    compared = [row for row in fingerprint_rows if row.get("oracle_strict")]
    strict_mismatched = [
        _as_int(row.get("call"))
        for row in compared
        if not bool(row.get("strict_matches"))
    ]
    normalized_mismatched = [
        _as_int(row.get("call"))
        for row in compared
        if not bool(row.get("normalized_matches"))
    ]
    return {
        "budget": {"model_calls": budget, "decisions": decisions},
        "policies": policies,
        "stdout_fingerprints": {
            "oracle": RUN2_ORACLE_RELPATH,
            "rows_compared": len(compared),
            "strict_matched": len(compared) - len(strict_mismatched),
            "normalized_matched": len(compared) - len(normalized_mismatched),
            "strict_mismatched_calls": strict_mismatched,
            "normalized_mismatched_calls": normalized_mismatched,
        },
    }


# --- Manifest, aggregate, verify --------------------------------------------


def _exp_fixture_manifest() -> dict[str, object]:
    """Freeze both fixture registries with their scripted outputs."""
    manifest: dict[str, object] = {}
    for name in ALL_CLASSES:
        spec = FIXTURES[name] if name in FIXTURES else PAIRED_FIXTURES[name]
        outputs = [
            {
                "decision": _decision_json(item),
                "stdout": _exec_output(item.name, item.resolved_arguments()),
            }
            for item in (*spec.opening, *spec.plateau)
            if item.kind == "action"
        ]
        entry: dict[str, object] = {
            "onset_call": spec.onset_call,
            "probe_failure": spec.probe_failure,
            "interrupt_opening": spec.interrupt_opening,
            "success_expected_without_intervention": (
                spec.success_expected_without_intervention
            ),
            "opening": [_decision_json(item) for item in spec.opening],
            "plateau": [_decision_json(item) for item in spec.plateau],
            "recovery": [_decision_json(item) for item in spec.recovery],
            "outputs": outputs,
        }
        if name in PAIRED_FIXTURES:
            entry["paired_class_note"] = PAIRED_CLASS_NOTE[name]
        if isinstance(spec, RetryHeavySpec):
            entry["retries_per_call"] = spec.retries_per_call
            entry["retry_overlay_note"] = (
                "labeled synthetic transport-retry consumption overlay; the "
                "underlying session ledger records one model call per "
                "decision"
            )
        manifest[name] = entry
    return manifest


def build_manifest() -> dict[str, object]:
    """Return the frozen run manifest for review and execution."""
    revision, dirty = _git_revision()
    return {
        "experiment": "EXP-0003",
        "document": "plans/experiments/exp-0003-burn-progress.md",
        "source": {
            "git_revision": revision,
            "git_dirty": dirty,
            "tool": TOOL_RELPATH,
            "tool_sha256": _sha256_file(Path(__file__)),
        },
        "fixtures": _exp_fixture_manifest(),
        "seeds": list(SEEDS),
        "policies": {name: POLICY_PARAMETERS[name] for name in POLICIES},
        "checkpoint_semantics": CHECKPOINT_SEMANTICS,
        "pre_registration": {
            "run2": RUN2_PRE_REGISTRATION,
            "paired_classes": PAIRED_PRE_REGISTRATION,
            "threshold_check": THRESHOLD_CHECK,
            "carried_classes": (
                "the six EXP-0001/0002 classes are carried unchanged and "
                "reported as measured comparisons against the EXP-0002 "
                "results; their fire counts are not re-registered here"
            ),
        },
        "budgets": {
            "model_calls": MAIN_CALL_BUDGET,
            "actions": ACTION_BUDGET,
            "context_tokens": CONTEXT_TOKEN_BUDGET,
            "evaluations": EVALUATION_BUDGET,
            "supervisor_calls": SUPERVISOR_CALL_ALLOCATION,
            "supervisor_tokens": SUPERVISOR_TOKEN_ALLOCATION,
            "supervisor_tokens_per_call": SUPERVISOR_TOKENS_PER_CALL,
        },
        "run2_replay": {
            "journal_sha256": RUN2_JOURNAL_SHA256,
            "oracle": RUN2_ORACLE_RELPATH,
            "pre_registration": RUN2_PRE_REGISTRATION,
            "provider_calls": 0,
        },
        "run4_replay": {
            "status": "input-missing",
            "detail": (
                "no sha256-verified run4 journal copy was available at "
                "manifest freeze; the slice records the missing input and "
                "stops per the predeclaration"
            ),
            "provider_calls": 0,
        },
        "executions": {
            "paired": "11 classes x 5 seeds x {P4, P5, P6} = 165 runs",
            "determinism_repeats": "55 P6 re-executions",
            "total": 220,
        },
        "terminal_conditions": [
            "external fixture-evaluator acceptance",
            "decision-budget end without acceptance",
        ],
        "retention": {
            "per_run": [
                EVENT_STREAM_NAME,
                "summary.json",
                "triggers.json",
                "artifacts/",
            ],
            "replay": [
                "replay/run2-{p4,p5,p6}-boundaries.csv",
                "replay/run2-stdout-fingerprints.csv",
                "replay/run2-replay-summary.json",
                "replay/run4-replay.json",
            ],
            "aggregate": [
                "manifest.json",
                "results.json",
                "report.md",
                "aggregate.py",
            ],
        },
        "retention_note": (
            "Each run's sqlite journal is compacted to a canonical gzip JSONL "
            "event-stream export with the source journal hash recorded; the "
            "artifact payloads are retained so fingerprints recompute from "
            "the bundle; summaries and tables are derived from the exported "
            "streams. The aggregate reports sensitivity and intervention "
            "cost together and selects no policy: the adoption decision is "
            "the operator's, per the predeclaration."
        ),
    }


def _load_summaries(bundle: Path) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for path in sorted(bundle.glob("runs/*/s*/*/summary.json")):
        summaries.append(json.loads(path.read_text(encoding="utf-8")))
    return summaries


def _trigger_rows(
    summary: Mapping[str, object], policy: str
) -> list[Mapping[str, object]]:
    return [
        row
        for row in _as_list(summary.get("triggers"))
        if isinstance(row, Mapping) and _as_str(row.get("policy")) == policy
    ]


def _expected_paired_fires(name: str, policy: str) -> int:
    """Return the pre-registered per-seed trigger count for a paired class."""
    entry = PAIRED_PRE_REGISTRATION.get(name, {}).get(policy, {})
    registered = _as_list(
        entry.get("burn_consumed", entry.get("checkpoint_consumed", []))
    )
    return len(registered)


def aggregate_bundle(bundle: Path) -> dict[str, object]:
    """Recompute the EXP-0003 sensitivity and cost tables from the bundle."""
    summaries = _load_summaries(bundle)
    index: dict[tuple[str, int, str], dict[str, object]] = {}
    for summary in summaries:
        key = (
            _as_str(summary.get("fixture")),
            _as_int(summary.get("seed")),
            _as_str(summary.get("policy"))
            + ("-repeat" if summary.get("repeat") else ""),
        )
        index[key] = summary

    reproducible = True
    for name in ALL_CLASSES:
        for seed in SEEDS:
            base = index.get((name, seed, "P6"))
            repeat = index.get((name, seed, "P6-repeat"))
            if base is None or repeat is None:
                reproducible = False
                continue
            compare = (
                "accepted",
                "accepted_at_call",
                "steps",
                "terminal",
                "triggers",
                "checkpoint_suppressions",
                "operator_escalations",
            )
            if any(base.get(field) != repeat.get(field) for field in compare):
                reproducible = False

    censored = [
        summary
        for summary in summaries
        if _as_str(summary.get("terminal"))
        not in {"accepted", "model_budget_exhausted"}
    ]

    replay: dict[str, object] = {}
    replay_path = bundle / "replay" / "run2-replay-summary.json"
    if replay_path.exists():
        replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay_policies = _as_mapping(replay.get("policies"))
    run4_path = bundle / "replay" / "run4-replay.json"
    run4 = (
        json.loads(run4_path.read_text(encoding="utf-8")) if run4_path.exists() else {}
    )

    candidates: dict[str, dict[str, object]] = {}
    for policy in CANDIDATES:
        sensitivity: dict[str, int] = {}
        missed: list[str] = []
        cost: dict[str, int] = {}
        supervisor_calls = 0
        supervisor_tokens = 0
        allocation_exceeded = 0
        escalations = 0
        suppressions = 0
        latencies: list[int] = []
        harms: list[str] = []
        for name in ALL_CLASSES:
            fires = 0
            for seed in SEEDS:
                summary = index.get((name, seed, policy), {})
                rows = _trigger_rows(summary, policy)
                fires += len(rows)
                for row in rows:
                    latencies.append(
                        _as_int(row.get("call")) - _as_int(summary.get("onset_call"))
                    )
                escalations += len(_as_list(summary.get("operator_escalations")))
                suppressions += len(_as_list(summary.get("checkpoint_suppressions")))
                comparator = "P4" if policy == "P5" else "P5"
                baseline = index.get((name, seed, comparator), {})
                if bool(baseline.get("accepted")) and not bool(summary.get("accepted")):
                    harms.append(f"{name}/s{seed}")
            sensitivity[name] = fires
            if name in BURN_SENSITIVITY_CLASSES:
                expected = _expected_paired_fires(name, policy)
                if fires < expected * len(SEEDS):
                    missed.append(
                        f"{name}: expected {expected} pre-registered "
                        f"fire(s) per seed, observed {fires} across "
                        f"{len(SEEDS)} seeds"
                    )
            if name in INTERVENTION_COST_CLASSES:
                cost[name] = fires
        for summary in summaries:
            if _as_str(summary.get("policy")) != policy:
                continue
            supervisor_calls += _as_int(summary.get("supervisor_calls"))
            supervisor_tokens += _as_int(summary.get("supervisor_tokens"))
            if summary.get("supervisor_allocation_exceeded"):
                allocation_exceeded += 1
        replay_entry = _as_mapping(replay_policies.get(policy))
        replay_missing = _as_list(replay_entry.get("missing_pre_registered_fires"))
        candidates[policy] = {
            "checkpoint_semantics": (
                CHECKPOINT_SEMANTICS if policy == "P6" else "carried from EXP-0002"
            ),
            "sensitivity_fires_by_class": sensitivity,
            "missed_pre_registered_sensitivity": missed,
            "intervention_cost_by_class": cost,
            "productive_control_fires": cost.get("productive", 0),
            "truthful_progress_fires": cost.get("novel-stdout-with-progress", 0),
            "supervisor_calls": supervisor_calls,
            "supervisor_tokens": supervisor_tokens,
            "trigger_latencies": latencies,
            "checkpoint_suppressions_recorded": suppressions,
            "operator_escalations_recorded": escalations,
            "supervisor_allocation_exceeded_runs": allocation_exceeded,
            "acceptance_harm_runs": harms,
            "run2_replay_admissible": (None if not replay else not replay_missing),
        }

    deviations: list[str] = []
    for policy in CANDIDATES:
        deviations.extend(
            f"{policy}: {item}"
            for item in candidates[policy]["missed_pre_registered_sensitivity"]
        )
        replay_missing = _as_list(
            _as_mapping(replay_policies.get(policy)).get("missing_pre_registered_fires")
        )
        if replay and replay_missing:
            deviations.append(
                f"{policy}: run2 replay missed pre-registered fires at "
                f"boundaries {replay_missing}"
            )
    if not replay:
        deviations.append("run2 replay summary missing from the bundle")
    if run4 and not run4.get("executed") and run4.get("status") != "missing-input":
        deviations.append(
            "run4 replay record is neither executed nor recorded missing-input"
        )

    return {
        "experiment": "EXP-0003",
        "bundle": _bundle_label(bundle),
        "runs": len(summaries),
        "fixtures": list(ALL_CLASSES),
        "seeds": list(SEEDS),
        "policies": list(POLICIES),
        "p6_determinism_reproducible": reproducible,
        "censored_runs": [
            {
                "run_id": summary.get("run_id"),
                "terminal": summary.get("terminal"),
                "error": summary.get("error"),
            }
            for summary in censored
        ],
        "run2_replay": replay,
        "run4_replay": run4,
        "candidates": candidates,
        "pre_registration_deviations": deviations,
        "admissible": not deviations and not censored and reproducible,
        "selection": (
            "none: EXP-0003 informs the operator's adoption decision and "
            "selects no policy (sensitivity and intervention cost are "
            "reported together, per the predeclaration)"
        ),
    }


def write_aggregate(
    bundle: Path, results: dict[str, object] | None = None
) -> dict[str, object]:
    """Write results.json and report.md for the bundle and return results."""
    computed = results if results is not None else aggregate_bundle(bundle)
    (bundle / "results.json").write_text(
        json.dumps(computed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (bundle / "report.md").write_text(_report_text(computed), encoding="utf-8")
    return computed


def _report_text(results: Mapping[str, object]) -> str:
    lines = [
        "# EXP-0003 burn-progress results",
        "",
        f"- Runs: {results.get('runs')}",
        (
            "- P6 determinism reproducible: "
            f"{results.get('p6_determinism_reproducible')}"
        ),
        f"- Admissible: {results.get('admissible')}",
        f"- Selection: {results.get('selection')}",
        "",
        "| Policy | Novel-no-progress | Pending-claims | Duplicate-progress "
        "| Retry-heavy | Productive | Truthful-progress | Supervisor calls "
        "| Escalations |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    candidates = results.get("candidates") or {}
    if isinstance(candidates, Mapping):
        for policy in CANDIDATES:
            data = candidates.get(policy)
            if not isinstance(data, Mapping):
                continue
            sensitivity = data.get("sensitivity_fires_by_class") or {}
            cost = data.get("intervention_cost_by_class") or {}
            lines.append(
                f"| {policy} "
                f"| {sensitivity.get('novel-stdout-no-progress', 0)} "
                f"| {sensitivity.get('pending-claims-only', 0)} "
                f"| {sensitivity.get('duplicate-progress', 0)} "
                f"| {sensitivity.get('retry-heavy-burn', 0)} "
                f"| {cost.get('productive', 0)} "
                f"| {cost.get('novel-stdout-with-progress', 0)} "
                f"| {data.get('supervisor_calls')} "
                f"| {data.get('operator_escalations_recorded')} |"
            )
    replay = results.get("run2_replay") or {}
    if isinstance(replay, Mapping) and replay:
        lines.append("")
        lines.append("## Run2 replay versus pre-registration")
        lines.append("")
        replay_policies = replay.get("policies") or {}
        if isinstance(replay_policies, Mapping):
            for policy in POLICIES:
                entry = replay_policies.get(policy)
                if not isinstance(entry, Mapping):
                    continue
                lines.append(
                    f"- {policy}: repetition at "
                    f"{entry.get('observed_repetition_boundaries')} "
                    f"(expected {entry.get('expected_repetition_boundaries')}); "
                    f"triggers at boundaries "
                    f"{entry.get('observed_trigger_boundaries')} "
                    f"(expected {entry.get('expected_trigger_boundaries')}, "
                    f"consumed {entry.get('expected_trigger_consumed')}); "
                    f"missing {entry.get('missing_pre_registered_fires')}; "
                    f"extra {entry.get('extra_fires')}"
                )
    run4 = results.get("run4_replay") or {}
    if isinstance(run4, Mapping) and run4:
        lines.append("")
        lines.append(f"## Run4 replay: {run4.get('status')} - {run4.get('detail')}")
    deviations = results.get("pre_registration_deviations") or []
    lines.append("")
    lines.append(
        "Pre-registration deviations: "
        + ("; ".join(str(item) for item in deviations) if deviations else "none")
    )
    lines.append("")
    lines.append(
        "Sensitivity and intervention cost are reported together; adoption "
        "is the operator's separate decision per "
        "`plans/experiments/exp-0003-burn-progress.md`."
    )
    return "\n".join(lines) + "\n"


# --- Bundle verification -------------------------------------------------------


def verify_bundle(bundle: Path) -> list[str]:
    """Check manifest identity, journal replay, and aggregate consistency."""
    problems: list[str] = []
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    source = _as_mapping(manifest.get("source"))
    tool = SOURCE_ROOT / TOOL_RELPATH
    if tool.exists() and source.get("tool_sha256") != _sha256_file(tool):
        problems.append("tool sha256 does not match the manifest")
    for summary_path in sorted(bundle.glob("runs/*/s*/*/summary.json")):
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        run_dir = summary_path.parent
        recorded = _as_mapping(summary.get("journal"))
        journal_path = run_dir / _as_str(recorded.get("path"), EVENT_STREAM_NAME)
        if not journal_path.exists():
            problems.append(f"{summary_path}: journal missing")
            continue
        if recorded.get("sha256") != _sha256_file(journal_path):
            problems.append(f"{summary_path}: journal sha256 mismatch")
        try:
            events = _read_events(journal_path)
        except (OSError, ValueError) as exc:
            problems.append(f"{summary_path}: journal unreadable: {exc}")
            continue
        if len(events) != _as_int(recorded.get("event_count")):
            problems.append(f"{summary_path}: event count mismatch")
        counters = derive_counters(events)
        expected = _as_mapping(summary.get("counters"))
        for name in ("actions", "evaluations", "model_calls", "commits"):
            if _as_int(expected.get(name)) != getattr(counters, name):
                problems.append(f"{summary_path}: counter {name} mismatch")
        if bool(summary.get("accepted")) != (counters.commits > 0):
            problems.append(f"{summary_path}: acceptance mismatch")
        store = run_dir / "artifacts"
        if not store.exists():
            problems.append(f"{summary_path}: artifact payloads missing")
    try:
        results = json.loads((bundle / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"results.json unreadable: {exc}")
        return problems
    recomputed = aggregate_bundle(bundle)
    for field_name in (
        "admissible",
        "p6_determinism_reproducible",
        "censored_runs",
        "pre_registration_deviations",
    ):
        if results.get(field_name) != recomputed.get(field_name):
            problems.append(f"results field {field_name} is not reproducible")
    for policy in CANDIDATES:
        recorded = _as_mapping(_as_mapping(results.get("candidates")).get(policy))
        fresh = _as_mapping(_as_mapping(recomputed.get("candidates")).get(policy))
        for field_name in (
            "sensitivity_fires_by_class",
            "intervention_cost_by_class",
            "supervisor_calls",
            "run2_replay_admissible",
            "missed_pre_registered_sensitivity",
        ):
            if recorded.get(field_name) != fresh.get(field_name):
                problems.append(
                    f"candidate {policy} field {field_name} is not reproducible"
                )
    return problems


def run_experiment(*, force: bool = False) -> dict[str, object]:
    """Generate the EXP-0003 bundle from a clean tree."""
    if _git_revision()[1]:
        raise SystemExit(
            "refusing to run EXP-0003 with tracked working-tree changes; "
            "commit them first"
        )
    if BUNDLE_DIR.exists() and any(BUNDLE_DIR.iterdir()):
        if not force:
            raise SystemExit(f"{BUNDLE_DIR} is not empty; pass --force to regenerate")
        shutil.rmtree(BUNDLE_DIR)
    (BUNDLE_DIR / "runs").mkdir(parents=True, exist_ok=True)
    manifest = build_manifest()
    (BUNDLE_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (BUNDLE_DIR / "aggregate.py").write_text(_aggregate_script_text(), encoding="utf-8")
    total = 0
    for name in ALL_CLASSES:
        spec = FIXTURES[name] if name in FIXTURES else PAIRED_FIXTURES[name]
        for seed in SEEDS:
            for policy in POLICIES:
                run_dir = BUNDLE_DIR / "runs" / name / f"s{seed}" / policy
                _run_once(spec, seed, policy, run_dir)
                total += 1
            repeat_dir = BUNDLE_DIR / "runs" / name / f"s{seed}" / "P6-repeat"
            _run_once(spec, seed, "P6", repeat_dir, repeat=True)
            total += 1
    results = aggregate_bundle(BUNDLE_DIR)
    results["executions"] = total
    write_aggregate(BUNDLE_DIR, results)
    return results


def _aggregate_script_text() -> str:
    return (
        '"""Recompute the EXP-0003 aggregate from the retained run journals."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "BUNDLE = Path(__file__).resolve().parent\n"
        "ROOT = BUNDLE.parents[1]\n"
        "sys.path.insert(0, str(ROOT))\n"
        "\n"
        "from tools.exp_0003_burn_progress import write_aggregate  # noqa: E402\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    write_aggregate(BUNDLE)\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the experiment, replay the live journals, or aggregate and verify."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("run", "replay", "manifest", "aggregate", "verify"),
        nargs="?",
        default="run",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--bundle", type=Path, default=BUNDLE_DIR)
    parser.add_argument("--run2-dir", type=Path, default=None)
    parser.add_argument("--run4-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "manifest":
        print(canonical_json(build_manifest()))
        return 0
    if args.command == "run":
        results = run_experiment(force=args.force)
        print(canonical_json(results))
        return 0
    if args.command == "replay":
        if args.run2_dir is None:
            parser.error("replay requires --run2-dir (a verified local copy)")
        summary = replay_run2(args.run2_dir, args.bundle)
        run4 = replay_run4(args.run4_dir, args.bundle)
        print(canonical_json({"run2": summary, "run4": run4}))
        return 0
    if args.command == "aggregate":
        results = write_aggregate(args.bundle)
        print(canonical_json(results))
        return 0
    problems = verify_bundle(args.bundle)
    if problems:
        for problem in problems:
            print(f"problem: {problem}")
        return 1
    print("verify: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
