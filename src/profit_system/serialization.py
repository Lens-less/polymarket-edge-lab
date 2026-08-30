"""Canonical serialization helpers for profit-system artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class SerializationError(ValueError):
    """Raised when a value cannot be serialized canonically."""


def canonical_decimal_string(value: Decimal) -> str:
    """Return a stable plain-string representation for a finite Decimal."""

    if not isinstance(value, Decimal):
        raise TypeError("value must be a Decimal")
    if not value.is_finite():
        raise SerializationError("non-finite Decimal values are not allowed")
    if value.is_zero():
        return "0"
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def canonical_timestamp(value: datetime) -> str:
    """Return a UTC ISO-8601 timestamp with microseconds and a Z suffix."""

    if not isinstance(value, datetime):
        raise TypeError("value must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SerializationError("datetime values must include a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _dataclass_to_mapping(value: Any) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for field in fields(value):
        if field.name.startswith("_"):
            continue
        document[field.name] = getattr(value, field.name)
    return document


def to_canonical_jsonable(value: Any) -> Any:
    """Convert a supported Python object into canonical JSON data."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise SerializationError(
            "binary float values are not allowed in canonical profit-system JSON"
        )
    if isinstance(value, Decimal):
        return canonical_decimal_string(value)
    if isinstance(value, datetime):
        return canonical_timestamp(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return to_canonical_jsonable(_dataclass_to_mapping(value))
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SerializationError("JSON object keys must be strings")
            normalized[key] = to_canonical_jsonable(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_canonical_jsonable(item) for item in value]
    raise SerializationError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode ``value`` as deterministic UTF-8 JSON."""

    return json.dumps(
        to_canonical_jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 digest of the canonical JSON bytes for ``value``."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
