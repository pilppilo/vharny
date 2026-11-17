"""Streamed provider replies (COD-76), against a local server-sent-events stub.

Each test talks to a real HTTP server on localhost through the real OpenAI SDK,
so the SDK's own stream parsing is exercised, not a fake of it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO

import pytest

from vharness.agent.cli import main
from vharness.agent.run_settings import RunSettings
from vharness.generators.openai_compat import OpenAICompatible

REPLY = '{"kind": "LineageQuery"}'


@dataclass
class _Behaviour:
    """What the stub does with the next request."""

    usage: bool = True
    drop_mid_stream: bool = False
    requests: list[dict] = field(default_factory=list)


def _chunk(delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


USAGE = {
    "prompt_tokens": 11,
    "completion_tokens": 9,
    "total_tokens": 20,
    "completion_tokens_details": {"reasoning_tokens": 7},
}


def _handler(behaviour: _Behaviour) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            behaviour.requests.append(body)
            if not body.get("stream"):
                self._json(
                    {
                        "id": "c",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "stub",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": REPLY},
                            }
                        ],
                        "usage": USAGE,
                    }
                )
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            pieces = [REPLY[:8], REPLY[8:16], REPLY[16:]]
            self._event(_chunk({"role": "assistant", "content": ""}))
            for index, piece in enumerate(pieces):
                self._event(_chunk({"content": piece}))
                if behaviour.drop_mid_stream and index == 0:
                    self.connection.shutdown(2)  # close without finishing
                    return
            self._event(_chunk({}, finish="stop"))
            if behaviour.usage:
                self._event(
                    {
                        "id": "c",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "stub",
                        "choices": [],
                        "usage": USAGE,
                    }
                )
            self.wfile.write(b"data: [DONE]\n\n")

        def _event(self, payload: dict) -> None:
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        def _json(self, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args) -> None:
            return None

    return Handler


@pytest.fixture
def stub(monkeypatch) -> Iterator[tuple[str, _Behaviour]]:
    """A local chat-completions server; requests bypass any proxy."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    behaviour = _Behaviour()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(behaviour))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", behaviour
    finally:
        server.shutdown()
        server.server_close()


def _generator(base_url: str, *, stream: bool) -> OpenAICompatible:
    return OpenAICompatible(
        base_url,
        "stub",
        "test-key",
        max_retries=0,
        length_retries=0,
        timeout=10.0,
        stream=stream,
    )


def test_streamed_reply_is_collected_with_its_usage(stub):
    base_url, behaviour = stub
    generator = _generator(base_url, stream=True)
    try:
        generation = generator.generate("system", "prompt")
    finally:
        generator.close()
    assert generation.ok
    assert generation.text == REPLY
    assert generation.finish_reason == "stop"
    assert (generation.prompt_tokens, generation.completion_tokens) == (11, 9)
    assert generation.reasoning_tokens == 7
    request = behaviour.requests[0]
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}
    assert "response_format" not in request


def test_unstreamed_requests_are_unchanged(stub):
    base_url, behaviour = stub
    generator = _generator(base_url, stream=False)
    try:
        generation = generator.generate("system", "prompt")
    finally:
        generator.close()
    assert generation.text == REPLY and generation.prompt_tokens == 11
    assert "stream" not in behaviour.requests[0]
    assert "stream_options" not in behaviour.requests[0]


def test_a_stream_cut_mid_reply_is_a_classified_transport_failure(stub):
    base_url, behaviour = stub
    behaviour.drop_mid_stream = True
    generator = _generator(base_url, stream=True)
    try:
        generation = generator.generate("system", "prompt")
    finally:
        generator.close()
    assert not generation.ok
    assert generation.text == ""
    assert generation.failure_class == "transient-connection"


def _probe(base_url: str, monkeypatch, *extra: str) -> tuple[int, dict]:
    monkeypatch.setenv("STUB_KEY", "test-key")
    output = StringIO()
    code = main(
        [
            "probe-provider",
            "--base-url",
            base_url,
            "--model",
            "stub",
            "--api-key-env",
            "STUB_KEY",
            "--provider-timeout",
            "10",
            *extra,
        ],
        output=output,
        error=StringIO(),
    )
    return code, json.loads(output.getvalue())


def test_probe_checks_streaming_and_its_usage(stub, monkeypatch):
    base_url, behaviour = stub
    code, report = _probe(base_url, monkeypatch, "--provider-stream")
    assert code == 0 and report["ready"] is True and report["stream"] is True
    assert report["prompt_tokens"] == 11
    assert behaviour.requests[-1]["stream"] is True


def test_probe_refuses_a_stream_without_usage(stub, monkeypatch):
    base_url, behaviour = stub
    behaviour.usage = False
    code, report = _probe(base_url, monkeypatch, "--provider-stream")
    assert code == 1 and report["ready"] is False
    assert report["failure_class"] == "usage-missing"


def test_provider_stream_is_recorded_only_when_set(tmp_path):
    settings = RunSettings(
        objective="o",
        workspace=str(tmp_path),
        base_url="http://localhost:1/v1",
        model="m",
        api_key_env="K",
        max_actions=8,
        max_model_calls=8,
        max_evaluations=4,
        max_steps=20,
        token_budget=16384,
        max_output_tokens=256,
        repair_budget=0,
        provider_stream=True,
    )
    payload = settings.to_payload()
    assert payload["provider_stream"] is True
    assert RunSettings.from_payload(payload).provider_stream is True
    plain = RunSettings.from_payload({**payload, "provider_stream": False})
    assert plain.provider_stream is False
    assert "provider_stream" not in plain.to_payload()
    without = {k: v for k, v in payload.items() if k != "provider_stream"}
    assert RunSettings.from_payload(without).provider_stream is False
