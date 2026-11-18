"""Application service coordinating one durable, externally executed session."""

# pylint: disable=too-many-instance-attributes,too-many-arguments,too-many-lines,too-many-public-methods
# Session owns explicit external boundaries by design; collapsing them would hide I/O.
# pylint: disable=too-many-locals,too-many-return-statements,too-many-branches
# Rebuild and command dispatch remain local, bounded coordinator responsibilities.
# Rebuild restores several independent durable indexes from one ordered stream.

from __future__ import annotations

from collections.abc import Mapping

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import math
from time import perf_counter_ns
from typing import Callable, NoReturn
from uuid import uuid4

from vharness.core import RETRYABLE_FAILURE_CLASSES, VERSION, ModelFailureClass

from .artifacts import ArtifactStore
from .codec import canonical_json, parse_json, parse_json_object, to_json_value
from .errors import BudgetExhausted, ContractError, IntegrityError, TransitionError
from .evidence import (
    OPERATIONAL_MEASUREMENT_SCOPE,
    RunCounters,
    RunManifest,
    counters,
    manifest,
)
from .journal import SearchFilter, SqliteJournal
from .models import (
    ActionProposal,
    ArtifactRef,
    Attempt,
    AttemptHistory,
    Checkpoint,
    CheckpointRequest,
    CommittedNode,
    EnvironmentContract,
    EvaluationCommand,
    EvaluationDecision,
    EvaluationMode,
    EvaluationReceipt,
    EvaluationContract,
    EvaluationRequest,
    Event,
    ExecutionReceipt,
    ExecutionStatus,
    Message,
    OPERATOR_ANSWER_WAIT,
    OPERATOR_EVALUATION_WAIT,
    KnowledgeSource,
    Observation,
    RecordEnvelope,
    JsonValue,
    OperatorCommand,
    Pause,
    PendingDispatch,
    PromotionDisposition,
    ResourceReservation,
    RunIdentity,
    Resume,
    SessionStatus,
    SessionView,
    StateRef,
    Steer,
    Stop,
    SUPERVISOR_REQUEST_PREFIX,
    TaskSpec,
    UsageMeasurement,
    frozen_mapping,
)
from .context import (
    RESERVED_OUTPUT_TOKENS,
    POLICY_VERSION,
    RETRIEVAL_EVENT_KINDS,
    SYSTEM_PROMPT,
    TOKEN_COUNTER,
    TOKEN_ESTIMATOR,
    ContextOverflow,
    ContextBundle,
    ContextItem,
    ContextManifest,
    ReceiptExcerpt,
    TokenAccounting,
    assemble_context,
    count_tokens,
    estimate_rate,
    estimate_serialized_tokens,
)
from .kernel import (
    RESULT_SCHEMAS,
    ActionChoice,
    AttemptQuery,
    AgentResult,
    CompletionProposal,
    DecisionInput,
    EvaluationChoice,
    GuidanceResponse,
    KnowledgeQuery,
    LineageQuery,
    InvestigationChange,
    MemoryUpdate,
    Model,
    ModelCallError,
    ModelConfiguration,
    ModelResponse,
    safe_model_error,
    safe_provider_error,
    bounded_model_attempts,
    bounded_model_duration,
    bounded_model_output,
    OperatorQuestion,
    PlanUpdate,
    ProgressClaimChange,
    WaitChoice,
    decide,
    configuration_for,
    reserved_output_tokens_for,
    render_request,
)
from .memory import (
    AlternativeRetention,
    attempt_evidence,
    missing_attempt_evidence,
    claim_fingerprint,
    knowledge_items,
    effective_knowledge_status,
    assess_knowledge_transition,
    assess_progress_claim,
    ProgressStatus,
    validated_evidence_ids,
    latest_execution_receipt_payload,
    latest_receipt_payload,
    observation_payload,
    objective_for_version,
    progress_claims,
    covering_checkpoint,
    DETERMINISTIC_EXCERPT_MODEL,
    DETERMINISTIC_EXCERPT_VERSION,
    Episode,
    episodes,
    excerpt_episode,
    episode_evidence_ids,
    protected_digests,
    remaining_budgets,
    select_evictions,
    working_memory,
)
from .operator import OperatorView, build_operator_view
from .ports import Environment, Evaluator, ReconciliationKind, Runtime
from .replay import projection_payload, reduce_event, replay
from .session_state import evaluation_receipt_from_payload, receipt_from_payload
from .transitions import (
    OBSERVED_STARTING_STATE,
    SessionState,
    ActionOperation,
    EvaluationOperation,
    EvaluationOperationStatus,
    OperationKind,
    ModelCallOperation,
    ReconciliationStatus,
    abandon_attempt,
    action_operation,
    admit_command,
    apply_steer,
    consume_evaluation,
    resume_session,
    evaluation_operation,
    fail_evaluation_verification,
    intend_action,
    intend_evaluation,
    intend_model_call,
    interrupted_model_reservations,
    model_operation,
    promote,
    provider_recovery_waiting,
    promotion_disposition,
    operator_evaluation_receipt,
    record_evaluation_intake,
    record_evaluation_usage,
    record_command_transition,
    receive_action_receipt,
    reconcile_action,
    reconcile_evaluation,
    start_attempt,
    settle_command,
    settle_model_call,
)
from .supervision import (
    MONITOR_POLICY_P7_VERSION,
    SUPERVISOR_SYSTEM_PROMPT,
    EVIDENCE_ACTIONS,
    Guidance,
    GuidanceDecision,
    MonitorPolicy,
    SupervisionCapability,
    SupervisionTrigger,
    SupervisorDigest,
    SupervisorResponse,
    active_guidance,
    build_digest,
    checkpoint_suppression,
    checkpoint_suppression_payload,
    escalation_stage,
    evaluate_supervision,
    facts_history,
    guidance_payload,
    intended_actions,
    policy_from_payload,
    policy_payload,
    render_digest,
    stdout_fingerprints,
    trigger_payload,
)

# ADR-0012: the Recent band renders bounded receipt excerpts pre-resolved
# here. The receipt bound caps the per-decision artifact reads; excerpts are
# char-bounded before the assembler applies its per-item token cap.
RECEIPT_CONTENT_MAX_RECEIPTS = 8
RECEIPT_EXCERPT_MAX_CHARS = 400
_CONTENT_RECEIPT_STATUSES = frozenset(
    {ExecutionStatus.SUCCEEDED.value, ExecutionStatus.ACCEPTED.value}
)


def _bounded_stdout_excerpt(data: bytes) -> str:
    """Extract a bounded stdout excerpt from one receipt artifact payload.

    Envelope payloads contribute their ``stdout`` text when it is inline;
    non-JSON payloads contribute their decoded text; envelopes that store
    stdout elsewhere contribute nothing (the digest still collapses repeats).
    """
    try:
        parsed = parse_json(data.decode("utf-8"))
    except (UnicodeDecodeError, ContractError):
        parsed = None
    if isinstance(parsed, Mapping):
        stdout = parsed.get("stdout")
        if not isinstance(stdout, str):
            return ""
    else:
        stdout = data.decode("utf-8", errors="replace")
    collapsed = " ".join(stdout.split())
    return collapsed[:RECEIPT_EXCERPT_MAX_CHARS]


def _valid_eviction_identity(
    digest: object, size: object, budget: object, reason: object
) -> bool:
    """Validate the durable identity fields shared by eviction events."""
    valid_digest = (
        isinstance(digest, str)
        and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
    )
    valid_size = isinstance(size, int) and not isinstance(size, bool) and size >= 0
    valid_budget = (
        isinstance(budget, int) and not isinstance(budget, bool) and budget >= 0
    )
    return valid_digest and valid_size and valid_budget and reason == "byte_budget"


# One derived durable fact in a validated atomic batch.
_Transition = tuple[str, SessionState, Mapping[str, object], str | None]


@dataclass(frozen=True, slots=True)
class DecisionHandle:
    """Durable identity of one prepared model decision awaiting application.

    ``prepare_decision`` returns this handle after persisting ``model_intended``.
    The blocking model call happens outside the writer; ``apply_decision`` then
    gates the result on the still-current objective, attempt, and request.
    """

    request: DecisionInput
    reservation: ResourceReservation
    manifest: ContextManifest
    visible_input: str

    @property
    def request_id(self) -> str:
        """Return the durable model request identity."""
        return self.request.request_id

    @property
    def objective_version(self) -> int:
        """Return the objective version that caused this decision."""
        return self.request.objective_version

    @property
    def attempt_id(self) -> str:
        """Return the attempt identity that caused this decision."""
        return self.request.attempt_id

    @property
    def cursor_at_intent(self) -> int:
        """Return the journal cursor captured before the model call."""
        return self.request.journal_cursor


@dataclass(frozen=True, slots=True)
class TransportRecoveryWindow:
    """Journal-derived consecutive decision-path failure counts."""

    retryable_failures: int
    failed_calls: int
    #: ADR-0022: decisions in the window that ended ``output-exhausted``.
    exhausted_decisions: int = 0


@dataclass(frozen=True, slots=True)
class _SupervisorConsult:
    """Durable identity of one supervisor consult awaiting its advisory call."""

    request_id: str
    reservation: ResourceReservation
    visible_input: str
    digest: SupervisorDigest


class Session:
    """A synchronously driven, single-writer durable session application API."""

    def __init__(
        self,
        session_id: str,
        state: SessionState,
        cursor: int,
        *,
        journal: SqliteJournal,
        artifacts: ArtifactStore,
        environment: Environment,
        runtime: Runtime,
        evaluator: Evaluator,
        supervision: SupervisionCapability | None = None,
        evaluation_mode: EvaluationMode = EvaluationMode.EXTERNAL_CHECK,
    ) -> None:
        self._session_id = session_id
        self._state = state
        self._cursor = cursor
        self._journal = journal
        self._artifacts = artifacts
        self._environment = environment
        self._runtime = runtime
        self._evaluator = evaluator
        self._supervision = supervision
        self._evaluation_mode = evaluation_mode
        #: COD-89: decoded journal events, extended only with new events.
        self._event_cache: tuple[Event, ...] = ()
        self._inbox: list[OperatorCommand] = []
        self._receipts: dict[str, ExecutionReceipt] = {}
        self._commands: dict[str, OperatorCommand] = {}
        self._pending_dispatch: PendingDispatch | None = None
        self._action_names: dict[str, str] = {}
        if (
            supervision is not None
            and supervision.policy.burn_trigger
            and (state.task.budgets.model_calls or 0) < 1
        ):
            raise ContractError(
                "the claim-free burn trigger requires a main model-call budget"
            )

    @classmethod
    def create(
        cls,
        session_id: str,
        task: TaskSpec,
        *,
        journal: SqliteJournal,
        artifacts: ArtifactStore,
        environment: Environment,
        runtime: Runtime,
        evaluator: Evaluator,
        run_identity: RunIdentity | None = None,
        initial_observation: Observation | None = None,
        knowledge_sources: tuple[KnowledgeSource, ...] = (),
        evaluation_contract: EvaluationContract | None = None,
        supervision: SupervisionCapability | None = None,
        evaluation_mode: EvaluationMode = EvaluationMode.EXTERNAL_CHECK,
    ) -> "Session":
        """Persist a seeded root without starting autonomous work or external I/O."""
        contract = environment.describe()
        if contract.environment_id != task.environment_id:
            raise ContractError(
                "task environment_id does not match environment contract"
            )
        identity = run_identity or _default_run_identity(
            task, contract, runtime, evaluator
        )
        if (
            identity.runtime != runtime.identity()
            or identity.evaluator != evaluator.identity()
        ):
            raise ContractError("run identity must match connector-owned identities")
        observed_at = datetime.now(timezone.utc).isoformat()
        observation = initial_observation or Observation(
            f"initial-observation-{session_id}",
            contract.initial_state,
            observed_at,
            "environment contract",
            RecordEnvelope(
                f"initial-observation-{session_id}",
                session_id,
                task.objective_version,
                observed_at,
                "environment contract",
            ),
        )
        evaluation = evaluation_contract or EvaluationContract(
            task.evaluation_contract_id,
            identity.evaluator,
            RecordEnvelope(
                task.evaluation_contract_id,
                session_id,
                task.objective_version,
                observed_at,
                "task contract",
            ),
        )
        if evaluation.contract_id != task.evaluation_contract_id:
            raise ContractError("evaluation contract does not match task")
        _validate_creation_input_envelopes(
            session_id=session_id,
            objective_version=task.objective_version,
            observation=observation,
            knowledge_sources=knowledge_sources,
            evaluation_contract=evaluation,
        )
        for reference in (
            observation.raw_record,
            *(source.artifact for source in knowledge_sources),
        ):
            if reference is not None:
                artifacts.read(reference)
        if evaluation.evaluator != identity.evaluator:
            raise ContractError(
                "evaluation contract evaluator differs from run identity"
            )
        root = CommittedNode(
            node_id=f"root-{session_id}",
            objective_version=task.objective_version,
            state=contract.initial_state,
        )
        state = SessionState(
            task=task,
            status=SessionStatus.CREATED,
            lineage_head=root,
            initial_observation=observation,
            knowledge_sources=knowledge_sources,
            evaluation_contract=evaluation,
            accepted_lineage=(root,),
        )
        session = cls(
            session_id,
            state,
            0,
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            supervision=supervision,
            evaluation_mode=evaluation_mode,
        )
        with artifacts.synchronized():
            # Registration precedes the creation event and shares its lock;
            # retention cannot snapshot this store between the two facts.
            artifacts.register_journal(journal.path)
            session._persist(
                "session_created",
                state,
                {
                    "task": task,
                    "root_node": root,
                    "environment": contract,
                    "run_identity": identity,
                    "initial_observation": observation,
                    "knowledge_sources": knowledge_sources,
                    "evaluation_contract": evaluation,
                    "operational_measurement_scope": OPERATIONAL_MEASUREMENT_SCOPE,
                },
            )
            if supervision is not None:
                session._persist(
                    "supervision_enabled",
                    session._state,
                    {"policy": policy_payload(supervision.policy)},
                )
        return session

    @classmethod
    def open(
        cls,
        session_id: str,
        *,
        journal: SqliteJournal,
        artifacts: ArtifactStore,
        environment: Environment,
        runtime: Runtime,
        evaluator: Evaluator,
        run_identity: RunIdentity | None = None,
        supervision: SupervisionCapability | None = None,
        evaluation_mode: EvaluationMode = EvaluationMode.EXTERNAL_CHECK,
    ) -> "Session":
        """Rebuild validated state from journal facts without external submission.

        ``evaluation_mode`` is a typed run setting kept outside the journal; the
        application persists it in the run manifest and supplies it again here.
        """
        artifacts.register_journal(journal.path)
        events = _all_events(journal, session_id)
        cursor = events[-1].sequence if events else 0
        if not events:
            raise IntegrityError(f"session {session_id!r} has no creation event")
        state = replay(events)
        _verify_open_dependencies(
            events[0],
            task=state.task,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            run_identity=run_identity,
        )
        enabled = any(event.kind == "supervision_enabled" for event in events)
        if enabled != (supervision is not None):
            raise ContractError(
                "supervision capability must match the journal's "
                "supervision_enabled event"
            )
        if supervision is not None:
            bound = next(
                event for event in events if event.kind == "supervision_enabled"
            )
            policy = bound.payload.get("policy")
            if (
                not isinstance(policy, Mapping)
                or policy_from_payload(policy) != supervision.policy
            ):
                raise ContractError(
                    "supervision policy does not match the bound run policy"
                )
        session = cls(
            session_id,
            state,
            cursor,
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            supervision=supervision,
            evaluation_mode=evaluation_mode,
        )
        session._event_cache = events  # already decoded and replayed
        for event in events:
            receipt = event.payload.get("receipt")
            if event.kind == "receipt_received" and isinstance(receipt, Mapping):
                parsed_receipt = receipt_from_payload(receipt)
                session._receipts[parsed_receipt.receipt_id] = parsed_receipt
        session._action_names = intended_actions(events)
        session._commands = {
            item.command.command_id: item.command for item in state.command_operations
        }
        session._inbox = [
            item.command
            for item in state.command_operations
            if item.disposition is None
        ]
        session._reconcile_interrupted_model_calls()
        return session

    @property
    def session_id(self) -> str:
        """Return the stable session identity."""
        return self._session_id

    def stop_for_budget(self, exhausted: BudgetExhausted) -> None:
        """Record a terminal budget outcome independently of operator stops."""
        if self._state.status in (
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.STOPPED,
        ):
            return
        self._persist(
            "budget_exhausted",
            replace(self._state, status=SessionStatus.STOPPED),
            {"resource": exhausted.resource, "reason": str(exhausted)},
        )

    def view(self) -> SessionView:
        """Return a read-only view at the current durable cursor."""
        return SessionView(
            session_id=self._session_id,
            status=self._state.status,
            task=self._state.task,
            cursor=self._cursor,
            lineage_head=self._state.lineage_head,
            active_attempt=self._state.active_attempt,
            pending_proposal_id=self._state.pending_proposal_id,
            wait_reason=self._state.wait_reason,
        )

    def operator_view(self) -> OperatorView:
        """Project objective, stream, evidence, budgets, questions, and steering."""
        return build_operator_view(
            self._session_id,
            self._state,
            self._journal_events(),
        )

    def events(self, after_sequence: int = 0, limit: int = 100) -> tuple[Event, ...]:
        """Return a bounded durable observation stream without causing new work."""
        return self._journal.events(
            self._session_id, after_sequence=after_sequence, limit=limit
        )

    def record_runner_exit(self, reason: str) -> Event:
        """Journal one runner_exited observation as the bounded runner gives up.

        A runner that stops without the session reaching a terminal status
        leaves the durable projection mid-flight by design; this identity event
        is the terminal fact the operator surface reads, not a state
        transition, so resume and replay contracts are untouched.
        """
        if not reason.strip():
            raise ContractError("runner exit requires a reason")
        event = Event(
            event_id=_new_id("event"),
            session_id=self._session_id,
            sequence=self._cursor + 1,
            kind="runner_exited",
            objective_version=self._state.task.objective_version,
            payload={"reason": reason},
            recorded_at=datetime.now(timezone.utc).isoformat(),
            causation_id=None,
        )
        derived = reduce_event(self._state, event)
        if derived != self._state:
            raise IntegrityError(
                "runner_exited must not change the durable session projection"
            )
        self._cursor = self._journal.append_batch([event], expected_cursor=self._cursor)
        return event

    def attempts(self) -> tuple[AttemptHistory, ...]:
        """Return every durable attempt, including rejected and abandoned work."""
        return self._state.attempt_history

    def accepted_lineage(self) -> tuple[CommittedNode, ...]:
        """Return only externally accepted lineage nodes in commit order."""
        return self._state.accepted_lineage

    def enforce_retention(self, byte_budget: int) -> AlternativeRetention:
        """Evict unprotected local bytes under the store-wide lock."""
        with self._artifacts.synchronized():
            return self._enforce_retention(byte_budget)

    def _enforce_retention(self, byte_budget: int) -> AlternativeRetention:
        """Evict unprotected local bytes; journal intent then availability."""
        if byte_budget < 0:
            raise ContractError("byte_budget must not be negative")
        events = self._journal_events()
        self._reconcile_retention(events)
        events = self._journal_events()
        inventory = self._artifacts.inventory()
        protected = self._workspace_protected_digests()
        planned = select_evictions(inventory, protected, byte_budget)
        sizes = dict(inventory)
        evicted: list[str] = []
        unavailable: list[str] = []
        for digest in planned:
            size = sizes.get(digest, 0)
            self._record_eviction_intent(digest, size, byte_budget)
            dropped = self._artifacts.drop(digest)
            self._record_eviction_outcome(digest, size, byte_budget, dropped)
            evicted.append(digest)
            if not dropped:
                unavailable.append(digest)
        remaining = sum(size for _digest, size in self._artifacts.inventory())
        return AlternativeRetention(
            byte_budget=byte_budget,
            bytes_used=remaining,
            protected=tuple(sorted(protected)),
            evicted=tuple(evicted),
            unavailable=tuple(unavailable),
        )

    def counters(self) -> RunCounters:
        """Return BENCH-0001 diagnostic counters from the durable journal."""
        return counters(self._journal_events())

    def manifest(self) -> RunManifest:
        """Return immutable run identity captured at durable session creation."""
        return manifest(self._journal_events())

    def enqueue(self, command: OperatorCommand) -> str:
        """Admit a command for the next bounded coordinator advancement."""
        if command.session_id != self._session_id:
            raise ContractError("command belongs to another session")
        existing = self._commands.get(command.command_id)
        if existing is not None:
            if existing != command:
                raise ContractError("command ID was reused with different content")
            return command.command_id
        admitted = admit_command(self._state, command)
        self._persist("command_admitted", admitted, {"command": command})
        self._commands[command.command_id] = command
        self._inbox.append(command)
        return command.command_id

    def advance(self) -> SessionView:
        """Apply queued operator commands; it never sleeps or dispatches new work."""
        controls = (Pause, Steer, Stop)
        commands = sorted(
            self._inbox,
            key=lambda command: 0 if isinstance(command, controls) else 1,
        )
        for command in commands:
            if isinstance(command, EvaluationDecision):
                if not self._apply_evaluation_decision(command):
                    self._inbox.remove(command)
                continue
            if isinstance(command, EvaluationCommand):
                try:
                    request, reservation, pending = self._prepare_evaluation(
                        self._state, command.state
                    )
                except (ContractError, TransitionError) as exc:
                    self._persist(
                        "command_rejected",
                        settle_command(self._state, command.command_id, "rejected"),
                        {"command_id": command.command_id, "reason": str(exc)},
                        causation_id=command.command_id,
                    )
                else:
                    self._persist_batch(
                        (
                            (
                                "evaluation_intended",
                                record_command_transition(
                                    pending,
                                    command.command_id,
                                    "evaluation_intended",
                                    evaluated_state=request.evaluated_state,
                                ),
                                self._evaluation_intent_fields(request, reservation),
                                command.command_id,
                            ),
                            (
                                "command_applied",
                                settle_command(
                                    record_command_transition(
                                        pending,
                                        command.command_id,
                                        "evaluation_intended",
                                        evaluated_state=request.evaluated_state,
                                    ),
                                    command.command_id,
                                    "applied",
                                ),
                                {"command_id": command.command_id},
                                command.command_id,
                            ),
                        )
                    )
                    self._inbox.remove(command)
                    self._dispatch_evaluation_if_external(request)
                    continue
                self._inbox.remove(command)
                continue
            try:
                batch = self._command_event_batch(command)
            except (ContractError, TransitionError) as exc:
                self._persist(
                    "command_rejected",
                    settle_command(self._state, command.command_id, "rejected"),
                    {"command_id": command.command_id, "reason": str(exc)},
                    causation_id=command.command_id,
                )
            else:
                self._persist_batch(batch)
            self._inbox.remove(command)
        self._invalidate_stale_deferred_dispatch()
        return self.view()

    def prepare_decision(
        self, model: Model, *, token_budget: int = 16384, dispatch: bool = True
    ) -> DecisionHandle | None:
        """Persist one model intent and return the handle that applies its result.

        Returns ``None`` when the session cannot schedule a model call now. The
        blocking model call itself runs outside this writer. With
        ``dispatch=False`` a triggered supervisor consult is left to the caller:
        its descriptor is retained and must be taken with
        ``take_pending_dispatch`` and applied through ``receive_guidance``.
        """
        self.advance()
        if self._state.status in {
            SessionStatus.PAUSED,
            SessionStatus.WAITING,
            SessionStatus.STOPPED,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
        }:
            return None
        self._require_schedulable()
        if self._state.pending_proposal_id is not None:
            return None
        if self._pending_supervisor_operation() is not None:
            return None
        if any(
            not operation.settled
            and not operation.request_id.startswith(SUPERVISOR_REQUEST_PREFIX)
            for operation in self._state.model_operations
        ):
            return None
        if self._state.active_attempt is None:
            self.begin_attempt()
        events = self._journal_events()
        remaining = remaining_budgets(self._state, events)
        (
            model_remaining,
            model_call_bound,
            allow_supervisor_consult,
        ) = _step_budget_preflight(remaining, model, self._supervision is not None)
        configuration = self._bind_model_configuration(model, token_budget)
        reserved_output_tokens = reserved_output_tokens_for(model)
        if self._supervision is not None:
            if self._apply_supervision_boundary(
                events,
                dispatch=dispatch,
                allow_supervisor_consult=allow_supervisor_consult,
            ):
                return None
            events = self._journal_events()
            remaining = remaining_budgets(self._state, events)
            model_remaining = _remaining_model_calls(remaining)
        if not _model_call_budget_covers(model_remaining, model_call_bound):
            raise BudgetExhausted("model call")
        working = working_memory(self._state, events)
        attempt = self._require_active_attempt()
        model_id = configuration.model_id
        request_id = _new_id("decision")
        remaining = remaining_budgets(self._state, events)
        if self._state.task.environment_id.startswith("arc-agi-3:"):
            remaining = self._arc_decision_budgets(remaining, events)
        observation = observation_payload(
            self._state,
            events,
            artifact_content=self._receipt_artifact_content(events),
        )
        latest_receipt = latest_receipt_payload(events)
        receipt_content = self._receipt_content_map(events)
        query = f"{working.task.objective} {working.current_plan}"
        if working.operator_direction:
            query = f"{query} {working.operator_direction}"
        hits = self._journal.search(
            self._session_id,
            query,
            filters=SearchFilter(kinds=RETRIEVAL_EVENT_KINDS),
        )

        def make_request(context: ContextBundle) -> DecisionInput:
            return DecisionInput(
                task=self._state.task,
                context=context,
                actions=self._environment.describe().actions,
                lineage_head=self._state.lineage_head,
                attempt_id=attempt.attempt_id,
                journal_cursor=self._cursor + 1,
                objective_version=self._state.task.objective_version,
                request_id=request_id,
                remaining_budgets=remaining,
                observation=observation,
                latest_receipt=latest_receipt,
                result_schemas=RESULT_SCHEMAS,
                model_id=model_id,
                policy_version=POLICY_VERSION,
                token_counter=TOKEN_COUNTER,
            )

        tokens_per_char = _observed_tokens_per_char(events)

        def request_token_counter(
            items: tuple[ContextItem, ...],
            text: str,
            context_manifest: ContextManifest,
        ) -> TokenAccounting:
            bundle = ContextBundle(items, context_manifest, text)
            serialized = render_request(make_request(bundle))
            input_tokens = count_tokens(serialized)
            system_tokens = count_tokens(SYSTEM_PROMPT)
            context_tokens = count_tokens(text)
            return TokenAccounting(
                input_tokens=input_tokens,
                system_prompt_tokens=system_tokens,
                reserved_output_tokens=reserved_output_tokens,
                serialization_overhead_tokens=max(0, input_tokens - context_tokens),
                estimated_input_tokens=estimate_serialized_tokens(
                    serialized, tokens_per_char
                ),
                estimated_system_prompt_tokens=estimate_serialized_tokens(
                    SYSTEM_PROMPT, tokens_per_char
                ),
                # ADR-0020: fit in real size once a provider has reported usage.
                fit_basis=(
                    TOKEN_COUNTER if tokens_per_char is None else TOKEN_ESTIMATOR
                ),
                estimate_rate=estimate_rate(tokens_per_char),
            )

        context_started_ns = perf_counter_ns()
        assembled = assemble_context(
            self._state,
            events,
            self._environment.describe(),
            token_budget=token_budget,
            hits=hits,
            reserved_output_tokens=reserved_output_tokens,
            receipt_content=receipt_content,
            request_token_counter=request_token_counter,
        )
        context_duration_ns = perf_counter_ns() - context_started_ns
        if isinstance(assembled, ContextOverflow):
            self._persist(
                "context_overflow",
                self._state,
                {"overflow": assembled, "duration_ns": context_duration_ns},
            )
            raise TransitionError("mandatory context exceeds token budget")
        # COD-73: record the calibration the estimate used, so it is reproducible.
        assembled = ContextBundle(
            assembled.items,
            replace(assembled.manifest, estimate_tokens_per_char=tokens_per_char),
            assembled.text,
        )
        request, visible_input, context_manifest = self._finalize_request(
            make_request,
            assembled,
            token_budget=token_budget,
            reserved_output_tokens=reserved_output_tokens,
            events=events,
            duration_ns=context_duration_ns,
        )
        reservation = ResourceReservation(
            reservation_id=_new_id("reservation"),
            operation_id=request.request_id,
            objective_version=self._state.task.objective_version,
            model_calls=_model_call_bound(model),
            tokens=self._decision_token_bound(
                model, context_manifest, tokens_per_char, reserved_output_tokens
            ),
        )
        intended = intend_model_call(
            self._state,
            reservation,
            request_id=request.request_id,
            input_text=visible_input,
            manifest=to_json_value(context_manifest),
            policy_version=request.policy_version,
            token_counter=request.token_counter,
            model_id=request.model_id,
            cursor=self._cursor,
        )
        self._persist(
            "model_intended",
            intended,
            self._model_record(
                request,
                reservation,
                context_manifest,
                visible_input,
            ),
        )
        return DecisionHandle(request, reservation, context_manifest, visible_input)

    def _arc_decision_budgets(
        self, remaining: Mapping[str, JsonValue], events: tuple[Event, ...]
    ) -> Mapping[str, JsonValue]:
        """Show whole-run limits at the start of an ARC puzzle decision."""
        limits = self._state.task.budgets
        result = dict(remaining)
        warnings: list[str] = []
        if limits.tokens is not None:
            used = self._state.ledger.conservative_tokens
            result["tokens"] = max(0, limits.tokens - used)
            if 5 * used >= 4 * limits.tokens:
                warnings.append("At least 80% of the run's token budget is used.")
        if limits.wall_clock_seconds is not None:
            created = datetime.fromisoformat(events[0].recorded_at)
            elapsed = (datetime.now(timezone.utc) - created).total_seconds()
            elapsed = max(0.0, elapsed)
            result["wall_clock_seconds"] = round(
                max(0.0, limits.wall_clock_seconds - elapsed), 3
            )
            if elapsed >= 0.8 * limits.wall_clock_seconds:
                warnings.append("At least 80% of the run's time budget is used.")
        if warnings:
            result["warnings"] = tuple(warnings)
        return result

    def _decision_token_bound(
        self,
        model: Model,
        context_manifest: ContextManifest,
        tokens_per_char: float | None,
        reserved_output_tokens: int,
    ) -> int | None:
        """Return a decision's worst-case billed tokens under a token limit.

        COD-86, ADR-0024: every call the decision can make, each billed as the
        calibrated prompt estimate plus the largest answer it may ask for.
        ``None`` without a token limit, so other journals are unchanged.
        """
        if self._state.task.budgets.tokens is None:
            return None
        calls = getattr(model, "max_calls_per_decision", None)
        if not isinstance(calls, int) or isinstance(calls, bool) or calls < 1:
            calls = _model_call_bound(model)
        answer = getattr(model, "max_output_tokens", None)
        if not isinstance(answer, int) or isinstance(answer, bool):
            answer = reserved_output_tokens
        prompt = (
            context_manifest.estimated_input_tokens or 0
        ) + estimate_serialized_tokens(SYSTEM_PROMPT, tokens_per_char)
        return calls * (prompt + max(answer, reserved_output_tokens))

    def _supervisor_token_bound(
        self, visible: str, events: tuple[Event, ...]
    ) -> int | None:
        """Return a supervisor consult's worst-case billed tokens, if limited."""
        if self._state.task.budgets.tokens is None:
            return None
        tokens_per_char = _observed_tokens_per_char(events)
        capability = self._supervision
        answer = (
            getattr(capability.supervisor, "max_output_tokens", None)
            if capability is not None
            else None
        )
        if not isinstance(answer, int) or isinstance(answer, bool):
            answer = RESERVED_OUTPUT_TOKENS
        return (
            estimate_serialized_tokens(visible, tokens_per_char)
            + estimate_serialized_tokens(SUPERVISOR_SYSTEM_PROMPT, tokens_per_char)
            + answer
        )

    def _finalize_request(
        self,
        make_request: Callable[[ContextBundle], DecisionInput],
        assembled: ContextBundle,
        *,
        token_budget: int,
        reserved_output_tokens: int,
        events: tuple[Event, ...],
        duration_ns: int,
    ) -> tuple[DecisionInput, str, ContextManifest]:
        """Settle the visible request and refuse a calibrated over-budget call."""
        request, visible_input, context_manifest = _stable_request(
            make_request, assembled
        )
        projected = _projected_input_tokens(events, visible_input)
        if projected is not None and projected + reserved_output_tokens > token_budget:
            # ADR-0016: once a provider has reported usage for this run, the
            # observed tokens-per-character ratio is authoritative enough to
            # refuse a projected over-budget call instead of sending it.
            overflow = ContextOverflow(
                missing_item_ids=(),
                token_budget=token_budget,
                tokens_required=projected + reserved_output_tokens,
            )
            self._persist(
                "context_overflow",
                self._state,
                {"overflow": overflow, "duration_ns": duration_ns},
            )
            raise TransitionError("projected context exceeds token budget")
        self._persist(
            "context_assembled",
            self._state,
            {"manifest": context_manifest, "duration_ns": duration_ns},
        )
        return request, visible_input, context_manifest

    def step(self, model: Model, *, token_budget: int = 16384) -> SessionView:
        """Take one model-chosen result through existing intent and receipt paths.

        Pause, stop, and waiting suppress new model calls and proposals. The
        coordinator does not impose inspect/act/evaluate order.
        """
        handle = self.prepare_decision(model, token_budget=token_budget)
        if handle is None:
            return self.view()
        try:
            response = decide(handle.request, model)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._record_model_failure(
                handle.request,
                handle.reservation,
                handle.manifest,
                handle.visible_input,
                exc,
                cursor=handle.cursor_at_intent + 1,
            )
        self.apply_decision(handle, response)
        self.advance()
        return self.view()

    def apply_decision(
        self,
        handle: DecisionHandle,
        response: ModelResponse,
        *,
        dispatch: bool = True,
    ) -> SessionView:
        """Record one model terminal and its durable application atomically.

        A duplicate request is a no-op when its content matches and an error
        when it conflicts. A result that no longer applies to the current
        objective, attempt, or request is recorded with the decision and
        suppressed, never dispatched. With ``dispatch=False`` the blocking port
        call is left to the caller: the descriptor is retained and must be taken
        with ``take_pending_dispatch`` and executed off the writer thread.
        """
        duplicate = self._duplicate_model_terminal(handle, response)
        if duplicate is not None:
            if duplicate:
                return self.view()
            raise ContractError(
                "model request was already applied with different content"
            )
        reason = self._decision_suppression(handle)
        decided = settle_model_call(self._state, handle.reservation, response.usage)
        breach = _token_breach_payload(handle, response)

        def _accounting_batch(state):
            if breach is None:
                return ()
            return (("token_budget_breach", state, breach, handle.request_id),)

        try:
            response_identity = _model_response_identity(response)
        except ContractError as exc:
            record = self._model_record(
                handle.request,
                handle.reservation,
                handle.manifest,
                handle.visible_input,
                cursor=handle.cursor_at_intent + 1,
                result_kind=type(response.result).__name__,
                output=response.output,
                finish_reason=response.finish_reason,
                model_id=response.model_id,
                attempts=response.attempts,
                usage=response.usage,
                duration_ms=response.duration_ms,
            )
            self._record_rejected_application(handle, decided, record, exc)
            raise
        record = self._model_record(
            handle.request,
            handle.reservation,
            handle.manifest,
            handle.visible_input,
            cursor=handle.cursor_at_intent + 1,
            result_kind=type(response.result).__name__,
            output=response.output,
            finish_reason=response.finish_reason,
            model_id=response.model_id,
            attempts=response.attempts,
            usage=response.usage,
            duration_ms=response.duration_ms,
            response_identity=response_identity,
        )

        def _repair_batch(state):
            return tuple(
                (
                    "model_repaired",
                    state,
                    {"request_id": handle.request_id, **dict(note)},
                    handle.request_id,
                )
                for note in response.repairs
            )

        if reason is not None:
            self._persist_batch(
                (
                    ("model_decided", decided, record, None),
                    (
                        "model_result_suppressed",
                        decided,
                        {"request_id": handle.request_id, "reason": reason},
                        handle.request_id,
                    ),
                    *_repair_batch(decided),
                    *_accounting_batch(decided),
                )
            )
            return self.view()
        try:
            transitions, pending = self._application_batch(
                decided, response.result, self._cursor + 2
            )
        except (ContractError, TransitionError) as exc:
            self._record_rejected_application(handle, decided, record, exc)
            raise
        final_state = transitions[-1][1] if transitions else decided
        self._persist_batch(
            (
                ("model_decided", decided, record, None),
                *transitions,
                *_repair_batch(final_state),
                *_accounting_batch(final_state),
            )
        )
        if pending is not None:
            if pending.kind == "action":
                # The deferred dispatch never passes through _dispatch_action,
                # so scope the receipt-time evidence fingerprints here, at
                # intent commit, exactly as the resume rebuild would.
                proposal = pending.proposal
                if proposal is None:  # pragma: no cover - PendingDispatch invariant
                    raise IntegrityError("pending action dispatch lacks its proposal")
                self._action_names[proposal.proposal_id] = proposal.action_name
            if dispatch:
                self._run_pending_dispatch(pending)
            else:
                if self._pending_dispatch is not None:
                    raise ContractError("an external dispatch is already pending")
                self._pending_dispatch = pending
        return self.view()

    def fail_decision(self, handle: DecisionHandle, error: Exception) -> NoReturn:
        """Record one failed model call for a prepared decision and propagate it.

        Used by an application that runs the blocking model call off the writer
        thread: the failure is settled through the same durable path as
        ``step`` before the error reaches the caller.
        """
        self._record_model_failure(
            handle.request,
            handle.reservation,
            handle.manifest,
            handle.visible_input,
            error,
            cursor=handle.cursor_at_intent + 1,
        )

    def settle_decision_failure(
        self, handle: DecisionHandle, error: Exception
    ) -> Mapping[str, object]:
        """Record one failed model call for a prepared decision without raising.

        The recovery policy decides what happens after settlement, so the
        coordinator needs the durable terminal payload instead of the raised
        error. The payload carries the typed ``failure_class`` and unknown
        measured usage exactly as ``model_malformed`` journals it.
        """
        return self._record_model_failure_payload(
            handle.request,
            handle.reservation,
            handle.manifest,
            handle.visible_input,
            error,
            cursor=handle.cursor_at_intent + 1,
        )

    def transport_recovery_window(self) -> TransportRecoveryWindow:
        """Return journal-derived consecutive decision-path failure counts.

        ``retryable_failures`` is the leading tail-run of retryable transport
        failures and bounds the automatic retry budget; ``failed_calls`` counts
        every consecutive decision-path failure since the last successful
        terminal. Both stop at a successful ``model_decided`` or a terminal
        from a superseded objective (steering starts new work with a fresh
        recovery budget), and the retryable run also stops at the first
        non-retryable failure. Supervisor terminals are separate work and never
        extend or reset the decision window. Being journal-derived, the counts
        survive a restart.
        """
        window = TransportRecoveryWindow(retryable_failures=0, failed_calls=0)
        objective_version = self._state.task.objective_version
        leading = True
        for event in reversed(self._journal_events()):
            if event.kind not in {"model_malformed", "model_decided"}:
                continue
            if event.objective_version != objective_version:
                return window
            request_id = event.payload.get("request_id")
            if isinstance(request_id, str) and request_id.startswith(
                SUPERVISOR_REQUEST_PREFIX
            ):
                continue
            if event.kind == "model_malformed":
                window = replace(window, failed_calls=window.failed_calls + 1)
                failure_class = event.payload.get("failure_class")
                if failure_class == ModelFailureClass.OUTPUT_EXHAUSTED.value:
                    window = replace(
                        window, exhausted_decisions=window.exhausted_decisions + 1
                    )
                if (
                    leading
                    and isinstance(failure_class, str)
                    and failure_class in RETRYABLE_FAILURE_CLASSES
                ):
                    window = replace(
                        window,
                        retryable_failures=window.retryable_failures + 1,
                    )
                else:
                    leading = False
                continue
            return window
        return window

    def record_provider_recovery_waiting(
        self,
        *,
        request_id: str,
        failure_class: str,
        consumed_calls: int,
        operator_action: bool,
        detail: str,
    ) -> SessionView:
        """Journal the provider-recovery wait and hold scheduling without calls.

        The event sets the durable ``WAITING`` projection under the
        provider-recovery reason; an operator Resume, steer, or stop ends it
        through the existing command transitions.
        """
        state = provider_recovery_waiting(
            self._state,
            request_id=request_id,
            failure_class=failure_class,
            consumed_calls=consumed_calls,
            operator_action=operator_action,
            detail=detail,
        )
        self._persist(
            "provider_recovery_waiting",
            state,
            {
                "request_id": request_id,
                "failure_class": failure_class,
                "consumed_calls": consumed_calls,
                "operator_action": operator_action,
                "detail": detail,
            },
            causation_id=request_id,
        )
        return self.view()

    def take_pending_dispatch(self) -> PendingDispatch | None:
        """Return and clear one deferred external call for a bounded worker."""
        pending, self._pending_dispatch = self._pending_dispatch, None
        return pending

    def _run_pending_dispatch(self, pending: PendingDispatch) -> None:
        """Execute one port call on the coordinator's own thread."""
        if pending.kind == "action":
            proposal = pending.proposal
            if proposal is None:  # pragma: no cover - model invariant
                raise IntegrityError("pending action dispatch lacks its proposal")
            self._dispatch_action(proposal)
            return
        request = pending.request
        if request is None:  # pragma: no cover - model invariant
            raise IntegrityError("pending evaluation dispatch lacks its request")
        self._dispatch_evaluation(request)

    def receive_evaluation(
        self, request: EvaluationRequest, receipt: EvaluationReceipt
    ) -> EvaluationReceipt:
        """Apply one externally produced receipt for its already-intended request."""
        return self._receive_evaluation_receipt(request, receipt)

    def receive_guidance(
        self, response: SupervisorResponse | ModelCallError | None
    ) -> SessionView:
        """Apply one externally produced supervisor result to its intended consult.

        The result and its usage are recorded exactly once, whatever events were
        recorded after the consult began (for example an admitted pause or a
        steer). A valid response records ``guidance_received``; ``None`` records
        ``supervisor_failed``. The guidance is delivered only while it still
        applies to the current objective and attempt; otherwise it is recorded
        and immediately suppressed, so control admission never discards a
        completed call's accounting and a stale result never steers new work.
        """
        if response is not None and not isinstance(
            response, (SupervisorResponse, ModelCallError)
        ):
            raise ContractError(
                "supervisor result must be a response, typed failure, or None"
            )
        operation = self._pending_supervisor_operation()
        if operation is None:
            raise ContractError("no supervisor consult is awaiting guidance")
        if operation.cursor is None:  # pragma: no cover - intent records a cursor
            raise IntegrityError("supervisor consult lacks its intent cursor")
        request_id = operation.request_id
        reservation = operation.reservation
        visible_input = operation.input_text or ""
        # The terminal records the call's own position, not the current cursor,
        # so a control recorded after the intent does not corrupt replay identity.
        cursor = operation.cursor + 1
        if response is None or isinstance(response, ModelCallError):
            self._record_supervisor_failure(
                request_id,
                reservation,
                visible_input,
                cursor,
                usage=response.usage if isinstance(response, ModelCallError) else None,
                error=(
                    safe_model_error(response)
                    if isinstance(response, ModelCallError)
                    else "supervisor call failed"
                ),
            )
            return self.view()
        self._record_supervisor_guidance(
            request_id, reservation, visible_input, response, cursor
        )
        if not self._guidance_applies(operation):
            self._suppress_guidance(response.guidance)
        return self.view()

    def _guidance_applies(self, operation: ModelCallOperation) -> bool:
        """Return whether a completed consult still applies to current work.

        A stopped session is terminal, a changed objective version supersedes
        the consult's diagnosis, and an abandoned attempt has no work to steer.
        """
        if self._state.status is SessionStatus.STOPPED:
            return False
        if (
            operation.reservation.objective_version
            != self._state.task.objective_version
        ):
            return False
        return self._state.active_attempt is not None

    def _suppress_guidance(self, guidance: Guidance) -> None:
        """Close guidance that no longer applies before it is delivered."""
        self._persist(
            "guidance_suppressed",
            self._state,
            {
                "expires_after_call": guidance.expires_after_call,
                "reason": "guidance superseded before it was delivered",
            },
        )

    def _record_rejected_application(
        self,
        handle: DecisionHandle,
        decided: SessionState,
        record: Mapping[str, object],
        error: ContractError | TransitionError,
    ) -> None:
        """Settle a known response and retain its rejected application atomically."""
        self._persist_batch(
            (
                ("model_decided", decided, record, None),
                (
                    "model_application_rejected",
                    decided,
                    {
                        "request_id": handle.request_id,
                        "reason": _application_rejection_reason(error),
                    },
                    handle.request_id,
                ),
            )
        )

    def _duplicate_model_terminal(
        self, handle: DecisionHandle, response: ModelResponse
    ) -> bool | None:
        """Return None when unapplied, True when equivalent, False when conflicting."""
        try:
            operation = model_operation(self._state, handle.request_id)
        except TransitionError:
            return None
        if not operation.settled:
            return None
        recorded = self._model_terminal_record(handle.request_id)
        if recorded is None:
            return False
        response_identity = recorded.get("response_identity")
        if not isinstance(response_identity, str):
            # Old terminal records did not retain enough content to prove that
            # a repeated response is semantically identical.
            return False
        return response_identity == _model_response_identity(response)

    def _model_terminal_record(self, request_id: str) -> Mapping[str, object] | None:
        """Return the durable terminal payload for one model request, if any."""
        for event in self._journal_events():
            if event.kind in {"model_decided", "model_malformed"} and (
                event.payload.get("request_id") == request_id
            ):
                return event.payload
        return None

    def _decision_suppression(self, handle: DecisionHandle) -> str | None:
        """Return the reason this decision must be suppressed, or None."""
        status = self._state.status
        if status not in {SessionStatus.CREATED, SessionStatus.RUNNING}:
            return f"session {status.value}"
        if handle.objective_version != self._state.task.objective_version:
            return "objective changed"
        active = self._state.active_attempt
        if active is None or active.attempt_id != handle.attempt_id:
            return "attempt changed"
        if self._cursor != handle.cursor_at_intent + 1:
            return "trajectory changed"
        if self._state.pending_proposal_id is not None:
            return "operation pending"
        if self._state.blocking_question_id is not None:
            return "operator question pending"
        return self._pending_control_reason()

    def _pending_control_reason(self) -> str | None:
        """Report a control admitted during the blocking call that now applies."""
        for command in self._inbox:
            if isinstance(command, Pause):
                return "pause pending"
            if isinstance(command, Stop):
                return "stop pending"
            if isinstance(command, Steer):
                return "steer pending"
        if self._inbox:
            return "operator command pending"
        return None

    def _record_model_failure(
        self,
        request: DecisionInput,
        reservation: ResourceReservation,
        context_manifest: ContextManifest,
        visible_input: str,
        error: Exception,
        *,
        cursor: int | None = None,
    ) -> NoReturn:
        """Settle and persist failed model work before propagating its error."""
        self._record_model_failure_payload(
            request,
            reservation,
            context_manifest,
            visible_input,
            error,
            cursor=cursor,
        )
        usage = error.usage if isinstance(error, ModelCallError) else None
        attempts = error.attempts if isinstance(error, ModelCallError) else ()
        output = error.output if isinstance(error, ModelCallError) else ""
        duration_ms = error.duration_ms if isinstance(error, ModelCallError) else None
        if isinstance(error, ModelCallError):
            raise error
        raise ModelCallError(
            safe_model_error(error),
            usage=usage,
            attempts=attempts,
            output=output,
            duration_ms=duration_ms,
        ) from error

    def _record_model_failure_payload(
        self,
        request: DecisionInput,
        reservation: ResourceReservation,
        context_manifest: ContextManifest,
        visible_input: str,
        error: Exception,
        *,
        cursor: int | None = None,
    ) -> Mapping[str, object]:
        """Settle and persist failed model work, returning its terminal payload."""
        usage = error.usage if isinstance(error, ModelCallError) else None
        attempts = error.attempts if isinstance(error, ModelCallError) else ()
        output = error.output if isinstance(error, ModelCallError) else ""
        duration_ms = error.duration_ms if isinstance(error, ModelCallError) else None
        failure_class = (
            error.failure_class if isinstance(error, ModelCallError) else None
        )
        payload = self._model_record(
            request,
            reservation,
            context_manifest,
            visible_input,
            cursor=cursor,
            error=safe_model_error(error),
            output=output,
            attempts=attempts,
            usage=usage,
            duration_ms=duration_ms,
            failure_class=failure_class,
        )
        repairs = error.repairs if isinstance(error, ModelCallError) else ()
        settled = settle_model_call(self._state, reservation, usage)
        batch: list[tuple[str, SessionState, Mapping[str, object], str | None]] = [
            ("model_malformed", settled, payload, None)
        ]
        for note in repairs:
            batch.append(
                (
                    "model_repaired",
                    settled,
                    {"request_id": request.request_id, **dict(note)},
                    request.request_id,
                )
            )
        self._persist_batch(tuple(batch))
        return payload

    def _application_batch(
        self, state: SessionState, result: AgentResult, start_sequence: int
    ) -> tuple[tuple[_Transition, ...], PendingDispatch | None]:
        """Derive the durable application transitions for one model result.

        Transitions are relative to ``state`` (the recorded model terminal) and
        are committed atomically with it. Any external port call is returned as
        a typed descriptor: the caller either runs it after the batch commits or
        defers it to a bounded worker, so a crash never records a decision
        without its intent.
        """
        if isinstance(result, ActionChoice):
            proposal, intended = self._prepare_action(
                state,
                result.action_name,
                result.arguments,
                rationale=result.rationale,
            )
            return (
                (
                    (
                        "action_intended",
                        intended,
                        {"proposal": proposal, "reservation": proposal.reservation},
                        None,
                    ),
                ),
                PendingDispatch(kind="action", proposal=proposal),
            )
        if isinstance(result, EvaluationChoice):
            request, reservation, pending = self._prepare_evaluation(state)
            dispatch = (
                None
                if self._evaluation_mode is EvaluationMode.OPERATOR
                else PendingDispatch(kind="evaluation", request=request)
            )
            return (
                (
                    (
                        "evaluation_intended",
                        pending,
                        self._evaluation_intent_fields(request, reservation),
                        None,
                    ),
                ),
                dispatch,
            )
        if isinstance(result, LineageQuery):
            nodes = self.accepted_lineage()
            if result.node_id is not None:
                nodes = tuple(node for node in nodes if node.node_id == result.node_id)
            return (
                (
                    (
                        "lineage_inspected",
                        state,
                        {
                            "node_ids": tuple(node.node_id for node in nodes),
                            "nodes": nodes,
                        },
                        None,
                    ),
                ),
                None,
            )
        if isinstance(result, AttemptQuery):
            events = self._journal_events()
            history = next(
                (
                    item
                    for item in state.attempt_history
                    if item.attempt.attempt_id == result.attempt_id
                ),
                None,
            )
            evidence = (
                missing_attempt_evidence(result.attempt_id)
                if history is None or history.event_end_cursor is None
                else attempt_evidence(history, events)
            )
            return ((("attempt_inspected", state, {"attempt": evidence}, None),), None)
        if isinstance(result, KnowledgeQuery):
            events = self._journal_events()
            items = knowledge_items(state, events)
            if result.item_id is not None:
                items = tuple(item for item in items if item.item_id == result.item_id)
            return (
                (
                    (
                        "knowledge_inspected",
                        state,
                        {
                            "item_ids": tuple(item.item_id for item in items),
                            "items": items,
                        },
                        None,
                    ),
                ),
                None,
            )
        if isinstance(result, MemoryUpdate):
            return (
                (self._memory_update_transition(state, result, start_sequence),),
                None,
            )
        if isinstance(result, GuidanceResponse):
            return self._guidance_transitions(state, result, start_sequence)
        if isinstance(result, OperatorQuestion):
            if state.blocking_question_id is not None:
                return (
                    (
                        (
                            "question_rejected",
                            state,
                            {"reason": "blocking question open"},
                            None,
                        ),
                    ),
                    None,
                )
            question_id = f"question-{start_sequence}"
            return (
                (
                    (
                        "question_recorded",
                        replace(
                            state,
                            status=SessionStatus.WAITING,
                            wait_reason=OPERATOR_ANSWER_WAIT,
                            blocking_question_id=question_id,
                        ),
                        {
                            "question_id": question_id,
                            "text": result.text,
                            "objective_version": state.task.objective_version,
                            "status": "open",
                        },
                        None,
                    ),
                ),
                None,
            )
        if isinstance(result, PlanUpdate):
            return (
                (
                    (
                        "plan_recorded",
                        state,
                        {"text": result.text, "source_event": start_sequence},
                        None,
                    ),
                ),
                None,
            )
        if isinstance(result, WaitChoice):
            if result.reason == OPERATOR_ANSWER_WAIT:
                return (
                    (("wait_rejected", state, {"reason": result.reason}, None),),
                    None,
                )
            return (
                (
                    (
                        "agent_waited",
                        replace(
                            state,
                            status=SessionStatus.WAITING,
                            wait_reason=result.reason,
                        ),
                        {"reason": result.reason},
                        None,
                    ),
                ),
                None,
            )
        if isinstance(result, InvestigationChange):
            return (
                (
                    (
                        "investigation_recorded",
                        state,
                        {
                            "investigation": {
                                "investigation_id": result.investigation_id,
                                "question": result.question,
                                "disposition": result.disposition.value,
                                "hypotheses": result.hypotheses,
                                "attempt_ids": result.attempt_ids,
                                "evidence_ids": result.evidence_ids,
                                "usage_ids": result.usage_ids,
                                "uncertainty": result.uncertainty,
                                "source_event": start_sequence,
                            }
                        },
                        None,
                    ),
                ),
                None,
            )
        if isinstance(result, ProgressClaimChange):
            return (
                (self._progress_claim_transition(state, result, start_sequence),),
                None,
            )
        if isinstance(result, CompletionProposal):
            if state.status is SessionStatus.COMPLETED:
                return ((), None)
            return (
                (
                    (
                        "completion_rejected",
                        state,
                        {
                            "rationale": result.rationale,
                            "reason": "no current external evidence",
                        },
                        None,
                    ),
                ),
                None,
            )
        raise ContractError(f"unsupported agent result {type(result).__name__}")

    def _memory_update_transition(
        self, state: SessionState, result: MemoryUpdate, source_event: int
    ) -> _Transition:
        """Derive one knowledge-recorded transition from a memory update."""
        events = self._journal_events()
        current = next(
            (
                item
                for item in knowledge_items(state, events)
                if item.item_id == result.item_id
            ),
            None,
        )
        status = effective_knowledge_status(
            None if current is None else current.status,
            result.status,
            result.evidence_ids,
            validated_evidence_ids(state, events),
        )
        return (
            "knowledge_recorded",
            state,
            {
                "item": {
                    "item_id": result.item_id,
                    "statement": result.statement,
                    "scope": result.scope,
                    "status": status.value,
                    "evidence_ids": result.evidence_ids,
                    "provenance": "agent",
                    "source_event": source_event,
                },
                "assessment": assess_knowledge_transition(
                    state=state,
                    events=events,
                    target_id=result.item_id,
                    statement=result.statement,
                    scope=result.scope,
                    evidence_ids=result.evidence_ids,
                ),
            },
            None,
        )

    def _progress_claim_transition(
        self, state: SessionState, result: ProgressClaimChange, source_event: int
    ) -> _Transition:
        """Derive one claim transition. Self-report and ID presence never support."""
        events = self._journal_events()
        fingerprint = (
            result.category.value,
            result.before,
            result.after,
            result.scope,
        )
        prior = next(
            (
                claim
                for claim in progress_claims(events)
                if claim_fingerprint(claim) == fingerprint
            ),
            None,
        )
        assessment = assess_progress_claim(
            state=state,
            events=events,
            claim_id=result.claim_id,
            category=result.category,
            before=result.before,
            after=result.after,
            scope=result.scope,
            evidence_ids=result.evidence_ids,
            assessor=result.assessor,
            prior_status=None if prior is None else prior.status,
        )
        status = assessment.status
        kind = (
            "progress_claim_rejected"
            if status is ProgressStatus.REJECTED
            else "progress_claim_recorded"
        )
        return (
            kind,
            state,
            {
                "claim": {
                    "claim_id": result.claim_id,
                    "category": result.category.value,
                    "before": result.before,
                    "after": result.after,
                    "scope": result.scope,
                    "evidence_ids": result.evidence_ids,
                    "assessor": result.assessor,
                    "status": status.value,
                    "source_event": source_event,
                },
                "assessment": assessment,
            },
            None,
        )

    def _guidance_transitions(
        self, state: SessionState, result: GuidanceResponse, start_sequence: int
    ) -> tuple[tuple[_Transition, ...], PendingDispatch | None]:
        """Derive guidance-response transitions, including a replan cascade.

        Accept and reject close the guidance; replan also abandons the current
        attempt and starts a fresh one. A response with no active guidance is
        recorded as rejected and changes nothing else.
        """
        events = self._journal_events()
        active = active_guidance(events)
        if active is None:
            return (
                (
                    (
                        "guidance_response_rejected",
                        state,
                        {
                            "decision": result.decision.value,
                            "reason": result.reason,
                            "error": "no active guidance to respond to",
                        },
                        None,
                    ),
                ),
                None,
            )
        source_id, _body = active
        kind = (
            "guidance_rejected"
            if result.decision is GuidanceDecision.REJECT
            else "guidance_accepted"
        )
        transitions: list[_Transition] = [
            (
                kind,
                state,
                {"decision": result.decision.value, "reason": result.reason},
                source_id,
            )
        ]
        if result.decision is GuidanceDecision.REPLAN:
            transitions.extend(
                self._replan_transitions(
                    state,
                    "agent replan response to supervisor guidance",
                    start_sequence + 1,
                )
            )
        return (tuple(transitions), None)

    def _replan_transitions(
        self, state: SessionState, reason: str, abandon_sequence: int
    ) -> tuple[_Transition, ...]:
        """Abandon the active attempt and start a fresh one in one batch."""
        attempt = state.active_attempt
        if attempt is None:
            raise TransitionError("no active attempt to replan")
        abandoned = abandon_attempt(state, abandon_sequence)
        started = start_attempt(
            abandoned, _new_id("attempt"), abandon_sequence + 1, from_observed=True
        )
        return (
            (
                "attempt_abandoned",
                abandoned,
                {"attempt_id": attempt.attempt_id, "reason": reason},
                None,
            ),
            (
                "attempt_started",
                started,
                _attempt_started_payload(started),
                None,
            ),
        )

    def _apply_supervision_boundary(
        self,
        events: tuple[Event, ...],
        *,
        dispatch: bool = True,
        allow_supervisor_consult: bool = True,
    ) -> bool:
        """Expire stale guidance, evaluate the monitor, and run one escalation rung.

        Returns True when this step must stop without a main-model call: an
        escalation rung exposed a blocking operator question and the session
        now waits for the operator's answer (the stage-3 ladder rung, or the
        stage-1 rung when no supervisor call fits the remaining budget per
        ADR-0013), or a stage-1 consult was deferred to a worker when
        ``dispatch`` is False.
        """
        capability = self._supervision
        if capability is None:  # pragma: no cover - the caller guards the flag
            return False
        upcoming = len(facts_history(events)) + 1
        active = active_guidance(events)
        if active is not None and active[1].expires_after_call < upcoming:
            self._persist(
                "guidance_expired",
                self._state,
                {
                    "expires_after_call": active[1].expires_after_call,
                    "reason": "guidance expiry passed without a recorded response",
                },
                causation_id=active[0],
            )
        trigger = evaluate_supervision(
            events,
            upcoming,
            capability.policy,
            # ADR-0023: only the board policy reads observations; p6 is unchanged.
            artifacts=(
                self._artifacts
                if capability.policy.version == MONITOR_POLICY_P7_VERSION
                else None
            ),
            model_call_budget=self._state.task.budgets.model_calls,
        )
        if trigger is None:
            self._journal_checkpoint_suppression(events, upcoming, capability.policy)
            return False
        # The stage counts only the prior triggers: the about-to-persist one
        # takes this rung.
        stage = escalation_stage(events)
        self._persist("supervision_triggered", self._state, trigger_payload(trigger))
        persisted = self._journal_events()
        if stage == 1:
            if not allow_supervisor_consult:
                # ADR-0013: a checkpoint the remaining budget cannot review is
                # never silently skipped. Expose it as a durable operator
                # question and wait; the trigger evidence sits in the adjacent
                # supervision_triggered event and the escalation payload.
                self._escalate_to_operator(
                    trigger,
                    stage,
                    reason="no supervisor call fits the remaining model-call budget",
                )
                return True
            if dispatch:
                self._consult_supervisor(persisted, trigger, capability)
            else:
                return self._defer_supervisor(persisted, trigger)
        elif stage == 2:
            self._checkpoint_replan(
                "supervisor escalation: checkpoint/replan after a repeated trigger"
            )
        else:
            self._escalate_to_operator(trigger, stage)
            return True
        return False

    def _journal_checkpoint_suppression(
        self,
        events: tuple[Event, ...],
        upcoming: int,
        policy: MonitorPolicy,
    ) -> None:
        """Journal one due-but-cooldown-suppressed checkpoint disposition.

        ADR-0013 journals the cooldown/suppression disposition of every due
        budget checkpoint; the event changes no projection and the boundary
        proceeds without a trigger.
        """
        suppression = checkpoint_suppression(
            events,
            upcoming,
            policy,
            model_call_budget=self._state.task.budgets.model_calls,
        )
        if suppression is not None:
            self._persist(
                "checkpoint_suppressed",
                self._state,
                checkpoint_suppression_payload(suppression),
            )

    def _intend_supervisor_consult(
        self,
        events: tuple[Event, ...],
        trigger: SupervisionTrigger,
    ) -> _SupervisorConsult | None:
        """Persist one supervisor intent and return its consult descriptor.

        Returns ``None`` when the consult cannot be reserved (for example an
        exhausted model-call budget); the failure is recorded and the run
        continues without guidance.
        """
        digest = build_digest(self._state, events, trigger)
        visible = render_digest(digest)
        request_id = f"{SUPERVISOR_REQUEST_PREFIX}{uuid4().hex}"
        reservation = ResourceReservation(
            reservation_id=_new_id("reservation"),
            operation_id=request_id,
            objective_version=self._state.task.objective_version,
            model_calls=1,
            tokens=self._supervisor_token_bound(visible, events),
        )
        try:
            intended = intend_model_call(
                self._state,
                reservation,
                request_id=request_id,
                input_text=visible,
                cursor=self._cursor,
            )
        except TransitionError as exc:
            self._persist(
                "supervisor_failed",
                self._state,
                {"request_id": None, "error": str(exc)},
            )
            return None
        self._persist(
            "supervisor_intended",
            intended,
            {
                "request_id": request_id,
                "reservation": reservation,
                "input": visible,
                "cursor": self._cursor,
            },
        )
        return _SupervisorConsult(request_id, reservation, visible, digest)

    def _consult_supervisor(
        self,
        events: tuple[Event, ...],
        trigger: SupervisionTrigger,
        capability: SupervisionCapability,
    ) -> None:
        """Run one advisory consult through the model-call reservation path.

        Supervisor failure is a typed event that settles the reservation; the
        run continues without guidance (ARCH-0001: bounded, never fatal).
        """
        consult = self._intend_supervisor_consult(events, trigger)
        if consult is None:
            return
        try:
            response = capability.supervisor.advise(consult.digest)
            if not isinstance(response, SupervisorResponse):
                raise ContractError("supervisor returned an invalid response")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            self._record_supervisor_failure(
                consult.request_id,
                consult.reservation,
                consult.visible_input,
                self._cursor,
                usage=exc.usage if isinstance(exc, ModelCallError) else None,
                error=safe_model_error(exc),
            )
            return
        self._record_supervisor_guidance(
            consult.request_id,
            consult.reservation,
            consult.visible_input,
            response,
            self._cursor,
        )

    def _defer_supervisor(
        self,
        events: tuple[Event, ...],
        trigger: SupervisionTrigger,
    ) -> bool:
        """Persist a supervisor intent and defer its blocking call to a worker.

        Returns True when a bounded worker must now run the consult; False when
        the consult could not be reserved and the run continues without it.
        """
        consult = self._intend_supervisor_consult(events, trigger)
        if consult is None:
            return False
        if self._pending_dispatch is not None:
            raise ContractError("an external dispatch is already pending")
        self._pending_dispatch = PendingDispatch(
            kind="supervision", digest=consult.digest
        )
        return True

    def _record_supervisor_failure(
        self,
        request_id: str,
        reservation: ResourceReservation,
        visible_input: str,
        cursor: int,
        *,
        usage: UsageMeasurement | None = None,
        error: str = "supervisor call failed",
    ) -> None:
        """Settle one supervisor consult as failed without guidance."""
        self._persist(
            "supervisor_failed",
            settle_model_call(self._state, reservation, usage),
            {
                "request_id": request_id,
                "reservation": reservation,
                "input": visible_input,
                "cursor": cursor,
                "usage": usage,
                "error": error,
            },
        )

    def _record_supervisor_guidance(
        self,
        request_id: str,
        reservation: ResourceReservation,
        visible_input: str,
        response: SupervisorResponse,
        cursor: int,
    ) -> None:
        """Settle one supervisor consult with its advisory guidance."""
        self._persist(
            "guidance_received",
            settle_model_call(self._state, reservation, response.usage),
            {
                "request_id": request_id,
                "reservation": reservation,
                "input": visible_input,
                "cursor": cursor,
                "usage": response.usage,
                "model_id": response.model_id,
                "guidance": guidance_payload(response.guidance),
            },
        )

    def _pending_supervisor_operation(self) -> ModelCallOperation | None:
        """Return the single unsettled supervisor intent, if one is pending."""
        pending = [
            operation
            for operation in self._state.model_operations
            if operation.request_id.startswith(SUPERVISOR_REQUEST_PREFIX)
            and not operation.settled
        ]
        if len(pending) > 1:
            raise IntegrityError("multiple supervisor consults are unsettled")
        return pending[0] if pending else None

    def _invalidate_stale_deferred_dispatch(self) -> None:
        """Settle a deferred dispatch that an applied control made inapplicable.

        A command applied after a deferred intent (for example a steer that
        changes the objective and abandons the attempt) can make that intent
        stale before its external call is taken. The call is never made: a
        runtime action or evaluation is settled as not submitted, and an
        advisory supervisor consult is settled as a failed consult. Recovery
        therefore never treats the intent as an unknown effect, and a stale
        result is never attributed to the new objective.
        """
        pending = self._pending_dispatch
        if pending is None:
            return
        if pending.kind == "action":
            proposal = pending.proposal
            if (
                proposal is not None
                and self._state.pending_proposal_id == proposal.proposal_id
            ):
                return
            self._pending_dispatch = None
            self._settle_unsubmitted_action(proposal)
            return
        if pending.kind == "evaluation":
            request = pending.request
            if (
                request is not None
                and self._state.pending_proposal_id == request.request_id
            ):
                return
            self._pending_dispatch = None
            self._settle_unsubmitted_evaluation(request)
            return
        operation = self._pending_supervisor_operation()
        if (
            operation is not None
            and operation.cursor is not None
            and self._cursor == operation.cursor + 1
        ):
            return
        self._pending_dispatch = None
        self._settle_invalidated_supervision(operation)

    def _settle_unsubmitted_action(self, proposal: ActionProposal | None) -> None:
        """Record one stale deferred action intent as not submitted."""
        if proposal is None:  # pragma: no cover - PendingDispatch invariant
            raise IntegrityError("deferred action dispatch lacks its proposal")
        state = reconcile_action(
            self._state, proposal.proposal_id, ReconciliationStatus.NOT_SUBMITTED
        )
        self._persist(
            "operation_reconciled",
            state,
            self._reconciliation_payload(
                proposal.proposal_id,
                OperationKind.ACTION,
                ReconciliationKind.NOT_SUBMITTED,
                None,
            ),
        )

    def _settle_unsubmitted_evaluation(self, request: EvaluationRequest | None) -> None:
        """Record one stale deferred evaluation intent as not submitted."""
        if request is None:  # pragma: no cover - PendingDispatch invariant
            raise IntegrityError("deferred evaluation dispatch lacks its request")
        state = reconcile_evaluation(
            self._state, request.request_id, ReconciliationStatus.NOT_SUBMITTED
        )
        self._persist(
            "operation_reconciled",
            state,
            self._reconciliation_payload(
                request.request_id,
                OperationKind.EVALUATION,
                ReconciliationKind.NOT_SUBMITTED,
                None,
            ),
        )

    def _settle_invalidated_supervision(
        self, operation: ModelCallOperation | None
    ) -> None:
        """Record one stale deferred supervisor consult as a failed consult."""
        if operation is None:  # pragma: no cover - the caller guards the intent
            return
        if operation.cursor is None:  # pragma: no cover - intent records a cursor
            raise IntegrityError("supervisor consult lacks its intent cursor")
        self._persist(
            "supervisor_failed",
            settle_model_call(self._state, operation.reservation, None),
            {
                "request_id": operation.request_id,
                "reservation": operation.reservation,
                "input": operation.input_text or "",
                "cursor": operation.cursor + 1,
                "usage": None,
                "error": "supervisor consult invalidated by a later control",
            },
        )

    def _checkpoint_replan(self, reason: str) -> None:
        """Close the active attempt and start a fresh one from the lineage head.

        Abandonment is persisted before the replacement attempt so a crash
        between them leaves the attempt closed, never half-replanned.
        """
        attempt = self._require_active_attempt()
        abandoned = abandon_attempt(self._state, self._cursor + 1)
        self._persist(
            "attempt_abandoned",
            abandoned,
            {"attempt_id": attempt.attempt_id, "reason": reason},
        )
        self.begin_attempt()

    def _escalate_to_operator(
        self, trigger: SupervisionTrigger, stage: int, *, reason: str | None = None
    ) -> None:
        """Escalate a supervision trigger to a blocking operator question.

        ``reason`` records a bounded escalation cause other than the stage
        ladder itself (ADR-0013: a checkpoint the remaining budget cannot
        review is exposed as a durable operator question instead of being
        silently skipped); the trigger evidence stays in the adjacent
        ``supervision_triggered`` event and the escalation payload either way.
        """
        question_id = f"question-{self._cursor + 2}"
        if reason is None:
            detail = f"repeated {trigger.cause} at call {trigger.call}" + (
                f" on fingerprint {trigger.fingerprint}" if trigger.fingerprint else ""
            )
        else:
            detail = (
                f"{trigger.cause} at call {trigger.call} could not be reviewed "
                f"by the supervisor: {reason}"
            )
        text = (
            f"Progress supervisor escalation (stage {stage}): {detail}. "
            "How should the agent proceed?"
        )
        payload: dict[str, JsonValue] = {
            "call": trigger.call,
            "cause": trigger.cause,
            "fingerprint": trigger.fingerprint,
            "stage": stage,
        }
        if reason is not None:
            payload["reason"] = reason
        self._persist_batch(
            (
                (
                    "operator_escalated",
                    self._state,
                    payload,
                    None,
                ),
                (
                    "question_recorded",
                    replace(
                        self._state,
                        status=SessionStatus.WAITING,
                        wait_reason=OPERATOR_ANSWER_WAIT,
                        blocking_question_id=question_id,
                    ),
                    {
                        "question_id": question_id,
                        "text": text,
                        "objective_version": self._state.task.objective_version,
                        "status": "open",
                    },
                    None,
                ),
            )
        )

    def begin_attempt(self, attempt_id: str | None = None) -> Attempt:
        """Start one scripted phase-one trajectory from the current committed head."""
        self._require_schedulable()
        state = start_attempt(
            self._state,
            attempt_id or _new_id("attempt"),
            self._cursor + 1,
            from_observed=True,
        )
        self._persist("attempt_started", state, _attempt_started_payload(state))
        if state.active_attempt is None:  # pragma: no cover - transition guarantee
            raise IntegrityError("attempt transition did not create an attempt")
        return state.active_attempt

    def submit_action(
        self,
        action_name: str,
        arguments: Mapping[str, JsonValue],
        *,
        rationale: str,
        reservation_cost: Decimal | None = None,
        proposal_id: str | None = None,
    ) -> ExecutionReceipt:
        """Record intent before proposing one action to the external runtime."""
        self._require_schedulable()
        proposal, intended = self._prepare_action(
            self._state,
            action_name,
            arguments,
            rationale=rationale,
            reservation_cost=reservation_cost,
            proposal_id=proposal_id,
        )
        self._persist(
            "action_intended",
            intended,
            {"proposal": proposal, "reservation": proposal.reservation},
        )
        return self._dispatch_action(proposal)

    def _prepare_action(
        self,
        session_state: SessionState,
        action_name: str,
        arguments: Mapping[str, JsonValue],
        *,
        rationale: str,
        reservation_cost: Decimal | None = None,
        proposal_id: str | None = None,
    ) -> tuple[ActionProposal, SessionState]:
        """Derive one durable action intent without submitting it."""
        attempt = session_state.active_attempt
        if attempt is None:
            raise TransitionError("no active attempt")
        contract = self._environment.describe()
        action = next(
            (item for item in contract.actions if item.name == action_name), None
        )
        if action is None:
            raise ContractError(f"unknown environment action {action_name!r}")
        _validate_action_arguments(arguments, action.argument_schema)
        proposal_id = proposal_id or _new_id("proposal")
        if proposal_id.startswith(SUPERVISOR_REQUEST_PREFIX):
            raise ContractError("proposal_id uses a reserved supervisor prefix")
        expected_state = attempt.result_state or attempt.starting_state
        if contract.capabilities.revisions and expected_state.revision is None:
            raise ContractError(
                "revision-capable runtime requires expected state revision"
            )
        reservation = ResourceReservation(
            reservation_id=_new_id("reservation"),
            operation_id=proposal_id,
            objective_version=session_state.task.objective_version,
            actions=1,
            cost=reservation_cost,
        )
        intended = intend_action(
            session_state,
            proposal_id,
            attempt.attempt_id,
            reservation,
            expected_revision=expected_state.revision,
            requires_revision=contract.capabilities.revisions,
        )
        proposal = ActionProposal(
            proposal_id=proposal_id,
            session_id=self._session_id,
            objective_version=session_state.task.objective_version,
            attempt_id=attempt.attempt_id,
            action_name=action_name,
            arguments=frozen_mapping(arguments),
            expected_state=expected_state,
            reservation=reservation,
            rationale=rationale,
            idempotency_key=proposal_id if contract.capabilities.idempotency else None,
            requires_revision=contract.capabilities.revisions,
        )
        return proposal, intended

    def _dispatch_action(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Submit one durable action intent and record its runtime receipt."""
        self._action_names[proposal.proposal_id] = proposal.action_name
        receipt = self._runtime.submit(proposal)
        self.receive_receipt(receipt)
        return receipt

    def receive_receipt(self, receipt: ExecutionReceipt) -> SessionView:
        """Durably ingest one runtime receipt before its state becomes visible."""
        existing = self._receipts.get(receipt.receipt_id)
        if existing is not None:
            if existing != receipt:
                raise ContractError("receipt ID was reused with different content")
            return self.view()
        try:
            action_operation(self._state, receipt.proposal_id)
        except TransitionError as exc:
            raise ContractError(
                "receipt does not correlate to a known proposal"
            ) from exc
        try:
            state = receive_action_receipt(self._state, receipt)
        except TransitionError as exc:
            raise ContractError(str(exc)) from exc
        evidence = self._receipt_evidence_fingerprints(receipt)
        payload: dict[str, object] = {"receipt": receipt}
        if evidence:
            payload["evidence_fingerprints"] = list(evidence)
        self._persist(
            "receipt_received",
            state,
            payload,
        )
        self._receipts[receipt.receipt_id] = receipt
        return self.view()

    def _receipt_evidence_fingerprints(
        self, receipt: ExecutionReceipt
    ) -> tuple[str, ...]:
        """Compute the R1/R2 evidence fingerprints for one runtime receipt.

        The ADR-0011 switch journals evidence fingerprints at receipt time so
        the monitor stays a pure function over the journal: the session
        resolves the receipt's stdout payload through its artifact store and
        the ``receipt_received`` event records both digests. Only successful
        receipts of evidence-bearing actions (``exec``/``job_read``)
        fingerprint; other receipts and statuses contribute nothing.
        """
        if receipt.raw_artifact is None or receipt.status not in (
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.ACCEPTED,
        ):
            return ()
        if self._action_names.get(receipt.proposal_id) not in EVIDENCE_ACTIONS:
            return ()
        return stdout_fingerprints(
            self._artifacts.read(receipt.raw_artifact), self._artifacts
        )

    def request_evaluation(
        self, state: StateRef | None = None
    ) -> EvaluationReceipt | None:
        """Request evaluation and promote only an applicable result.

        In operator evaluation mode the durable request waits for an
        ``EvaluationDecision`` instead of dispatching an evaluator; the return
        value is ``None`` until the operator answers.
        """
        self._require_schedulable()
        request, reservation, pending = self._prepare_evaluation(self._state, state)
        self._persist(
            "evaluation_intended",
            pending,
            self._evaluation_intent_fields(request, reservation),
        )
        if self._evaluation_mode is EvaluationMode.OPERATOR:
            return None
        return self._dispatch_evaluation(request)

    def _evaluation_intent_fields(
        self, request: EvaluationRequest, reservation: ResourceReservation
    ) -> dict[str, object]:
        """Return the persisted intent payload for the active evaluation mode."""
        fields: dict[str, object] = {"request": request, "reservation": reservation}
        if self._evaluation_mode is EvaluationMode.OPERATOR:
            fields["wait_reason"] = OPERATOR_EVALUATION_WAIT
        return fields

    def _dispatch_evaluation_if_external(self, request: EvaluationRequest) -> None:
        """Dispatch to the external checker only in external-check mode."""
        if self._evaluation_mode is EvaluationMode.OPERATOR:
            return
        self._dispatch_evaluation(request)

    def _prepare_evaluation(
        self, session_state: SessionState, evaluated: StateRef | None = None
    ) -> tuple[EvaluationRequest, ResourceReservation, SessionState]:
        """Derive one durable evaluation intent without calling the evaluator."""
        if session_state.status not in (SessionStatus.CREATED, SessionStatus.RUNNING):
            raise TransitionError(
                f"session is not schedulable while {session_state.status.value}"
            )
        attempt = session_state.active_attempt
        if attempt is None:
            raise TransitionError("no active attempt")
        evaluated_state = evaluated or attempt.result_state or attempt.starting_state
        request = EvaluationRequest(
            request_id=_new_id("evaluation"),
            session_id=self._session_id,
            objective_version=session_state.task.objective_version,
            attempt_id=attempt.attempt_id,
            evaluated_state=evaluated_state,
            baseline_node_id=session_state.lineage_head.node_id,
            evaluator_version=session_state.task.evaluation_contract_id,
        )
        reservation = ResourceReservation(
            reservation_id=_new_id("reservation"),
            operation_id=request.request_id,
            objective_version=request.objective_version,
            evaluations=1,
        )
        pending = intend_evaluation(
            session_state,
            request,
            reservation,
            wait_reason=self._evaluation_wait_reason(),
        )
        return request, reservation, pending

    def _evaluation_wait_reason(self) -> str:
        """Return the durable wait reason for a new evaluation intent."""
        if self._evaluation_mode is EvaluationMode.OPERATOR:
            return OPERATOR_EVALUATION_WAIT
        return "evaluation pending"

    def _dispatch_evaluation(self, request: EvaluationRequest) -> EvaluationReceipt:
        """Consume one externally supplied evaluation after its intent is durable."""
        receipt = self._evaluator.evaluate(request)
        return self._receive_evaluation_receipt(request, receipt)

    def _receive_evaluation_receipt(
        self,
        request: EvaluationRequest,
        receipt: EvaluationReceipt,
        *,
        command_id: str | None = None,
    ) -> EvaluationReceipt:
        """Verify and consume an evaluator receipt for its already-intended request.

        Re-applying a terminal evaluation never manufactures success: a consumed
        receipt is a no-op, a recorded verification failure stays a failure, and
        a receipt that contradicts a proved not-submitted operation is refused.
        A retained intake is re-verified before consumption, so evidence
        corrupted after intake cannot promote.
        """
        operation = evaluation_operation(self._state, request.request_id)
        if operation.status is EvaluationOperationStatus.RECEIPT_CONSUMED:
            self._validate_settled_evaluation(operation, receipt)
            return receipt
        if operation.status is EvaluationOperationStatus.VERIFICATION_FAILED:
            self._validate_settled_evaluation(operation, receipt)
            raise IntegrityError(
                "evaluation verification already failed for this request"
            )
        if operation.status is EvaluationOperationStatus.NOT_SUBMITTED:
            raise ContractError(
                "evaluation receipt contradicts a not-submitted reconciliation"
            )
        disposition = promotion_disposition(self._state, request, receipt)
        if operation.status is EvaluationOperationStatus.INTENDED:
            self._persist(
                "evaluation_intake",
                record_evaluation_intake(
                    self._state,
                    request.request_id,
                    receipt.receipt_id,
                    canonical_json(receipt),
                ),
                {
                    "request_id": request.request_id,
                    "receipt_id": receipt.receipt_id,
                    "raw_receipt": canonical_json(receipt),
                },
            )
        elif (
            operation.receipt_id != receipt.receipt_id
            or operation.raw_receipt != canonical_json(receipt)
        ):
            raise ContractError("evaluation receipt conflicts with durable intake")
        if operation.status is EvaluationOperationStatus.INTAKE_RECORDED and (
            not self._evaluation_consumable()
        ):
            return receipt
        try:
            self._verify_evaluation_evidence(receipt)
        except IntegrityError as exc:
            self._persist(
                "evaluation_verification_failed",
                fail_evaluation_verification(
                    self._state, request.request_id, receipt.receipt_id, receipt.usage
                ),
                {
                    "request_id": request.request_id,
                    "receipt_id": receipt.receipt_id,
                    "reason": str(exc),
                },
            )
            raise
        if self._state.status in (SessionStatus.PAUSED, SessionStatus.STOPPED):
            return receipt
        return self._persist_evaluation_consumption(
            request, receipt, disposition, command_id=command_id
        )

    def _persist_evaluation_consumption(
        self,
        request: EvaluationRequest,
        receipt: EvaluationReceipt,
        disposition: PromotionDisposition,
        *,
        command_id: str | None = None,
    ) -> EvaluationReceipt:
        """Consume one verified intake, binding its decision command atomically.

        ``command_applied`` for an operator decision shares the promotion batch,
        so consumption and the command disposition commit together.
        """
        consumed = consume_evaluation(
            self._state, receipt, disposition, self._cursor + 1
        )
        if command_id is not None:
            consumed = record_command_transition(
                consumed, command_id, "evaluation_received"
            )
        if disposition is not PromotionDisposition.ACCEPTED:
            transitions: list[
                tuple[str, SessionState, Mapping[str, object], str | None]
            ] = [
                (
                    "evaluation_received",
                    consumed,
                    {"receipt": receipt, "disposition": disposition.value},
                    command_id,
                )
            ]
            if command_id is not None:
                transitions.append(
                    (
                        "command_applied",
                        settle_command(consumed, command_id, "applied"),
                        {"command_id": command_id},
                        command_id,
                    )
                )
            self._persist_batch(tuple(transitions))
            return receipt
        promoted = promote(
            consumed, request, receipt, _new_id("node"), self._cursor + 2
        )
        transitions = [
            (
                "evaluation_received",
                consumed,
                {"receipt": receipt, "disposition": disposition.value},
                command_id,
            ),
            (
                "lineage_promoted",
                promoted,
                {
                    "node": promoted.lineage_head,
                    "evaluation_receipt_id": receipt.receipt_id,
                },
                None,
            ),
        ]
        if command_id is not None:
            transitions.append(
                (
                    "command_applied",
                    settle_command(promoted, command_id, "applied"),
                    {"command_id": command_id},
                    command_id,
                )
            )
        self._persist_batch(tuple(transitions))
        return receipt

    @staticmethod
    def _validate_settled_evaluation(
        operation: EvaluationOperation, receipt: EvaluationReceipt
    ) -> None:
        """Require a repeated evaluation receipt to match its durable intake."""
        if (
            operation.receipt_id != receipt.receipt_id
            or operation.raw_receipt != canonical_json(receipt)
        ):
            raise ContractError("evaluation receipt conflicts with durable intake")

    def _evaluation_consumable(self) -> bool:
        """Whether a retained evaluation intake may be consumed now."""
        return self._state.status not in (
            SessionStatus.PAUSED,
            SessionStatus.STOPPED,
        )

    def _reject_command(self, command: OperatorCommand, reason: str) -> None:
        """Record one visible command rejection with its reason."""
        self._persist(
            "command_rejected",
            settle_command(self._state, command.command_id, "rejected"),
            {"command_id": command.command_id, "reason": reason},
            causation_id=command.command_id,
        )

    def _pending_decision_command(self, request_id: str) -> EvaluationDecision | None:
        """Return an unsettled decision command that answers ``request_id``."""
        for item in self._state.command_operations:
            command = item.command
            if (
                item.disposition is None
                and item.transition_kind is None
                and isinstance(command, EvaluationDecision)
                and command.request_id == request_id
            ):
                return command
        return None

    def _command_disposition(self, command_id: str) -> str | None:
        """Return the recorded disposition of one admitted command, if any."""
        for item in self._state.command_operations:
            if item.command.command_id == command_id:
                return item.disposition
        return None

    def _settle_equivalent_decision(self, command: EvaluationDecision) -> None:
        """Settle a decision whose equivalent answer was already consumed.

        Recovery may consume retained intake before the queued decision command
        is settled. Recording the same binding keeps the command correlation and
        returns the applied disposition instead of a false rejection.
        """
        bound = record_command_transition(
            self._state, command.command_id, "evaluation_received"
        )
        self._persist(
            "command_applied",
            settle_command(bound, command.command_id, "applied"),
            {
                "command_id": command.command_id,
                "transition_kind": "evaluation_received",
            },
            causation_id=command.command_id,
        )

    @staticmethod
    def _operator_evaluation_receipt(
        request: EvaluationRequest, decision: EvaluationDecision
    ) -> EvaluationReceipt:
        """Derive the operator-acceptance receipt for one decision.

        Receipt identity is deterministic from the request plus the decision
        content, so replaying the same decision reproduces the same receipt.
        """
        return operator_evaluation_receipt(request, decision)

    def _apply_evaluation_decision(self, command: EvaluationDecision) -> bool:
        """Apply one operator decision; ``True`` keeps it queued for later.

        Admission and durable intake are allowed while paused or stopped.
        Consumption and promotion happen only while the session is consumable
        and the request, objective, attempt, and lineage head still apply.
        """
        try:
            operation = evaluation_operation(self._state, command.request_id)
        except TransitionError:
            self._reject_command(command, "no evaluation request answers this decision")
            return False
        receipt = self._operator_evaluation_receipt(operation.request, command)
        status = operation.status
        if status in (
            EvaluationOperationStatus.RECEIPT_CONSUMED,
            EvaluationOperationStatus.VERIFICATION_FAILED,
        ):
            try:
                self._validate_settled_evaluation(operation, receipt)
            except ContractError as exc:
                self._reject_command(command, str(exc))
                return False
            if self._command_disposition(command.command_id) is not None:
                return False
            if status is EvaluationOperationStatus.RECEIPT_CONSUMED:
                self._settle_equivalent_decision(command)
            else:
                self._reject_command(
                    command,
                    "identical decision matches a failed evaluation verification",
                )
            return False
        if status is EvaluationOperationStatus.NOT_SUBMITTED:
            self._reject_command(
                command, "decision contradicts a not-submitted reconciliation"
            )
            return False
        if status is EvaluationOperationStatus.INTAKE_RECORDED:
            if operation.raw_receipt != canonical_json(receipt):
                self._reject_command(
                    command, "conflicting decision for this evaluation request"
                )
                return False
            if not self._evaluation_consumable():
                return True
            try:
                self._verify_evaluation_evidence(receipt)
            except IntegrityError as exc:
                self._persist(
                    "evaluation_verification_failed",
                    fail_evaluation_verification(
                        self._state,
                        command.request_id,
                        receipt.receipt_id,
                        receipt.usage,
                    ),
                    {
                        "request_id": command.request_id,
                        "receipt_id": receipt.receipt_id,
                        "reason": str(exc),
                    },
                    causation_id=command.command_id,
                )
                self._reject_command(command, str(exc))
                return False
            disposition = promotion_disposition(self._state, operation.request, receipt)
            self._persist_evaluation_consumption(
                operation.request,
                receipt,
                disposition,
                command_id=command.command_id,
            )
            return False
        intake = record_evaluation_intake(
            self._state,
            command.request_id,
            receipt.receipt_id,
            canonical_json(receipt),
        )
        try:
            self._verify_evaluation_evidence(receipt)
        except IntegrityError as exc:
            self._persist_batch(
                (
                    (
                        "evaluation_intake",
                        intake,
                        {
                            "request_id": command.request_id,
                            "receipt_id": receipt.receipt_id,
                            "raw_receipt": canonical_json(receipt),
                        },
                        command.command_id,
                    ),
                    (
                        "evaluation_verification_failed",
                        fail_evaluation_verification(
                            intake,
                            command.request_id,
                            receipt.receipt_id,
                            receipt.usage,
                        ),
                        {
                            "request_id": command.request_id,
                            "receipt_id": receipt.receipt_id,
                            "reason": str(exc),
                        },
                        command.command_id,
                    ),
                )
            )
            self._reject_command(command, str(exc))
            return False
        self._persist(
            "evaluation_intake",
            intake,
            {
                "request_id": command.request_id,
                "receipt_id": receipt.receipt_id,
                "raw_receipt": canonical_json(receipt),
            },
            causation_id=command.command_id,
        )
        if not self._evaluation_consumable():
            return True
        disposition = promotion_disposition(self._state, operation.request, receipt)
        self._persist_evaluation_consumption(
            operation.request, receipt, disposition, command_id=command.command_id
        )
        return False

    def receive_evaluation_usage(
        self, request_id: str, usage: UsageMeasurement
    ) -> SessionView:
        """Durably apply one late evaluator measurement to its known operation."""
        try:
            state = record_evaluation_usage(self._state, request_id, usage)
        except TransitionError as exc:
            raise ContractError(str(exc)) from exc
        if state == self._state:
            return self.view()
        self._persist(
            "evaluation_usage_received",
            state,
            {"request_id": request_id, "usage": usage},
        )
        return self.view()

    def _verify_evaluation_evidence(self, receipt: EvaluationReceipt) -> None:
        """Verify required evaluator artifacts before dependent local transitions."""
        evidence = (*receipt.evidence, *receipt.completion_evidence)
        if receipt.raw_receipt is not None:
            evidence = (*evidence, receipt.raw_receipt)
        available = {item.digest for item in evidence}
        missing = set(receipt.required_evidence) - available
        if missing:
            raise IntegrityError("evaluation receipt lacks required evidence")
        if receipt.completed and not receipt.completion_evidence:
            raise IntegrityError("completed evaluation lacks completion evidence")
        for reference in evidence:
            self._read_artifact(reference, "evaluation_evidence")

    def reconcile_pending(self) -> SessionView:
        """Recover one durable operation without resubmitting uncertain work."""
        operation_id = self._state.pending_proposal_id
        if operation_id is None:
            return self.view()
        operation = next(
            item
            for item in self._state.operations
            if self._operation_id(item) == operation_id
        )
        if isinstance(operation, ActionOperation):
            return self._reconcile_action(operation)
        return self._reconcile_evaluation(operation)

    def _reconcile_action(self, operation: ActionOperation) -> SessionView:
        if (
            operation.reconciliation_receipt is not None
            and operation.reconciliation_receipt.receipt_id not in self._receipts
        ):
            return self.receive_receipt(operation.reconciliation_receipt)
        result = self._runtime.reconcile(operation.proposal_id)
        reconciliation = ReconciliationStatus(result.kind.value)
        state = reconcile_action(
            self._state,
            operation.proposal_id,
            reconciliation,
            result.receipt,
        )
        if state == self._state:
            return self.view()
        self._persist(
            "operation_reconciled",
            state,
            self._reconciliation_payload(
                operation.proposal_id,
                OperationKind.ACTION,
                result.kind,
                result.receipt,
            ),
        )
        if result.receipt is not None:
            return self.receive_receipt(result.receipt)
        return self.view()

    def _reconcile_evaluation(self, operation: EvaluationOperation) -> SessionView:
        if operation.status is EvaluationOperationStatus.INTAKE_RECORDED:
            if operation.raw_receipt is None:  # pragma: no cover - transition invariant
                raise IntegrityError("evaluation intake lacks a durable raw receipt")
            pending = self._pending_decision_command(operation.request.request_id)
            self._receive_evaluation_receipt(
                operation.request,
                evaluation_receipt_from_payload(
                    parse_json_object(operation.raw_receipt)
                ),
                command_id=None if pending is None else pending.command_id,
            )
            return self.view()
        if operation.reconciliation_receipt is not None:
            self._receive_evaluation_receipt(
                operation.request, operation.reconciliation_receipt
            )
            return self.view()
        result = self._evaluator.reconcile(operation.request.request_id)
        reconciliation = ReconciliationStatus(result.kind.value)
        state = reconcile_evaluation(
            self._state,
            operation.request.request_id,
            reconciliation,
            result.receipt,
        )
        if state == self._state:
            return self.view()
        self._persist(
            "operation_reconciled",
            state,
            self._reconciliation_payload(
                operation.request.request_id,
                OperationKind.EVALUATION,
                result.kind,
                result.receipt,
            ),
        )
        if result.receipt is not None:
            self._receive_evaluation_receipt(operation.request, result.receipt)
        return self.view()

    @staticmethod
    def _operation_id(operation: ActionOperation | EvaluationOperation) -> str:
        return (
            operation.proposal_id
            if isinstance(operation, ActionOperation)
            else operation.request.request_id
        )

    @staticmethod
    def _reconciliation_payload(
        operation_id: str,
        operation_kind: OperationKind,
        result: ReconciliationKind,
        receipt: ExecutionReceipt | EvaluationReceipt | None,
    ) -> Mapping[str, object]:
        """Build the single durable recovery event contract for either port."""
        return {
            "operation_id": operation_id,
            "operation_kind": operation_kind.value,
            "result": result.value,
            "receipt": receipt,
        }

    def checkpoint(self) -> Checkpoint:
        """Persist an explicit checkpoint marker at the current durable state."""
        checkpoint = self._checkpoint_for_current_state()
        self._persist("checkpoint", self._state, {"checkpoint": checkpoint})
        return checkpoint

    def compact_episodes(self, source_start: int, source_end: int) -> Episode:
        """Record a source-linked summary after a covering checkpoint.

        Source events are not rewritten or deleted. Compaction is explicit and
        does not run on every checkpoint.
        """
        if source_start < 1 or source_end < source_start:
            raise ContractError("episode source span is invalid")
        events = self._journal_events()
        if covering_checkpoint(events, source_end) is None:
            raise TransitionError("episode span has no covering checkpoint")
        for episode in episodes(events):
            if (
                episode.source_start == source_start
                and episode.source_end == source_end
            ):
                raise TransitionError("episode span is already compacted")
        span = tuple(
            event for event in events if source_start <= event.sequence <= source_end
        )
        if not span:
            raise TransitionError("episode source span is absent from the journal")
        objective_versions = {event.objective_version for event in span}
        if len(objective_versions) != 1:
            raise TransitionError("episode source span crosses objective versions")
        summary = excerpt_episode(
            objective_for_version(events, span[0].objective_version), span
        )
        evidence_ids = episode_evidence_ids(span)
        episode = Episode(
            episode_id=f"episode-{source_start}-{source_end}",
            source_start=source_start,
            source_end=source_end,
            summary=summary,
            evidence_ids=tuple(evidence_ids),
            model_id=DETERMINISTIC_EXCERPT_MODEL,
            model_version=DETERMINISTIC_EXCERPT_VERSION,
            source_event=self._cursor + 1,
        )
        self._persist(
            "episode_recorded",
            self._state,
            {
                "episode": {
                    "episode_id": episode.episode_id,
                    "source_start": episode.source_start,
                    "source_end": episode.source_end,
                    "summary": episode.summary,
                    "evidence_ids": episode.evidence_ids,
                    "model_id": episode.model_id,
                    "model_version": episode.model_version,
                    "source_event": episode.source_event,
                }
            },
        )
        return episode

    def _prepare_command_transition(
        self, command: OperatorCommand
    ) -> tuple[str, SessionState, Mapping[str, object], str | None]:
        """Derive one pure command transition before its atomic acknowledgement."""
        if isinstance(command, Message):
            return (
                "operator_message",
                self._state,
                {"command": command},
                command.command_id,
            )
        if isinstance(command, Steer):
            return (
                "objective_steered",
                apply_steer(
                    self._state,
                    command.objective,
                    command.success_evidence,
                    self._cursor + 1,
                ),
                {"command": command},
                command.command_id,
            )
        if isinstance(command, Pause):
            if self._state.status not in {
                SessionStatus.CREATED,
                SessionStatus.RUNNING,
                SessionStatus.WAITING,
            }:
                raise TransitionError(
                    "session_paused is not a legal command transition"
                )
            return (
                "session_paused",
                replace(self._state, status=SessionStatus.PAUSED),
                {"command": command},
                command.command_id,
            )
        if isinstance(command, Resume):
            return (
                "session_resumed",
                resume_session(self._state),
                {"command": command},
                command.command_id,
            )
        if isinstance(command, Stop):
            if self._state.status in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.STOPPED,
            }:
                raise TransitionError(
                    "session_stopped is not a legal command transition"
                )
            return (
                "session_stopped",
                replace(self._state, status=SessionStatus.STOPPED),
                {"command": command},
                command.command_id,
            )
        if isinstance(command, CheckpointRequest):
            checkpoint = self._checkpoint_for_current_state()
            return (
                "checkpoint",
                self._state,
                {"checkpoint": checkpoint},
                command.command_id,
            )
        raise ContractError(f"unsupported phase-one command: {type(command).__name__}")

    def _command_event_batch(
        self, command: OperatorCommand
    ) -> tuple[tuple[str, SessionState, Mapping[str, object], str | None], ...]:
        """Build the durable event batch for one applied operator command."""
        if isinstance(command, Message):
            return self._message_event_batch(command)
        if isinstance(command, Steer) and self._state.blocking_question_id is not None:
            return self._steer_event_batch(command)
        kind, state, payload, causation = self._prepare_command_transition(command)
        transitioned = record_command_transition(state, command.command_id, kind)
        settled = settle_command(transitioned, command.command_id, "applied")
        transitions: list[_Transition] = [(kind, transitioned, payload, causation)]
        if isinstance(command, Steer):
            transitions.extend(self._guidance_suppression_transitions(transitioned))
        transitions.append(
            (
                "command_applied",
                settled,
                {"command_id": command.command_id},
                command.command_id,
            )
        )
        return tuple(transitions)

    def _guidance_suppression_transitions(
        self, state: SessionState
    ) -> tuple[_Transition, ...]:
        """Close guidance that an objective change supersedes before delivery.

        Only the guidance's applicability is closed; its completed supervisor
        call stays settled with its recorded usage.
        """
        active = active_guidance(self._journal_events())
        if active is None:
            return ()
        source, guidance = active
        return (
            (
                "guidance_suppressed",
                state,
                {
                    "expires_after_call": guidance.expires_after_call,
                    "reason": "guidance superseded by an objective steer",
                },
                source,
            ),
        )

    def _message_event_batch(
        self, command: Message
    ) -> tuple[tuple[str, SessionState, Mapping[str, object], str | None], ...]:
        """Apply a message, answering a blocking question when one is open."""
        if self._state.wait_reason != OPERATOR_ANSWER_WAIT:
            if command.question_id is not None:
                raise TransitionError("message question_id has no open question")
            kind, state, payload, causation = self._prepare_command_transition(command)
            transitioned = record_command_transition(state, command.command_id, kind)
            settled = settle_command(transitioned, command.command_id, "applied")
            return (
                (kind, transitioned, payload, causation),
                (
                    "command_applied",
                    settled,
                    {"command_id": command.command_id},
                    command.command_id,
                ),
            )
        open_id = self._state.blocking_question_id
        if open_id is None:
            raise TransitionError("operator answer pending without an open question")
        if command.question_id is not None and command.question_id != open_id:
            raise TransitionError("message does not answer the open question")
        messaged = record_command_transition(
            self._state, command.command_id, "operator_message"
        )
        answered = replace(
            messaged,
            status=(
                self._state.status
                if self._state.status in {SessionStatus.PAUSED, SessionStatus.STOPPED}
                else SessionStatus.RUNNING
            ),
            wait_reason=None,
            blocking_question_id=None,
        )
        settled = settle_command(answered, command.command_id, "applied")
        return (
            (
                "operator_message",
                messaged,
                {"command": command},
                command.command_id,
            ),
            (
                "question_answered",
                answered,
                {
                    "question_id": open_id,
                    "command_id": command.command_id,
                    "text": command.text,
                },
                command.command_id,
            ),
            (
                "command_applied",
                settled,
                {"command_id": command.command_id},
                command.command_id,
            ),
        )

    def _steer_event_batch(
        self, command: Steer
    ) -> tuple[tuple[str, SessionState, Mapping[str, object], str | None], ...]:
        """Retire a blocking question, then apply an objective change."""
        question_id = self._state.blocking_question_id
        if question_id is None:
            raise TransitionError("steer retirement requires an open question")
        retired = self._state
        steered = apply_steer(
            retired,
            command.objective,
            command.success_evidence,
            self._cursor + 2,
        )
        transitioned = record_command_transition(
            steered, command.command_id, "objective_steered"
        )
        settled = settle_command(transitioned, command.command_id, "applied")
        return (
            (
                "question_retired",
                retired,
                {
                    "question_id": question_id,
                    "reason": "objective_steered",
                    "objective_version": retired.task.objective_version,
                },
                command.command_id,
            ),
            (
                "objective_steered",
                transitioned,
                {"command": command},
                command.command_id,
            ),
            *self._guidance_suppression_transitions(transitioned),
            (
                "command_applied",
                settled,
                {"command_id": command.command_id},
                command.command_id,
            ),
        )

    def _workspace_protected_digests(self) -> frozenset[str]:
        """Union protection roots for every session sharing this store."""
        protected: set[str] = set()
        peers: list[SqliteJournal] = []
        seen = {self._journal.path.resolve()}
        try:
            for path in self._artifacts.registered_journals():
                resolved = path.resolve()
                if resolved in seen or not path.is_file():
                    continue
                seen.add(resolved)
                peers.append(SqliteJournal(path))
            for journal in (self._journal, *peers):
                for session_id in journal.session_ids():
                    events = _all_events(journal, session_id)
                    if not events:
                        continue
                    state = (
                        self._state
                        if journal is self._journal and session_id == self._session_id
                        else replay(events)
                    )
                    protected.update(protected_digests(state, events))
        finally:
            for journal in peers:
                journal.close()
        return frozenset(protected)

    def _reconcile_retention(self, events: tuple[Event, ...]) -> None:
        """Finish drops whose intent survived a crash without an outcome."""
        intended: dict[str, Mapping[str, object]] = {}
        completed: set[str] = set()
        protected = self._workspace_protected_digests()
        for event in events:
            if event.kind not in {"artifact_eviction_intended", "artifact_evicted"}:
                continue
            digest = event.payload.get("digest")
            size = event.payload.get("size")
            budget = event.payload.get("byte_budget")
            reason = event.payload.get("reason")
            available = event.payload.get("available")
            dropped = event.payload.get("dropped")
            if not _valid_eviction_identity(digest, size, budget, reason):
                raise IntegrityError("malformed artifact eviction identity")
            assert isinstance(digest, str)
            if event.kind == "artifact_eviction_intended":
                if digest in intended:
                    raise IntegrityError("duplicate artifact eviction intent")
                intended[digest] = dict(event.payload)
            elif event.kind == "artifact_evicted":
                if not isinstance(available, bool) or not isinstance(dropped, bool):
                    raise IntegrityError("malformed artifact eviction outcome")
                if dropped and available:
                    raise IntegrityError("dropped artifact cannot remain available")
                completed.add(digest)
        for digest, payload in intended.items():
            if digest in completed:
                continue
            size_value = payload["size"]
            budget_value = payload["byte_budget"]
            assert isinstance(size_value, int) and isinstance(budget_value, int)
            dropped = False if digest in protected else self._artifacts.drop(digest)
            self._record_eviction_outcome(digest, size_value, budget_value, dropped)

    def _record_eviction_intent(self, digest: str, size: int, byte_budget: int) -> None:
        self._persist(
            "artifact_eviction_intended",
            self._state,
            {
                "digest": digest,
                "size": size,
                "reason": "byte_budget",
                "byte_budget": byte_budget,
            },
        )

    def _record_eviction_outcome(
        self, digest: str, size: int, byte_budget: int, dropped: bool
    ) -> None:
        self._persist(
            "artifact_evicted",
            self._state,
            {
                "digest": digest,
                "size": size,
                "reason": "byte_budget",
                "byte_budget": byte_budget,
                "available": self._artifacts.available(digest),
                "dropped": dropped,
            },
        )

    def _journal_events(self) -> tuple[Event, ...]:
        """Every journal event, decoding each one only once (COD-89).

        Events are immutable and the journal is append-only, so the cache is
        extended with whatever follows its last sequence, including events
        another writer appended.
        """
        after = self._event_cache[-1].sequence if self._event_cache else 0
        new = _events_after(self._journal, self._session_id, after)
        if new:
            self._event_cache = (*self._event_cache, *new)
        return self._event_cache

    def _persist(
        self,
        kind: str,
        state: SessionState,
        fields: Mapping[str, object],
        *,
        causation_id: str | None = None,
    ) -> Event:
        return self._persist_batch(((kind, state, fields, causation_id),))[0]

    def _persist_batch(
        self,
        transitions: tuple[
            tuple[str, SessionState, Mapping[str, object], str | None], ...
        ],
    ) -> tuple[Event, ...]:
        """Validate and commit a contiguous set of derived facts atomically."""
        projection: SessionState | None = None if self._cursor == 0 else self._state
        events: list[Event] = []
        for kind, state, fields, causation_id in transitions:
            payload = to_json_value(fields)
            if not isinstance(payload, Mapping):  # pragma: no cover - codec invariant
                raise IntegrityError("durable event payload must be an object")
            event = Event(
                event_id=_new_id("event"),
                session_id=self._session_id,
                sequence=self._cursor + len(events) + 1,
                kind=kind,
                objective_version=state.task.objective_version,
                payload=payload,
                recorded_at=datetime.now(timezone.utc).isoformat(),
                causation_id=causation_id,
            )
            derived = reduce_event(projection, event)
            if derived != state:
                raise IntegrityError(
                    f"{kind} does not derive the requested durable session projection"
                )
            events.append(event)
            projection = derived
        self._cursor = self._journal.append_batch(events, expected_cursor=self._cursor)
        if projection is None:  # pragma: no cover - non-empty transition guarantee
            raise IntegrityError("cannot persist an empty transition batch")
        self._state = projection
        return tuple(events)

    def _checkpoint_for_current_state(self) -> Checkpoint:
        """Build the checkpoint envelope for the projection before its marker."""
        snapshot = projection_payload(self._state)
        return Checkpoint(
            checkpoint_id=_new_id("checkpoint"),
            session_id=self._session_id,
            objective_version=self._state.task.objective_version,
            cursor=self._cursor,
            projection_digest=hashlib.sha256(
                canonical_json(snapshot).encode()
            ).hexdigest(),
            pending_operation_ids=(
                (self._state.pending_proposal_id,)
                if self._state.pending_proposal_id
                else ()
            ),
        )

    def _require_schedulable(self) -> None:
        if self._state.status not in (SessionStatus.CREATED, SessionStatus.RUNNING):
            raise TransitionError(
                f"session is not schedulable while {self._state.status.value}"
            )

    def _require_active_attempt(self) -> Attempt:
        if self._state.active_attempt is None:
            raise TransitionError("no active attempt")
        return self._state.active_attempt

    def _reconcile_interrupted_model_calls(self) -> None:
        """Settle model reservations left active by a crash or unexpected failure."""
        for reservation in interrupted_model_reservations(self._state):
            operation = next(
                item
                for item in self._state.model_operations
                if item.request_id == reservation.operation_id
            )
            measurement = UsageMeasurement(
                measurement_id=f"{reservation.operation_id}-interrupted",
                model_calls=reservation.model_calls,
            )
            supervisor = operation.request_id.startswith(SUPERVISOR_REQUEST_PREFIX)
            fields: dict[str, object] = {
                "request_id": operation.request_id,
                "reservation": reservation,
                "error": (
                    "interrupted supervisor call"
                    if supervisor
                    else "interrupted model call"
                ),
                "input": operation.input_text or "",
                "usage": measurement,
                "cursor": operation.cursor,
            }
            if not supervisor:
                fields.update(
                    {
                        "output": "",
                        "attempts": (),
                        "duration_ms": None,
                    }
                )
                if operation.manifest is not None:
                    fields["manifest"] = operation.manifest
                if operation.policy_version is not None:
                    fields["policy_version"] = operation.policy_version
                if operation.token_counter is not None:
                    fields["token_counter"] = operation.token_counter
                if operation.model_id is not None:
                    fields["model_id"] = operation.model_id
            self._persist(
                "supervisor_failed" if supervisor else "model_malformed",
                settle_model_call(self._state, reservation, measurement),
                fields,
            )

    def _bind_model_configuration(
        self, model: Model, token_budget: int
    ) -> ModelConfiguration:
        """Bind and verify all Phase-2 model settings before model work."""
        configuration = configuration_for(model, token_budget)
        events = self._journal_events()
        bound = next(
            (event for event in events if event.kind == "model_configuration_bound"),
            None,
        )
        if bound is None:
            self._persist(
                "model_configuration_bound",
                self._state,
                {"configuration": configuration},
            )
            return configuration
        durable = bound.payload.get("configuration")
        if not isinstance(durable, Mapping) or canonical_json(
            durable
        ) != canonical_json(configuration):
            raise ContractError("model configuration differs from durable binding")
        return configuration

    def _model_record(
        self,
        request: DecisionInput,
        reservation: ResourceReservation,
        context_manifest: object,
        visible_input: str,
        *,
        cursor: int | None = None,
        **fields: object,
    ) -> dict[str, object]:
        # A terminal replays at the intent's own cursor even when durable
        # controls were admitted while the blocking model call ran.
        intent_cursor = self._cursor if cursor is None else cursor
        payload: dict[str, object] = {
            "request_id": request.request_id,
            "reservation": reservation,
            "input": visible_input,
            "manifest": context_manifest,
            "policy_version": request.policy_version,
            "token_counter": request.token_counter,
            "model_id": request.model_id,
            "cursor": intent_cursor,
        }
        attempts = fields.get("attempts")
        if isinstance(attempts, tuple):
            fields = dict(fields)
            fields["attempts"] = tuple(
                replace(
                    attempt,
                    error=(
                        None
                        if attempt.error is None
                        else safe_provider_error(attempt.error)
                    ),
                )
                for attempt in attempts
            )
        payload.update(fields)
        # Durable identity comes from the pre-call request, never provider
        # metadata or a caller-supplied terminal field.
        payload["model_id"] = request.model_id
        payload["cursor"] = intent_cursor
        payload["output"] = bounded_model_output(payload.get("output", ""))
        payload["finish_reason"] = (
            bounded_model_output(payload.get("finish_reason", "error"))[:128] or "error"
        )
        payload["duration_ms"] = bounded_model_duration(payload.get("duration_ms"))
        payload["attempts"] = bounded_model_attempts(payload.get("attempts", ()))
        if not isinstance(payload.get("usage"), UsageMeasurement):
            payload["usage"] = None
        return payload

    def _receipt_artifact_content(self, events: tuple[Event, ...]) -> JsonValue | None:
        """Decode execution-receipt or initial-observation bytes into content."""
        receipt = latest_execution_receipt_payload(events)
        raw = receipt.get("raw_artifact") if receipt else None
        if raw is None:
            observation = self._state.initial_observation
            if observation is None or observation.raw_record is None:
                return None
            record = observation.raw_record
            return self._decode_artifact(
                record.digest, record.size, record.media_type, record.provenance
            )
        if not isinstance(raw, Mapping):
            return None
        digest = raw.get("digest")
        size = raw.get("size")
        media_type = raw.get("media_type")
        provenance = raw.get("provenance")
        if (
            not isinstance(digest, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(media_type, str)
            or not isinstance(provenance, str)
        ):
            return None
        return self._decode_artifact(digest, size, media_type, provenance)

    def _receipt_content_map(
        self, events: tuple[Event, ...]
    ) -> dict[str, ReceiptExcerpt]:
        """Pre-resolve bounded stdout excerpts and digests (ADR-0012).

        The most recent successful evidence-bearing receipts supply Recent-band
        excerpt content and the ADR-0008 normalized digests used for repeat
        collapsing. Reads stay journaled; receipts beyond the bound or without
        a readable artifact render without excerpt content.
        """
        content: dict[str, ReceiptExcerpt] = {}
        for event in reversed(events):
            if len(content) >= RECEIPT_CONTENT_MAX_RECEIPTS:
                break
            if event.kind != "receipt_received":
                continue
            payload = event.payload.get("receipt")
            if not isinstance(payload, Mapping):
                continue
            if payload.get("status") not in _CONTENT_RECEIPT_STATUSES:
                continue
            raw = payload.get("raw_artifact")
            if not isinstance(raw, Mapping):
                continue
            digest = raw.get("digest")
            size = raw.get("size")
            media_type = raw.get("media_type")
            provenance = raw.get("provenance")
            if (
                not isinstance(digest, str)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or not isinstance(media_type, str)
                or not isinstance(provenance, str)
            ):
                continue
            data: bytes | None = None
            try:
                data = self._read_artifact(
                    ArtifactRef(digest, size, media_type, provenance),
                    "context_receipt_content",
                )
                fingerprints = stdout_fingerprints(data, self._artifacts)
            except (ContractError, IntegrityError):
                # Excerpt content is an optional enhancement: an unreadable
                # artifact renders without an excerpt instead of blocking the
                # decision. The digest link on the rendered receipt stays.
                continue
            normalized = fingerprints[1] if len(fingerprints) > 1 else None
            content[str(payload.get("receipt_id"))] = ReceiptExcerpt(
                _bounded_stdout_excerpt(data), normalized
            )
        return content

    def _decode_artifact(
        self, digest: str, size: int, media_type: str, provenance: str
    ) -> JsonValue | None:
        data = self._read_artifact(
            ArtifactRef(digest, size, media_type, provenance),
            "context_observation",
        )
        try:
            parsed = parse_json(data.decode("utf-8"))
        except (UnicodeDecodeError, ContractError):
            return data.decode("utf-8", errors="replace")
        if parsed is None or isinstance(parsed, (str, int, float, bool)):
            return parsed
        if isinstance(parsed, list):
            return tuple(parsed)
        if isinstance(parsed, dict):
            return frozen_mapping(parsed)
        return data.decode("utf-8", errors="replace")

    def _read_artifact(self, reference: ArtifactRef, purpose: str) -> bytes:
        """Record one successful operational read within this session's scope."""
        measured = self._artifacts.read_measured(reference)
        self._persist(
            "artifact_read",
            self._state,
            {
                "scope": OPERATIONAL_MEASUREMENT_SCOPE,
                "purpose": purpose,
                "digest": reference.digest,
                "bytes_read": measured.bytes_read,
            },
        )
        return measured.data


def _model_call_bound(model: Model) -> int:
    """Reserve the first call plus the model's declared repair bound."""
    repair_budget = getattr(model, "repair_budget", 0)
    if not isinstance(repair_budget, int) or isinstance(repair_budget, bool):
        return 1
    if repair_budget < 0:
        return 1
    return 1 + repair_budget


def _remaining_model_calls(budgets: Mapping[str, object]) -> int | None:
    """Return the finite remaining model-call allowance, if available."""
    model_budget = budgets.get("model_calls")
    remaining = (
        model_budget.get("remaining") if isinstance(model_budget, Mapping) else None
    )
    if isinstance(remaining, bool) or not isinstance(remaining, int):
        return None
    return remaining


def _step_budget_preflight(
    budgets: Mapping[str, object], model: Model, supervised: bool
) -> tuple[int | None, int, bool]:
    """Reject exhausted work and reserve capacity for decisions and supervision."""
    if _budget_exhausted(budgets.get("actions")) and _budget_exhausted(
        budgets.get("evaluations")
    ):
        # ADR-0024: no dispatchable work remains, a budget outcome like any other.
        raise BudgetExhausted("action and evaluation")
    remaining = _remaining_model_calls(budgets)
    bound = _model_call_bound(model)
    if not supervised and not _model_call_budget_covers(remaining, bound):
        raise BudgetExhausted("model call")
    allow_consult = not supervised or _model_call_budget_covers(remaining, bound + 1)
    return remaining, bound, allow_consult


def _model_call_budget_covers(remaining: int | None, required: int) -> bool:
    """Return whether the finite call budget covers the requested reservation."""
    return remaining is None or remaining >= required


def _budget_exhausted(value: object) -> bool:
    """Return whether a finite action/evaluation budget has no capacity left."""
    if not isinstance(value, Mapping):
        return False
    remaining = value.get("remaining")
    return (
        isinstance(remaining, int)
        and not isinstance(remaining, bool)
        and remaining <= 0
    )


def _projected_input_tokens(events: tuple[Event, ...], text: str) -> int | None:
    """Project input tokens from provider-reported usage, or None without one.

    ADR-0016: the runtime cannot know a provider tokenizer before the first
    report, so the recorded serialized count keeps its existing admission role
    until a provider reports. From then on the worst observed
    tokens-per-character ratio projects the candidate request, and an
    over-budget projection is refused rather than sent.
    """
    ratio = _observed_tokens_per_char(events)
    return None if ratio is None else math.ceil(len(text) * ratio)


def _observed_tokens_per_char(events: tuple[Event, ...]) -> float | None:
    """The worst provider tokens-per-character ratio in the journal, or None."""
    intents: dict[str, int] = {}
    ratios: list[float] = []
    for event in events:
        if event.kind == "model_intended":
            request_id = event.payload.get("request_id")
            input_text = event.payload.get("input")
            if isinstance(request_id, str) and isinstance(input_text, str):
                intents[request_id] = len(input_text)
        elif event.kind in ("model_decided", "model_malformed"):
            request_id = event.payload.get("request_id")
            chars = intents.get(request_id) if isinstance(request_id, str) else None
            tokens = _provider_prompt_tokens(event.payload)
            if chars and tokens:
                ratios.append(tokens / chars)
    return max(ratios) if ratios else None


def _provider_prompt_tokens(payload: Mapping[str, object]) -> int | None:
    """Return the worst provider-reported prompt count carried by a terminal."""
    value = payload.get("input_tokens")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        value = usage.get("input_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    attempts = payload.get("attempts")
    if isinstance(attempts, (list, tuple)):
        reported = [
            attempt.get("prompt_tokens")
            for attempt in attempts
            if isinstance(attempt, Mapping)
        ]
        numbers = [
            item
            for item in reported
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        ]
        if numbers:
            return max(numbers)
    return None


def _stable_request(
    make_request: Callable[[ContextBundle], DecisionInput],
    assembled: ContextBundle,
) -> tuple[DecisionInput, str, ContextManifest]:
    """Render the request until the manifest's token count describes it.

    The context manifest is part of the visible request, so writing its own
    ``serialized_input_tokens`` changes the serialization. Re-render until the
    recorded count matches the dispatched text (ADR-0016); the count converges
    within one or two passes because only the digit width of that field changes.
    """
    context_manifest = assembled.manifest
    request = make_request(assembled)
    text = render_request(request)
    tokens_per_char = context_manifest.estimate_tokens_per_char
    for _ in range(3):
        estimated = estimate_serialized_tokens(text, tokens_per_char)
        if estimated == context_manifest.estimated_input_tokens:
            return request, text, context_manifest
        context_manifest = replace(
            context_manifest,
            estimated_input_tokens=estimated,
        )
        request = make_request(
            ContextBundle(assembled.items, context_manifest, assembled.text)
        )
        text = render_request(request)
    return request, text, context_manifest


def _token_breach_payload(
    handle: DecisionHandle, response: ModelResponse
) -> Mapping[str, int | str] | None:
    """Return the provider-usage breach payload, or None when the call fit.

    ADR-0016: the admission estimate is conservative, but a provider report can
    still exceed the declared window. The worst reported prompt across the
    decision's calls is compared with the reserved output and the budget; the
    result is journaled, never retried automatically and never silently passed.
    """
    context_manifest = handle.manifest
    reported = [
        attempt.prompt_tokens
        for attempt in response.attempts
        if attempt.prompt_tokens is not None
    ]
    provider_input = max(reported) if reported else None
    if provider_input is None and response.usage is not None:
        provider_input = response.usage.input_tokens
    if provider_input is None:
        return None
    # COD-88: a grown answer limit is what the call actually reserved.
    requested = [
        attempt.output_limit
        for attempt in response.attempts
        if attempt.output_limit is not None
    ]
    reserved = max(requested, default=context_manifest.reserved_output_tokens or 0)
    if provider_input + reserved <= context_manifest.token_budget:
        return None
    return {
        "request_id": handle.request_id,
        "estimated_input_tokens": context_manifest.estimated_input_tokens or 0,
        "provider_input_tokens": provider_input,
        "reserved_output_tokens": reserved,
        "token_budget": context_manifest.token_budget,
    }


def _model_response_identity(response: ModelResponse) -> str:
    """Hash the full typed response and measured accounting for deduplication."""
    output = response.output.encode("utf-8")
    identity = {
        "result": response.result,
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "output_bytes": len(output),
        "model_id": response.model_id,
        "finish_reason": response.finish_reason,
        "usage": response.usage,
        "attempts": response.attempts,
        "duration_ms": response.duration_ms,
        "repairs": [dict(note) for note in response.repairs],
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def _application_rejection_reason(error: ContractError | TransitionError) -> str:
    """Return a bounded reason without retaining caller-controlled error text."""
    if isinstance(error, TransitionError) and "budget exhausted" in str(error):
        return "resource budget exhausted"
    if isinstance(error, ContractError):
        return "invalid model result"
    return "model result cannot be applied"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _all_events(journal: SqliteJournal, session_id: str) -> tuple[Event, ...]:
    """Read the complete journal without changing the bounded public API."""
    return _events_after(journal, session_id, 0)


def _events_after(
    journal: SqliteJournal, session_id: str, after_sequence: int
) -> tuple[Event, ...]:
    """Read every event after ``after_sequence``, page by page."""
    events: list[Event] = []
    cursor = after_sequence
    while True:
        page = journal.events(session_id, after_sequence=cursor, limit=100)
        if not page:
            return tuple(events)
        events.extend(page)
        cursor = page[-1].sequence


def _validate_creation_input_envelopes(
    *,
    session_id: str,
    objective_version: int,
    observation: Observation,
    knowledge_sources: tuple[KnowledgeSource, ...],
    evaluation_contract: EvaluationContract,
) -> None:
    """Require durable provenance for each input that can affect session work."""
    envelopes = (
        observation.envelope,
        *(source.envelope for source in knowledge_sources),
        evaluation_contract.envelope,
    )
    record_ids = (
        observation.observation_id,
        *(source.source_id for source in knowledge_sources),
        evaluation_contract.contract_id,
    )
    if len(set(record_ids)) != len(record_ids) or any(
        envelope.record_id != record_id
        for envelope, record_id in zip(envelopes, record_ids)
    ):
        raise ContractError(
            "creation input record and envelope identities must be unique"
        )
    if any(
        envelope.recorded_at != timestamp or envelope.provenance != provenance
        for envelope, timestamp, provenance in (
            (observation.envelope, observation.observed_at, observation.provenance),
            *(
                (source.envelope, source.observed_at, source.provenance)
                for source in knowledge_sources
            ),
        )
    ):
        raise ContractError("creation input envelope timestamp or provenance differs")
    if any(
        item.session_id != session_id or item.objective_version != objective_version
        for item in envelopes
    ):
        raise ContractError("creation input envelope does not belong to this session")


def _verify_open_dependencies(
    creation: Event,
    *,
    task: TaskSpec,
    environment: Environment,
    runtime: Runtime,
    evaluator: Evaluator,
    run_identity: RunIdentity | None,
) -> None:
    """Reject a reopen whose supplied dependencies differ from durable facts."""
    persisted_contract = creation.payload.get("environment")
    if not isinstance(persisted_contract, Mapping):
        raise IntegrityError("session creation lacks an environment contract")
    supplied_contract = to_json_value(environment.describe())
    if canonical_json(persisted_contract) != canonical_json(supplied_contract):
        raise ContractError("reopen environment contract differs from durable creation")
    expected = creation.payload.get("run_identity")
    supplied = run_identity or _default_run_identity(
        task, environment.describe(), runtime, evaluator
    )
    if (
        supplied.runtime != runtime.identity()
        or supplied.evaluator != evaluator.identity()
    ):
        raise ContractError("reopen identity does not match supplied connector")
    if not isinstance(expected, Mapping) or canonical_json(expected) != canonical_json(
        supplied
    ):
        raise ContractError(
            "reopen runtime or evaluator identity differs from creation"
        )


def _default_run_identity(
    task: TaskSpec,
    contract: EnvironmentContract,
    runtime: Runtime,
    evaluator: Evaluator,
) -> RunIdentity:
    """Freeze a transparent fallback identity for connectors without one."""
    configuration = canonical_json({"task": task, "environment": contract})
    return RunIdentity(
        package_version=VERSION,
        configuration_digest=hashlib.sha256(configuration.encode("utf-8")).hexdigest(),
        runtime=runtime.identity(),
        evaluator=evaluator.identity(),
    )


def _validate_action_arguments(  # pylint: disable=too-many-statements
    arguments: Mapping[str, object], schema: Mapping[str, object]
) -> None:
    """Validate the small object-schema subset advertised by Phase 1 actions."""
    if not isinstance(arguments, Mapping) or not all(
        isinstance(key, str) for key in arguments
    ):
        raise ContractError("action arguments must be a string-keyed object")
    required = schema.get("required", ())
    if not isinstance(required, (tuple, list)) or not all(
        isinstance(key, str) for key in required
    ):
        raise ContractError("action schema required must be an array of strings")
    absent = set(required) - set(arguments)
    if absent:
        raise ContractError(f"action arguments omit required fields: {sorted(absent)}")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ContractError("action schema properties must be an object")
    unsupported = {
        key
        for key in schema
        if key not in {"type", "required", "properties", "additionalProperties"}
    }
    if schema.get("type", "object") != "object" or unsupported:
        raise ContractError("action schema uses unsupported constructs")
    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, bool):
        raise ContractError("action schema additionalProperties must be a boolean")
    if not additional and set(arguments) - set(properties):
        raise ContractError("action arguments include undeclared fields")
    for name, property_schema in properties.items():
        if not isinstance(name, str) or not isinstance(property_schema, Mapping):
            raise ContractError("action schema property must be a named object")
        declared_type = property_schema.get("type")
        if declared_type not in ("string", "integer", "boolean"):
            raise ContractError(f"action schema has unsupported type for {name!r}")
        allowed = {"type"}
        if declared_type == "integer":
            allowed = {"type", "minimum", "maximum"}
        if declared_type == "string":
            allowed = {"type", "maxLength"}
        if set(property_schema) - allowed:
            raise ContractError(
                f"action schema property {name!r} uses unsupported constructs"
            )
        for bound_name in ("minimum", "maximum", "maxLength"):
            if bound_name not in property_schema:
                continue
            bound = property_schema[bound_name]
            if isinstance(bound, bool) or not isinstance(bound, int):
                raise ContractError(
                    f"action schema property {name!r} {bound_name} must be an integer"
                )
    for name, value in arguments.items():
        property_schema = properties.get(name)
        if not isinstance(property_schema, Mapping):
            continue
        declared_type = property_schema.get("type")
        if declared_type == "string" and not isinstance(value, str):
            raise ContractError(f"action argument {name!r} must be a string")
        if declared_type == "integer" and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise ContractError(f"action argument {name!r} must be an integer")
        if declared_type == "boolean" and not isinstance(value, bool):
            raise ContractError(f"action argument {name!r} must be a boolean")
        max_length = property_schema.get("maxLength")
        if (
            isinstance(value, str)
            and isinstance(max_length, int)
            and len(value) > max_length
        ):
            raise ContractError(
                f"action argument {name!r} must be at most {max_length} characters"
            )
        if declared_type != "integer":
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        minimum = property_schema.get("minimum")
        maximum = property_schema.get("maximum")
        if (
            isinstance(minimum, int)
            and not isinstance(minimum, bool)
            and value < minimum
        ):
            raise ContractError(f"action argument {name!r} must be >= {minimum}")
        if (
            isinstance(maximum, int)
            and not isinstance(maximum, bool)
            and value > maximum
        ):
            raise ContractError(f"action argument {name!r} must be <= {maximum}")


def _attempt_started_payload(state: SessionState) -> dict[str, object]:
    """``attempt_started`` payload; the marker tells replay which rule applies."""
    return {
        "attempt": state.active_attempt,
        "starting_state": OBSERVED_STARTING_STATE,
    }
