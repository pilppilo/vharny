"""Mechanical contract for a real local filesystem workspace."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from ..artifacts import ArtifactStore
from ..codec import canonical_json
from ..errors import ContractError
from ..models import (
    ActionDefinition,
    EnvironmentContract,
    JsonValue,
    Observation,
    RecordEnvelope,
    RuntimeCapabilities,
    StateRef,
)

_STRING = {"type": "string"}
_NONNEGATIVE_INTEGER = {"type": "integer", "minimum": 0}


class WorkspaceEnvironment:
    """Describe file and process mechanics without imposing an agent workflow."""

    def __init__(
        self, workspace: str | Path, artifacts: ArtifactStore | None = None
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ContractError("workspace must be a directory")
        self.artifacts = artifacts

    def describe(self) -> EnvironmentContract:
        """Return the typed actions and irreversible trajectory semantics."""
        actions = (
            _action(
                "read",
                "Read a workspace file",
                {
                    "path": _STRING,
                    "start": _NONNEGATIVE_INTEGER,
                    "length": _NONNEGATIVE_INTEGER,
                },
                ("path",),
            ),
            _action(
                "write",
                "Create or replace a text file",
                {"path": _STRING, "content": _STRING},
                ("path", "content"),
            ),
            _action(
                "exec",
                "Start a command as an owned job",
                {
                    "command": _STRING,
                    "cwd": _STRING,
                    "pty": {"type": "boolean"},
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                },
                ("command",),
            ),
            _action(
                "job_read",
                "Read new command output",
                {
                    "job_id": _STRING,
                    "stdout_offset": _NONNEGATIVE_INTEGER,
                    "stderr_offset": _NONNEGATIVE_INTEGER,
                    "artifact_offset": _NONNEGATIVE_INTEGER,
                    "artifact_limit": {"type": "integer", "minimum": 1},
                },
                ("job_id",),
            ),
            _action(
                "job_write",
                "Send input or EOF to a command",
                {"job_id": _STRING, "input": _STRING, "eof": {"type": "boolean"}},
                ("job_id", "input"),
            ),
            _action(
                "job_stop",
                "Stop an owned command process group",
                {"job_id": _STRING},
                ("job_id",),
            ),
        )
        return EnvironmentContract(
            "local-workspace",
            "1",
            StateRef("workspace", str(self.workspace), restorable=False),
            actions,
            RuntimeCapabilities(
                cancellation=False,
                idempotency=True,
                reconciliation=True,
                revisions=False,
            ),
        )

    def initial_observation(
        self,
        session_id: str,
        *,
        objective_version: int = 1,
        target: str | None = None,
    ) -> Observation:
        """Publish a bounded top-level inventory as the initial observation.

        ``target`` is optional operator task data (for example an HTB box
        address). It is included in the observation for the model and never
        selects behavior or restricts capabilities.
        """
        if self.artifacts is None:
            raise ContractError("initial_observation requires an ArtifactStore")
        entries: list[dict[str, JsonValue]] = []
        paths = sorted(self.workspace.iterdir(), key=lambda item: item.name)
        for path in paths[:500]:
            item = path.lstat()
            entries.append(
                {
                    "name": path.name,
                    "kind": (
                        "symlink"
                        if path.is_symlink()
                        else "directory" if path.is_dir() else "file"
                    ),
                    "size": item.st_size,
                }
            )
        payload: dict[str, JsonValue] = {
            "workspace": str(self.workspace),
            "entries": tuple(entries),
            "truncated": len(paths) > len(entries),
        }
        if target is not None:
            payload["target"] = target
        encoded = canonical_json(payload).encode("utf-8")
        reference = self.artifacts.put(
            encoded,
            media_type="application/json",
            provenance="workspace-initial-inventory",
        )
        now = datetime.now(timezone.utc).isoformat()
        record_id = f"workspace-observation-{session_id}-{objective_version}"
        return Observation(
            record_id,
            self.describe().initial_state,
            now,
            "local workspace inventory",
            RecordEnvelope(
                record_id,
                session_id,
                objective_version,
                now,
                "local workspace inventory",
            ),
            raw_record=reference,
        )


def _action(
    name: str,
    description: str,
    properties: Mapping[str, JsonValue],
    required: tuple[str, ...],
) -> ActionDefinition:
    return ActionDefinition(
        name,
        description,
        {
            "type": "object",
            "properties": dict(properties),
            "required": required,
            "additionalProperties": False,
        },
    )
