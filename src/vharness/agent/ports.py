"""Narrow interfaces to externally controlled runtime and evaluation systems."""

# pylint: disable=too-few-public-methods,unnecessary-ellipsis
# Protocols declare small, real integration boundaries rather than concrete services.

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .models import (
    ActionProposal,
    ComponentIdentity,
    EnvironmentContract,
    EvaluationReceipt,
    EvaluationRequest,
    ExecutionReceipt,
)


class ReconciliationKind(str, Enum):
    """Whether an interrupted operation can be resolved externally."""

    RECEIPT = "receipt"
    NOT_SUBMITTED = "not_submitted"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """External reconciliation outcome for one interrupted operation."""

    kind: ReconciliationKind
    receipt: ExecutionReceipt | None = None


@dataclass(frozen=True, slots=True)
class EvaluationReconciliationResult:
    """Evaluator-owned resolution of one interrupted evaluation request."""

    kind: ReconciliationKind
    receipt: EvaluationReceipt | None = None


class Environment(Protocol):
    """Provides mechanical task state and action definitions."""

    def describe(self) -> EnvironmentContract:
        """Return the trusted externally supplied environment contract."""
        ...


class Runtime(Protocol):
    """Proposes effects to an external runtime; it does not execute locally."""

    def identity(self) -> ComponentIdentity:
        """Return connector-owned immutable identity."""
        ...

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Submit a proposal and return a current runtime receipt."""
        ...

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        """Report externally known status for an earlier operation."""
        ...


class Evaluator(Protocol):
    """Requests externally authoritative evaluation."""

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        """Evaluate state according to the configured external contract."""
        ...

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        """Report evaluator-owned state for an earlier evaluation request."""
        ...

    def identity(self) -> ComponentIdentity:
        """Return evaluator-owned immutable identity."""
        ...
