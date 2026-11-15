"""Durable Runtime port for real workspace and local process effects."""

# The adapter boundaries carry correlated port configuration and action facts;
# action-specific validation remains explicit at the untrusted input boundary.
# pylint: disable=too-many-arguments,too-many-branches

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Mapping

from ..artifacts import ArtifactStore
from ..codec import canonical_json
from ..errors import ContractError, PersistenceError
from ..models import (
    ActionProposal,
    ArtifactRef,
    ComponentIdentity,
    ExecutionReceipt,
    ExecutionStatus,
    StateRef,
    UsageMeasurement,
)
from ..ports import ReconciliationKind, ReconciliationResult
from .process_jobs import ProcessJobs
from .runtime_store import RuntimeStore, proposal_fingerprint

_OUTPUT_CHUNK = 64 * 1024
_DEFAULT_EXCERPT = 32 * 1024


class ProcessRuntime:
    """Synchronous Runtime port backed by a local worker and durable dispatch ledger.

    The worker retains live handles only in memory. On restart, a durable receipt
    is returned when one exists; an intent without a receipt is UNKNOWN and is
    never re-executed. Stored PIDs are diagnostics only and are never signaled.
    Process deadlines are enforced lazily when a job is read or stopped; this
    runtime does not own a background watchdog.
    """

    def __init__(
        self,
        workspace: str | Path,
        artifacts: ArtifactStore,
        store_path: str | Path,
        *,
        timeout_seconds: float = 300.0,
        excerpt_bytes: int = _DEFAULT_EXCERPT,
        shell: str | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ContractError("timeout_seconds must be finite and positive")
        if excerpt_bytes <= 0:
            raise ContractError("excerpt_bytes must be positive")
        if os.name != "posix":
            raise ContractError("local process runtime currently requires POSIX")
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ContractError("workspace must be a directory")
        self.artifacts = artifacts
        self.store = RuntimeStore(store_path)
        self.timeout_seconds = timeout_seconds
        self.excerpt_bytes = excerpt_bytes
        self.shell = shell or os.environ.get("SHELL", "/bin/sh")
        self.jobs = ProcessJobs(
            self.workspace,
            artifacts,
            self.store,
            shell=self.shell,
            timeout_seconds=timeout_seconds,
            excerpt_bytes=excerpt_bytes,
        )

    def identity(self) -> ComponentIdentity:
        """Return the immutable connector identity for this workspace worker."""
        configuration = canonical_json(
            {
                "workspace": str(self.workspace),
                "shell": self.shell,
                "timeout_seconds": self.timeout_seconds,
                "excerpt_bytes": self.excerpt_bytes,
            }
        ).encode("utf-8")
        return ComponentIdentity(
            "local-process-runtime", "1", hashlib.sha256(configuration).hexdigest()
        )

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        """Apply one action with write-ahead intent and ID/content idempotency.

        Invalid arguments are a durable REJECTED receipt, never an escaping
        exception: the coordinator must be able to hand the model feedback
        instead of stranding the session on a receipt that cannot arrive.
        """
        started = time.monotonic()
        fingerprint = proposal_fingerprint(proposal)
        existing, prior = self.store.prepare(proposal.proposal_id, fingerprint)
        if prior is not None:
            return prior
        if existing:
            return self._receipt(
                proposal,
                ExecutionStatus.INDETERMINATE,
                error="dispatch intent exists without a receipt; outcome is unknown",
                duration_ms=int((time.monotonic() - started) * 1000),
                actions=None,
            )
        try:
            self._validate_action(proposal.action_name, proposal.arguments)
            payload = self._dispatch(proposal)
            status = ExecutionStatus.SUCCEEDED
            error = None
        except (ContractError, OSError, PersistenceError) as exc:
            payload = {"error": str(exc), "kind": _error_kind(exc)}
            status = (
                ExecutionStatus.REJECTED
                if isinstance(exc, ContractError)
                else ExecutionStatus.FAILED
            )
            error = f"{_error_kind(exc)}: {exc}"
        artifact = self._publish_json(payload, "process-runtime-result")
        receipt = self._receipt(
            proposal,
            status,
            error=error,
            artifact=artifact,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return self.store.finish(receipt, fingerprint)

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        """Return a durable receipt or UNKNOWN for an unresolved dispatch."""
        receipt = self.store.receipt(operation_id)
        if receipt is not None:
            return ReconciliationResult(ReconciliationKind.RECEIPT, receipt)
        return ReconciliationResult(ReconciliationKind.UNKNOWN)

    def close(self, *, stop_jobs: bool = False) -> None:
        """Close local resources, optionally stopping only jobs still owned here."""
        self.jobs.close(stop_jobs=stop_jobs)
        self.store.close()

    def stop_owned_jobs(self) -> tuple[Mapping[str, object], ...]:
        """Request shutdown of every job this live worker owns and report outcomes.

        Each report carries the observed status and exit code. A job whose
        process cannot be confirmed dead stays ``running``; the caller must
        treat that as an unknown survivor, never as an effect reversal.
        """
        return self.jobs.stop_all()

    def _dispatch(self, proposal: ActionProposal) -> dict[str, object]:
        action = proposal.action_name
        arguments = proposal.arguments
        if action == "read":
            return self._read_file(arguments)
        if action == "write":
            return self._write_file(arguments)
        if action == "exec":
            return self._start_job(proposal)
        if action == "job_read":
            return self.jobs.read(_string(arguments, "job_id"), arguments)
        if action == "job_write":
            job_id = _string(arguments, "job_id")
            data = _string(arguments, "input").encode("utf-8")
            return self.jobs.write(
                job_id,
                data,
                eof=_boolean(arguments, "eof", default=False),
            )
        if action == "job_stop":
            return self.jobs.stop(_string(arguments, "job_id"))
        raise ContractError(f"unsupported workspace action: {action}")

    def _read_file(self, arguments: Mapping[str, object]) -> dict[str, object]:
        path = self._resolve_path(_string(arguments, "path"))
        start = _integer(arguments, "start", default=0)
        length = _integer(arguments, "length", default=self.excerpt_bytes)
        if start < 0 or length < 0:
            raise ContractError("read start and length must be nonnegative")
        length = min(length, self.excerpt_bytes)
        try:
            artifacts, selected, total_bytes = self._read_file_snapshot(
                path, start, length
            )
        except OSError as exc:
            raise PersistenceError(f"could not read {path}: {exc}") from exc
        try:
            excerpt = selected.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            excerpt = selected.decode("utf-8", errors="replace")
            encoding = "utf-8-replacement"
        return {
            "path": str(path.relative_to(self.workspace)),
            "offset": start,
            "excerpt": excerpt,
            "encoding": encoding,
            "truncated": start + len(selected) < total_bytes,
            "total_bytes": total_bytes,
            "artifacts": tuple(_artifact_json(item) for item in artifacts),
        }

    def _write_file(self, arguments: Mapping[str, object]) -> dict[str, object]:
        path = self._resolve_path(_string(arguments, "path"))
        content = _string(arguments, "content").encode("utf-8")
        previous = (
            self._publish_file_chunks(path, "workspace-file-before-write")
            if path.is_file()
            else ()
        )
        new_content = self.artifacts.put(
            content,
            media_type="text/plain; charset=utf-8",
            provenance="workspace-file-write",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".vharness-write-", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _sync_directory(path.parent)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise PersistenceError(f"could not write {path}: {exc}") from exc
        return {
            "path": str(path.relative_to(self.workspace)),
            "previous": tuple(_artifact_json(item) for item in previous),
            "written": _artifact_json(new_content),
            "bytes_written": len(content),
        }

    def _start_job(self, proposal: ActionProposal) -> dict[str, object]:
        arguments = proposal.arguments
        command = _string(arguments, "command")
        cwd = self._resolve_path(_string(arguments, "cwd", default="."))
        if not cwd.is_dir():
            raise ContractError("exec cwd must be a directory")
        timeout_value = arguments.get("timeout_seconds")
        timeout = (
            self.timeout_seconds
            if timeout_value is None
            else float(_integer(arguments, "timeout_seconds", default=1))
        )
        if timeout <= 0:
            raise ContractError("timeout_seconds must be positive")
        return self.jobs.start(
            proposal.proposal_id,
            _job_id(proposal),
            command,
            cwd,
            pty_mode=_boolean(arguments, "pty", default=False),
            timeout_seconds=timeout,
        )

    def _resolve_path(self, value: str) -> Path:
        candidate = (self.workspace / value).resolve(strict=False)
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise ContractError(
                "workspace path escapes the selected directory"
            ) from exc
        return candidate

    def _publish_file_chunks(
        self, path: Path, provenance: str
    ) -> tuple[ArtifactRef, ...]:
        references: list[ArtifactRef] = []
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(_OUTPUT_CHUNK):
                    references.append(
                        self.artifacts.put(
                            chunk,
                            media_type="application/octet-stream",
                            provenance=provenance,
                        )
                    )
        except OSError as exc:
            raise PersistenceError(
                f"could not publish file content {path}: {exc}"
            ) from exc
        return tuple(references)

    def _read_file_snapshot(
        self, path: Path, start: int, length: int
    ) -> tuple[tuple[ArtifactRef, ...], bytes, int]:
        references: list[ArtifactRef] = []
        selected = bytearray()
        total_bytes = 0
        window_end = start + length
        with path.open("rb") as stream:
            while chunk := stream.read(_OUTPUT_CHUNK):
                chunk_start = total_bytes
                chunk_end = chunk_start + len(chunk)
                left = max(start, chunk_start)
                right = min(window_end, chunk_end)
                if left < right:
                    selected.extend(chunk[left - chunk_start : right - chunk_start])
                references.append(
                    self.artifacts.put(
                        chunk,
                        media_type="application/octet-stream",
                        provenance="workspace-file-read",
                    )
                )
                total_bytes = chunk_end
        return tuple(references), bytes(selected), total_bytes

    def _publish_json(
        self, payload: Mapping[str, object], provenance: str
    ) -> ArtifactRef:
        return self.artifacts.put(
            canonical_json(payload).encode("utf-8"),
            media_type="application/json",
            provenance=provenance,
        )

    def _receipt(
        self,
        proposal: ActionProposal,
        status: ExecutionStatus,
        *,
        error: str | None = None,
        artifact: ArtifactRef | None = None,
        duration_ms: int = 0,
        actions: int | None = 1,
    ) -> ExecutionReceipt:
        return ExecutionReceipt(
            receipt_id=f"receipt-{uuid.uuid4().hex}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=status,
            operation_id=proposal.proposal_id,
            state=StateRef(
                "workspace-runtime",
                f"trajectory:{proposal.proposal_id}",
                restorable=False,
            ),
            error=error,
            usage=(
                None
                if actions is None
                else UsageMeasurement(
                    measurement_id=f"runtime-usage-{proposal.proposal_id}",
                    actions=actions,
                    duration_ms=duration_ms,
                )
            ),
            raw_artifact=artifact,
        )

    def _validate_action(self, action: str, arguments: Mapping[str, object]) -> None:
        required: dict[str, tuple[str, ...]] = {
            "read": ("path",),
            "write": ("path", "content"),
            "exec": ("command",),
            "job_read": ("job_id",),
            "job_write": ("job_id", "input"),
            "job_stop": ("job_id",),
        }
        if action not in required:
            raise ContractError(f"unsupported workspace action: {action}")
        if not isinstance(arguments, Mapping):
            raise ContractError("action arguments must be an object")
        for name in required[action]:
            if name not in arguments:
                raise ContractError(f"{action} requires argument {name}")
        for key, value in arguments.items():
            if not isinstance(key, str) or not isinstance(
                value, (str, int, float, bool, tuple, list, dict)
            ):
                raise ContractError("action arguments must be JSON-compatible")
        if action in ("read", "write"):
            path = self._resolve_path(_string(arguments, "path"))
            if action == "read" and not path.is_file():
                raise ContractError("read path must name an existing file")
        if action == "write":
            _string(arguments, "content")
        if action == "exec":
            _string(arguments, "command")
            _string(arguments, "cwd", default=".")
            _boolean(arguments, "pty", default=False)
            if "timeout_seconds" in arguments:
                _integer(arguments, "timeout_seconds", default=1)
            cwd = self._resolve_path(_string(arguments, "cwd", default="."))
            if not cwd.is_dir():
                raise ContractError("exec cwd must be a directory")
        if action == "read":
            _integer(arguments, "start", default=0)
            _integer(arguments, "length", default=self.excerpt_bytes)
        if action in ("job_read", "job_write", "job_stop"):
            _string(arguments, "job_id")
        if action == "job_write":
            _string(arguments, "input")
            _boolean(arguments, "eof", default=False)
        if action == "job_read":
            _integer(arguments, "stdout_offset", default=0)
            _integer(arguments, "stderr_offset", default=0)
            _integer(arguments, "artifact_offset", default=0)
            _integer(arguments, "artifact_limit", default=32)


def _string(
    arguments: Mapping[str, object], name: str, *, default: str | None = None
) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")
    return value


def _integer(arguments: Mapping[str, object], name: str, *, default: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be an integer")
    return value


def _boolean(arguments: Mapping[str, object], name: str, *, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ContractError(f"{name} must be a boolean")
    return value


def _job_id(proposal: ActionProposal) -> str:
    return f"job-{proposal.proposal_id}"


def _artifact_json(reference: ArtifactRef) -> dict[str, object]:
    return {
        "digest": reference.digest,
        "size": reference.size,
        "media_type": reference.media_type,
        "provenance": reference.provenance,
    }


def _error_kind(error: BaseException) -> str:
    if isinstance(error, FileNotFoundError):
        return "tool_not_found_or_path_missing"
    if isinstance(error, PermissionError):
        return "permission_error"
    return "execution_error"


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
