"""Non-secret run settings and the durable run-manifest identity.

One run's operator-facing configuration lives in a single explicit,
serializable record; the manifest record is what makes a run directory
reopenable with the same identity. Secrets are never held here: only the
*name* of the key environment variable is persisted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from ..generators.openai_compat import RESPONSE_FORMAT_JSON_OBJECT
from .errors import ContractError
from .models import EvaluationMode

DEFAULT_CHECK_TIMEOUT_SECONDS = 600.0
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 120.0
DEFAULT_TRANSPORT_RECOVERY_MAX_RETRIES = 2
DEFAULT_TRANSPORT_RETRY_DELAYS_SECONDS = (2.0, 8.0)
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


def _required_delay(value: object, index: int) -> float:
    """Decode one positive retry delay, failing closed on bad input."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ContractError(
            f"transport recovery delay {index} must be a positive number"
        )
    return float(value)


# A run's non-secret configuration is one explicit, serializable record.
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
    transport_recovery: TransportRecoverySettings = TransportRecoverySettings()

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
            if self.check_command is not None
            else EvaluationMode.OPERATOR
        )

    @property
    def success_evidence(self) -> str:
        """Describe the operator-selected acceptance condition for the task."""
        if self.check_command is not None:
            return "operator check command exits zero"
        return "operator evaluation acceptance"

    def to_payload(self) -> dict[str, object]:
        """Return the non-secret settings as a JSON-compatible object."""
        return {
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
        }

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
            token_budget=_required_int(value, "token_budget"),
            max_output_tokens=_required_int(value, "max_output_tokens"),
            repair_budget=_required_int(value, "repair_budget"),
            target=_optional_str(value, "target"),
            check_command=_optional_str_tuple(value, "check_command"),
            check_timeout_seconds=check_timeout_seconds,
            shell=_optional_str(value, "shell"),
            provider_timeout=provider_timeout,
            transport_recovery=TransportRecoverySettings.from_payload(
                value.get("transport_recovery")
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
