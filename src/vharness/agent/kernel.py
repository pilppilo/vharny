"""Typed agent results and the scripted model decision boundary."""

# pylint: disable=too-few-public-methods,too-many-instance-attributes,duplicate-code
# pylint: disable=too-many-lines
# Model and ScriptedModel expose one decide() boundary by design.
# Write-side records mirror memory projections; they are not a shared module.

from __future__ import annotations

import json
import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, Union

from ..core import MODEL_FAILURE_CLASSES, ModelFailureClass
from .codec import MAX_JSON_BYTES, parse_json_object, to_json_value
from .context import (
    COEFFICIENTS,
    POLICY_VERSION,
    RESERVED_OUTPUT_TOKENS,
    SYSTEM_PROMPT,
    TOKEN_COUNTER,
    ContextBundle,
)
from .errors import ContractError
from .memory import (
    InvestigationDisposition,
    KnowledgeStatus,
    ProgressCategory,
)
from .models import (
    ActionDefinition,
    CommittedNode,
    JsonValue,
    TaskSpec,
    UsageMeasurement,
    frozen_mapping,
)
from .supervision import GuidanceDecision

_MISSING = object()

# Keep provider-controlled visible fields small enough that a terminal event
# can always be serialized together with its bounded identity and usage.
_MAX_MODEL_OUTPUT_BYTES = 64 * 1024
_MAX_MODEL_ID_BYTES = 256
_MAX_FINISH_REASON_BYTES = 128
_MAX_MODEL_ATTEMPTS = 64


class ModelCallError(ContractError):
    """A model transport, parse, or repair failure that still consumed calls."""

    # The typed failure class is one more constructor dimension by design.
    # pylint: disable=too-many-arguments
    def __init__(
        self,
        message: str,
        *,
        usage: UsageMeasurement | None = None,
        attempts: tuple["ModelAttempt", ...] = (),
        output: str = "",
        duration_ms: int | None = None,
        failure_class: str | None = None,
        repairs: tuple["Mapping[str, str]", ...] = (),
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.attempts = attempts
        self.output = output
        self.duration_ms = duration_ms
        self.failure_class = failure_class
        self.repairs = repairs


def safe_model_error(error: BaseException) -> str:
    """Return a bounded diagnostic category without retaining provider text."""
    if isinstance(error, ModelCallError):
        message = str(error).lower()
        if "output exhausted" in message:
            return "provider output exhausted"
        if "transport" in message:
            return "model transport failure"
        if "malformed" in message:
            return "malformed model output"
        return "model call failure"
    return "model connector failure"


def safe_provider_error(_error: object) -> str:
    """Collapse provider-controlled exception text to a stable safe detail."""
    return "provider transport failure"


def normalize_failure_class(value: object) -> str | None:
    """Return a known sanitized failure class, or None for anything else."""
    if isinstance(value, ModelFailureClass):
        return value.value
    if isinstance(value, str) and value in MODEL_FAILURE_CLASSES:
        return value
    return None


RESULT_SCHEMAS: Mapping[str, JsonValue] = {
    "ActionChoice": {
        "type": "object",
        "required": ("kind", "action_name", "rationale"),
        "properties": {
            "kind": {"const": "ActionChoice"},
            "action_name": {"type": "string"},
            "rationale": {"type": "string"},
            "arguments": {"type": "object"},
        },
    },
    "LineageQuery": {
        "type": "object",
        "required": ("kind",),
        "properties": {
            "kind": {"const": "LineageQuery"},
            "node_id": {"type": ("string", "null")},
        },
    },
    "AttemptQuery": {
        "type": "object",
        "required": ("kind", "attempt_id"),
        "properties": {
            "kind": {"const": "AttemptQuery"},
            "attempt_id": {"type": "string"},
        },
    },
    "KnowledgeQuery": {
        "type": "object",
        "required": ("kind",),
        "properties": {
            "kind": {"const": "KnowledgeQuery"},
            "item_id": {"type": ("string", "null")},
        },
    },
    "MemoryUpdate": {
        "type": "object",
        "required": ("kind", "item_id", "statement", "scope", "status"),
        "properties": {
            "kind": {"const": "MemoryUpdate"},
            "item_id": {"type": "string"},
            "statement": {"type": "string"},
            "scope": {"type": "string"},
            "status": {
                "enum": ("hypothesis", "supported", "contradicted", "retired"),
            },
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
        },
    },
    "EvaluationChoice": {
        "type": "object",
        "required": ("kind", "rationale"),
        "properties": {
            "kind": {"const": "EvaluationChoice"},
            "rationale": {"type": "string"},
        },
    },
    "OperatorQuestion": {
        "type": "object",
        "required": ("kind", "text"),
        "properties": {
            "kind": {"const": "OperatorQuestion"},
            "text": {"type": "string"},
        },
    },
    "WaitChoice": {
        "type": "object",
        "required": ("kind", "reason"),
        "properties": {
            "kind": {"const": "WaitChoice"},
            "reason": {"type": "string"},
        },
    },
    "PlanUpdate": {
        "type": "object",
        "required": ("kind", "text"),
        "properties": {
            "kind": {"const": "PlanUpdate"},
            "text": {"type": "string"},
        },
    },
    "CompletionProposal": {
        "type": "object",
        "required": ("kind", "rationale"),
        "properties": {
            "kind": {"const": "CompletionProposal"},
            "rationale": {"type": "string"},
        },
    },
    "InvestigationChange": {
        "type": "object",
        "required": ("kind", "investigation_id", "question"),
        "properties": {
            "kind": {"const": "InvestigationChange"},
            "investigation_id": {"type": "string"},
            "question": {"type": "string"},
            "disposition": {
                "enum": ("active", "paused", "concluded", "abandoned"),
            },
            "hypotheses": {"type": "array", "items": {"type": "string"}},
            "attempt_ids": {"type": "array", "items": {"type": "string"}},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "usage_ids": {"type": "array", "items": {"type": "string"}},
            "uncertainty": {"type": "string"},
        },
    },
    "ProgressClaimChange": {
        "type": "object",
        "required": ("kind", "claim_id", "category", "before", "after", "scope"),
        "properties": {
            "kind": {"const": "ProgressClaimChange"},
            "claim_id": {"type": "string"},
            "category": {"enum": ("outcome", "knowledge")},
            "before": {"type": "string"},
            "after": {"type": "string"},
            "scope": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "assessor": {"type": "string"},
        },
    },
    "GuidanceResponse": {
        "type": "object",
        "required": ("kind", "decision", "reason"),
        "properties": {
            "kind": {"const": "GuidanceResponse"},
            "decision": {"enum": ("accept", "reject", "replan")},
            "reason": {"type": "string"},
        },
    },
}


@dataclass(frozen=True, slots=True)
class ActionChoice:
    """Propose one environment action. The coordinator binds IDs and reservation."""

    action_name: str
    rationale: str
    arguments: Mapping[str, JsonValue] = field(default_factory=frozen_mapping)

    def __post_init__(self) -> None:
        if not self.action_name.strip() or not self.rationale.strip():
            raise ContractError("action choice requires a name and rationale")
        object.__setattr__(self, "arguments", frozen_mapping(self.arguments))


@dataclass(frozen=True, slots=True)
class LineageQuery:
    """Inspect committed lineage without an external effect."""

    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptQuery:
    """Inspect one prior attempt and its source-linked event evidence."""

    attempt_id: str

    def __post_init__(self) -> None:
        if not self.attempt_id.strip():
            raise ContractError("attempt query requires an attempt_id")


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    """Inspect supplied or recorded knowledge without an external effect."""

    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryUpdate:
    """Record an explicit knowledge transition with evidence links."""

    item_id: str
    statement: str
    scope: str
    status: KnowledgeStatus
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("item_id", self.item_id),
            ("statement", self.statement),
            ("scope", self.scope),
        ):
            if not value.strip():
                raise ContractError(f"memory update {name} must be non-empty")


@dataclass(frozen=True, slots=True)
class EvaluationChoice:
    """Request external evaluation of the current attempt state."""

    rationale: str

    def __post_init__(self) -> None:
        if not self.rationale.strip():
            raise ContractError("evaluation choice requires a rationale")


@dataclass(frozen=True, slots=True)
class OperatorQuestion:
    """Ask the operator something; does not execute an environment action."""

    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ContractError("operator question text must be non-empty")


@dataclass(frozen=True, slots=True)
class WaitChoice:
    """Wait on a named external condition without dispatching work."""

    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ContractError("wait reason must be non-empty")


@dataclass(frozen=True, slots=True)
class PlanUpdate:
    """Record the current plan the next decision must see."""

    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ContractError("plan text must be non-empty")


@dataclass(frozen=True, slots=True)
class InvestigationChange:
    """Open, update, pause, conclude, abandon, or reopen an investigation."""

    investigation_id: str
    question: str
    disposition: InvestigationDisposition = InvestigationDisposition.ACTIVE
    hypotheses: tuple[str, ...] = ()
    attempt_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    usage_ids: tuple[str, ...] = ()
    uncertainty: str = ""

    def __post_init__(self) -> None:
        if not self.investigation_id.strip() or not self.question.strip():
            raise ContractError("investigation change requires an id and question")


@dataclass(frozen=True, slots=True)
class ProgressClaimChange:
    """Propose a before/after claim. The coordinator decides if it is supported."""

    claim_id: str
    category: ProgressCategory
    before: str
    after: str
    scope: str
    evidence_ids: tuple[str, ...] = ()
    assessor: str = "agent"

    def __post_init__(self) -> None:
        for name, value in (
            ("claim_id", self.claim_id),
            ("before", self.before),
            ("after", self.after),
            ("scope", self.scope),
            ("assessor", self.assessor),
        ):
            if not value.strip():
                raise ContractError(f"progress claim {name} must be non-empty")


@dataclass(frozen=True, slots=True)
class CompletionProposal:
    """Propose local completion. Only current external evidence can accept it."""

    rationale: str

    def __post_init__(self) -> None:
        if not self.rationale.strip():
            raise ContractError("completion proposal requires a rationale")


@dataclass(frozen=True, slots=True)
class GuidanceResponse:
    """Accept, reject, or replan on the active supervisor guidance, with a reason.

    The coordinator records the response as a durable event; replan also starts
    a fresh attempt from the current lineage head.
    """

    decision: GuidanceDecision
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, GuidanceDecision):
            raise ContractError("guidance response decision must be a GuidanceDecision")
        if not self.reason.strip():
            raise ContractError("guidance response requires a concise reason")


AgentResult = Union[
    ActionChoice,
    LineageQuery,
    AttemptQuery,
    KnowledgeQuery,
    MemoryUpdate,
    EvaluationChoice,
    OperatorQuestion,
    WaitChoice,
    PlanUpdate,
    CompletionProposal,
    InvestigationChange,
    ProgressClaimChange,
    GuidanceResponse,
]


@dataclass(frozen=True, slots=True)
class DecisionInput:
    """Visible decision request. Hidden chain-of-thought is not included."""

    task: TaskSpec
    context: ContextBundle
    actions: tuple[ActionDefinition, ...]
    lineage_head: CommittedNode
    attempt_id: str
    journal_cursor: int
    objective_version: int
    request_id: str
    remaining_budgets: Mapping[str, JsonValue]
    observation: Mapping[str, JsonValue]
    latest_receipt: Mapping[str, JsonValue]
    result_schemas: Mapping[str, JsonValue]
    model_id: str
    policy_version: int
    token_counter: str

    def __post_init__(self) -> None:
        for name, value in (
            ("request_id", self.request_id),
            ("attempt_id", self.attempt_id),
            ("model_id", self.model_id),
            ("token_counter", self.token_counter),
        ):
            if not value.strip():
                raise ContractError(f"decision {name} must be non-empty")
        if self.journal_cursor < 0 or self.objective_version < 1:
            raise ContractError("decision cursor and objective_version are invalid")
        if self.policy_version < 1:
            raise ContractError("decision policy_version must be positive")
        object.__setattr__(
            self, "remaining_budgets", frozen_mapping(self.remaining_budgets)
        )
        object.__setattr__(self, "observation", frozen_mapping(self.observation))
        object.__setattr__(self, "latest_receipt", frozen_mapping(self.latest_receipt))
        object.__setattr__(self, "result_schemas", frozen_mapping(self.result_schemas))


@dataclass(frozen=True, slots=True)
class ModelConfiguration:
    """Immutable Phase-2 model settings bound before the first model call."""

    model_id: str
    provider_id: str
    policy_version: int
    system_prompt_digest: str
    output_settings: Mapping[str, JsonValue]
    repair_settings: Mapping[str, JsonValue]
    retry_settings: Mapping[str, JsonValue]
    token_counter: str
    context_coefficients: Mapping[str, float]
    token_budget: int

    def __post_init__(self) -> None:
        for name, value in (
            ("model_id", self.model_id),
            ("provider_id", self.provider_id),
            ("token_counter", self.token_counter),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"model configuration {name} must be non-empty")
        if self.policy_version < 1 or self.token_budget < 1:
            raise ContractError("model configuration version and budget are invalid")
        if len(self.system_prompt_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.system_prompt_digest
        ):
            raise ContractError("model configuration system prompt digest is invalid")
        object.__setattr__(
            self, "output_settings", frozen_mapping(self.output_settings)
        )
        object.__setattr__(
            self, "repair_settings", frozen_mapping(self.repair_settings)
        )
        object.__setattr__(self, "retry_settings", frozen_mapping(self.retry_settings))
        object.__setattr__(
            self, "context_coefficients", frozen_mapping(self.context_coefficients)
        )


@dataclass(frozen=True, slots=True)
class ModelAttempt:
    """One visible provider call, including repair and failed attempts."""

    output: str
    finish_reason: str
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int | None = None
    # Provider-reported reasoning tokens, when the endpoint reports them.
    # The reasoning text is never recorded; the count is what distinguishes a
    # reasoning-only response from an empty one.
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Visible model output plus usage. Provider-hidden reasoning is omitted."""

    result: AgentResult
    output: str
    model_id: str
    finish_reason: str
    usage: UsageMeasurement | None = None
    attempts: tuple[ModelAttempt, ...] = ()
    duration_ms: int | None = None
    repairs: tuple[Mapping[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _BoundedGeneration:
    """Sanitized provider output used before any durable model record."""

    text: str
    model: str
    finish_reason: str
    error: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency: float | None
    failure_class: str | None = None
    reasoning_tokens: int | None = None


class Model(Protocol):
    """Returns one typed result for a decision request."""

    # pylint: disable=unnecessary-ellipsis
    def decide(self, request: DecisionInput) -> ModelResponse:
        """Produce a visible result. Must not invoke tools."""
        ...


class ScriptedModel:
    """Deterministic model that yields pre-typed results in order."""

    def __init__(self, results: tuple[AgentResult, ...], *, model_id: str = "scripted"):
        self._remaining = list(results)
        self.model_id = model_id
        self.calls = 0
        self.requests: list[DecisionInput] = []

    def decide(self, request: DecisionInput) -> ModelResponse:
        """Pop the next scripted result or fail if the script is exhausted."""
        self.calls += 1
        self.requests.append(request)
        usage = UsageMeasurement(
            measurement_id=f"{request.request_id}-usage",
            model_calls=1,
            duration_ms=0,
        )
        attempt = ModelAttempt(
            output="",
            finish_reason="error",
            error="scripted model has no remaining results",
            duration_ms=0,
        )
        if not self._remaining:
            raise ModelCallError(
                "scripted model has no remaining results",
                usage=usage,
                attempts=(attempt,),
                duration_ms=0,
            )
        result = self._remaining.pop(0)
        output = type(result).__name__
        return ModelResponse(
            result=result,
            output=output,
            model_id=self.model_id,
            finish_reason="stop",
            usage=usage,
            attempts=(
                ModelAttempt(output=output, finish_reason="stop", duration_ms=0),
            ),
            duration_ms=0,
        )


def decide(request: DecisionInput, model: Model) -> ModelResponse:
    """Invoke the model and require a typed agent result."""
    response = model.decide(request)
    if not isinstance(response, ModelResponse):
        raise ContractError("model produced an invalid response")
    if response.result is None:
        raise ContractError("model produced no typed result")
    return _normalize_model_response(request, response)


class GeneratedText(Protocol):
    """Visible generator output. Hidden chain-of-thought is omitted."""

    # pylint: disable=unnecessary-ellipsis
    @property
    def text(self) -> str:
        """Visible completion text."""
        ...

    @property
    def model(self) -> str:
        """Provider model identity."""
        ...

    @property
    def finish_reason(self) -> str:
        """Provider-reported finish reason."""
        ...

    @property
    def error(self) -> str | None:
        """Safe error detail when the call failed."""
        ...

    @property
    def failure_class(self) -> str | None:
        """Typed sanitized failure class, when the provider supplies one."""
        ...

    @property
    def prompt_tokens(self) -> int | None:
        """Prompt tokens reported by the provider, when available."""
        ...

    @property
    def completion_tokens(self) -> int | None:
        """Completion tokens reported by the provider, when available."""
        ...


class TextGenerator(Protocol):
    """Existing generator boundary: system+prompt in, visible text out."""

    # pylint: disable=unnecessary-ellipsis
    def generate(self, system: str, prompt: str) -> GeneratedText:
        """Return one visible completion. Must not invoke tools."""
        ...


_RESULT_KINDS = {
    "ActionChoice": ActionChoice,
    "LineageQuery": LineageQuery,
    "AttemptQuery": AttemptQuery,
    "KnowledgeQuery": KnowledgeQuery,
    "MemoryUpdate": MemoryUpdate,
    "EvaluationChoice": EvaluationChoice,
    "OperatorQuestion": OperatorQuestion,
    "WaitChoice": WaitChoice,
    "PlanUpdate": PlanUpdate,
    "CompletionProposal": CompletionProposal,
    "InvestigationChange": InvestigationChange,
    "ProgressClaimChange": ProgressClaimChange,
    "GuidanceResponse": GuidanceResponse,
}


# ADR-0015: required fields the coordinator may repair at decode because they
# annotate the model's reasoning. Every other required field - and every type,
# contract, and enum - is operational and is never repaired or backfilled.
OBSERVATIONAL_FIELDS: Mapping[str, str] = {
    "ActionChoice": "rationale",
    "EvaluationChoice": "rationale",
    "CompletionProposal": "rationale",
    "WaitChoice": "reason",
    "GuidanceResponse": "reason",
}
OBSERVATIONAL_REPAIR_FAILURE_CLASS = "observational-field"


def parse_agent_result(text: str) -> AgentResult:
    """Parse one visible JSON object into a typed agent result."""
    if not isinstance(text, str):
        raise ContractError("model output must be text")
    payload = parse_json_object(_json_object(text))
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in _RESULT_KINDS:
        raise ContractError(f"unsupported agent result kind {kind!r}")
    _validate_result_payload(kind, payload)
    body = {key: value for key, value in payload.items() if key != "kind"}
    for key, value in list(body.items()):
        if isinstance(value, list):
            body[key] = tuple(value)
    if kind == "MemoryUpdate" and "status" in body:
        body["status"] = KnowledgeStatus(str(body["status"]))
    if kind == "InvestigationChange" and "disposition" in body:
        body["disposition"] = InvestigationDisposition(str(body["disposition"]))
    if kind == "ProgressClaimChange" and "category" in body:
        body["category"] = ProgressCategory(str(body["category"]))
    if kind == "GuidanceResponse" and "decision" in body:
        body["decision"] = GuidanceDecision(str(body["decision"]))
    try:
        return _RESULT_KINDS[kind](**body)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ContractError(f"invalid {kind} fields: {exc}") from exc


def _json_object(text: str) -> str:
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ContractError("model output exceeds the supported byte bound")
    stripped = text.strip()
    if not stripped:
        raise ContractError("model output is empty")
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ContractError("model output does not contain a JSON object")
    return stripped[start : end + 1]


def _validate_result_payload(kind: str, payload: Mapping[str, object]) -> None:
    """Validate model JSON types before constructing typed result records."""
    schema = RESULT_SCHEMAS[kind]
    if not isinstance(schema, Mapping):
        raise ContractError(f"result schema {kind} is invalid")
    required = schema.get("required", ())
    properties = schema.get("properties", {})
    if not isinstance(required, (tuple, list)) or not isinstance(properties, Mapping):
        raise ContractError(f"result schema {kind} is invalid")
    missing = [name for name in required if name not in payload]
    if missing:
        raise ContractError(f"{kind} is missing required fields: {missing}")
    unknown = set(payload) - set(properties)
    if unknown:
        raise ContractError(f"{kind} has unsupported fields: {sorted(unknown)}")
    for name, value in payload.items():
        _validate_result_field(kind, name, value, properties.get(name))


def _validate_result_field(kind: str, name: str, value: object, rule: object) -> None:
    """Validate one result property against its declared JSON type rule."""
    if not isinstance(rule, Mapping):
        raise ContractError(f"{kind} field {name} has no type rule")
    if "const" in rule and value != rule["const"]:
        raise ContractError(f"{kind} field {name} has an invalid value")
    enum = rule.get("enum")
    if isinstance(enum, (tuple, list)) and value not in enum:
        raise ContractError(f"{kind} field {name} has an invalid value")
    expected = rule.get("type")
    if expected is not None and not _json_type_matches(value, expected):
        raise ContractError(f"{kind} field {name} has an invalid JSON type")
    if expected == "array" and isinstance(value, list):
        item_rule = rule.get("items")
        if isinstance(item_rule, Mapping) and any(
            not _json_type_matches(item, item_rule.get("type")) for item in value
        ):
            raise ContractError(f"{kind} field {name} contains an invalid JSON type")


def _json_type_matches(value: object, expected: object) -> bool:
    """Match the small JSON-schema type subset used by result contracts."""
    expected_types = expected if isinstance(expected, tuple) else (expected,)
    for item in expected_types:
        if item == "null" and value is None:
            return True
        if item == "string" and isinstance(value, str):
            return True
        if item == "object" and isinstance(value, dict):
            return True
        if item == "array" and isinstance(value, list):
            return True
    return False


def _observational_marker(annotation: str) -> str:
    return f"<{annotation} omitted by provider; recovered by decode>"


def _missing_observational_field(
    payload: Mapping[str, object], kind: str
) -> str | None:
    """Return the annotation the payload is missing, or None when not repairable."""
    annotation = OBSERVATIONAL_FIELDS.get(kind)
    schema = RESULT_SCHEMAS.get(kind)
    if annotation is None or annotation in payload:
        return None
    if not isinstance(schema, Mapping):
        return None
    required = schema.get("required", ())
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping) or not isinstance(required, (tuple, list)):
        return None
    missing = [name for name in required if name not in payload]
    if missing != [annotation]:
        return None
    if any(name not in properties for name in payload):
        return None
    return annotation


def repair_observational_output(
    text: str,
) -> tuple[AgentResult, Mapping[str, str]] | None:
    """Accept one result whose only schema defect is a missing observational field.

    Rung 1 of the ADR-0015 ladder: the model's choice is untouched and the
    missing annotation is replaced with a deterministic marker, at no provider
    cost. ``None`` on any other defect, so operational contracts keep their
    existing repair and failure behavior and unknown fields are never stripped.
    """
    try:
        payload = parse_json_object(_json_object(text))
    except (ContractError, json.JSONDecodeError, ValueError):
        return None
    kind = payload.get("kind")
    if not isinstance(kind, str):
        return None
    annotation = _missing_observational_field(payload, kind)
    if annotation is None:
        return None
    completed = dict(payload)
    marker = _observational_marker(annotation)
    completed[annotation] = marker
    try:
        result = parse_agent_result(json.dumps(completed))
    except (ContractError, json.JSONDecodeError, ValueError):
        # A deeper defect - a wrong type, an invalid enum, a bad dataclass field
        # - is operational and belongs to a later rung.
        return None
    note: Mapping[str, str] = {
        "failure_class": OBSERVATIONAL_REPAIR_FAILURE_CLASS,
        "result_kind": kind,
        "field": annotation,
        "marker": marker,
    }
    return result, note


OPERATIONAL_REPAIR_FAILURE_CLASS = "operational-field"
OPERATIONAL_REPAIR_MARKER = "<operational field corrected by compact re-ask>"


@dataclass(frozen=True, slots=True)
class ResultDefect:
    """One classified defect of a provider result payload (ADR-0015 rung 2)."""

    failure_class: str
    kind: str | None
    field: str | None
    detail: str
    payload: Mapping[str, object] | None = None


def classify_result_defect(text: str) -> ResultDefect | None:
    """Classify a malformed result payload, or return None when it validates.

    Rung 2 repairs one typed operational-field defect with a compact pinned
    re-ask; unparseable and kind-unsupported text keeps the existing
    malformed-JSON round, and defects without a named field stay there too.
    """
    try:
        payload = parse_json_object(_json_object(text))
    except (ContractError, json.JSONDecodeError, ValueError) as exc:
        return ResultDefect("unparseable", None, None, str(exc))
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in _RESULT_KINDS:
        return ResultDefect(
            "kind-unsupported",
            kind if isinstance(kind, str) else None,
            None,
            f"unsupported agent result kind {kind!r}",
            payload,
        )
    return _classify_result_payload(payload, kind)


def _classify_result_payload(
    payload: Mapping[str, object], kind: str
) -> ResultDefect | None:
    """Classify one payload whose kind is a known result schema."""
    schema = RESULT_SCHEMAS[kind]
    required = schema.get("required", ()) if isinstance(schema, Mapping) else None
    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else None
    required_names = (
        [name for name in required if isinstance(name, str)]
        if isinstance(required, (tuple, list))
        else []
    )
    required_count = len(required) if isinstance(required, (tuple, list)) else -1
    if (
        not isinstance(schema, Mapping)
        or not isinstance(properties, Mapping)
        or required_count < 0
        or len(required_names) != required_count
    ):
        return ResultDefect(
            OPERATIONAL_REPAIR_FAILURE_CLASS,
            kind,
            None,
            f"result schema {kind} is invalid",
            payload,
        )
    missing = [name for name in required_names if name not in payload]
    if missing:
        annotation = _missing_observational_field(payload, kind)
        failure = (
            "observational-field"
            if annotation is not None
            else OPERATIONAL_REPAIR_FAILURE_CLASS
        )
        return ResultDefect(
            failure,
            kind,
            annotation if annotation is not None else missing[0],
            f"{kind} is missing required fields: {missing}",
            payload,
        )
    unknown = sorted(set(payload) - set(properties))
    if unknown:
        return ResultDefect(
            OPERATIONAL_REPAIR_FAILURE_CLASS,
            kind,
            unknown[0],
            f"{kind} has unsupported fields: {unknown}",
            payload,
        )
    try:
        _validate_result_payload(kind, payload)
    except ContractError as exc:
        return ResultDefect(
            OPERATIONAL_REPAIR_FAILURE_CLASS, kind, None, str(exc), payload
        )
    return None


def _is_compact_repair(defect: ResultDefect | None) -> bool:
    """Return whether a classified defect can take the compact re-ask."""
    return (
        defect is not None
        and defect.failure_class == OPERATIONAL_REPAIR_FAILURE_CLASS
        and defect.kind is not None
        and defect.field is not None
        and defect.payload is not None
    )


def _repair_conflict(
    original: Mapping[str, object],
    corrected: Mapping[str, object],
    repaired_field: str,
) -> str | None:
    """Return the first pinned-field change in a rung-2 repair, or None.

    The choice is pinned: the kind and every field the original already
    carried, other than the repaired field, must parse-equal. A drift means
    the re-ask restated a different decision rather than correcting the
    defect, which escalates instead of being accepted.
    """
    if original.get("kind") != corrected.get("kind"):
        return "result kind changed"
    for name, value in original.items():
        if name in ("kind", repaired_field):
            continue
        if name not in corrected or corrected.get(name) != value:
            return f"field {name} changed"
    return None


def _compact_repair_prompt(
    request: DecisionInput, previous_output: str, defect: ResultDefect
) -> str:
    """Build the rung-2 re-ask: request id, prior output, typed defect only.

    The full decision context is deliberately not re-sent; the pinned fields
    make the correction local to one defective result.
    """
    schema = RESULT_SCHEMAS.get(defect.kind or "", {})
    required_values = schema.get("required", ()) if isinstance(schema, Mapping) else ()
    required = (
        list(required_values) if isinstance(required_values, (tuple, list)) else []
    )
    return (
        "One decision result failed schema validation. Re-emit it corrected.\n"
        f"request_id={request.request_id}\n"
        f"result_kind={defect.kind}\n"
        f"defect={defect.detail}\n"
        f"required_fields={required}\n"
        f"previous_output={previous_output}\n"
        "Return exactly one complete JSON object of the same kind. Keep every "
        "other field, including action_name and arguments, identical to "
        "previous_output. No markdown, no prose, no tool calls."
    )


def render_request(request: DecisionInput) -> str:
    """Serialize the visible decision request, including schemas and identities."""
    manifest = request.context.manifest
    actions = [
        {
            "name": action.name,
            "description": action.description,
            "argument_schema": _jsonable(action.argument_schema),
        }
        for action in request.actions
    ]
    return json.dumps(
        {
            "actions": actions,
            "attempt_id": request.attempt_id,
            "context": request.context.text,
            "journal_cursor": request.journal_cursor,
            "latest_receipt": _jsonable(request.latest_receipt),
            "lineage_head": request.lineage_head.node_id,
            "manifest": {
                "allocation": _jsonable(manifest.allocation),
                "coefficients": _jsonable(manifest.coefficients),
                # COD-73, ADR-0020: counts, not the growing item lists; the
                # journal keeps the full manifest for audit.
                "included_by_band": _band_counts(manifest.included),
                "journal_cursor": manifest.journal_cursor,
                "omitted_by_reason": _band_counts(manifest.omitted),
                "policy_version": manifest.policy_version,
                "reserved_output_tokens": manifest.reserved_output_tokens,
                "serialization_overhead_tokens": manifest.serialization_overhead_tokens,
                "serialized_input_tokens": manifest.serialized_input_tokens,
                "system_prompt_tokens": manifest.system_prompt_tokens,
                "token_budget": manifest.token_budget,
                "token_counter": manifest.token_counter,
                "tokens_used": manifest.tokens_used,
            },
            "task_id": request.task.task_id,
            "completion_mode": request.task.completion_mode.value,
            "constraints": list(request.task.constraints),
            "environment_id": request.task.environment_id,
            "evaluation_contract_id": request.task.evaluation_contract_id,
            "success_evidence": request.task.success_evidence,
            "model_id": request.model_id,
            "objective": request.task.objective,
            "objective_version": request.objective_version,
            "observation": _jsonable(request.observation),
            "policy_version": request.policy_version,
            "remaining_budgets": _jsonable(request.remaining_budgets),
            "request_id": request.request_id,
            "result_schemas": _jsonable(request.result_schemas),
            "token_counter": request.token_counter,
        },
        sort_keys=True,
    )


def _band_counts(pairs: tuple[tuple[str, str], ...]) -> dict[str, int]:
    """Count (item id, label) pairs by label: band for included, reason for omitted."""
    counts: dict[str, int] = {}
    for _item_id, label in pairs:
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def _render_request(request: DecisionInput) -> str:
    return render_request(request)


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


class _CoordinatorOwnedGenerator:
    """Disable provider-hidden retries for one generate() call."""

    def __init__(self, inner: TextGenerator) -> None:
        self._inner = inner

    def generate(self, system: str, prompt: str) -> GeneratedText:
        """Call the inner generator with retries forced off, then restore them."""
        inner = self._inner
        saved: dict[str, object] = {}
        for name in ("max_retries", "length_retries"):
            if hasattr(inner, name):
                saved[name] = getattr(inner, name)
                setattr(inner, name, 0)
        try:
            return inner.generate(system, prompt)
        finally:
            for name, value in saved.items():
                setattr(inner, name, value)


def _bounded_text(value: object, *, fallback: str, limit: int) -> str:
    """Return provider text bounded by UTF-8 bytes and safe primitive types."""
    if not isinstance(value, str):
        return fallback
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _bounded_integer(value: object, *, maximum: int = 2**31 - 1) -> int | None:
    """Accept only bounded, non-negative integer telemetry."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > maximum:
        return None
    return value


def _bounded_attempts(value: object) -> tuple[ModelAttempt, ...]:
    """Normalize model attempts before they cross the durable event boundary."""
    if not isinstance(value, (tuple, list)):
        return ()
    attempts: list[ModelAttempt] = []
    for raw in value[:_MAX_MODEL_ATTEMPTS]:
        attempts.append(
            ModelAttempt(
                output=_bounded_text(
                    getattr(raw, "output", None),
                    fallback="",
                    limit=_MAX_MODEL_OUTPUT_BYTES,
                ),
                finish_reason=_bounded_text(
                    getattr(raw, "finish_reason", None),
                    fallback="error",
                    limit=_MAX_FINISH_REASON_BYTES,
                ),
                error=(
                    None
                    if getattr(raw, "error", None) is None
                    else safe_provider_error(getattr(raw, "error", None))
                ),
                prompt_tokens=_bounded_integer(getattr(raw, "prompt_tokens", None)),
                completion_tokens=_bounded_integer(
                    getattr(raw, "completion_tokens", None)
                ),
                duration_ms=_bounded_integer(getattr(raw, "duration_ms", None)),
                reasoning_tokens=_bounded_integer(
                    getattr(raw, "reasoning_tokens", None)
                ),
            )
        )
    return tuple(attempts)


def _bounded_duration(value: object) -> int | None:
    """Normalize an optional duration without allowing non-finite values."""
    return _bounded_integer(value)


def _normalize_model_response(
    request: DecisionInput, response: ModelResponse
) -> ModelResponse:
    """Bound all model-controlled fields before Session persists a terminal."""
    return ModelResponse(
        result=response.result,
        output=_bounded_text(
            response.output, fallback="", limit=_MAX_MODEL_OUTPUT_BYTES
        ),
        # The request identity is bound by the coordinator; provider metadata
        # must not replace it in the durable terminal event.
        model_id=request.model_id,
        finish_reason=_bounded_text(
            response.finish_reason,
            fallback="error",
            limit=_MAX_FINISH_REASON_BYTES,
        ),
        usage=(
            response.usage if isinstance(response.usage, UsageMeasurement) else None
        ),
        attempts=_bounded_attempts(response.attempts),
        duration_ms=_bounded_duration(response.duration_ms),
        repairs=response.repairs,
    )


def bounded_model_output(value: object) -> str:
    """Bound a model output before it is included in a terminal event."""
    return _bounded_text(value, fallback="", limit=_MAX_MODEL_OUTPUT_BYTES)


def bounded_model_attempts(value: object) -> tuple[ModelAttempt, ...]:
    """Bound model attempt records supplied by any Model implementation."""
    return _bounded_attempts(value)


def bounded_model_duration(value: object) -> int | None:
    """Bound model duration metadata supplied by any Model implementation."""
    return _bounded_duration(value)


class CompatibleModel:
    """Adapt a text generator. Every transport and repair call is counted."""

    def __init__(
        self,
        generator: TextGenerator,
        *,
        model_id: str | None = None,
        repair_budget: int = 1,
        reserved_output_tokens: int | None = None,
    ) -> None:
        if repair_budget < 0:
            raise ContractError("repair_budget must not be negative")
        generator_output_tokens = getattr(generator, "max_tokens", _MISSING)
        if reserved_output_tokens is None:
            if generator_output_tokens is _MISSING:
                generator_output_tokens = RESERVED_OUTPUT_TOKENS
        else:
            explicit_output_tokens = _validate_reserved_output_tokens(
                reserved_output_tokens
            )
            if generator_output_tokens is not _MISSING:
                configured_output_tokens = _validate_reserved_output_tokens(
                    generator_output_tokens
                )
                if explicit_output_tokens != configured_output_tokens:
                    raise ContractError(
                        "reserved output tokens must match generator max_tokens"
                    )
            generator_output_tokens = explicit_output_tokens
        self.reserved_output_tokens = _validate_reserved_output_tokens(
            generator_output_tokens
        )
        self._generator = _CoordinatorOwnedGenerator(generator)
        self.model_id = model_id or "compatible"
        self.provider_id = getattr(generator, "provider_id", "compatible")
        self.repair_budget = repair_budget
        self.output_settings: Mapping[str, JsonValue] = {
            "reserved_output_tokens": self.reserved_output_tokens,
        }
        self.retry_settings: Mapping[str, JsonValue] = {
            "max_retries": getattr(generator, "max_retries", 0),
            "length_retries": getattr(generator, "length_retries", 0),
            "coordinator_owned": True,
        }
        self.calls = 0

    def decide(self, request: DecisionInput) -> ModelResponse:
        """Generate a typed result; malformed output may be repaired within budget.

        A result whose only schema defect is one missing observational field
        (ADR-0015 rung 1) is repaired in place at no provider cost before any
        budgeted repair round.
        """
        prompt = render_request(request)
        attempts: list[ModelAttempt] = []
        generation = self._invoke(SYSTEM_PROMPT, prompt, attempts)
        if generation.error:
            raise self._failure(
                request,
                "model transport failed",
                attempts,
                failure_class=generation.failure_class,
            )
        repairs = 0
        repair_notes: list[Mapping[str, str]] = []
        while True:
            try:
                if not generation.text.strip():
                    raise _ProviderOutputExhausted(
                        "provider output exhausted: empty or length-limited output"
                    )
                result = parse_agent_result(generation.text)
                break
            except _ProviderOutputExhausted as exc:
                if repairs >= self.repair_budget:
                    raise self._failure(
                        request,
                        str(exc),
                        attempts,
                        failure_class=ModelFailureClass.OUTPUT_EXHAUSTED.value,
                        repairs=tuple(repair_notes),
                    ) from exc
                repairs += 1
                generation, note = self._repair_output_exhausted(
                    request, generation, attempts
                )
                repair_notes.append(note)
                continue
            except (ContractError, json.JSONDecodeError, ValueError) as exc:
                if _provider_output_exhausted(generation):
                    if repairs >= self.repair_budget:
                        raise self._failure(
                            request,
                            "provider output exhausted: length-limited output",
                            attempts,
                            failure_class=ModelFailureClass.OUTPUT_EXHAUSTED.value,
                            repairs=tuple(repair_notes),
                        ) from exc
                    repairs += 1
                    generation, note = self._repair_output_exhausted(
                        request, generation, attempts
                    )
                    repair_notes.append(note)
                    continue
                repaired = repair_observational_output(generation.text)
                if repaired is not None:
                    # ADR-0015 rung 1: no provider call, no repair budget.
                    result, note = repaired
                    repair_notes.append(note)
                    break
                defect = classify_result_defect(generation.text)
                if repairs >= self.repair_budget:
                    raise self._failure(
                        request,
                        f"malformed model output: {exc}",
                        attempts,
                        failure_class=ModelFailureClass.MALFORMED_OUTPUT.value,
                        repairs=tuple(repair_notes),
                    ) from exc
                repairs += 1
                if _is_compact_repair(defect):
                    # ADR-0015 rung 2: one compact pinned re-ask, no full context.
                    generation, result, note = self._repair_operational(
                        request, generation, defect, attempts
                    )
                    repair_notes.append(note)
                    break
                repair_prompt = (
                    f"{prompt}\nprevious_output={generation.text!r}\n"
                    "error=malformed JSON; return one object whose kind is a "
                    "result schema name such as ActionChoice"
                )
                generation = self._invoke(SYSTEM_PROMPT, repair_prompt, attempts)
                if generation.error:
                    raise self._failure(
                        request,
                        "model transport failed",
                        attempts,
                        failure_class=generation.failure_class,
                    ) from None
        duration_ms = _attempts_duration(attempts)
        return ModelResponse(
            result=result,
            output=generation.text,
            model_id=generation.model or self.model_id,
            finish_reason=generation.finish_reason or "stop",
            usage=self._usage(request, attempts),
            attempts=tuple(attempts),
            duration_ms=duration_ms,
            repairs=tuple(repair_notes),
        )

    def _repair_operational(
        self,
        request: DecisionInput,
        prior: GeneratedText,
        defect: ResultDefect | None,
        attempts: list[ModelAttempt],
    ) -> tuple[GeneratedText, AgentResult, Mapping[str, str]]:
        """Issue the compact pinned re-ask and validate the corrected object."""
        if defect is None:
            raise ContractError("compact repair requires a typed operational defect")
        kind = defect.kind
        defect_field = defect.field
        pinned = defect.payload
        if kind is None or defect_field is None or pinned is None:
            raise ContractError("compact repair requires a typed operational defect")
        generation = self._invoke(
            SYSTEM_PROMPT,
            _compact_repair_prompt(request, prior.text, defect),
            attempts,
        )
        if generation.error:
            raise self._failure(
                request,
                "model transport failed",
                attempts,
                failure_class=generation.failure_class,
            ) from None
        try:
            result = parse_agent_result(generation.text)
            corrected = parse_json_object(_json_object(generation.text))
        except (ContractError, json.JSONDecodeError, ValueError) as exc:
            raise self._failure(
                request,
                f"malformed model output: {exc}",
                attempts,
                failure_class=ModelFailureClass.MALFORMED_OUTPUT.value,
            ) from exc
        conflict = _repair_conflict(pinned, corrected, defect_field)
        if conflict is not None:
            raise self._failure(
                request,
                f"conflicting repair: {conflict}",
                attempts,
                failure_class=ModelFailureClass.MALFORMED_OUTPUT.value,
            )
        note: Mapping[str, str] = {
            "failure_class": defect.failure_class,
            "result_kind": kind,
            "field": defect_field,
            "marker": OPERATIONAL_REPAIR_MARKER,
        }
        return generation, result, note

    def _repair_output_exhausted(
        self,
        request: DecisionInput,
        prior: GeneratedText,
        attempts: list[ModelAttempt],
    ) -> tuple[GeneratedText, Mapping[str, str]]:
        """Re-ask a decision whose visible output was absent at the ceiling.

        ADR-0017: one bounded re-ask at the same reserved output ceiling, so
        the ceiling and the reservation stay one contract. The re-ask carries
        the prior attempt's finish reason and reported reasoning count and no
        new context.
        """
        reasoning = getattr(prior, "reasoning_tokens", None)
        reasoning_text = "unknown" if reasoning is None else str(reasoning)
        prompt = (
            f"{render_request(request)}\n"
            "previous_attempt=empty or length-limited output\n"
            f"finish_reason={prior.finish_reason or 'unknown'}\n"
            f"reasoning_tokens={reasoning_text}\n"
            "error=the visible answer was cut off or absent; return one object "
            "whose kind is a result schema name such as ActionChoice, and emit "
            "it now without extending your reasoning"
        )
        generation = self._invoke(SYSTEM_PROMPT, prompt, attempts)
        if generation.error:
            raise self._failure(
                request,
                "model transport failed",
                attempts,
                failure_class=generation.failure_class,
            ) from None
        note: Mapping[str, str] = {
            "failure_class": ModelFailureClass.OUTPUT_EXHAUSTED.value,
            "result_kind": "none",
            "field": "output-ceiling",
            "marker": "bounded re-ask at the reserved ceiling; no new context",
        }
        return generation, note

    def _invoke(
        self,
        system: str,
        prompt: str,
        attempts: list[ModelAttempt],
    ) -> GeneratedText:
        started = time.monotonic()
        self.calls += 1
        try:
            generation = self._generator.generate(system, prompt)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return _failed_generation(exc, started, self.model_id, attempts)
        generation, duration_ms = _sanitize_generation(
            generation, started, self.model_id
        )
        attempts.append(_model_attempt(generation, duration_ms))
        return generation

    def _failure(
        self,
        request: DecisionInput,
        message: str,
        attempts: list[ModelAttempt],
        *,
        failure_class: str | None = None,
        repairs: tuple[Mapping[str, str], ...] = (),
    ) -> ModelCallError:
        output = attempts[-1].output if attempts else ""
        return ModelCallError(
            message,
            usage=self._usage(request, attempts),
            attempts=tuple(attempts),
            output=output,
            duration_ms=_attempts_duration(attempts),
            failure_class=failure_class,
            repairs=repairs,
        )

    def _usage(
        self, request: DecisionInput, attempts: list[ModelAttempt]
    ) -> UsageMeasurement:
        return UsageMeasurement(
            measurement_id=f"{request.request_id}-usage",
            model_calls=len(attempts),
            input_tokens=_optional_attempt_total(
                attempts, lambda item: item.prompt_tokens
            ),
            output_tokens=_optional_attempt_total(
                attempts, lambda item: item.completion_tokens
            ),
            duration_ms=_attempts_duration(attempts),
        )


class _ProviderOutputExhausted(Exception):
    """Visible output is absent or provider-truncated, so JSON repair cannot help."""


def _provider_output_exhausted(generation: GeneratedText) -> bool:
    """Identify empty or explicitly token-limited provider output."""
    if not generation.text.strip():
        return True
    return generation.finish_reason.strip().lower() in {
        "length",
        "max_tokens",
        "max_output_tokens",
        "token_limit",
    }


def _failed_generation(
    error: Exception,
    started: float,
    model_id: str,
    attempts: list[ModelAttempt],
) -> _BoundedGeneration:
    """Keep transport failures bounded while retaining their measured duration."""
    duration_ms = max(0, int(round((time.monotonic() - started) * 1000)))
    safe_error = safe_provider_error(error)
    attempts.append(
        ModelAttempt(
            output="",
            finish_reason="error",
            error=safe_error,
            duration_ms=duration_ms,
        )
    )
    return _BoundedGeneration(
        text="",
        model=model_id,
        finish_reason="error",
        error=safe_error,
        prompt_tokens=None,
        completion_tokens=None,
        latency=None,
    )


def _sanitize_generation(
    generation: GeneratedText, started: float, model_id: str
) -> tuple[_BoundedGeneration, int]:
    """Bound provider-controlled fields before they enter visible model records."""
    latency = getattr(generation, "latency", None)
    if isinstance(latency, (int, float)) and not isinstance(latency, bool):
        latency_value = float(latency)
        duration_ms = (
            int(round(latency_value * 1000))
            if math.isfinite(latency_value) and latency_value >= 0
            else int(round((time.monotonic() - started) * 1000))
        )
    else:
        latency_value = None
        duration_ms = int(round((time.monotonic() - started) * 1000))
    error = _bounded_provider_error(getattr(generation, "error", None))
    prompt_tokens = _bounded_integer(getattr(generation, "prompt_tokens", None))
    completion_tokens = _bounded_integer(getattr(generation, "completion_tokens", None))
    reasoning_tokens = _bounded_integer(getattr(generation, "reasoning_tokens", None))
    if error is not None:
        # The response was lost: any token counts riding on a failed
        # generation are not trustworthy usage, so the cost stays unknown
        # instead of establishing a false zero (ADR-0013).
        prompt_tokens = None
        completion_tokens = None
        reasoning_tokens = None
    bounded = _BoundedGeneration(
        text=_bounded_text(
            getattr(generation, "text", None),
            fallback="",
            limit=_MAX_MODEL_OUTPUT_BYTES,
        ),
        model=_bounded_text(
            getattr(generation, "model", None),
            fallback=model_id,
            limit=_MAX_MODEL_ID_BYTES,
        ),
        finish_reason=_bounded_text(
            getattr(generation, "finish_reason", None),
            fallback="error",
            limit=_MAX_FINISH_REASON_BYTES,
        ),
        error=error,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency=latency_value,
        failure_class=normalize_failure_class(
            getattr(generation, "failure_class", None)
        ),
        reasoning_tokens=reasoning_tokens,
    )
    return bounded, duration_ms


def _bounded_provider_error(error: object) -> str | None:
    return None if error is None else safe_provider_error(error)


def _model_attempt(generation: _BoundedGeneration, duration_ms: int) -> ModelAttempt:
    return ModelAttempt(
        output=generation.text,
        finish_reason=generation.finish_reason,
        error=generation.error,
        prompt_tokens=generation.prompt_tokens,
        completion_tokens=generation.completion_tokens,
        duration_ms=_bounded_integer(duration_ms),
        reasoning_tokens=generation.reasoning_tokens,
    )


def _optional_attempt_total(
    attempts: tuple[ModelAttempt, ...] | list[ModelAttempt],
    extractor: Callable[[ModelAttempt], int | None],
) -> int | None:
    """Sum attempt telemetry only when every attempt reports that field."""
    values = tuple(extractor(attempt) for attempt in attempts)
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _attempts_duration(attempts: tuple[ModelAttempt, ...] | list[ModelAttempt]) -> int:
    return sum(item.duration_ms or 0 for item in attempts)


def configuration_for(model: Model, token_budget: int) -> ModelConfiguration:
    """Capture provider-visible settings used by one model decision stream."""
    model_id = getattr(model, "model_id", "unknown")
    provider_id = getattr(model, "provider_id", type(model).__name__)
    if not isinstance(model_id, str) or not model_id.strip():
        model_id = "unknown"
    else:
        model_id = _bounded_text(
            model_id, fallback="unknown", limit=_MAX_MODEL_ID_BYTES
        )
    if not isinstance(provider_id, str) or not provider_id.strip():
        provider_id = type(model).__name__
    else:
        provider_id = _bounded_text(
            provider_id, fallback=type(model).__name__, limit=_MAX_MODEL_ID_BYTES
        )
    repair_budget = getattr(model, "repair_budget", 0)
    if not isinstance(repair_budget, int) or isinstance(repair_budget, bool):
        repair_budget = 0
    output = _settings(getattr(model, "output_settings", {}))
    output["reserved_output_tokens"] = reserved_output_tokens_for(model)
    retry = _settings(getattr(model, "retry_settings", {}))
    retry.setdefault("coordinator_owned", True)
    return ModelConfiguration(
        model_id=model_id,
        provider_id=provider_id,
        policy_version=POLICY_VERSION,
        system_prompt_digest=hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        output_settings=output,
        repair_settings={"repair_budget": repair_budget},
        retry_settings=retry,
        token_counter=TOKEN_COUNTER,
        context_coefficients=COEFFICIENTS,
        token_budget=token_budget,
    )


def reserved_output_tokens_for(model: Model) -> int:
    """Return the validated output reservation for one model boundary."""
    missing = object()
    configured = getattr(model, "reserved_output_tokens", missing)
    if configured is missing:
        settings = getattr(model, "output_settings", {})
        configured = (
            settings.get("reserved_output_tokens", RESERVED_OUTPUT_TOKENS)
            if isinstance(settings, Mapping)
            else RESERVED_OUTPUT_TOKENS
        )
    return _validate_reserved_output_tokens(configured)


def _validate_reserved_output_tokens(value: object) -> int:
    """Validate a model's positive integer output reservation."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError("reserved output tokens must be a positive integer")
    return value


def _settings(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): to_json_value(item)
        for key, item in value.items()
        if isinstance(key, str)
    }
