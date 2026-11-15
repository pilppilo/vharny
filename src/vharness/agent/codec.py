"""Canonical JSON conversion at durable and external boundaries."""

# pylint: disable=too-many-return-statements
# Each JSON shape has one direct conversion branch at this narrow boundary.

from __future__ import annotations

import json
import math
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Mapping, cast

from .errors import ContractError
from .models import JsonValue

MAX_JSON_BYTES = 1_000_000
MAX_JSON_DEPTH = 64


def to_json_value(value: object) -> JsonValue:
    """Convert supported immutable domain data into JSON-compatible values."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("JSON values must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ContractError("JSON decimal values must be finite")
        return str(value)
    if isinstance(value, Enum):
        return to_json_value(value.value)
    if is_dataclass(value):
        encoded = {
            item.name: to_json_value(getattr(value, item.name))
            for item in fields(value)
        }
        encoded["__type__"] = type(value).__name__
        return encoded
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ContractError("JSON object keys must be strings")
        return {key: to_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(to_json_value(item) for item in value)
    raise ContractError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Produce stable UTF-8 JSON used for durable payloads and content hashing."""
    try:
        json_value = to_json_value(value)
        encoded = json.dumps(
            json_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except RecursionError as exc:
        raise ContractError("JSON payload nesting exceeds the supported bound") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ContractError("JSON payload exceeds the supported byte bound")
    _validate_depth(json_value)
    return encoded


def parse_json_object(raw: str | bytes) -> Mapping[str, JsonValue]:
    """Parse one external JSON object and reject non-object/non-finite input."""
    value = parse_json(raw)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ContractError("expected a JSON object")
    return cast(Mapping[str, JsonValue], value)


def parse_json(raw: str | bytes) -> JsonValue:
    """Parse bounded JSON from an external or persisted payload."""
    if not isinstance(raw, (str, bytes)):
        raise ContractError("JSON payload must be text or bytes")
    size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if size > MAX_JSON_BYTES:
        raise ContractError("JSON payload exceeds the supported byte bound")
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError(f"invalid JSON object: {exc}") from exc
    try:
        _validate_depth(value)
    except RecursionError as exc:
        raise ContractError("JSON payload nesting exceeds the supported bound") from exc
    return cast(JsonValue, value)


def _validate_depth(value: object, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ContractError("JSON payload nesting exceeds the supported bound")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("JSON object keys must be strings")
            _validate_depth(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_depth(item, depth + 1)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")
