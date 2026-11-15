"""Deterministic environment-contract fixtures for Phase 1 evidence."""

from __future__ import annotations

from dataclasses import dataclass

from .models import ActionDefinition, EnvironmentContract, RuntimeCapabilities, StateRef


@dataclass(frozen=True, slots=True)
class CandidateRestorableEnvironment:
    """A deterministic fake whose externally supplied state is restorable."""

    environment_id: str = "candidate-restorable"

    def describe(self) -> EnvironmentContract:
        """Return the deterministic restorable contract."""
        return _contract(self.environment_id, restorable=True)


@dataclass(frozen=True, slots=True)
class TrajectoryOnlyEnvironment:
    """A deterministic fake whose state reference is a trajectory cursor only."""

    environment_id: str = "trajectory-only"

    def describe(self) -> EnvironmentContract:
        """Return the deterministic trajectory-only contract."""
        return _contract(self.environment_id, restorable=False)


def _contract(environment_id: str, *, restorable: bool) -> EnvironmentContract:
    return EnvironmentContract(
        environment_id=environment_id,
        version="deterministic-v1",
        initial_state=StateRef(
            environment_id, "initial", digest="0" * 64, restorable=restorable
        ),
        actions=(ActionDefinition("inspect", "Deterministic inspection"),),
        capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
    )
