"""Non-secret run settings and the durable run-manifest identity.

One run's operator-facing configuration lives in a single explicit,
serializable record; the manifest record is what makes a run directory
reopenable with the same identity. Secrets are never held here: only the
*name* of the key environment variable is persisted.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from ..generators.openai_compat import RESPONSE_FORMAT_JSON_OBJECT
from .errors import ContractError
from .models import EvaluationMode
from .pricing import RateCard

DEFAULT_CHECK_TIMEOUT_SECONDS = 600.0
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 120.0
DEFAULT_TRANSPORT_RECOVERY_MAX_RETRIES = 2
DEFAULT_TRANSPORT_RETRY_DELAYS_SECONDS = (2.0, 8.0)
DEFAULT_ARC_CALL_TIMEOUT_SECONDS = 30.0
ARC_OPERATION_MODES = ("normal", "online", "offline")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class TransportRecoverySettings:
    """Versioned coordinator-owned transport recovery policy for one run.

    ``max_retries`` bounds the additional transport attempts a recovery window
    may dispatch; ``retry_delays_seconds`` holds one positive backoff delay per
    retry attempt. Each retried call still consumes the run's model-call budget,
    and a retry may duplicate remote provider work whose cost stays unknown.
    """

    max_retries: int = DEFAULT_TRANSPORT_RECOVERY_MAX_RETRIES
    retry_delays_seconds: tuple[float, ...] = DEFAULT_TRANSPORT_RETRY_DELAYS_SECONDS
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ContractError("unsupported transport recovery settings version")
        if self.max_retries < 0:
            raise ContractError("transport recovery max_retries must not be negative")
        if len(self.retry_delays_seconds) != self.max_retries:
            raise ContractError(
                "transport recovery must hold one positive delay per retry attempt"
            )
        for delay in self.retry_delays_seconds:
            if isinstance(delay, bool) or not isinstance(delay, (int, float)):
                raise ContractError("transport recovery delays must be numbers")
            if delay <= 0:
                raise ContractError("transport recovery delays must be positive")

    def to_payload(self) -> dict[str, object]:
        """Return the recovery policy as a JSON-compatible object."""
        return {
            "version": self.version,
            "max_retries": self.max_retries,
            "retry_delays_seconds": list(self.retry_delays_seconds),
        }

    @classmethod
    def from_payload(cls, value: object) -> TransportRecoverySettings:
        """Decode one persisted recovery policy, failing closed on bad input."""
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ContractError("transport recovery settings must be an object")
        version = value.get("version")
        if version is None:
            version = 1
        if isinstance(version, bool) or not isinstance(version, int):
            raise ContractError("transport recovery version must be an integer")
        max_retries = value.get("max_retries")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise ContractError("transport recovery max_retries must be an integer")
        raw_delays = value.get("retry_delays_seconds")
        if raw_delays is None:
            raw_delays = []
        if not isinstance(raw_delays, (list, tuple)):
            raise ContractError(
                "transport recovery retry_delays_seconds must be an array"
            )
        delays = tuple(
            _required_delay(item, index) for index, item in enumerate(raw_delays)
        )
        return cls(
            max_retries=max_retries,
            retry_delays_seconds=delays,
            version=version,
        )


def _validate_child_env_allow(allow: tuple[str, ...] | None, api_key_env: str) -> None:
    """Fail closed on a malformed allow-list or one exposing the provider key."""
    if allow is None:
        return
    for name in allow:
        if not _ENV_NAME.match(name):
            raise ContractError(
                f"child_env_allow entry {name!r} is not a variable name"
            )
    if api_key_env in allow:
        raise ContractError(
            "child_env_allow must not include the provider key variable"
        )


def _optional_flag(value: Mapping[str, object], key: str) -> bool:
    """A persisted boolean that defaults to False when absent."""
    flag = value.get(key, False)
    if not isinstance(flag, bool):
        raise ContractError(f"run manifest {key} must be a boolean")
    return flag


def _optional_positive_int(value: Mapping[str, object], key: str) -> int | None:
    """A persisted positive integer that is absent when unset."""
    item = value.get(key)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, int) or item < 1:
        raise ContractError(f"run manifest {key} must be a positive integer")
    return item


def _optional_positive_decimal(value: Mapping[str, object], key: str) -> Decimal | None:
    """A persisted positive decimal, stored as a string, absent when unset."""
    item = value.get(key)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, str):
        raise ContractError(f"run manifest {key} must be a decimal string")
    try:
        parsed = Decimal(item)
    except InvalidOperation as exc:
        raise ContractError(f"run manifest {key} must be a decimal string") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ContractError(f"run manifest {key} must be a positive decimal")
    return parsed


def _optional_rate_card(value: Mapping[str, object]) -> RateCard | None:
    """A persisted rate card, absent when unset."""
    item = value.get("rate_card")
    if item is None:
        return None
    if not isinstance(item, Mapping):
        raise ContractError("run manifest rate_card must be an object")
    return RateCard.from_payload(item)


def _validate_provider_options(settings: RunSettings) -> None:
    """Check the optional provider settings: streaming and answer-limit growth."""
    if not isinstance(settings.provider_stream, bool):
        raise ContractError("provider_stream must be a boolean")
    initial = settings.initial_output_tokens
    if initial is None:
        return
    if isinstance(initial, bool) or not isinstance(initial, int) or initial < 1:
        raise ContractError("initial_output_tokens must be a positive integer")
    if initial > settings.max_output_tokens:
        raise ContractError("initial_output_tokens must not exceed max_output_tokens")


def _validate_run_limits(settings: RunSettings) -> None:
    """Check the optional whole-run limits: wall-clock time and tokens."""
    for name, value in (
        ("max_wall_clock_seconds", settings.max_wall_clock_seconds),
        ("max_tokens", settings.max_tokens),
    ):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 1
        ):
            raise ContractError(f"{name} must be a positive integer")
    if settings.max_cost is not None:
        if (
            not isinstance(settings.max_cost, Decimal)
            or not settings.max_cost.is_finite()
            or settings.max_cost <= 0
        ):
            raise ContractError("max_cost must be a positive decimal")
        if settings.rate_card is None:
            raise ContractError("max_cost requires a rate card to price calls")
    if settings.rate_card is not None and not isinstance(settings.rate_card, RateCard):
        raise ContractError("rate_card must be a RateCard")


def _validate_arc(settings: RunSettings) -> None:
    """ARC runs play one game: no check command, no target, a positive limit."""
    if settings.arc is None:
        return
    if not isinstance(settings.arc, ArcRunSettings):
        raise ContractError("arc must be ArcRunSettings")
    if settings.check_command is not None or settings.target is not None:
        raise ContractError("ARC runs take no check_command or target")
    if settings.max_actions < 1:
        raise ContractError("ARC runs need max_actions of at least 1")


def _required_delay(value: object, index: int) -> float:
    """Decode one positive retry delay, failing closed on bad input."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ContractError(
            f"transport recovery delay {index} must be a positive number"
        )
    return float(value)


# A run's non-secret configuration is one explicit, serializable record.
@dataclass(frozen=True, slots=True)
class ArcRunSettings:
    """Which ARC-AGI-3 game a run plays, and how the toolkit is reached.

    The run's ``max_actions`` doubles as the runtime's game-action limit.
    """

    game_id: str
    seed: int = 0
    operation_mode: str = "normal"
    call_timeout_seconds: float = DEFAULT_ARC_CALL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.game_id, str) or not self.game_id.strip():
            raise ContractError("ARC game_id must be a non-empty string")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ContractError("ARC seed must be an integer")
        if self.seed < 0:
            raise ContractError("ARC seed must not be negative")
        if self.operation_mode not in ARC_OPERATION_MODES:
            raise ContractError(
                f"ARC operation_mode must be one of {', '.join(ARC_OPERATION_MODES)}"
            )
        timeout = self.call_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ContractError("ARC call_timeout_seconds must be finite and positive")

    def to_payload(self) -> dict[str, object]:
        """Return the ARC settings as a JSON-compatible object."""
        return {
            "game_id": self.game_id,
            "seed": self.seed,
            "operation_mode": self.operation_mode,
            "call_timeout_seconds": self.call_timeout_seconds,
        }

    @classmethod
    def from_payload(cls, value: object) -> ArcRunSettings:
        """Decode persisted ARC settings, failing closed on bad input."""
        if not isinstance(value, Mapping):
            raise ContractError("run manifest arc must be an object")
        timeout = value.get("call_timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ContractError("run manifest arc call_timeout_seconds is invalid")
        operation_mode = value.get("operation_mode")
        if not isinstance(operation_mode, str):
            raise ContractError("run manifest arc operation_mode is invalid")
        return cls(
            game_id=_required_str(value, "game_id"),
            seed=_required_int(value, "seed"),
            operation_mode=operation_mode,
            call_timeout_seconds=float(timeout),
        )


# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True, slots=True)
class RunSettings:
    """Non-secret configuration for one operator-directed run.

    ``shell`` is resolved before the manifest is written so reopening
    reconstructs the same runtime identity.
    """

    objective: str
    workspace: str
    base_url: str | None
    model: str | None
    api_key_env: str
    max_actions: int
    max_model_calls: int
    max_evaluations: int
    max_steps: int
    token_budget: int
    max_output_tokens: int
    repair_budget: int
    target: str | None = None
    check_command: tuple[str, ...] | None = None
    check_timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS
    shell: str | None = None
    response_format: str | None = None
    provider_timeout: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS
    #: COD-76: stream replies so long reasoning never leaves the connection idle.
    provider_stream: bool = False
    #: COD-88: start each decision at this answer limit and double it on an
    #: exhausted reply, up to ``max_output_tokens``. None keeps one fixed limit.
    initial_output_tokens: int | None = None
    transport_recovery: TransportRecoverySettings = TransportRecoverySettings()
    #: Variables child processes may see; ``None`` passes all but provider secrets.
    child_env_allow: tuple[str, ...] | None = None
    #: Set for an ARC-AGI-3 run; ``workspace`` is then where games are downloaded.
    arc: ArcRunSettings | None = None
    #: COD-86: seconds from session creation until the run stops.
    max_wall_clock_seconds: int | None = None
    #: COD-86: billed provider tokens (prompt plus answer) for the whole run.
    max_tokens: int | None = None
    #: COD-86: dollars of provider spend for the whole run, priced at ``rate_card``.
    max_cost: Decimal | None = None
    #: COD-86: operator-supplied rates; recorded in the manifest so a reopened
    #: run prices at the same rates. Prices each call even without a limit.
    rate_card: RateCard | None = None

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ContractError("run objective must be non-empty")
        if not self.workspace.strip():
            raise ContractError("run workspace must be non-empty")
        if not _ENV_NAME.match(self.api_key_env):
            raise ContractError("api_key_env must be an environment variable name")
        if self.check_command is not None and (
            not self.check_command
            or not all(isinstance(item, str) and item for item in self.check_command)
        ):
            raise ContractError("check_command must be a non-empty argv tuple")
        for name, value in (
            ("max_actions", self.max_actions),
            ("max_model_calls", self.max_model_calls),
            ("max_evaluations", self.max_evaluations),
            ("repair_budget", self.repair_budget),
        ):
            if value < 0:
                raise ContractError(f"{name} must not be negative")
        for name, value in (
            ("max_steps", self.max_steps),
            ("token_budget", self.token_budget),
            ("max_output_tokens", self.max_output_tokens),
        ):
            if value < 1:
                raise ContractError(f"{name} must be positive")
        if self.check_timeout_seconds <= 0:
            raise ContractError("check_timeout_seconds must be positive")
        if self.provider_timeout <= 0:
            raise ContractError("provider_timeout must be positive")
        if self.response_format not in (None, RESPONSE_FORMAT_JSON_OBJECT):
            raise ContractError("response_format must be 'json_object' or None")
        if not isinstance(self.transport_recovery, TransportRecoverySettings):
            raise ContractError("transport_recovery must be TransportRecoverySettings")
        _validate_child_env_allow(self.child_env_allow, self.api_key_env)
        _validate_provider_options(self)
        _validate_run_limits(self)
        _validate_arc(self)

    @property
    def max_recovery_seconds(self) -> float:
        """Return the derived worst-case duration of one full recovery window.

        A window is the initial call plus every configured retry, each bounded
        by the per-call provider timeout, plus the backoff delays between them.
        """
        recovery = self.transport_recovery
        return self.provider_timeout * (1 + recovery.max_retries) + sum(
            recovery.retry_delays_seconds
        )

    @property
    def evaluation_mode(self) -> EvaluationMode:
        """Return the persisted evaluation mode selected by the run settings."""
        return (
            EvaluationMode.EXTERNAL_CHECK
            if self.check_command is not None or self.arc is not None
            else EvaluationMode.OPERATOR
        )

    @property
    def success_evidence(self) -> str:
        """Describe the operator-selected acceptance condition for the task."""
        if self.arc is not None:
            return "the official ARC-AGI-3 scorecard marks the game completed"
        if self.check_command is not None:
            return "operator check command exits zero"
        return "operator evaluation acceptance"

    def to_payload(self) -> dict[str, object]:
        """Return the non-secret settings as a JSON-compatible object.

        ``arc``, ``provider_stream`` and ``initial_output_tokens`` appear only
        when set, so other manifests are unchanged.
        """
        payload: dict[str, object] = {
            "objective": self.objective,
            "workspace": self.workspace,
            "target": self.target,
            "check_command": (
                None if self.check_command is None else list(self.check_command)
            ),
            "check_timeout_seconds": self.check_timeout_seconds,
            "base_url": self.base_url,
            "model": self.model,
            "response_format": self.response_format,
            "provider_timeout": self.provider_timeout,
            "api_key_env": self.api_key_env,
            "shell": self.shell,
            "max_actions": self.max_actions,
            "max_model_calls": self.max_model_calls,
            "max_evaluations": self.max_evaluations,
            "max_steps": self.max_steps,
            "token_budget": self.token_budget,
            "max_output_tokens": self.max_output_tokens,
            "repair_budget": self.repair_budget,
            "transport_recovery": self.transport_recovery.to_payload(),
            "child_env_allow": (
                None if self.child_env_allow is None else list(self.child_env_allow)
            ),
        }
        if self.arc is not None:
            payload["arc"] = self.arc.to_payload()
        if self.max_wall_clock_seconds is not None:
            payload["max_wall_clock_seconds"] = self.max_wall_clock_seconds
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.max_cost is not None:
            payload["max_cost"] = str(self.max_cost)
        if self.rate_card is not None:
            payload["rate_card"] = self.rate_card.to_payload()
        if self.provider_stream:
            payload["provider_stream"] = True
        if self.initial_output_tokens is not None:
            payload["initial_output_tokens"] = self.initial_output_tokens
        return payload

    @classmethod
    def from_payload(cls, value: Mapping[str, object]) -> RunSettings:
        """Decode one persisted settings object, failing closed on bad input."""
        timeout = value.get("check_timeout_seconds")
        if (
            isinstance(timeout, (int, float))
            and not isinstance(timeout, bool)
            and timeout > 0
        ):
            check_timeout_seconds = float(timeout)
        else:
            check_timeout_seconds = DEFAULT_CHECK_TIMEOUT_SECONDS
        p_timeout = value.get("provider_timeout")
        if (
            isinstance(p_timeout, (int, float))
            and not isinstance(p_timeout, bool)
            and p_timeout > 0
        ):
            provider_timeout = float(p_timeout)
        else:
            provider_timeout = DEFAULT_PROVIDER_TIMEOUT_SECONDS
        return cls(
            objective=_required_str(value, "objective"),
            workspace=_required_str(value, "workspace"),
            base_url=_optional_str(value, "base_url"),
            model=_optional_str(value, "model"),
            response_format=_optional_str(value, "response_format"),
            api_key_env=_required_str(value, "api_key_env"),
            max_actions=_required_int(value, "max_actions"),
            max_model_calls=_required_int(value, "max_model_calls"),
            max_evaluations=_required_int(value, "max_evaluations"),
            max_steps=_required_int(value, "max_steps"),
            max_wall_clock_seconds=_optional_positive_int(
                value, "max_wall_clock_seconds"
            ),
            max_tokens=_optional_positive_int(value, "max_tokens"),
            max_cost=_optional_positive_decimal(value, "max_cost"),
            rate_card=_optional_rate_card(value),
            token_budget=_required_int(value, "token_budget"),
            max_output_tokens=_required_int(value, "max_output_tokens"),
            repair_budget=_required_int(value, "repair_budget"),
            target=_optional_str(value, "target"),
            check_command=_optional_str_tuple(value, "check_command"),
            check_timeout_seconds=check_timeout_seconds,
            shell=_optional_str(value, "shell"),
            provider_timeout=provider_timeout,
            provider_stream=_optional_flag(value, "provider_stream"),
            initial_output_tokens=_optional_positive_int(
                value, "initial_output_tokens"
            ),
            transport_recovery=TransportRecoverySettings.from_payload(
                value.get("transport_recovery")
            ),
            child_env_allow=_optional_str_tuple(value, "child_env_allow"),
            arc=(
                None
                if value.get("arc") is None
                else ArcRunSettings.from_payload(value.get("arc"))
            ),
        )


@dataclass(frozen=True, slots=True)
class RunManifestRecord:
    """Durable, non-secret identity of one run directory."""

    session_id: str
    evaluation_mode: str
    created_at: str
    package_version: str
    settings: RunSettings

    def to_payload(self) -> dict[str, object]:
        """Return the manifest as a JSON-compatible object."""
        return {
            "session_id": self.session_id,
            "evaluation_mode": self.evaluation_mode,
            "created_at": self.created_at,
            "package_version": self.package_version,
            "settings": self.settings.to_payload(),
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, object]) -> RunManifestRecord:
        """Decode one persisted manifest, failing closed on bad input."""
        settings = value.get("settings")
        if not isinstance(settings, Mapping):
            raise ContractError("run manifest settings must be an object")
        return cls(
            session_id=_required_str(value, "session_id"),
            evaluation_mode=_required_str(value, "evaluation_mode"),
            created_at=_required_str(value, "created_at"),
            package_version=_required_str(value, "package_version"),
            settings=RunSettings.from_payload(settings),
        )


# --- Settings payload helpers ---


def _required_str(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ContractError(f"run manifest {name} must be a non-empty string")
    return item


def _optional_str(value: Mapping[str, object], name: str) -> str | None:
    item = value.get(name)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ContractError(f"run manifest {name} must be a string or null")
    return item


def _required_int(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ContractError(f"run manifest {name} must be an integer")
    return item


def _optional_str_tuple(
    value: Mapping[str, object], name: str
) -> tuple[str, ...] | None:
    item = value.get(name)
    if item is None:
        return None
    if not isinstance(item, (list, tuple)) or not all(
        isinstance(entry, str) and entry for entry in item
    ):
        raise ContractError(f"run manifest {name} must be a list of strings")
    return tuple(item)
