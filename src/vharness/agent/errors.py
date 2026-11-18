"""Errors raised by the Vharness session library."""


class AgentError(RuntimeError):
    """Base class for caller-visible session library failures."""


class ContractError(AgentError, ValueError):
    """A caller or external boundary supplied invalid data."""


class TransitionError(AgentError):
    """A requested local state transition is not currently legal."""


#: ADR-0024: the resources a ``budget_exhausted`` outcome may name.
BUDGET_RESOURCES = frozenset(
    {
        "action",
        "model call",
        "evaluation",
        "action and evaluation",
        "token",
        "time",
        "cost",
    }
)


class BudgetExhausted(TransitionError):
    """A configured limit refused admission for an external operation."""

    def __init__(self, resource: str) -> None:
        if resource not in BUDGET_RESOURCES:
            raise ValueError(f"unknown budget resource: {resource}")
        self.resource = resource
        super().__init__(f"{resource} budget exhausted")


class PersistenceError(AgentError):
    """A durable session record could not be read or written."""


class IntegrityError(PersistenceError):
    """Required durable state or evidence is corrupt or incompatible."""
