"""Local control transport: a bounded inbox and a same-host socket surface.

The transport carries serialized frames only. It holds no SQL, no durable
state, and no scheduling authority: the coordinator drains the inbox, decodes
each frame, and answers admission. The durable applied/rejected disposition is
recorded later by the session writer, never here.
"""

from __future__ import annotations

import fcntl
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Full, Queue
import socket
import stat
import threading
from typing import BinaryIO, Mapping

from .codec import canonical_json, parse_json_object, to_json_value
from .errors import ContractError
from .models import JsonValue, OperatorCommand

MAX_FRAME_BYTES = 64 * 1024
MAX_PENDING_REQUESTS = 64
DEFAULT_FRAME_TIMEOUT_SECONDS = 10.0
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 30.0
_ACCEPT_POLL_SECONDS = 0.2

ACCEPTED = "accepted"
REJECTED = "rejected"

CONTROL_SOCKET_NAME = "control.sock"


def control_socket_path(run_dir: Path) -> Path:
    """Return the control socket path owned by one run directory."""
    return run_dir / CONTROL_SOCKET_NAME


@dataclass(frozen=True, slots=True)
class ControlReply:
    """The transport answer to one control frame."""

    status: str
    reason: str | None = None


@dataclass(slots=True)
class ControlRequest:
    """One control frame awaiting its coordinator admission answer."""

    frame: Mapping[str, JsonValue]
    _replies: Queue = field(default_factory=Queue, repr=False)

    def respond(self, status: str, reason: str | None = None) -> None:
        """Answer one frame; the coordinator calls this exactly once."""
        self._replies.put(ControlReply(status, reason))

    def wait(self, timeout: float) -> ControlReply:
        """Wait for the coordinator's bounded admission answer."""
        try:
            return self._replies.get(timeout=timeout)
        except Empty:
            return ControlReply(REJECTED, "coordinator did not answer in time")


class TransportInbox:
    """A bounded, single-consumer queue of serialized control frames."""

    def __init__(self, max_pending: int = MAX_PENDING_REQUESTS) -> None:
        if max_pending < 1:
            raise ContractError("control inbox bound must be positive")
        self._max_pending = max_pending
        self._requests: Queue[ControlRequest] = Queue(maxsize=max_pending)

    def submit(
        self,
        frame: Mapping[str, JsonValue],
        *,
        timeout: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    ) -> ControlReply:
        """Admit one frame and wait for its bounded coordinator answer."""
        request = ControlRequest(frame)
        try:
            self._requests.put_nowait(request)
        except Full:
            return ControlReply(REJECTED, "control inbox is full")
        return request.wait(timeout)

    def post(self, frame: Mapping[str, JsonValue]) -> ControlRequest:
        """Enqueue one frame without waiting for its admission answer.

        Unlike :meth:`submit`, a full inbox is an error rather than a reply,
        because the caller holds the coordinator and can drain it directly.
        """
        request = ControlRequest(frame)
        try:
            self._requests.put_nowait(request)
        except Full as exc:
            raise ContractError("control inbox is full") from exc
        return request

    def drain(self) -> tuple[ControlRequest, ...]:
        """Remove every pending frame; only the coordinator calls this."""
        drained: list[ControlRequest] = []
        while True:
            try:
                drained.append(self._requests.get_nowait())
            except Empty:
                return tuple(drained)

    def pending(self) -> int:
        """Return the number of frames awaiting coordinator admission."""
        return self._requests.qsize()


class ControlServer:
    """Serve bounded control frames on a local socket into one inbox."""

    def __init__(self, inbox: TransportInbox, path: Path) -> None:
        self._inbox = inbox
        self._path = path
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._ownership_file: BinaryIO | None = None

    def start(self) -> None:
        """Bind the socket and begin accepting frames."""
        ownership_file = self._path.with_name(f"{self._path.name}.lock").open("a+b")
        try:
            fcntl.flock(ownership_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            ownership_file.close()
            raise ContractError("another coordinator owns this run directory") from exc
        self._ownership_file = ownership_file
        try:
            self._prepare_path()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(self._path))
            listener.listen(8)
            listener.settimeout(_ACCEPT_POLL_SECONDS)
        except OSError as exc:
            if "listener" in locals():
                listener.close()
            self._release_ownership()
            raise ContractError(f"could not serve control socket: {exc}") from exc
        except Exception:
            self._release_ownership()
            raise
        self._listener = listener
        self._thread = threading.Thread(
            target=self._serve, name="vharness-control", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """Stop accepting, unlink the owned socket, and release the run lock."""
        self.stop_serving()
        self._release_ownership()

    def stop_serving(self) -> None:
        """Stop the listener while retaining exclusive run ownership."""
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=DEFAULT_FRAME_TIMEOUT_SECONDS)
        listener, self._listener = self._listener, None
        owned = self._ownership_file is not None
        if listener is not None:
            listener.close()
        if owned and self._path.exists():
            self._path.unlink(missing_ok=True)

    def _release_ownership(self) -> None:
        """Release the exclusive run-directory lock, if acquired."""
        ownership_file, self._ownership_file = self._ownership_file, None
        if ownership_file is not None:
            fcntl.flock(ownership_file.fileno(), fcntl.LOCK_UN)
            ownership_file.close()

    def _prepare_path(self) -> None:
        """Refuse a served socket and replace a stale one."""
        if not self._path.exists():
            return
        mode = self._path.lstat().st_mode
        if not stat.S_ISSOCK(mode):
            raise ContractError("control socket path is not a socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(DEFAULT_FRAME_TIMEOUT_SECONDS)
            probe.connect(str(self._path))
        except OSError:
            self._path.unlink(missing_ok=True)
            return
        finally:
            probe.close()
        raise ContractError("control socket is already served")

    def _serve(self) -> None:
        while not self._stopping.is_set():
            listener = self._listener
            if listener is None:  # pragma: no cover - close() clears it
                return
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:  # pragma: no cover - listener closed during accept
                return
            with connection:
                self._handle(connection)

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(DEFAULT_FRAME_TIMEOUT_SECONDS)
        try:
            raw = self._read_frame(connection)
            reply = self._submit_frame(raw)
        except (ContractError, OSError) as exc:
            reply = ControlReply(REJECTED, str(exc))
        try:
            connection.sendall(canonical_json(_reply_payload(reply)).encode("utf-8"))
        except OSError:  # pragma: no cover - client vanished before its answer
            pass

    def _read_frame(self, connection: socket.socket) -> bytes:
        chunks: list[bytes] = []
        received = 0
        while True:
            chunk = connection.recv(MAX_FRAME_BYTES)
            if not chunk:
                return b"".join(chunks)
            received += len(chunk)
            if received > MAX_FRAME_BYTES:
                raise ContractError("control frame exceeds the supported byte bound")
            chunks.append(chunk)

    def _submit_frame(self, raw: bytes) -> ControlReply:
        if not raw:
            return ControlReply(REJECTED, "control frame is empty")
        frame = parse_json_object(raw)
        return self._inbox.submit(frame)


class ControlClient:
    """Submit one control frame to a running coordinator and read its answer."""

    # The client is a single-operation transport: one frame, one bounded answer.
    # pylint: disable=too-few-public-methods

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    ) -> None:
        self._path = path
        self._timeout = timeout

    def submit(self, frame: Mapping[str, JsonValue]) -> ControlReply:
        """Send one bounded frame and return the coordinator's answer."""
        payload = canonical_json(frame).encode("utf-8")
        if len(payload) > MAX_FRAME_BYTES:
            raise ContractError("control frame exceeds the supported byte bound")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self._timeout)
            connection.connect(str(self._path))
            connection.sendall(payload)
            connection.shutdown(socket.SHUT_WR)
            raw = _read_reply(connection)
        except OSError as exc:
            raise ContractError(f"control socket is unavailable: {exc}") from exc
        finally:
            connection.close()
        return _decode_reply(raw)


def command_frame(command: OperatorCommand) -> dict[str, JsonValue]:
    """Wrap one typed operator command as a transport frame."""
    return {"kind": "command", "command": to_json_value(command)}


def frame_command(frame: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    """Extract the command payload from one command frame."""
    if frame.get("kind") != "command":
        raise ContractError("control frame kind must be command")
    command = frame.get("command")
    if not isinstance(command, Mapping):
        raise ContractError("control frame command must be an object")
    return command


def _read_reply(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = connection.recv(MAX_FRAME_BYTES)
        if not chunk:
            return b"".join(chunks)
        received += len(chunk)
        if received > MAX_FRAME_BYTES:
            raise ContractError("control reply exceeds the supported byte bound")
        chunks.append(chunk)


def _reply_payload(reply: ControlReply) -> Mapping[str, object]:
    payload: dict[str, object] = {"status": reply.status}
    if reply.reason is not None:
        payload["reason"] = reply.reason
    return payload


def _decode_reply(raw: bytes) -> ControlReply:
    if not raw:
        raise ContractError("control reply is empty")
    payload = parse_json_object(raw)
    status = payload.get("status")
    if status not in (ACCEPTED, REJECTED):
        raise ContractError("control reply status is invalid")
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ContractError("control reply reason must be a string")
    return ControlReply(str(status), reason)
