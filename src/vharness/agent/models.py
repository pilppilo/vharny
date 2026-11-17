"""Immutable domain records for durable agent sessions."""

# pylint: disable=too-many-instance-attributes
# Immutable boundary records intentionally name every persisted contract field.

from __future__ import annotations

from collections.abc import Mapping

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeAlias, TypedDict

from .errors import ContractError

if TYPE_CHECKING:  # pragma: no cover - type-only import avoids a supervision cycle
    from .supervision import SupervisorDigest

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = (
    JsonPrimitive | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)

SCHEMA_VERSION = 1

# Supervisor model calls are reserved and measured through the same ledger as
# main model calls; their operation IDs carry this prefix so accounting can
# report the supervisor subset without a second ledger.
SUPERVISOR_REQUEST_PREFIX = "supervisor-"


def frozen_mapping(
    values: Mapping[str, JsonValue] | None = None,
) -> Mapping[str, JsonValue]:
    """Copy a JSON-shaped mapping so domain records never share caller mutation."""
    copied = {key: _freeze_json(value) for key, value in (values or {}).items()}
    return MappingProxyType(copied)


def _freeze_json(value: JsonValue) -> JsonValue:
    """Recursively copy JSON data accepted at an immutable domain boundary."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-empty string")


class CompletionMode(str, Enum):
    """Whether external evidence may complete the current objective."""

    FINITE = "finite"
    ONGOING = "ongoing"


class EvaluationMode(str, Enum):
    """Typed run setting selecting how evaluation requests are answered.

    ``EXTERNAL_CHECK`` dispatches each request to the configured external
    checker. ``OPERATOR`` persists the request and waits for a durable
    ``EvaluationDecision``; it is selected explicitly, never inferred from
    prose such as an acceptance-semantics string.
    """

    EXTERNAL_CHECK = "external-check"
    OPERATOR = "operator-evaluation"


class SessionStatus(str, Enum):
    """The local scheduler lifecycle of one durable session."""

    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


OPERATOR_ANSWER_WAIT = "operator answer"

# A no-checker run exposes an operator evaluation request; the session waits
# under this reason until the operator answers with an EvaluationDecision.
OPERATOR_EVALUATION_WAIT = "operator evaluation"

# Automatic provider recovery ended (retries exhausted, or an operator-action
# failure such as authentication): the session waits without burning calls
# until the operator fixes availability and resumes, steers, or stops.
PROVIDER_RECOVERY_WAIT = "provider recovery"


class ExecutionStatus(str, Enum):
    """The latest externally observed operation status."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


class AttemptDisposition(str, Enum):
    """The durable outcome of one internal search trajectory."""

    ACTIVE = "active"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ABANDONED = "abandoned"
    STALE = "stale"
    INCOMPARABLE = "incomparable"
    INVALID = "invalid"
    HARD_CONSTRAINT_FAILED = "hard_constraint_failed"


class PromotionDisposition(str, Enum):
    """Whether an evaluation applies to the current committed lineage."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCOMPARABLE = "incomparable"
    STALE = "stale"
    INVALID = "invalid"
    HARD_CONSTRAINT_FAILED = "hard_constraint_failed"


class ObjectiveDirection(str, Enum):
    """The external evaluator's declared preference for one named objective."""

    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


@dataclass(frozen=True, slots=True)
class EvaluationObjective:
    """One named external objective and its comparison direction."""

    name: str
    direction: ObjectiveDirection

    def __post_init__(self) -> None:
        _nonempty(self.name, "evaluation objective name")


@dataclass(frozen=True, slots=True)
class RecordEnvelope:
    """Common durable identity and provenance for externally supplied records."""

    record_id: str
    session_id: str
    objective_version: int
    recorded_at: str
    provenance: str
    causation_id: str | None = None
    correlation_id: str | None = None
    schema_version: int = SCHEMA_VERSION
    extensions: Mapping[str, JsonValue] = field(default_factory=frozen_mapping)

    def __post_init__(self) -> None:
        for name, value in (
            ("record ID", self.record_id),
            ("record session ID", self.session_id),
            ("record timestamp", self.recorded_at),
            ("record provenance", self.provenance),
        ):
            _nonempty(value, name)
        if self.objective_version < 1:
            raise ContractError("record objective_version must be positive")
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("unsupported record schema_version")
        object.__setattr__(self, "extensions", frozen_mapping(self.extensions))


@dataclass(frozen=True, slots=True)
class ComponentIdentity:
    """Versioned identity of an external connector used for one run."""

    component_id: str
    version: str
    configuration_digest: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.component_id, "component identity")
        _nonempty(self.version, "component version")
        if self.configuration_digest is not None and (
            len(self.configuration_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.configuration_digest)
        ):
            raise ContractError(
                "component configuration_digest must be a SHA-256 digest"
            )


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Versioned identities frozen before a session records external work."""

    package_version: str
    configuration_digest: str
    runtime: ComponentIdentity
    evaluator: ComponentIdentity

    def __post_init__(self) -> None:
        _nonempty(self.package_version, "package version")
        if len(self.configuration_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.configuration_digest
        ):
            raise ContractError("run configuration_digest must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class EvaluationContract:
    """The externally owned evaluator contract selected for a task."""

    contract_id: str
    evaluator: ComponentIdentity
    envelope: RecordEnvelope
    objectives: tuple[EvaluationObjective, ...] = ()
    hard_constraints: tuple[str, ...] = ()
    acceptance_semantics: str = "external evaluator judgment"
    required_evidence: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _nonempty(self.contract_id, "evaluation contract ID")
        _nonempty(self.acceptance_semantics, "evaluation acceptance semantics")
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("unsupported evaluation contract schema_version")
        if len({item.name for item in self.objectives}) != len(self.objectives):
            raise ContractError("evaluation objective names must be unique")
        if len(set(self.hard_constraints)) != len(self.hard_constraints) or not all(
            isinstance(item, str) and item for item in self.hard_constraints
        ):
            raise ContractError(
                "evaluation hard constraints must be unique non-empty strings"
            )
        if len(set(self.required_evidence)) != len(self.required_evidence) or not all(
            isinstance(item, str) and item for item in self.required_evidence
        ):
            raise ContractError("evaluation contract evidence rules must be non-empty")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A verified immutable object held by the local artifact store."""

    digest: str
    size: int
    media_type: str
    provenance: str

    def __post_init__(self) -> None:
        if len(self.digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.digest
        ):
            raise ContractError(
                "artifact digest must be a lowercase SHA-256 hex digest"
            )
        if self.size < 0:
            raise ContractError("artifact size must not be negative")
        _nonempty(self.media_type, "artifact media_type")
        _nonempty(self.provenance, "artifact provenance")


@dataclass(frozen=True, slots=True)
class Observation:
    """One externally supplied observation retained with its provenance."""

    observation_id: str
    state: StateRef
    observed_at: str
    provenance: str
    envelope: RecordEnvelope
    raw_record: ArtifactRef | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("observation ID", self.observation_id),
            ("observation timestamp", self.observed_at),
            ("observation provenance", self.provenance),
        ):
            _nonempty(value, name)
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("unsupported observation schema_version")


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    """A versioned, externally attributable knowledge input."""

    source_id: str
    provenance: str
    observed_at: str
    envelope: RecordEnvelope
    artifact: ArtifactRef | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("knowledge source ID", self.source_id),
            ("knowledge source provenance", self.provenance),
            ("knowledge source timestamp", self.observed_at),
        ):
            _nonempty(value, name)
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("unsupported knowledge source schema_version")


@dataclass(frozen=True, slots=True)
class StateRef:
    """An opaque externally owned state or trajectory cursor."""

    owner: str
    value: str
    digest: str | None = None
    revision: str | None = None
    restorable: bool = False

    def __post_init__(self) -> None:
        _nonempty(self.owner, "state owner")
        _nonempty(self.value, "state value")
        if self.digest is not None and (
            len(self.digest) != 64
            or any(char not in "0123456789abcdef" for char in self.digest)
        ):
            raise ContractError("state digest must be a SHA-256 digest when supplied")
        if not isinstance(self.restorable, bool):
            raise ContractError("state restorable must be a boolean")


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Scheduling limits. None means the configured limit is not bounded."""

    actions: int | None = None
    model_calls: int | None = None
    evaluations: int | None = None
    cost: Decimal | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("actions", self.actions),
            ("model_calls", self.model_calls),
            ("evaluations", self.evaluations),
        ):
            if value is not None and value < 0:
                raise ContractError(f"budget {name} must not be negative")
        if self.cost is not None:
            if not isinstance(self.cost, Decimal) or not self.cost.is_finite():
                raise ContractError("budget cost must be a finite Decimal")
            if self.cost < 0:
                raise ContractError("budget cost must not be negative")


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Task intent and the current operator-controlled objective version."""

    task_id: str
    objective: str
    completion_mode: CompletionMode
    success_evidence: str
    budgets: BudgetLimits
    environment_id: str
    evaluation_contract_id: str
    objective_version: int = 1
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("task_id", self.task_id),
            ("objective", self.objective),
            ("success_evidence", self.success_evidence),
            ("environment_id", self.environment_id),
            ("evaluation_contract_id", self.evaluation_contract_id),
        ):
            _nonempty(value, name)
        if self.objective_version < 1:
            raise ContractError("objective_version must be positive")

    def steered(self, objective: str, success_evidence: str) -> "TaskSpec":
        """Return the next durable objective without mutating historical intent."""
        return TaskSpec(
            task_id=self.task_id,
            objective=objective,
            completion_mode=self.completion_mode,
            success_evidence=success_evidence,
            budgets=self.budgets,
            environment_id=self.environment_id,
            evaluation_contract_id=self.evaluation_contract_id,
            objective_version=self.objective_version + 1,
            constraints=self.constraints,
        )


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Trusted connector claims; model output cannot expand these capabilities."""

    cancellation: bool = False
    idempotency: bool = False
    reconciliation: bool = False
    revisions: bool = False


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    """One action declared by an external environment contract."""

    name: str
    description: str
    argument_schema: Mapping[str, JsonValue] = field(default_factory=frozen_mapping)

    def __post_init__(self) -> None:
        _nonempty(self.name, "action name")
        _nonempty(self.description, "action description")
        object.__setattr__(
            self, "argument_schema", frozen_mapping(self.argument_schema)
        )


@dataclass(frozen=True, slots=True)
class EnvironmentContract:
    """Mechanical environment state and declared action surface."""

    environment_id: str
    version: str
    initial_state: StateRef
    actions: tuple[ActionDefinition, ...]
    capabilities: RuntimeCapabilities

    def __post_init__(self) -> None:
        _nonempty(self.environment_id, "environment_id")
        _nonempty(self.version, "environment version")
        names = [action.name for action in self.actions]
        if len(names) != len(set(names)):
            raise ContractError("environment action names must be unique")


@dataclass(frozen=True, slots=True)
class UsageMeasurement:
    """Measured resource use. Missing cost remains unknown, never zero by default."""

    measurement_id: str
    actions: int | None = 0
    model_calls: int | None = 0
    evaluations: int | None = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    cost: Decimal | None = None

    def __post_init__(self) -> None:
        _nonempty(self.measurement_id, "measurement_id")
        for name, value in (
            ("actions", self.actions),
            ("model_calls", self.model_calls),
            ("evaluations", self.evaluations),
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("duration_ms", self.duration_ms),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise ContractError(f"measured {name} must be an integer or null")
            if value is not None and value < 0:
                raise ContractError(f"measured {name} must not be negative")
        if self.cost is not None:
            if not isinstance(self.cost, Decimal) or not self.cost.is_finite():
                raise ContractError("measured cost must be a finite Decimal")
            if self.cost < 0:
                raise ContractError("measured cost must not be negative")


@dataclass(frozen=True, slots=True)
class ResourceReservation:
    """Declared bound recorded before a state-changing external request."""

    reservation_id: str
    operation_id: str
    objective_version: int
    actions: int = 0
    evaluations: int = 0
    cost: Decimal | None = None
    model_calls: int = 0

    def __post_init__(self) -> None:
        _nonempty(self.reservation_id, "reservation_id")
        _nonempty(self.operation_id, "operation_id")
        if (
            self.objective_version < 1
            or self.actions < 0
            or self.evaluations < 0
            or self.model_calls < 0
        ):
            raise ContractError("reservation values must be non-negative")
        if self.cost is not None:
            if not isinstance(self.cost, Decimal) or not self.cost.is_finite():
                raise ContractError("reserved cost must be a finite Decimal")
            if self.cost < 0:
                raise ContractError("reserved cost must not be negative")


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """A typed proposed effect to send to the separately controlled runtime."""

    proposal_id: str
    session_id: str
    objective_version: int
    attempt_id: str
    action_name: str
    arguments: Mapping[str, JsonValue]
    expected_state: StateRef
    reservation: ResourceReservation
    rationale: str
    idempotency_key: str | None = None
    requires_revision: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("proposal_id", self.proposal_id),
            ("session_id", self.session_id),
            ("attempt_id", self.attempt_id),
            ("action_name", self.action_name),
            ("rationale", self.rationale),
        ):
            _nonempty(value, name)
        if self.objective_version < 1:
            raise ContractError("proposal objective_version must be positive")
        if not isinstance(self.requires_revision, bool):
            raise ContractError("proposal requires_revision must be a boolean")
        if self.reservation.operation_id != self.proposal_id:
            raise ContractError("reservation operation_id must equal proposal_id")
        object.__setattr__(self, "arguments", frozen_mapping(self.arguments))


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """An immutable observation from the runtime, including nonterminal status."""

    receipt_id: str
    proposal_id: str
    objective_version: int
    status: ExecutionStatus
    operation_id: str | None = None
    state: StateRef | None = None
    error: str | None = None
    usage: UsageMeasurement | None = None
    raw_artifact: ArtifactRef | None = None

    def __post_init__(self) -> None:
        _nonempty(self.receipt_id, "receipt_id")
        _nonempty(self.proposal_id, "proposal_id")
        if self.objective_version < 1:
            raise ContractError("receipt objective_version must be positive")


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    """An external evaluation request, anchored to a state and lineage head."""

    request_id: str
    session_id: str
    objective_version: int
    attempt_id: str
    evaluated_state: StateRef
    baseline_node_id: str
    evaluator_version: str

    def __post_init__(self) -> None:
        for name, value in (
            ("request_id", self.request_id),
            ("session_id", self.session_id),
            ("attempt_id", self.attempt_id),
            ("baseline_node_id", self.baseline_node_id),
            ("evaluator_version", self.evaluator_version),
        ):
            _nonempty(value, name)
        if self.objective_version < 1:
            raise ContractError("evaluation objective version must be positive")

    def receipt_identity(self) -> "ReceiptIdentity":
        """Return the request fields every evaluation receipt must copy exactly."""
        return ReceiptIdentity(
            request_id=self.request_id,
            objective_version=self.objective_version,
            evaluated_state=self.evaluated_state,
            baseline_node_id=self.baseline_node_id,
            evaluator_version=self.evaluator_version,
        )


class ReceiptIdentity(TypedDict):
    """The request identity an evaluation receipt copies verbatim."""

    request_id: str
    objective_version: int
    evaluated_state: StateRef
    baseline_node_id: str
    evaluator_version: str


@dataclass(frozen=True, slots=True)
class EvaluationReceipt:
    """Native external evaluation result; Vharness does not rescore it."""

    receipt_id: str
    request_id: str
    objective_version: int
    evaluated_state: StateRef
    baseline_node_id: str
    evaluator_version: str
    accepted: bool
    comparison: str
    objectives: Mapping[str, JsonValue] = field(default_factory=frozen_mapping)
    evidence: tuple[ArtifactRef, ...] = ()
    valid: bool = True
    hard_constraints: Mapping[str, bool] = field(default_factory=dict)
    completed: bool = False
    completion_evidence: tuple[ArtifactRef, ...] = ()
    required_evidence: tuple[str, ...] = ()
    usage: UsageMeasurement | None = None
    raw_receipt: ArtifactRef | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("receipt_id", self.receipt_id),
            ("request_id", self.request_id),
            ("baseline_node_id", self.baseline_node_id),
            ("evaluator_version", self.evaluator_version),
            ("comparison", self.comparison),
        ):
            _nonempty(value, name)
        if self.objective_version < 1:
            raise ContractError("evaluation receipt objective_version must be positive")
        if not isinstance(self.valid, bool) or not isinstance(self.completed, bool):
            raise ContractError("evaluation validity and completion must be booleans")
        if not all(isinstance(value, bool) for value in self.hard_constraints.values()):
            raise ContractError("hard constraint outcomes must be booleans")
        if not all(isinstance(item, str) and item for item in self.required_evidence):
            raise ContractError("required evidence rules must be non-empty strings")
        object.__setattr__(self, "objectives", frozen_mapping(self.objectives))
        object.__setattr__(
            self, "hard_constraints", MappingProxyType(dict(self.hard_constraints))
        )


@dataclass(frozen=True, slots=True)
class PendingDispatch:
    """One blocking external call the coordinator must run outside its writer.

    The coordinator persists the intent, hands this descriptor to a bounded
    worker, and applies the returned result through ``receive_receipt``,
    ``receive_evaluation``, or ``receive_guidance``. The session never executes
    the port call itself when a decision is applied with ``dispatch=False`` or
    when a supervision consult is deferred.
    """

    kind: str
    proposal: ActionProposal | None = None
    request: EvaluationRequest | None = None
    digest: SupervisorDigest | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("action", "evaluation", "supervision"):
            raise ContractError(
                "pending dispatch kind must be action, evaluation, or supervision"
            )
        if self.kind == "action":
            if (
                self.proposal is None
                or self.request is not None
                or self.digest is not None
            ):
                raise ContractError("action dispatch requires only a proposal")
        elif self.kind == "evaluation":
            if (
                self.request is None
                or self.proposal is not None
                or self.digest is not None
            ):
                raise ContractError("evaluation dispatch requires only a request")
        elif (
            self.digest is None or self.proposal is not None or self.request is not None
        ):
            raise ContractError("supervision dispatch requires only a digest")


@dataclass(frozen=True, slots=True)
class Attempt:
    """One multi-action search trajectory based on a committed lineage state."""

    attempt_id: str
    objective_version: int
    base_node_id: str
    starting_state: StateRef
    disposition: AttemptDisposition = AttemptDisposition.ACTIVE
    result_state: StateRef | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("attempt_id", self.attempt_id),
            ("base_node_id", self.base_node_id),
        ):
            _nonempty(value, name)
        if self.objective_version < 1:
            raise ContractError("attempt objective_version must be positive")


@dataclass(frozen=True, slots=True)
class AttemptHistory:
    """Replay-derived durable history for one attempt, including unsuccessful work."""

    attempt: Attempt
    event_start_cursor: int
    event_end_cursor: int | None = None
    evaluation_request_ids: tuple[str, ...] = ()
    evaluation_receipt_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.event_start_cursor < 1:
            raise ContractError("attempt history start cursor must be positive")
        if (
            self.event_end_cursor is not None
            and self.event_end_cursor < self.event_start_cursor
        ):
            raise ContractError("attempt history end cursor precedes its start")


@dataclass(frozen=True, slots=True)
class CommittedNode:
    """A single-parent externally accepted state in the committed lineage."""

    node_id: str
    objective_version: int
    state: StateRef
    parent_id: str | None = None
    evaluation_receipt_id: str | None = None
    objectives: Mapping[str, JsonValue] = field(default_factory=frozen_mapping)

    def __post_init__(self) -> None:
        _nonempty(self.node_id, "node_id")
        if self.objective_version < 1:
            raise ContractError("node objective_version must be positive")
        object.__setattr__(self, "objectives", frozen_mapping(self.objectives))


@dataclass(frozen=True, slots=True)
class Event:
    """Canonical append-only session fact."""

    event_id: str
    session_id: str
    sequence: int
    kind: str
    objective_version: int
    payload: Mapping[str, JsonValue]
    recorded_at: str
    causation_id: str | None = None
    correlation_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("session_id", self.session_id),
            ("kind", self.kind),
            ("recorded_at", self.recorded_at),
        ):
            _nonempty(value, name)
        if self.sequence < 1 or self.objective_version < 1:
            raise ContractError("event sequence and objective_version must be positive")
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError("unsupported event schema_version")
        object.__setattr__(self, "payload", frozen_mapping(self.payload))


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A projection digest anchored to the journal cursor it represents."""

    checkpoint_id: str
    session_id: str
    objective_version: int
    cursor: int
    projection_digest: str
    pending_operation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("checkpoint_id", self.checkpoint_id),
            ("session_id", self.session_id),
            ("projection_digest", self.projection_digest),
        ):
            _nonempty(value, name)
        if self.objective_version < 1 or self.cursor < 0:
            raise ContractError("checkpoint objective_version and cursor are invalid")
        if len(self.projection_digest) != 64:
            raise ContractError("checkpoint projection_digest must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    """Base record for durable human interaction with one session."""

    command_id: str
    session_id: str

    def __post_init__(self) -> None:
        _nonempty(self.command_id, "command_id")
        _nonempty(self.session_id, "command session_id")


@dataclass(frozen=True, slots=True)
class Message(OperatorCommand):
    """A durable operator message that does not alter task acceptance."""

    text: str
    question_id: str | None = None

    def __post_init__(self) -> None:
        OperatorCommand.__post_init__(self)
        _nonempty(self.text, "message text")
        if self.question_id is not None:
            _nonempty(self.question_id, "message question_id")


@dataclass(frozen=True, slots=True)
class Steer(OperatorCommand):
    """An objective-changing command that creates a new objective version."""

    objective: str
    success_evidence: str

    def __post_init__(self) -> None:
        OperatorCommand.__post_init__(self)
        _nonempty(self.objective, "steer objective")
        _nonempty(self.success_evidence, "steer success_evidence")


@dataclass(frozen=True, slots=True)
class Pause(OperatorCommand):
    """Prevent new local work while retaining any external operation state."""


@dataclass(frozen=True, slots=True)
class Resume(OperatorCommand):
    """Resume local scheduling after a durable pause."""


@dataclass(frozen=True, slots=True)
class Stop(OperatorCommand):
    """Stop local scheduling without claiming external effects were undone."""


@dataclass(frozen=True, slots=True)
class CheckpointRequest(OperatorCommand):
    """Ask the coordinator to persist a checkpoint at the current cursor."""


@dataclass(frozen=True, slots=True)
class EvaluationCommand(OperatorCommand):
    """Ask the coordinator to request external evaluation of a supplied state."""

    state: StateRef


@dataclass(frozen=True, slots=True)
class EvaluationDecision(OperatorCommand):
    """The operator's durable answer to one evaluation request.

    Accepting a milestone records ``evidence``; accepting completion records
    ``completion_evidence``. A rejected decision never completes the objective.
    Receipt identity is derived from ``request_id`` plus the decision content,
    so replaying the same decision is a no-op.
    """

    request_id: str
    accepted: bool
    reason: str
    completed: bool = False
    evidence_ref: ArtifactRef | None = None

    def __post_init__(self) -> None:
        OperatorCommand.__post_init__(self)
        _nonempty(self.request_id, "evaluation decision request_id")
        _nonempty(self.reason, "evaluation decision reason")
        if not isinstance(self.accepted, bool) or not isinstance(self.completed, bool):
            raise ContractError("evaluation decision flags must be booleans")
        if not self.accepted and self.completed:
            raise ContractError("a rejected evaluation decision cannot complete")
        if self.accepted and self.evidence_ref is None:
            raise ContractError("an accepted evaluation decision requires evidence")
        if self.evidence_ref is not None and not isinstance(
            self.evidence_ref, ArtifactRef
        ):
            raise ContractError(
                "evaluation decision evidence_ref must be an ArtifactRef"
            )


@dataclass(frozen=True, slots=True)
class SessionView:
    """Immutable projection returned to library callers."""

    session_id: str
    status: SessionStatus
    task: TaskSpec
    cursor: int
    lineage_head: CommittedNode
    active_attempt: Attempt | None
    pending_proposal_id: str | None = None
    wait_reason: str | None = None
