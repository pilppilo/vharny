"""Owned local process groups, PTYs, and complete chunked output capture."""

# These boundaries intentionally gather related launch options and live-handle
# facts; _pump handles pipe/PTY and EOF/error cases explicitly. Process and spool
# handles deliberately outlive their creation scopes until close/stop.
# pylint: disable=too-many-arguments,too-many-branches,consider-using-with

from __future__ import annotations

import errno
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Mapping

try:
    import pty
except ImportError:  # POSIX-only module; pipe mode is unavailable on Windows too.
    pty = None  # type: ignore[assignment]

from ..artifacts import ArtifactStore
from ..errors import ContractError
from .child_environment import ChildEnvironment
from .runtime_store import RuntimeStore

_OUTPUT_CHUNK = 64 * 1024
_PUMP_BYTE_BUDGET = 256 * 1024
_PUMP_TIME_BUDGET = 0.05


@dataclass
# pylint: disable=too-many-instance-attributes
class _Job:
    """Process-local handle and spooled output for one launched command."""

    job_id: str
    process: subprocess.Popen[bytes]
    pty_mode: bool
    deadline: float
    stdout_file: BinaryIO
    stderr_file: BinaryIO
    master_fd: int | None = None
    output_artifacts: list[dict[str, object]] = field(default_factory=list)
    timed_out: bool = False


class ProcessJobs:
    """Own only live handles; persisted PIDs are diagnostics, never recovery handles."""

    def __init__(
        self,
        workspace: Path,
        artifacts: ArtifactStore,
        store: RuntimeStore,
        *,
        shell: str,
        timeout_seconds: float,
        excerpt_bytes: int,
        environment: ChildEnvironment,
    ) -> None:
        self.workspace = workspace
        self.environment = environment
        self.artifacts = artifacts
        self.store = store
        self.shell = shell
        self.timeout_seconds = timeout_seconds
        self.excerpt_bytes = excerpt_bytes
        self._jobs: dict[str, _Job] = {}

    def start(
        self,
        proposal_id: str,
        job_id: str,
        command: str,
        cwd: Path,
        *,
        pty_mode: bool,
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Start a process group and durably retain its identity before receipt."""
        if job_id in self._jobs:
            raise ContractError("job ID is already owned by this runtime")
        # The spools outlive this method and are closed with the owned job.
        stdout_file = tempfile.TemporaryFile()  # pylint: disable=consider-using-with
        stderr_file = tempfile.TemporaryFile()  # pylint: disable=consider-using-with
        master_fd: int | None = None
        child_env = self.environment.build(os.environ)
        try:
            if pty_mode:
                if pty is None:
                    raise ContractError("PTY jobs require the POSIX pty module")
                master_fd, slave_fd = pty.openpty()
                process = subprocess.Popen(  # pylint: disable=consider-using-with
                    [sys.executable, "-c", _PTY_BOOTSTRAP, self.shell, command],
                    cwd=cwd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    close_fds=True,
                    env=child_env,
                )
                os.close(slave_fd)
                os.set_blocking(master_fd, False)
            else:
                # Process lifetime is owned by the returned job, not this scope.
                process = subprocess.Popen(
                    command,
                    shell=True,
                    executable=self.shell,
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    bufsize=0,
                    env=child_env,
                )
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        os.set_blocking(stream.fileno(), False)
            job = _Job(
                job_id,
                process,
                pty_mode,
                time.monotonic() + timeout_seconds,
                stdout_file,
                stderr_file,
                master_fd,
            )
            self._jobs[job_id] = job
            self.store.record_job(
                proposal_id,
                job_id,
                process.pid,
                os.getpgid(process.pid),
                os.getsid(process.pid),
                _linux_start_marker(process.pid),
            )
            self._pump(job)
            return {
                "job_id": job_id,
                **self.read(job_id, {"stdout_offset": 0, "stderr_offset": 0}),
            }
        except BaseException:
            if job_id not in self._jobs:
                stdout_file.close()
                stderr_file.close()
                if master_fd is not None:
                    os.close(master_fd)
            raise

    def read(self, job_id: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """Pump output and return a bounded excerpt from caller-owned offsets."""
        job = self._owned_job(job_id)
        stdout_offset = _integer(arguments, "stdout_offset", default=0)
        stderr_offset = _integer(arguments, "stderr_offset", default=0)
        if stdout_offset < 0 or stderr_offset < 0:
            raise ContractError("job output offsets must be nonnegative")
        output = self._pump(job)
        stdout = _read_spool(job.stdout_file, stdout_offset, self.excerpt_bytes)
        stderr = _read_spool(job.stderr_file, stderr_offset, self.excerpt_bytes)
        artifact_offset = _integer(arguments, "artifact_offset", default=0)
        artifact_limit = min(_integer(arguments, "artifact_limit", default=32), 128)
        if artifact_offset < 0 or artifact_limit < 1:
            raise ContractError("artifact page offset/limit are invalid")
        artifact_page = job.output_artifacts[
            artifact_offset : artifact_offset + artifact_limit
        ]
        return {
            "job_id": job.job_id,
            "status": output["status"],
            "exit_code": output["exit_code"],
            "stdout_offset": stdout_offset,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stdout_next_offset": stdout_offset + len(stdout),
            "stderr_offset": stderr_offset,
            "stderr": stderr.decode("utf-8", errors="replace"),
            "stderr_next_offset": stderr_offset + len(stderr),
            "output_artifacts": tuple(artifact_page),
            "artifact_offset": artifact_offset,
            "artifact_next_offset": artifact_offset + len(artifact_page),
            "artifact_total": len(job.output_artifacts),
            "timed_out": job.timed_out,
        }

    def write(self, job_id: str, data: bytes, *, eof: bool) -> dict[str, object]:
        """Write input to an owned pipe/terminal and optionally send EOF."""
        job = self._owned_job(job_id)
        if job.process.poll() is not None:
            raise ContractError("cannot write to an exited job")
        if job.pty_mode:
            if eof:
                data += b"\x04"
            _write_fd(job.master_fd, data)
        else:
            stream = job.process.stdin
            if stream is None or stream.closed:
                raise ContractError("job input is already closed")
            if data:
                stream.write(data)
                stream.flush()
            if eof:
                stream.close()
        return {"job_id": job_id, "bytes_written": len(data), "eof": eof}

    def stop(self, job_id: str) -> dict[str, object]:
        """Terminate the process group only while this owner holds its handle."""
        job = self._owned_job(job_id)
        if job.process.poll() is None:
            _terminate_group(job.process, 1.0)
            self._pump(job)
        return {
            "job_id": job_id,
            "status": "exited" if job.process.poll() is not None else "running",
            "exit_code": job.process.poll(),
        }

    def stop_all(self) -> tuple[dict[str, object], ...]:
        """Request shutdown of every owned job and report each observed outcome."""
        return tuple(self.stop(job_id) for job_id in tuple(self._jobs))

    def close(self, *, stop_jobs: bool) -> None:
        """Close local handles; optionally stop jobs owned by this live instance."""
        for job_id, job in tuple(self._jobs.items()):
            if stop_jobs and job.process.poll() is None:
                self.stop(job_id)
            if job.master_fd is not None:
                os.close(job.master_fd)
                job.master_fd = None
            for stream in (job.process.stdin, job.process.stdout, job.process.stderr):
                if stream is not None:
                    stream.close()
            job.stdout_file.close()
            job.stderr_file.close()
        self._jobs.clear()

    def _pump(self, job: _Job) -> dict[str, object]:
        if time.monotonic() >= job.deadline and job.process.poll() is None:
            job.timed_out = True
            _terminate_group(job.process, 0.5)
        streams: list[tuple[int, str]] = []
        if job.pty_mode:
            if job.master_fd is not None:
                streams.append((job.master_fd, "stdout"))
        else:
            if job.process.stdout is not None:
                streams.append((job.process.stdout.fileno(), "stdout"))
            if job.process.stderr is not None:
                streams.append((job.process.stderr.fileno(), "stderr"))
        selector = selectors.DefaultSelector()
        deadline = time.monotonic() + _PUMP_TIME_BUDGET
        total_read = 0
        try:
            for fd, name in streams:
                try:
                    selector.register(fd, selectors.EVENT_READ, name)
                except (OSError, ValueError):
                    continue
            while time.monotonic() < deadline and total_read < _PUMP_BYTE_BUDGET:
                events = selector.select(timeout=0)
                if not events:
                    break
                for key, _ in events:
                    try:
                        chunk = os.read(key.fd, _OUTPUT_CHUNK)
                    except OSError as exc:
                        if exc.errno in (errno.EIO, errno.EBADF):
                            selector.unregister(key.fd)
                            continue
                        raise
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    total_read += len(chunk)
                    spool = job.stdout_file if key.data == "stdout" else job.stderr_file
                    spool.seek(0, os.SEEK_END)
                    spool.write(chunk)
                    spool.flush()
                    reference = self.artifacts.put(
                        chunk,
                        media_type="application/octet-stream",
                        provenance=f"process-output-{key.data}",
                    )
                    job.output_artifacts.append(
                        {
                            "artifact": {
                                "digest": reference.digest,
                                "size": reference.size,
                                "media_type": reference.media_type,
                                "provenance": reference.provenance,
                            },
                            "stream": key.data,
                            "offset": spool.tell() - len(chunk),
                            "length": len(chunk),
                        }
                    )
        finally:
            selector.close()
        return {
            "status": "running" if job.process.poll() is None else "exited",
            "exit_code": job.process.poll(),
        }

    def _owned_job(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ContractError("job is unknown to this runtime owner")
        return job


def _integer(arguments: Mapping[str, object], name: str, *, default: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be an integer")
    return value


def _read_spool(stream: BinaryIO, offset: int, limit: int) -> bytes:
    stream.seek(0, os.SEEK_END)
    total = stream.tell()
    if offset > total:
        raise ContractError("job output offset is beyond the available output")
    stream.seek(offset)
    return stream.read(limit)


def _write_fd(fd: int | None, data: bytes) -> None:
    if fd is None:
        raise ContractError("job has no interactive terminal")
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def terminate_process_group(process: subprocess.Popen[bytes], grace: float) -> str:
    """Terminate one owned process group and report uncertain survivors.

    Escalation follows group membership, not the leader's fate: a child that
    ignores ``SIGTERM`` is killed even when its leader already exited. Returns
    ``"none"`` only when the group is confirmed empty; ``"unknown"`` means a
    descendant may survive the kill.
    """
    pgid = process.pid
    _signal_group(pgid, signal.SIGTERM)
    _reap_leader(process, grace)
    if _group_exists(pgid):
        _signal_group(pgid, signal.SIGKILL)
        _reap_leader(process, grace)
    deadline = time.monotonic() + grace
    while _group_exists(pgid):
        if time.monotonic() >= deadline:
            return "unknown"
        time.sleep(0.02)
    return "none"


def _reap_leader(process: subprocess.Popen[bytes], grace: float) -> None:
    """Wait for the group leader without blocking past ``grace``."""
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        # The bounded wait elapsed; group escalation handles any survivor.
        pass


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_group(process: subprocess.Popen[bytes], grace: float) -> None:
    terminate_process_group(process, grace)


_PTY_BOOTSTRAP = (
    "import fcntl, os, sys, termios; "
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
    "os.execv(sys.argv[1], [sys.argv[1], '-c', sys.argv[2]])"
)


def _linux_start_marker(pid: int) -> str | None:
    """Read Linux process start ticks to distinguish PID reuse in diagnostics."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return None
    closing_paren = stat.rfind(")")
    if closing_paren < 0:
        return None
    fields = stat[closing_paren + 1 :].split()
    return fields[19] if len(fields) > 19 else None
