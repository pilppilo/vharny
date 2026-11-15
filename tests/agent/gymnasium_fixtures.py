"""In-process Gymnasium-style fakes used by the agent environment tests."""

from __future__ import annotations

import math
from typing import Mapping, Protocol

from vharness.agent.artifacts import ArtifactStore
from vharness.agent.environments import GymnasiumRuntimeCore, _action_receipt
from vharness.agent.errors import ContractError
from vharness.agent.models import (
    ActionProposal,
    ComponentIdentity,
    EnvironmentContract,
    ExecutionReceipt,
    ExecutionStatus,
    JsonValue,
    frozen_mapping,
)


class GymnasiumEnv(Protocol):
    """Official Gymnasium reset/step surface. Adapters do not rescore it."""

    def reset(self) -> tuple[JsonValue, Mapping[str, JsonValue]]:
        """Return the official observation and info."""
        ...

    def step(
        self, action: JsonValue
    ) -> tuple[JsonValue, float, bool, bool, Mapping[str, JsonValue]]:
        """Return observation, reward, terminated, truncated, and info."""
        ...


class DiscreteReach:
    """Deterministic discrete env: increment observation until it reaches 3."""

    def __init__(self) -> None:
        self._observation = 0

    def reset(self) -> tuple[JsonValue, Mapping[str, JsonValue]]:
        """Return observation 0."""
        self._observation = 0
        return 0, {}

    def step(
        self, action: JsonValue
    ) -> tuple[JsonValue, float, bool, bool, Mapping[str, JsonValue]]:
        """Apply an integer action; action 1 increments toward 3."""
        if action == 1:
            self._observation = min(self._observation + 1, 3)
        terminated = self._observation >= 3
        reward = 1.0 if terminated else 0.0
        return self._observation, reward, terminated, False, {}


class ContinuousDrive:
    """Deterministic 1-D plant: add the action to position until near zero."""

    def __init__(self) -> None:
        self._observation = 1.0

    def reset(self) -> tuple[JsonValue, Mapping[str, JsonValue]]:
        """Return position 1.0."""
        self._observation = 1.0
        return 1.0, {}

    def step(
        self, action: JsonValue
    ) -> tuple[JsonValue, float, bool, bool, Mapping[str, JsonValue]]:
        """Apply a real-valued delta. Termination is |position| <= 0.05."""
        if not isinstance(action, float) or isinstance(action, bool):
            raise ContractError("continuous Gymnasium action must be a finite float")
        self._observation += action
        terminated = abs(self._observation) <= 0.05
        reward = 0.0 if terminated else -abs(self._observation)
        return self._observation, reward, terminated, False, {}


class GymnasiumAdapter(GymnasiumRuntimeCore):
    """Environment/runtime adapter around official reset/step values."""

    def __init__(
        self,
        env: GymnasiumEnv,
        *,
        environment_id: str,
        action_type: str,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        if action_type not in {"integer", "string"}:
            raise ContractError("Gymnasium action_type must be integer or string")
        super().__init__(environment_id, artifacts, "gymnasium-fixture-tuple")
        self._env = env
        self._action_type = action_type

    def describe(self) -> EnvironmentContract:
        """Declare official reset/step actions for this environment."""
        action_schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": {"action": {"type": self._action_type}},
            "required": ("action",),
            "additionalProperties": False,
        }
        return self._contract(action_schema)

    def identity(self) -> ComponentIdentity:
        """Identify the Gymnasium-fixture runtime."""
        return ComponentIdentity("gymnasium-fixture-runtime", "v1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Call official reset or step and retain the returned values."""
        if proposal.action_name == "reset":
            observation, info = self._env.reset()
            self._clear_episode(observation, _json_object_map(info))
        elif proposal.action_name == "step":
            action = _gym_action(proposal, self._action_type)
            observation, reward, terminated, truncated, info = self._env.step(action)
            self._record_step(
                observation, reward, terminated, truncated, _json_object_map(info)
            )
        else:
            return self._fail_unknown(proposal)
        return self._succeed(proposal)


def discrete_gymnasium_fixture() -> GymnasiumAdapter:
    """Discrete control fixture with official reset/step values."""
    return GymnasiumAdapter(
        DiscreteReach(),
        environment_id="gymnasium-discrete",
        action_type="integer",
    )


def continuous_gymnasium_fixture() -> GymnasiumAdapter:
    """Continuous/control fixture with official reset/step values."""
    return GymnasiumAdapter(
        ContinuousDrive(),
        environment_id="gymnasium-continuous",
        action_type="string",
    )


def _gym_action(proposal: ActionProposal, action_type: str) -> JsonValue:
    value = proposal.arguments.get("action")
    if action_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError("discrete Gymnasium action must be an integer")
        return value
    if not isinstance(value, str):
        raise ContractError("continuous Gymnasium action must be a string")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ContractError(
            "continuous Gymnasium action must be a finite float"
        ) from exc
    if not math.isfinite(parsed):
        raise ContractError("continuous Gymnasium action must be a finite float")
    return parsed


def _json_object_map(
    value: Mapping[str, JsonValue] | Mapping[str, object],
) -> dict[str, JsonValue]:
    copied: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            copied[key] = item
    return copied
