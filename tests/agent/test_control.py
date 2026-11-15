"""Local control transport contracts: bounded frames and no durable state."""

from __future__ import annotations

from pathlib import Path
import socket
import threading
import time

import pytest

from vharness.agent.codec import parse_json_object
from vharness.agent.control import (
    ACCEPTED,
    MAX_FRAME_BYTES,
    REJECTED,
    ControlClient,
    ControlServer,
    TransportInbox,
    command_frame,
    control_socket_path,
    frame_command,
)
from vharness.agent.errors import ContractError
from vharness.agent.models import Pause


@pytest.fixture
def served(tmp_path):
    inbox = TransportInbox()
    path = control_socket_path(tmp_path)
    server = ControlServer(inbox, path)
    server.start()
    yield inbox, path, server
    server.close()


def _coordinator(inbox, collected, status=ACCEPTED, reason=None):
    """Answer pending frames from a coordinator-like background thread."""

    def run():
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            for request in inbox.drain():
                collected.append(request.frame)
                request.respond(status, reason)
                return
            time.sleep(0.01)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _raw_submit(path: Path, payload: bytes) -> dict:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(10.0)
    connection.connect(str(path))
    connection.sendall(payload)
    connection.shutdown(socket.SHUT_WR)
    chunks: list[bytes] = []
    while True:
        chunk = connection.recv(MAX_FRAME_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
    connection.close()
    return dict(parse_json_object(b"".join(chunks)))


def test_command_frame_round_trip(served):
    inbox, path, _server = served
    collected: list = []
    _coordinator(inbox, collected)
    reply = ControlClient(path).submit(
        command_frame(Pause(command_id="pause-1", session_id="session-1"))
    )
    assert reply.status == ACCEPTED
    deadline = time.monotonic() + 5.0
    while not collected and time.monotonic() < deadline:
        time.sleep(0.01)
    command = frame_command(collected[0])
    assert command["__type__"] == "Pause"
    assert command["command_id"] == "pause-1"


def test_malformed_frame_is_rejected(served):
    _inbox, path, _server = served
    reply = _raw_submit(path, b"this is not json")
    assert reply["status"] == REJECTED
    assert "invalid JSON" in reply["reason"]


def test_oversized_frame_is_rejected(served):
    _inbox, path, _server = served
    reply = _raw_submit(path, b"x" * (MAX_FRAME_BYTES + 1))
    assert reply["status"] == REJECTED
    assert "byte bound" in reply["reason"]


def test_empty_frame_is_rejected(served):
    _inbox, path, _server = served
    reply = _raw_submit(path, b"")
    assert reply["status"] == REJECTED
    assert "empty" in reply["reason"]


def test_full_inbox_is_reported_without_dropping(tmp_path):
    inbox = TransportInbox(max_pending=1)
    first = inbox.submit({"kind": "command"}, timeout=0.05)
    assert first.status == REJECTED
    assert "did not answer" in (first.reason or "")
    second = inbox.submit({"kind": "command"}, timeout=0.05)
    assert second.status == REJECTED
    assert second.reason == "control inbox is full"
    drained = inbox.drain()
    assert len(drained) == 1
    drained[0].respond(ACCEPTED)
    assert inbox.pending() == 0


def test_second_server_refuses_a_served_socket(served):
    inbox, path, _server = served
    other = ControlServer(inbox, path)
    with pytest.raises(
        ContractError, match="another coordinator owns this run directory"
    ):
        other.start()


def test_stale_socket_file_is_replaced(tmp_path):
    path = control_socket_path(tmp_path)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    inbox = TransportInbox()
    server = ControlServer(inbox, path)
    server.start()
    try:
        collected: list = []
        _coordinator(inbox, collected)
        reply = ControlClient(path).submit({"kind": "command", "command": {}})
        assert reply.status == ACCEPTED
    finally:
        server.close()


def test_non_socket_path_is_refused(tmp_path):
    path = control_socket_path(tmp_path)
    path.write_text("not a socket", encoding="utf-8")
    server = ControlServer(TransportInbox(), path)
    with pytest.raises(ContractError, match="not a socket"):
        server.start()


def test_close_removes_the_socket(tmp_path):
    inbox = TransportInbox()
    path = control_socket_path(tmp_path)
    server = ControlServer(inbox, path)
    server.start()
    server.close()
    assert not path.exists()
    with pytest.raises(ContractError, match="unavailable"):
        ControlClient(path, timeout=1.0).submit({"kind": "command"})


def test_frame_validation():
    with pytest.raises(ContractError, match="kind"):
        frame_command({})
    with pytest.raises(ContractError, match="kind"):
        frame_command({"kind": "status"})
    with pytest.raises(ContractError, match="object"):
        frame_command({"kind": "command", "command": []})


def test_inbox_bound_must_be_positive():
    with pytest.raises(ContractError, match="bound"):
        TransportInbox(max_pending=0)


def test_control_transport_holds_no_durable_state():
    import vharness.agent.control as control

    source = Path(control.__file__).read_text(encoding="utf-8")
    assert "sqlite" not in source
    assert "journal" not in source
    assert "artifacts" not in source
