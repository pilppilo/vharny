"""FrozenLake checkpoint and CartPole diagnostic on the official runtime."""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping

from ..artifacts import ArtifactStore
from ..codec import canonical_json
from ..environments import (
    FROZENLAKE_GOAL,
    GymnasiumEvaluator,
    GymnasiumRuntimeCore,
    NativeGoal,
    _digest,
    goal_digest,
    published_observation,
)
from ..errors import ContractError
from ..models import (
    ActionProposal,
    ComponentIdentity,
    EnvironmentContract,
    ExecutionReceipt,
    JsonValue,
    Observation,
    frozen_mapping,
)

try:
    import gymnasium as gymnasium_lib
except ImportError as exc:  # pragma: no cover - exercised when extra is missing
    gymnasium_lib = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

FROZENLAKE_ENV_ID = "FrozenLake-v1"
CARTPOLE_ENV_ID = "CartPole-v1"
FROZENLAKE_MAKE_KWARGS: Mapping[str, JsonValue] = MappingProxyType(
    {"is_slippery": False}
)
_SUPPORTED_ENV_IDS = frozenset({FROZENLAKE_ENV_ID, CARTPOLE_ENV_ID})
OFFICIAL_RUNTIME_ID = "gymnasium-official-runtime"
OFFICIAL_EVALUATOR_ID = "gymnasium-official-evaluator"
OFFICIAL_EVALUATOR_CONTRACT = "gymnasium-official-evaluator-v1"


def require_gymnasium() -> Any:
    """Return the gymnasium module or raise an installable-extra error.

    Typed as Any because the optional extra has no in-repo stubs.
    """
    if gymnasium_lib is None:
        raise ContractError(
            "Gymnasium extra is missing; install vharness[gymnasium]"
        ) from _IMPORT_ERROR
    return gymnasium_lib


def json_safe_gym_value(value: object) -> JsonValue:
    """Convert official Gymnasium values into JSON-safe domain data."""
    # Numpy scalars, mappings, and sequences each need an explicit JSON form.
    # pylint: disable=too-many-return-statements
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("Gymnasium numeric values must be finite")
        return float(value)
    item = getattr(value, "item", None)
    if callable(item) and getattr(value, "ndim", None) == 0:
        return json_safe_gym_value(item())
    if isinstance(value, Mapping):
        return {str(key): json_safe_gym_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(json_safe_gym_value(item) for item in value)
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return json_safe_gym_value(tolist())
    raise ContractError(f"unsupported Gymnasium value type {type(value)!r}")


def discrete_action_schema(*, minimum: int, maximum: int) -> dict[str, JsonValue]:
    """Publish Discrete bounds on the step action."""
    if minimum > maximum:
        raise ContractError("Discrete action bounds are inverted")
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "integer",
                "minimum": minimum,
                "maximum": maximum,
            }
        },
        "required": ("action",),
        "additionalProperties": False,
    }


# pylint: disable=too-many-instance-attributes
class OfficialGymnasiumRuntime(GymnasiumRuntimeCore):
    """Official runtime for the FrozenLake checkpoint and CartPole diagnostic."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        env_id: str = FROZENLAKE_ENV_ID,
        seed: int = 0,
        make_kwargs: Mapping[str, JsonValue] | None = None,
    ) -> None:
        if env_id not in _SUPPORTED_ENV_IDS:
            raise ContractError(
                "official Gymnasium runtime supports the FrozenLake-v1 "
                "checkpoint and CartPole-v1 control diagnostic only"
            )
        defaults = FROZENLAKE_MAKE_KWARGS if env_id == FROZENLAKE_ENV_ID else {}
        kwargs = dict(defaults if make_kwargs is None else make_kwargs)
        library = require_gymnasium()
        env = library.make(env_id, **kwargs)
        space = env.action_space
        if type(space).__name__ != "Discrete":
            env.close()
            raise ContractError("official Gymnasium checkpoint supports Discrete only")
        start = int(getattr(space, "start", 0))
        count = int(space.n)
        super().__init__(env_id, artifacts, "gymnasium-official-tuple")
        self.seed = seed
        self._make_kwargs = frozen_mapping(kwargs)
        self._library = library
        self._env = env
        self._closed = False
        self._action_minimum = start
        self._action_maximum = start + count - 1
        try:
            observation, info = env.reset(seed=seed)
            self._record_reset(observation, info)
        except Exception:
            env.close()
            raise

    @property
    def closed(self) -> bool:
        """Whether close() has already released the official env."""
        return self._closed

    def describe(self) -> EnvironmentContract:
        """Declare official reset/step using the post-reset observation."""
        return self._contract(
            discrete_action_schema(
                minimum=self._action_minimum,
                maximum=self._action_maximum,
            )
        )

    def identity(self) -> ComponentIdentity:
        """Identify Gymnasium version, env, seed, and make configuration."""
        configuration = canonical_json(
            {
                "env_id": self.environment_id,
                "make_kwargs": dict(self._make_kwargs),
                "seed": self.seed,
            }
        )
        return ComponentIdentity(
            OFFICIAL_RUNTIME_ID,
            str(self._library.__version__),
            configuration_digest=_digest(configuration),
        )

    def initial_observation(
        self, session_id: str, *, objective_version: int = 1
    ) -> Observation:
        """Publish the official construction reset as the session observation."""
        return published_observation(
            session_id=session_id,
            state=self.describe().initial_state,
            provenance="gymnasium-official-reset",
            artifact=self._tuple_artifact("gymnasium-official-initial"),
            objective_version=objective_version,
        )

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Call official reset or step and retain the returned tuple."""
        self._require_open()
        if proposal.action_name == "reset":
            observation, info = self._env.reset(seed=self.seed)
            self._record_reset(observation, info)
        elif proposal.action_name == "step":
            action = proposal.arguments.get("action")
            if isinstance(action, bool) or not isinstance(action, int):
                raise ContractError("discrete Gymnasium action must be an integer")
            observation, reward, terminated, truncated, info = self._env.step(action)
            self._record_step(
                json_safe_gym_value(observation),
                _finite_float(json_safe_gym_value(reward)),
                _boolean(json_safe_gym_value(terminated), "terminated"),
                _boolean(json_safe_gym_value(truncated), "truncated"),
                _mapping(json_safe_gym_value(info)),
            )
        else:
            return self._fail_unknown(proposal)
        return self._succeed(proposal)

    def close(self) -> None:
        """Release the official Gymnasium environment."""
        if self._closed:
            return
        self._env.close()
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise ContractError("official Gymnasium runtime is closed")

    def _record_reset(self, observation: object, info: object) -> None:
        self._clear_episode(
            json_safe_gym_value(observation),
            _mapping(json_safe_gym_value(info)),
        )


class OfficialGymnasiumEvaluator(GymnasiumEvaluator):
    """Evaluator-owned native goal over requested-state official tuples."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        goal: NativeGoal = FROZENLAKE_GOAL,
        *,
        gymnasium_version: str | None = None,
    ) -> None:
        library = require_gymnasium()
        super().__init__(
            artifacts,
            goal,
            component_id=OFFICIAL_EVALUATOR_ID,
            version=gymnasium_version or str(library.__version__),
            configuration_digest=goal_digest(goal),
            provenance="gymnasium-official-evaluator",
        )


def _mapping(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ContractError("Gymnasium info must be a mapping")
    return {str(key): item for key, item in value.items()}


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"Gymnasium {name} must be a boolean")
    return value


def _finite_float(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("Gymnasium reward must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ContractError("Gymnasium reward must be a finite number")
    return parsed
