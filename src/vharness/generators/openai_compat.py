"""OpenAI-compatible HTTP generator: retry, truncation retry, SQLite cache, usage stats.

With ``stream=True`` each reply is streamed and collected (COD-76). A
non-streamed request sends nothing while a reasoning model thinks, and an idle
limit somewhere between client and provider ended such calls at 300 s even
with a 600 s client timeout. Streamed tokens, reasoning included, keep the
connection busy. Usage is requested in the final chunk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypedDict

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAI,
)
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from openai.types.chat.completion_create_params import ResponseFormat

from ..config import EndpointConfig
from ..core import Generation, ModelFailureClass
from .base import Generator, register_builtin

DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.0
RESPONSE_FORMAT_JSON_OBJECT = "json_object"
_VALID_RESPONSE_FORMATS = (RESPONSE_FORMAT_JSON_OBJECT,)


# Set by the ARC coordinator only, inside its provider worker.
provider_deadline: ContextVar[float | None] = ContextVar(
    "provider_deadline", default=None
)


class _StreamCut(Exception):
    """A streamed reply ended before the provider sent a finish reason."""


def _transient(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, _StreamCut)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def _failure_class(exc: Exception) -> str:
    """Classify one caught provider exception without retaining its text."""
    if isinstance(exc, APITimeoutError):
        return ModelFailureClass.TRANSIENT_TIMEOUT.value
    if isinstance(exc, (APIConnectionError, _StreamCut)):
        return ModelFailureClass.TRANSIENT_CONNECTION.value
    if isinstance(exc, APIStatusError):
        if exc.status_code in (401, 403):
            return ModelFailureClass.AUTHENTICATION.value
        if exc.status_code == 429:
            return ModelFailureClass.RATE_LIMIT.value
        if exc.status_code >= 500:
            return ModelFailureClass.TRANSIENT_SERVER.value
        return ModelFailureClass.INVALID_REQUEST.value
    return ModelFailureClass.UNKNOWN.value


def _reasoning_tokens(usage: object) -> int | None:
    """Return provider-reported reasoning tokens, or ``None`` when not reported.

    Reasoning text is never retained; only this count crosses into the model
    record, so a reasoning-only response is distinguishable from an empty one.
    """
    details = getattr(usage, "completion_tokens_details", None)
    value = getattr(details, "reasoning_tokens", None) if details is not None else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _cached_prompt_tokens(usage: object) -> int | None:
    """Return provider-reported cached prompt tokens, or ``None`` if unreported."""
    details = getattr(usage, "prompt_tokens_details", None)
    value = getattr(details, "cached_tokens", None) if details is not None else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class _FormatKwargs(TypedDict, total=False):
    response_format: ResponseFormat


@dataclass(frozen=True, slots=True)
class _Message:
    content: str | None


@dataclass(frozen=True, slots=True)
class _Choice:
    message: _Message
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class _StreamedResponse:
    """A streamed reply collected into the shape of a non-streamed one."""

    choices: tuple[_Choice, ...]
    usage: object | None


def _collect_stream(chunks: Iterable[object]) -> _StreamedResponse:
    """Join streamed content deltas; keep the last finish reason and the usage.

    A stream that closes without a finish reason was cut in transit; its partial
    text is never returned as an answer.
    """
    collector = _StreamCollector()
    for chunk in chunks:
        collector.add(chunk)
    return collector.response()


class _StreamCollector:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.finish: str | None = None
        self.usage: object | None = None
        self.saw_choice = False

    def add(self, chunk: object) -> None:
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            self.usage = chunk_usage
        for choice in getattr(chunk, "choices", None) or ():
            self.saw_choice = True
            text = getattr(getattr(choice, "delta", None), "content", None)
            if text:
                self.parts.append(text)
            reason = getattr(choice, "finish_reason", None)
            if reason:
                self.finish = reason

    def response(self) -> _StreamedResponse:
        if self.saw_choice and self.finish is None:
            raise _StreamCut("the stream ended before a finish reason")
        choices = (
            (_Choice(_Message("".join(self.parts)), self.finish),)
            if self.saw_choice
            else ()
        )
        return _StreamedResponse(choices, self.usage)


@register_builtin
class OpenAICompatible(Generator):
    name = "openai"
    help = "any OpenAI-compatible chat-completions endpoint (Ollama, vLLM, llama.cpp, hosted APIs, …)"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "no-key",
        *,
        timeout: float = 120.0,
        max_retries: int = 3,
        length_retries: int = 1,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        cache_path: str | None = None,
        response_format: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        stream: bool = False,
    ) -> None:
        if (
            response_format is not None
            and response_format not in _VALID_RESPONSE_FORMATS
        ):
            raise ValueError(
                f"response_format must be one of {_VALID_RESPONSE_FORMATS} or None"
            )
        self.model = model
        self.base_url = base_url
        self.max_retries = max_retries
        self.length_retries = length_retries
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.stream = stream
        self.response_format = response_format
        self.stats = {
            "queries": 0,
            "cache_hits": 0,
            "api_errors": 0,
            "parse_notice": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "latencies": [],
        }
        self._extra_headers = dict(extra_headers) if extra_headers else None
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
            default_headers=self._extra_headers,
        )
        self._cache_path = cache_path
        self._cache_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._cache: sqlite3.Connection | None = None
        if cache_path:
            self._cache = sqlite3.connect(cache_path, check_same_thread=False)
            self._cache.execute(
                "CREATE TABLE IF NOT EXISTS responses "
                "(key TEXT PRIMARY KEY, text TEXT NOT NULL, created_at REAL DEFAULT (strftime('%s','now')))"
            )
            self._cache.commit()

    @classmethod
    def from_config(cls, cfg: EndpointConfig, **kwargs) -> "OpenAICompatible":
        return cls(cfg.base_url, cfg.model, cfg.api_key, **kwargs)

    def close(self) -> None:
        if self._cache is not None:
            self._cache.close()
            self._cache = None

    def _cache_key(self, system: str, prompt: str) -> str:
        h = hashlib.sha256()
        for part in (
            self.base_url,
            self.model,
            self.response_format or "",
            system,
            prompt,
        ):
            h.update(part.encode("utf-8", "replace"))
            h.update(b"\x00")
        return h.hexdigest()

    def _cache_get(self, key: str) -> str | None:
        if self._cache is None:
            return None
        with self._cache_lock:
            row = self._cache.execute(
                "SELECT text FROM responses WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def _cache_put(self, key: str, text: str) -> None:
        if self._cache is None:
            return
        with self._cache_lock:
            self._cache.execute(
                "INSERT OR REPLACE INTO responses (key, text) VALUES (?, ?)",
                (key, text),
            )
            self._cache.commit()

    def summary(self) -> str:
        with self._stats_lock:
            s = {
                key: (list(value) if key == "latencies" else value)
                for key, value in self.stats.items()
            }
        lat = s["latencies"]
        p50 = sorted(lat)[len(lat) // 2] if lat else 0.0
        return (
            f"queries={s['queries']} cache_hits={s['cache_hits']} api_errors={s['api_errors']} "
            f"tokens={s['prompt_tokens'] + s['completion_tokens']} latency_p50={p50:.2f}s"
        )

    def _response_format_kwargs(self) -> _FormatKwargs:
        """Return the request kwargs enforcing provider-side JSON when selected.

        Absent, not a sentinel, when no format is chosen: the request then
        carries no ``response_format`` key at all.
        """
        if self.response_format is None:
            return {}
        return {"response_format": {"type": "json_object"}}

    def _request(
        self, system: str, prompt: str, max_tokens: int
    ) -> ChatCompletion | _StreamedResponse:
        """One chat-completions call, streamed and collected when enabled.

        Stream errors raise inside this call, so a connection lost mid-stream
        is classified and retried like any other transport failure.
        """
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        deadline = provider_deadline.get()
        if deadline is not None:
            return asyncio.run(self._request_until(messages, max_tokens, deadline))
        if not self.stream:
            return self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=max_tokens,
                **self._response_format_kwargs(),
            )
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            **self._response_format_kwargs(),
        )
        try:
            return _collect_stream(stream)
        finally:
            stream.close()

    async def _request_until(
        self,
        messages: list[ChatCompletionMessageParam],
        max_tokens: int,
        deadline: float,
    ) -> ChatCompletion | _StreamedResponse:
        """Cancel the HTTP exchange at the run deadline, including stream reads."""
        remaining = deadline - time.time()
        return await asyncio.wait_for(
            self._request_async(messages, max_tokens), timeout=remaining
        )

    async def _request_async(
        self, messages: list[ChatCompletionMessageParam], max_tokens: int
    ) -> ChatCompletion | _StreamedResponse:
        async with AsyncOpenAI(
            base_url=self.base_url,
            api_key=self._client.api_key,
            timeout=self._client.timeout,
            max_retries=0,
            default_headers=self._extra_headers,
        ) as client:
            if not self.stream:
                return await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=max_tokens,
                    **self._response_format_kwargs(),
                )
            stream = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                **self._response_format_kwargs(),
            )
            async with stream:
                collector = _StreamCollector()
                async for chunk in stream:
                    collector.add(chunk)
                return collector.response()

    def generate(self, system: str, prompt: str) -> Generation:
        key = self._cache_key(system, prompt)
        cached = self._cache_get(key)
        if cached is not None:
            with self._stats_lock:
                self.stats["cache_hits"] += 1
            return Generation(text=cached, model=self.model, cached=True)

        with self._stats_lock:
            self.stats["queries"] += 1
        max_tokens = self.max_tokens
        # Default length_retries=1 preserves the legacy extra truncation attempt.
        # Agent adapters set length_retries=0 so the coordinator owns retries.
        total_attempts = self.max_retries + 1 + self.length_retries
        last_error: str | None = None
        last_failure_class: str | None = None
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_reasoning_tokens: int | None = 0
        total_cached_prompt_tokens: int | None = 0
        total_latency = 0.0
        for attempt in range(total_attempts):
            started = time.perf_counter()
            try:
                response = self._request(system, prompt, max_tokens)
            except Exception as e:  # noqa: BLE001 — classified below
                latency = time.perf_counter() - started
                total_latency += latency
                with self._stats_lock:
                    self.stats["latencies"].append(latency)
                last_error = f"{type(e).__name__}: {e}"
                last_failure_class = _failure_class(e)
                if _transient(e) and attempt < total_attempts - 1:
                    time.sleep(min(30.0, (2**attempt) + random.random()))
                    continue
                break

            latency = time.perf_counter() - started
            usage = getattr(response, "usage", None)
            p_toks = (
                (getattr(usage, "prompt_tokens", 0) or 0) if usage is not None else 0
            )
            c_toks = (
                (getattr(usage, "completion_tokens", 0) or 0)
                if usage is not None
                else 0
            )
            total_latency += latency
            total_prompt_tokens += p_toks
            total_completion_tokens += c_toks
            reasoning_tokens = _reasoning_tokens(usage)
            if reasoning_tokens is None:
                total_reasoning_tokens = None
            elif total_reasoning_tokens is not None:
                total_reasoning_tokens += reasoning_tokens
            cached_prompt_tokens = _cached_prompt_tokens(usage)
            if cached_prompt_tokens is None:
                total_cached_prompt_tokens = None
            elif total_cached_prompt_tokens is not None:
                total_cached_prompt_tokens += cached_prompt_tokens
            with self._stats_lock:
                self.stats["latencies"].append(latency)
                if usage is not None:
                    self.stats["prompt_tokens"] += p_toks
                    self.stats["completion_tokens"] += c_toks

            if not getattr(response, "choices", None):
                last_error = "endpoint returned empty choices (content filter?)"
                last_failure_class = ModelFailureClass.INVALID_REQUEST.value
                break
            raw = (response.choices[0].message.content or "").strip()
            finish = response.choices[0].finish_reason
            if finish == "length" and attempt < total_attempts - 1:
                # Truncated JSON — retry with a bigger budget before giving up.
                last_error = (
                    "truncated (finish_reason=length); retried with larger max_tokens"
                )
                max_tokens *= 2
                continue

            self._cache_put(key, raw)
            return Generation(
                text=raw,
                model=self.model,
                finish_reason=finish or "",
                latency=total_latency,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                reasoning_tokens=total_reasoning_tokens,
                cached_prompt_tokens=total_cached_prompt_tokens,
            )

        with self._stats_lock:
            self.stats["api_errors"] += 1
        return Generation(
            text="",
            model=self.model,
            error=last_error,
            latency=total_latency,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            failure_class=last_failure_class,
        )
