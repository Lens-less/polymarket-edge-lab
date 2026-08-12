"""Deterministic, read-only inventory and quality audit for Edge Lab data.

The builder deliberately distinguishes three evidence classes:

* L0: fixtures, diagnostics, lifecycle-only captures, or failed integrity.
* L1: public snapshots, price-history replays, and touch/shadow evidence.
* L2: finalized, checksum-valid forward depth/stream observations.

Legacy artifacts under ``research/profit_redesign_2026-07-24`` are capped at
L1 even if a filename says "replay" or the payload contains an order book.
They were not written by the forward :class:`CaptureStore` contract.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .data_store import (
    DataContractError,
    canonical_json_bytes,
    canonical_event_fingerprint,
    canonical_record_id,
)


MANIFEST_SCHEMA_VERSION = "edge-data-manifest.v1"
QUALITY_SCHEMA_VERSION = "edge-data-quality.v1"
OUTPUT_MANIFEST_NAME = "DATA_MANIFEST.json"
OUTPUT_QUALITY_NAME = "DATA_QUALITY.json"

_LEVEL_ORDER = {"L0": 0, "L1": 1, "L2": 2}
_EXPECTED_CAPTURE_SOURCES = (
    "clob_http",
    "clob_market_ws",
    "gamma_http",
    "rewards_http",
    "rtds_ws",
    "rules_http",
)
_NON_DOMAIN_KINDS = frozenset(
    {"diagnostic", "lifecycle", "quarantine", "unknown"}
)
_LEGACY_L1_PATTERNS = (
    "_current",
    "_observe",
    "_replay",
    "_robust",
    "complete_set_scan",
    "reward_scan",
)
_OUTPUT_NAMES = frozenset({OUTPUT_MANIFEST_NAME, OUTPUT_QUALITY_NAME})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_COUNT_FIELDS = (
    "attempted_count",
    "record_count",
    "duplicate_count",
    "unique_event_count",
    "semantic_duplicate_count",
    "time_regression_count",
    "sequence_gap_count",
    "sequence_missing_count",
    "sequence_regression_count",
    "schema_drift_count",
    "schema_change_count",
)


@dataclass(frozen=True)
class DataAuditBundle:
    """The two machine-readable documents emitted by the audit builder."""

    manifest: dict[str, Any]
    quality: dict[str, Any]


class UnsafeInputPathError(ValueError):
    """Raised before reading an input that escapes its declared audit root."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open_flags(*, directory: bool) -> int:
    required = ("O_NOFOLLOW", "O_DIRECTORY")
    missing = [name for name in required if not hasattr(os, name)]
    if missing:
        raise UnsafeInputPathError(
            "descriptor-bound audit reads are unavailable on this platform"
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    flags |= getattr(os, "O_CLOEXEC", 0)
    return flags


def _relative_input_parts(path: Path, *, allowed_root: Path) -> tuple[str, ...]:
    root = Path(os.path.abspath(allowed_root))
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise UnsafeInputPathError(
            "input path is lexically outside its audit root"
        ) from exc
    if not relative.parts:
        raise UnsafeInputPathError("audit input is not a regular file")
    if any(part in ("", ".", "..") for part in relative.parts):
        raise UnsafeInputPathError("input path contains an unsafe component")
    return relative.parts


def _raise_unsafe_open(
    exc: OSError,
    *,
    parent_fd: int | None = None,
    name: str | None = None,
) -> None:
    if parent_fd is not None and name is not None:
        try:
            mode = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            ).st_mode
        except OSError:
            mode = None
        if mode is not None and stat.S_ISLNK(mode):
            raise UnsafeInputPathError(
                "symbolic links are not accepted as audit inputs"
            ) from exc
    if exc.errno in (errno.ENOENT, errno.ESTALE):
        raise UnsafeInputPathError("input disappeared during audit") from exc
    if exc.errno in (errno.ELOOP, errno.EMLINK):
        raise UnsafeInputPathError(
            "symbolic links are not accepted as audit inputs"
        ) from exc
    raise UnsafeInputPathError(
        f"unable to open audit input safely: {exc.strerror or exc}"
    ) from exc


def _open_root_directory_fd(allowed_root: Path) -> int:
    """Anchor an absolute audit root without following any path component."""

    absolute = Path(os.path.abspath(allowed_root))
    if not absolute.is_absolute():
        raise UnsafeInputPathError("audit root must be absolute")
    descriptor: int | None = None
    try:
        descriptor = os.open(os.path.sep, _open_flags(directory=True))
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise UnsafeInputPathError("audit root anchor is not a directory")
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    _open_flags(directory=True),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                _raise_unsafe_open(
                    exc,
                    parent_fd=descriptor,
                    name=component,
                )
            if not stat.S_ISDIR(os.fstat(next_descriptor).st_mode):
                os.close(next_descriptor)
                raise UnsafeInputPathError(
                    "audit root component is not a directory"
                )
            os.close(descriptor)
            descriptor = next_descriptor
        result = descriptor
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_regular_input_fd(path: Path, *, allowed_root: Path) -> int:
    """Open an input through root-anchored, no-follow file descriptors."""

    parts = _relative_input_parts(path, allowed_root=allowed_root)
    root_fd: int | None = None
    parent_fd: int | None = None
    file_fd: int | None = None
    try:
        try:
            root_fd = _open_root_directory_fd(allowed_root)
        except OSError as exc:
            _raise_unsafe_open(exc)
        parent_fd = root_fd
        for component in parts[:-1]:
            try:
                next_fd = os.open(
                    component,
                    _open_flags(directory=True),
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                _raise_unsafe_open(
                    exc,
                    parent_fd=parent_fd,
                    name=component,
                )
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise UnsafeInputPathError(
                    "audit input ancestor is not a directory"
                )
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
        try:
            file_fd = os.open(
                parts[-1],
                _open_flags(directory=False),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            _raise_unsafe_open(
                exc,
                parent_fd=parent_fd,
                name=parts[-1],
            )
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise UnsafeInputPathError("audit input is not a regular file")
        result = file_fd
        file_fd = None
        return result
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None and parent_fd != root_fd:
            os.close(parent_fd)
        if root_fd is not None:
            os.close(root_fd)


def _validate_input_path(path: Path, *, allowed_root: Path) -> None:
    descriptor = _open_regular_input_fd(path, allowed_root=allowed_root)
    os.close(descriptor)


def _read_bytes(path: Path, *, allowed_root: Path) -> bytes:
    descriptor = _open_regular_input_fd(path, allowed_root=allowed_root)
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        return handle.read()


def _relative(path: Path, project_root: Path) -> str:
    absolute = Path(os.path.abspath(path))
    return absolute.relative_to(project_root).as_posix()


def _safe_root(path: str | Path, *, project_root: Path, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_relative_to(project_root):
        raise ValueError(f"{label} must be inside project_root")
    return resolved


def _partition_input_paths(
    paths: Iterable[Path],
    *,
    allowed_root: Path,
    project_root: Path,
) -> tuple[list[Path], list[dict[str, str]]]:
    safe: list[Path] = []
    unsafe: list[dict[str, str]] = []
    for path in sorted(paths):
        try:
            _validate_input_path(path, allowed_root=allowed_root)
        except (OSError, UnsafeInputPathError) as exc:
            unsafe.append(
                {
                    "path": _relative(path, project_root),
                    "reason": str(exc),
                    "evidence_level": "L0",
                }
            )
        else:
            safe.append(path)
    return safe, unsafe


def _capture_path_inventory(
    capture_root: Path,
    *,
    attempts: int = 5,
) -> tuple[dict[str, list[Path]], bool]:
    def scan() -> dict[str, list[Path]]:
        raw_root = capture_root / "raw"
        raw_files = (
            [path for path in raw_root.rglob("*") if not path.is_dir()]
            if raw_root.is_dir()
            else []
        )
        checkpoints = (
            list((capture_root / "checkpoints").glob("*.json"))
            if (capture_root / "checkpoints").is_dir()
            else []
        )
        return {
            "manifests": sorted(
                path
                for path in raw_files
                if path.name.endswith(".manifest.json")
            ),
            "raw": sorted(
                path for path in raw_files if path.name.endswith(".jsonl")
            ),
            "partials": sorted(
                path
                for path in raw_files
                if path.name.endswith(".jsonl.partial")
            ),
            "checkpoints": sorted(checkpoints),
        }

    previous = scan()
    for _ in range(attempts):
        current = scan()
        if current == previous:
            return current, True
        previous = current
    return previous, False


def _json_object(data: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        parsed = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(parsed, dict):
        return None, f"expected JSON object, got {type(parsed).__name__}"
    return parsed, None


def _iso_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(
            f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _timestamp_range(values: Iterable[str | None]) -> dict[str, Any]:
    present = sorted(value for value in values if value is not None)
    return {
        "min": present[0] if present else None,
        "max": present[-1] if present else None,
        "count": len(present),
    }


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _is_nonnegative_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _decimal_value(
    value: Any,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    positive: bool = False,
) -> Decimal | None:
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if not isinstance(value, (str, int, Decimal)):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    if positive and parsed <= 0:
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def _positive_epoch_ms(value: Any) -> bool:
    if isinstance(value, str):
        return value.isdigit() and int(value) > 0
    return _is_nonnegative_int(value) and value > 0


def _valid_book_level(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and _decimal_value(
            value.get("price"), minimum=Decimal("0"), maximum=Decimal("1")
        )
        is not None
        and _decimal_value(value.get("size"), positive=True) is not None
    )


def _valid_book_levels(value: Any) -> bool:
    return bool(
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and all(_valid_book_level(item) for item in value)
    )


def _valid_clob_book_payload(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    bids = value.get("bids")
    asks = value.get("asks")
    if not _valid_book_levels(bids) or not _valid_book_levels(asks):
        return False
    if not bids and not asks:
        return False
    return bool(
        isinstance(value.get("asset_id"), str)
        and value.get("asset_id")
        and isinstance(value.get("market"), str)
        and value.get("market")
        and _positive_epoch_ms(value.get("timestamp"))
    )


def _valid_clob_ws_payload(event_type: str, value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if not (
        isinstance(value.get("market"), str)
        and value.get("market")
        and _positive_epoch_ms(value.get("timestamp"))
    ):
        return False
    if event_type == "book":
        return _valid_clob_book_payload(value)
    if event_type == "price_change":
        changes = value.get("price_changes")
        if (
            not isinstance(changes, Sequence)
            or isinstance(changes, (str, bytes, bytearray))
            or not changes
        ):
            return False
        return all(
            isinstance(change, Mapping)
            and isinstance(change.get("asset_id"), str)
            and bool(change.get("asset_id"))
            and _decimal_value(
                change.get("price"),
                minimum=Decimal("0"),
                maximum=Decimal("1"),
            )
            is not None
            and _decimal_value(
                change.get("size"), minimum=Decimal("0")
            )
            is not None
            for change in changes
        )
    if event_type == "best_bid_ask":
        bid = _decimal_value(
            value.get("best_bid"),
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        )
        ask = _decimal_value(
            value.get("best_ask"),
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        )
        return bool(
            isinstance(value.get("asset_id"), str)
            and value.get("asset_id")
            and bid is not None
            and ask is not None
            and bid <= ask
        )
    return False


def _valid_rtds_payload(event_type: str, value: Any) -> bool:
    expected_topics = {
        "crypto_prices.update": "crypto_prices",
        "crypto_prices_chainlink.update": "crypto_prices_chainlink",
    }
    expected_topic = expected_topics.get(event_type)
    if expected_topic is None or not isinstance(value, Mapping):
        return False
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        return False
    return bool(
        value.get("topic") == expected_topic
        and value.get("type") == "update"
        and _positive_epoch_ms(value.get("timestamp"))
        and isinstance(payload.get("symbol"), str)
        and payload.get("symbol")
        and _positive_epoch_ms(payload.get("timestamp"))
        and _decimal_value(payload.get("value"), positive=True) is not None
        and _decimal_value(
            payload.get("full_accuracy_value"), positive=True
        )
        is not None
    )


def _valid_clob_http_response(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("resource") != "clob_book":
        return False
    provenance = value.get("provenance")
    raw_json = value.get("raw_json")
    request_key = value.get("request_key")
    if (
        not isinstance(provenance, Mapping)
        or not isinstance(raw_json, Mapping)
        or not isinstance(request_key, str)
        or not request_key
    ):
        return False
    body_utf8 = provenance.get("body_utf8")
    body_bytes = provenance.get("body_bytes")
    if not isinstance(body_utf8, str) or not _is_nonnegative_int(body_bytes):
        return False
    encoded_body = body_utf8.encode("utf-8")
    try:
        parsed_body = json.loads(body_utf8, parse_float=Decimal)
        bodies_match = (
            canonical_json_bytes(parsed_body)
            == canonical_json_bytes(raw_json)
        )
    except (
        DataContractError,
        json.JSONDecodeError,
        UnicodeError,
        ValueError,
    ):
        return False
    params = provenance.get("request_params")
    return bool(
        _valid_clob_book_payload(raw_json)
        and raw_json.get("asset_id") == request_key
        and provenance.get("method") == "GET"
        and provenance.get("status_code") == 200
        and provenance.get("source") == "clob_book"
        and provenance.get("url") == "https://clob.polymarket.com/book"
        and params == {"token_id": request_key}
        and _is_sha256(provenance.get("body_sha256"))
        and len(encoded_body) == body_bytes
        and _sha256(encoded_body) == provenance.get("body_sha256")
        and bodies_match
    )


def _l2_record_validation(
    *,
    source: str,
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    kind: str,
    event_type: str,
) -> tuple[bool, bool, str | None]:
    if source == "clob_market_ws" and kind == "data":
        if event_type in {
            "last_trade_price",
            "market_resolved",
            "new_market",
            "tick_size_change",
        }:
            return False, False, None
        if event_type not in {"book", "price_change", "best_bid_ask"}:
            return True, False, "unknown_clob_ws_data_event"
        usable = bool(
            row.get("event_at") is not None
            and _valid_clob_ws_payload(event_type, payload.get("payload"))
        )
        return True, usable, None if usable else "invalid_clob_ws_l2_payload"
    if source == "rtds_ws" and kind == "data":
        usable = bool(
            row.get("event_at") is not None
            and _valid_rtds_payload(event_type, payload.get("payload"))
        )
        return True, usable, None if usable else "invalid_rtds_l2_payload"
    if (
        source == "clob_http"
        and kind == "snapshot"
        and event_type in {"clob_snapshot", "resnapshot"}
    ):
        snapshot = payload.get("payload")
        responses = (
            snapshot.get("responses")
            if isinstance(snapshot, Mapping)
            else None
        )
        usable = bool(
            isinstance(responses, Sequence)
            and not isinstance(responses, (str, bytes, bytearray))
            and any(_valid_clob_http_response(item) for item in responses)
        )
        return True, usable, None if usable else "no_valid_clob_book_response"
    return False, False, None


def _matches_schema_descriptor(value: Any, descriptor: Any) -> bool:
    if descriptor == "null":
        return value is None
    if descriptor == "boolean":
        return isinstance(value, bool)
    if descriptor == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if descriptor == "float":
        return isinstance(value, float)
    if isinstance(descriptor, str) and descriptor in {
        "string",
        "decimal-string",
    }:
        return isinstance(value, str)
    if isinstance(descriptor, Mapping) and set(descriptor) == {"object"}:
        fields = descriptor.get("object")
        return bool(
            isinstance(value, Mapping)
            and isinstance(fields, Mapping)
            and set(value) == set(fields)
            and all(
                _matches_schema_descriptor(value[key], fields[key])
                for key in fields
            )
        )
    if isinstance(descriptor, Mapping) and set(descriptor) == {"array"}:
        alternatives = descriptor.get("array")
        if (
            not isinstance(value, Sequence)
            or isinstance(value, (str, bytes, bytearray))
            or not isinstance(alternatives, list)
        ):
            return False
        if not alternatives:
            return not value
        return all(
            any(
                _matches_schema_descriptor(item, alternative)
                for alternative in alternatives
            )
            for item in value
        )
    return False


def _capture_envelope_errors(
    row: Mapping[str, Any],
    *,
    expected_source: str,
    expected_schema_version: str | None,
    schema_descriptors: Mapping[str, Any] | None,
) -> list[str]:
    errors: list[str] = []
    required = {
        "schema_version",
        "source",
        "received_at",
        "event_at",
        "sequence",
        "payload",
        "record_id",
        "event_fingerprint",
        "schema_fingerprint",
    }
    missing = sorted(required - set(row))
    if missing:
        errors.append(f"missing_fields:{','.join(missing)}")
        return errors
    extra = sorted(set(row) - required)
    if extra:
        errors.append(f"unexpected_fields:{','.join(extra)}")
    if row.get("source") != expected_source:
        errors.append("source_mismatch")
    schema = row.get("schema_version")
    if not isinstance(schema, str) or not schema:
        errors.append("invalid_schema_version")
    elif expected_schema_version is not None and schema != expected_schema_version:
        errors.append("schema_version_mismatch")
    if _iso_timestamp(row.get("received_at")) is None:
        errors.append("invalid_received_at")
    event_at = row.get("event_at")
    if event_at is not None and _iso_timestamp(event_at) is None:
        errors.append("invalid_event_at")
    sequence = row.get("sequence")
    if sequence is not None and not _is_nonnegative_int(sequence):
        errors.append("invalid_sequence")
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        errors.append("invalid_payload")
        return errors
    if not _is_sha256(row.get("record_id")):
        errors.append("invalid_record_id")
    if not _is_sha256(row.get("event_fingerprint")):
        errors.append("invalid_event_fingerprint")
    if not _is_sha256(row.get("schema_fingerprint")):
        errors.append("invalid_schema_fingerprint")
    try:
        if row.get("record_id") != canonical_record_id(row):
            errors.append("record_id_mismatch")
        if row.get("event_fingerprint") != canonical_event_fingerprint(row):
            errors.append("event_fingerprint_mismatch")
    except (DataContractError, TypeError, ValueError) as exc:
        errors.append(f"canonicalization_failed:{type(exc).__name__}")
    schema_fingerprint = row.get("schema_fingerprint")
    if schema_descriptors is not None and _is_sha256(schema_fingerprint):
        descriptor = schema_descriptors.get(schema_fingerprint)
        if descriptor is None:
            errors.append("schema_fingerprint_not_declared")
        elif not _matches_schema_descriptor(payload, descriptor):
            errors.append("payload_schema_descriptor_mismatch")
    return errors


def _nonempty_text(value: Any) -> str | None:
    return value if isinstance(value, str) and bool(value) else None


def _recorder_entity_label(
    source: str,
    domain_payload: Mapping[str, Any],
    event_type: str,
) -> str:
    nested = domain_payload.get("payload")
    nested_mapping = nested if isinstance(nested, Mapping) else {}
    identifier: Any = None
    for key in (
        "asset_id",
        "token_id",
        "market",
        "condition_id",
        "conditionId",
        "symbol",
    ):
        identifier = domain_payload.get(key)
        if identifier not in (None, ""):
            break
        identifier = nested_mapping.get(key)
        if identifier not in (None, ""):
            break
    topic = domain_payload.get("topic")
    if source == "rtds_ws":
        return f"{source}:{topic or event_type}:{identifier or 'stream'}"
    return f"{source}:{identifier or 'stream'}"


def _domain_entity(
    domain_payload: Mapping[str, Any],
) -> tuple[str, str] | None:
    nested = domain_payload.get("payload")
    nested_mapping = nested if isinstance(nested, Mapping) else {}
    groups = (
        ("asset_id", ("asset_id", "token_id")),
        ("condition_id", ("condition_id", "conditionId", "market")),
        ("symbol", ("symbol",)),
    )
    for entity_type, names in groups:
        for name in names:
            direct = _nonempty_text(domain_payload.get(name))
            if direct is not None:
                return entity_type, direct
            nested_value = _nonempty_text(nested_mapping.get(name))
            if nested_value is not None:
                return entity_type, nested_value
    return None


def _time_entity(
    *,
    source: str,
    capture_payload: Mapping[str, Any],
    event_type: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    domain = capture_payload.get("payload")
    if not isinstance(domain, Mapping):
        return None, ["missing_domain_payload"]
    session_id = _nonempty_text(capture_payload.get("session_id"))
    connection_id = _nonempty_text(capture_payload.get("connection_id"))
    frame_index = capture_payload.get("frame_index")
    monotonic_ns = capture_payload.get("monotonic_ns")
    entity = _domain_entity(domain)
    missing: list[str] = []
    if source in {"clob_market_ws", "rtds_ws"}:
        if session_id is None:
            missing.append("missing_session_id")
        if connection_id is None:
            missing.append("missing_connection_id")
        if not _is_nonnegative_int(frame_index):
            missing.append("missing_or_invalid_frame_index")
        if not _is_nonnegative_int(monotonic_ns):
            missing.append("missing_or_invalid_monotonic_ns")
    elif entity is None and source.endswith("_http"):
        entity = ("snapshot_stream", source)
    if entity is None:
        missing.append("missing_stable_entity")
    if missing:
        return None, missing
    assert entity is not None
    topic = (
        _nonempty_text(domain.get("topic"))
        if source == "rtds_ws"
        else None
    )
    descriptor = {
        "session_id": session_id,
        "connection_id": connection_id,
        "source": source,
        "topic": topic,
        "event_type": event_type,
        "entity_type": entity[0],
        "entity": entity[1],
        "recorder_entity": _recorder_entity_label(
            source,
            domain,
            event_type,
        ),
    }
    return descriptor, []


def _time_frame_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    frame_index = payload.get("frame_index")
    monotonic_ns = payload.get("monotonic_ns")
    return {
        "frame_index": (
            frame_index if _is_nonnegative_int(frame_index) else None
        ),
        "frame_hash": (
            payload.get("frame_hash")
            if _is_sha256(payload.get("frame_hash"))
            else None
        ),
        "monotonic_ns": (
            monotonic_ns if _is_nonnegative_int(monotonic_ns) else None
        ),
        "server_timestamp": payload.get("server_timestamp"),
    }


def _time_entity_key(descriptor: Mapping[str, Any]) -> str:
    return _canonical_bytes(descriptor).decode("utf-8")


def _classify_time_observations(
    observations: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(
        observations,
        key=lambda item: (
            str(item.get("source") or ""),
            str(item.get("session_id") or ""),
            str(item.get("connection_id") or ""),
            (
                0
                if _is_nonnegative_int(item.get("monotonic_ns"))
                else 1
            ),
            (
                int(item["monotonic_ns"])
                if _is_nonnegative_int(item.get("monotonic_ns"))
                else 0
            ),
            (
                int(item["frame_index"])
                if (
                    _is_nonnegative_int(item.get("monotonic_ns"))
                    and _is_nonnegative_int(item.get("frame_index"))
                )
                else 0
            ),
            str(item.get("received_at") or ""),
            (
                int(item["line_number"])
                if _is_nonnegative_int(item.get("line_number"))
                else 2**31
            ),
            str(item.get("record_id") or ""),
        ),
    )
    confirmed: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    last_by_entity: dict[str, dict[str, Mapping[str, Any]]] = {}
    last_by_channel: dict[str, dict[str, Mapping[str, Any]]] = {}
    for item in ordered:
        entity = item.get("entity")
        if isinstance(entity, Mapping):
            state_key = _time_entity_key(entity)
            state = last_by_entity.setdefault(state_key, {})
            target = confirmed
        else:
            channel = {
                "session_id": item.get("session_id"),
                "connection_id": item.get("connection_id"),
                "source": item.get("source"),
                "event_type": item.get("event_type"),
            }
            state_key = _time_entity_key(channel)
            state = last_by_channel.setdefault(state_key, {})
            target = ambiguous
        for clock in ("received_at", "event_at"):
            current = item.get(clock)
            if not isinstance(current, str):
                continue
            previous = state.get(clock)
            if (
                previous is not None
                and current < str(previous[clock])
            ):
                regression = {
                    "clock": clock,
                    "previous": previous[clock],
                    "current": current,
                    "previous_record_id": previous["record_id"],
                    "record_id": item["record_id"],
                    "previous_frame_index": previous["frame_index"],
                    "frame_index": item["frame_index"],
                    "previous_frame_hash": previous["frame_hash"],
                    "frame_hash": item["frame_hash"],
                    "previous_server_timestamp": previous[
                        "server_timestamp"
                    ],
                    "current_server_timestamp": item[
                        "server_timestamp"
                    ],
                    "previous_monotonic_ns": previous["monotonic_ns"],
                    "monotonic_ns": item["monotonic_ns"],
                }
                if isinstance(entity, Mapping):
                    regression["entity"] = dict(entity)
                else:
                    regression["channel"] = channel
                    regression["reason"] = item[
                        "missing_entity_fields"
                    ]
                target.append(regression)
            else:
                state[clock] = item
    return confirmed, ambiguous


def _diagnostic_join_key(
    item: Mapping[str, Any],
) -> tuple[str, ...] | None:
    values = (
        item.get("session_id"),
        item.get("connection_id"),
        item.get("entity"),
        item.get("trigger_frame_hash"),
        item.get("previous_server_timestamp"),
        item.get("current_server_timestamp"),
    )
    if any(value is None for value in values):
        return None
    return tuple(str(value) for value in values)


def _crosscheck_time_diagnostics(
    regressions: Sequence[dict[str, Any]],
    diagnostics: Sequence[Mapping[str, Any]],
) -> tuple[int, int]:
    diagnostic_keys = Counter(
        key
        for item in diagnostics
        if (key := _diagnostic_join_key(item)) is not None
    )
    matched_regressions = 0
    for regression in regressions:
        entity = regression.get("entity")
        if (
            regression.get("clock") != "event_at"
            or not isinstance(entity, Mapping)
        ):
            regression["recorder_diagnostic_observed"] = None
            continue
        candidate = {
            "session_id": entity.get("session_id"),
            "connection_id": entity.get("connection_id"),
            "entity": entity.get("recorder_entity"),
            "trigger_frame_hash": regression.get("frame_hash"),
            "previous_server_timestamp": regression.get(
                "previous_server_timestamp"
            ),
            "current_server_timestamp": regression.get(
                "current_server_timestamp"
            ),
        }
        regression_key = _diagnostic_join_key(candidate)
        observed = (
            regression_key is not None
            and diagnostic_keys[regression_key] > 0
        )
        regression["recorder_diagnostic_observed"] = observed
        if observed and regression_key is not None:
            matched_regressions += 1
            diagnostic_keys[regression_key] -= 1
    incomplete_diagnostics = sum(
        1 for item in diagnostics if _diagnostic_join_key(item) is None
    )
    unmatched_diagnostics = (
        incomplete_diagnostics + sum(diagnostic_keys.values())
    )
    return matched_regressions, unmatched_diagnostics


def _analyze_jsonl(
    data: bytes,
    *,
    expected_source: str,
    expected_schema_version: str | None,
    schema_descriptors: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kind_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    resource_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    schema_versions: set[str] = set()
    record_ids: Counter[str] = Counter()
    event_fingerprints: Counter[str] = Counter()
    received_times: list[str | None] = []
    event_times: list[str | None] = []
    invalid_lines: list[dict[str, Any]] = []
    invalid_envelopes: list[dict[str, Any]] = []
    l2_validation_errors: list[dict[str, Any]] = []
    sequence_present_count = 0
    valid_json_count = 0
    usable_l2_record_count = 0
    l2_candidate_record_count = 0
    last_received: str | None = None
    last_event: str | None = None
    last_sequence: int | None = None
    recorder_global_time_regressions: list[dict[str, Any]] = []
    time_observations: list[dict[str, Any]] = []
    non_domain_time_order_excluded_count = 0
    time_regression_diagnostics: list[dict[str, Any]] = []
    sequence_gaps: list[dict[str, int]] = []
    sequence_regressions: list[dict[str, int]] = []
    schema_fingerprint_order: list[str] = []

    lines = data.splitlines()
    blank_line_count = sum(1 for line in lines if not line.strip())
    nonempty_lines = [
        (index, line)
        for index, line in enumerate(lines, start=1)
        if line.strip()
    ]
    for line_number, line in nonempty_lines:
        try:
            row = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            invalid_lines.append(
                {
                    "line": line_number,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if not isinstance(row, dict):
            invalid_envelopes.append(
                {"line": line_number, "errors": ["row_is_not_object"]}
            )
            continue
        valid_json_count += 1
        envelope_errors = _capture_envelope_errors(
            row,
            expected_source=expected_source,
            expected_schema_version=expected_schema_version,
            schema_descriptors=schema_descriptors,
        )
        if envelope_errors:
            invalid_envelopes.append(
                {"line": line_number, "errors": envelope_errors}
            )
            continue
        source = row.get("source")
        if isinstance(source, str) and source:
            source_counts[source] += 1
        schema = row.get("schema_version")
        if isinstance(schema, str) and schema:
            schema_versions.add(schema)
        if row.get("sequence") is not None:
            sequence_present_count += 1
        received = _iso_timestamp(row.get("received_at"))
        event = _iso_timestamp(row.get("event_at"))
        received_times.append(received)
        event_times.append(event)
        record_id = str(row["record_id"])
        payload = row.get("payload")
        assert isinstance(payload, Mapping)
        event_type_value = payload.get("event_type") or payload.get("type")
        normalized_time_event = (
            event_type_value
            if isinstance(event_type_value, str) and event_type_value
            else "unknown"
        )
        frame_metadata = _time_frame_metadata(payload)
        if last_received is not None and received is not None and received < last_received:
            recorder_global_time_regressions.append(
                {
                    "clock": "received_at",
                    "previous": last_received,
                    "current": received,
                    "record_id": record_id,
                }
            )
        if received is not None:
            last_received = received
        if last_event is not None and event is not None and event < last_event:
            recorder_global_time_regressions.append(
                {
                    "clock": "event_at",
                    "previous": last_event,
                    "current": event,
                    "record_id": record_id,
                }
            )
        if event is not None:
            last_event = event
        sequence = row.get("sequence")
        if isinstance(sequence, int):
            if last_sequence is not None:
                if sequence > last_sequence + 1:
                    sequence_gaps.append(
                        {
                            "after": last_sequence,
                            "before": sequence,
                            "missing_start": last_sequence + 1,
                            "missing_end": sequence - 1,
                            "missing_count": sequence - last_sequence - 1,
                        }
                    )
                elif sequence <= last_sequence:
                    sequence_regressions.append(
                        {"previous": last_sequence, "current": sequence}
                    )
            last_sequence = sequence
        record_id = row.get("record_id")
        if isinstance(record_id, str) and record_id:
            record_ids[record_id] += 1
        event_fingerprint = row.get("event_fingerprint")
        if isinstance(event_fingerprint, str) and event_fingerprint:
            event_fingerprints[event_fingerprint] += 1
        schema_fingerprint = row.get("schema_fingerprint")
        if isinstance(schema_fingerprint, str):
            schema_fingerprint_order.append(schema_fingerprint)

        kind = payload.get("kind")
        normalized_kind = (
            kind if isinstance(kind, str) and kind else "unknown"
        )
        kind_counts[normalized_kind] += 1
        event_type = payload.get("event_type") or payload.get("type")
        normalized_event = (
            event_type
            if isinstance(event_type, str) and event_type
            else "unknown"
        )
        event_counts[normalized_event] += 1
        if normalized_kind in _NON_DOMAIN_KINDS:
            non_domain_time_order_excluded_count += 1
        else:
            entity, missing_entity_fields = _time_entity(
                source=expected_source,
                capture_payload=payload,
                event_type=normalized_event,
            )
            time_observations.append(
                {
                    "record_id": record_id,
                    "line_number": line_number,
                    "source": expected_source,
                    "session_id": _nonempty_text(
                        payload.get("session_id")
                    ),
                    "connection_id": _nonempty_text(
                        payload.get("connection_id")
                    ),
                    "event_type": normalized_event,
                    "received_at": received,
                    "event_at": event,
                    "entity": entity,
                    "missing_entity_fields": missing_entity_fields,
                    **frame_metadata,
                }
            )
        if (
            normalized_kind == "diagnostic"
            and normalized_event == "anomaly.time_regression"
        ):
            detail = payload.get("detail")
            diagnostic_entity = (
                _nonempty_text(detail.get("entity"))
                if isinstance(detail, Mapping)
                else None
            )
            time_regression_diagnostics.append(
                {
                    "record_id": record_id,
                    "source": expected_source,
                    "session_id": _nonempty_text(
                        payload.get("session_id")
                    ),
                    "connection_id": _nonempty_text(
                        payload.get("connection_id")
                    ),
                    "entity": diagnostic_entity,
                    "previous_server_timestamp": (
                        detail.get("previous_server_timestamp")
                        if isinstance(detail, Mapping)
                        else None
                    ),
                    "current_server_timestamp": (
                        detail.get("current_server_timestamp")
                        if isinstance(detail, Mapping)
                        else None
                    ),
                    "trigger_frame_hash": (
                        payload.get("trigger_frame_hash")
                        if _is_sha256(payload.get("trigger_frame_hash"))
                        else None
                    ),
                }
            )
        candidate, usable, l2_error = _l2_record_validation(
            source=expected_source,
            row=row,
            payload=payload,
            kind=normalized_kind,
            event_type=normalized_event,
        )
        if candidate:
            l2_candidate_record_count += 1
        if usable:
            usable_l2_record_count += 1
        elif l2_error is not None:
            l2_validation_errors.append(
                {"line": line_number, "error": l2_error}
            )
        inner_payload = payload.get("payload")
        if isinstance(inner_payload, Mapping):
            responses = inner_payload.get("responses")
            if isinstance(responses, Sequence) and not isinstance(
                responses, (str, bytes, bytearray)
            ):
                for response in responses:
                    if not isinstance(response, Mapping):
                        continue
                    resource = response.get("resource")
                    if isinstance(resource, str) and resource:
                        resource_counts[resource] += 1

    time_regressions, ambiguous_time_regressions = (
        _classify_time_observations(time_observations)
    )
    matched_diagnostics, unmatched_diagnostics = (
        _crosscheck_time_diagnostics(
            time_regressions,
            time_regression_diagnostics,
        )
    )

    domain_record_count = sum(
        count
        for kind, count in kind_counts.items()
        if kind not in _NON_DOMAIN_KINDS
    )
    schema_counts = Counter(schema_fingerprint_order)
    first_schema = (
        schema_fingerprint_order[0] if schema_fingerprint_order else None
    )
    schema_drift_count = sum(
        1
        for fingerprint in schema_fingerprint_order[1:]
        if fingerprint != first_schema
    )
    schema_change_count = sum(
        1
        for previous, current in zip(
            schema_fingerprint_order,
            schema_fingerprint_order[1:],
            strict=False,
        )
        if previous != current
    )
    semantic_duplicates = sum(
        count - 1 for count in event_fingerprints.values() if count > 1
    )
    return {
        "line_count": len(nonempty_lines),
        "valid_json_count": valid_json_count,
        "invalid_json_count": len(invalid_lines),
        "invalid_lines": invalid_lines,
        "invalid_envelope_count": len(invalid_envelopes),
        "invalid_envelope_lines": [
            item["line"] for item in invalid_envelopes
        ],
        "capture_envelope_error_count": len(invalid_envelopes),
        "capture_envelope_errors": invalid_envelopes,
        "trailing_newline": not data or data.endswith(b"\n"),
        "blank_line_count": blank_line_count,
        "kind_counts": dict(sorted(kind_counts.items())),
        "event_type_counts": dict(sorted(event_counts.items())),
        "resource_counts": dict(sorted(resource_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "schema_versions": sorted(schema_versions),
        "domain_record_count": domain_record_count,
        "l2_candidate_record_count": l2_candidate_record_count,
        "usable_l2_record_count": usable_l2_record_count,
        "invalid_l2_candidate_count": len(l2_validation_errors),
        "l2_validation_errors": l2_validation_errors,
        "sequence_present_count": sequence_present_count,
        "record_id_duplicate_count": sum(
            count - 1 for count in record_ids.values() if count > 1
        ),
        "unique_event_count_recomputed": len(event_fingerprints),
        "semantic_duplicate_count_observed": semantic_duplicates,
        "recorder_global_time_regression_count_recomputed": len(
            recorder_global_time_regressions
        ),
        "recorder_global_time_regressions_recomputed": (
            recorder_global_time_regressions
        ),
        "time_regression_count_recomputed": len(time_regressions),
        "time_regressions_recomputed": time_regressions,
        "ambiguous_time_regression_count_recomputed": len(
            ambiguous_time_regressions
        ),
        "ambiguous_time_regressions_recomputed": (
            ambiguous_time_regressions
        ),
        "recorder_time_regression_diagnostic_count": len(
            time_regression_diagnostics
        ),
        "recorder_time_regression_diagnostics": (
            time_regression_diagnostics
        ),
        "confirmed_time_regression_diagnostic_match_count": (
            matched_diagnostics
        ),
        "unmatched_recorder_time_regression_diagnostic_count": (
            unmatched_diagnostics
        ),
        "non_domain_time_order_excluded_count": (
            non_domain_time_order_excluded_count
        ),
        "_time_observations": time_observations,
        "sequence_gap_count_recomputed": len(sequence_gaps),
        "sequence_missing_count_recomputed": sum(
            item["missing_count"] for item in sequence_gaps
        ),
        "sequence_gaps_recomputed": sequence_gaps,
        "sequence_regression_count_recomputed": len(sequence_regressions),
        "sequence_regressions_recomputed": sequence_regressions,
        "schema_fingerprint_counts_recomputed": dict(
            sorted(schema_counts.items())
        ),
        "schema_drift_count_recomputed": schema_drift_count,
        "schema_change_count_recomputed": schema_change_count,
        "coverage": {
            "received_at": _timestamp_range(received_times),
            "event_at": _timestamp_range(event_times),
        },
    }


def _is_l2_recording(source: str, content: Mapping[str, Any]) -> bool:
    del source
    return bool(
        int(content.get("usable_l2_record_count", 0)) > 0
        and int(content.get("invalid_l2_candidate_count", 0)) == 0
    )


def _classify_finalized_batch(
    *,
    source: str,
    content: Mapping[str, Any],
    integrity_valid: bool,
) -> tuple[str, str]:
    if not integrity_valid:
        return "L0", "integrity failure blocks empirical use"
    if int(content.get("domain_record_count", 0)) == 0:
        return "L0", "finalized batch contains only lifecycle/diagnostic records"
    if _is_l2_recording(source, content):
        return (
            "L2",
            "checksum-valid forward depth/stream observations under CaptureStore",
        )
    return "L1", "checksum-valid public snapshots or metadata observations"


def _manifest_counts(
    manifest: Mapping[str, Any], *, errors: list[str]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in _MANIFEST_COUNT_FIELDS:
        value = manifest.get(name)
        if not _is_nonnegative_int(value):
            errors.append(f"invalid_manifest_count:{name}")
            counts[name] = 0
        else:
            counts[name] = value
    return counts


def _manifest_schema_metadata(
    value: Any, *, errors: list[str]
) -> tuple[dict[str, int], dict[str, Any]]:
    if not isinstance(value, list):
        errors.append("invalid_manifest_schema_fingerprints")
        return {}, {}
    counts: dict[str, int] = {}
    descriptors: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, Mapping):
            errors.append("invalid_manifest_schema_fingerprint_entry")
            continue
        fingerprint = item.get("fingerprint")
        count = item.get("count")
        if not _is_sha256(fingerprint) or not _is_nonnegative_int(count):
            errors.append("invalid_manifest_schema_fingerprint_entry")
            continue
        if fingerprint in counts:
            errors.append("duplicate_manifest_schema_fingerprint")
            continue
        descriptor = item.get("descriptor")
        try:
            descriptor_hash = _sha256(canonical_json_bytes(descriptor))
        except (DataContractError, TypeError, ValueError):
            errors.append("invalid_manifest_schema_descriptor")
            continue
        if descriptor_hash != fingerprint:
            errors.append("manifest_schema_descriptor_hash_mismatch")
            continue
        counts[fingerprint] = count
        descriptors[fingerprint] = descriptor
    return (
        dict(sorted(counts.items())),
        dict(sorted(descriptors.items())),
    )


def _recomputed_time_range(content: Mapping[str, Any]) -> dict[str, Any]:
    coverage = content.get("coverage")
    if not isinstance(coverage, Mapping):
        return {
            "received_at": {"min": None, "max": None},
            "event_at": {"min": None, "max": None},
        }
    result: dict[str, Any] = {}
    for name in ("received_at", "event_at"):
        value = coverage.get(name)
        result[name] = {
            "min": value.get("min") if isinstance(value, Mapping) else None,
            "max": value.get("max") if isinstance(value, Mapping) else None,
        }
    return result


def _batch_entry(
    *,
    manifest_path: Path,
    capture_root: Path,
    project_root: Path,
) -> dict[str, Any]:
    manifest_bytes = _read_bytes(
        manifest_path, allowed_root=capture_root
    )
    manifest, parse_error = _json_object(manifest_bytes)
    expected_batch_id = manifest_path.name.removesuffix(".manifest.json")
    expected_source = manifest_path.parent.name
    companion = manifest_path.with_name(
        f"{expected_batch_id}.jsonl"
    )
    errors: list[str] = []
    if parse_error:
        errors.append(f"invalid_manifest: {parse_error}")
    parsed = manifest or {}
    source = expected_source
    batch_id = expected_batch_id
    if parsed.get("manifest_version") != "capture-manifest.v1":
        errors.append("unsupported_or_missing_manifest_version")
    schema_version = parsed.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        errors.append("invalid_or_missing_manifest_schema_version")
        schema_version = None
    if parsed.get("source") != expected_source:
        errors.append("manifest_source_does_not_match_directory")
    if parsed.get("batch_id") != expected_batch_id:
        errors.append("manifest_batch_id_does_not_match_filename")
    if _iso_timestamp(parsed.get("finalized_at")) is None:
        errors.append("invalid_or_missing_manifest_finalized_at")
    manifest_schema_counts, schema_descriptors = _manifest_schema_metadata(
        parsed.get("schema_fingerprints"), errors=errors
    )

    raw_bytes = b""
    content = _analyze_jsonl(
        raw_bytes,
        expected_source=expected_source,
        expected_schema_version=schema_version,
        schema_descriptors=schema_descriptors,
    )
    actual_checksum: str | None = None
    raw_path: str | None = None
    companion_available = False
    if not os.path.lexists(companion):
        errors.append("raw_file_missing")
    else:
        try:
            _validate_input_path(companion, allowed_root=capture_root)
        except UnsafeInputPathError as exc:
            errors.append(f"unsafe_raw_input:{exc}")
        else:
            companion_available = True
            raw_path = _relative(companion, project_root)
            raw_bytes = _read_bytes(companion, allowed_root=capture_root)
            actual_checksum = _sha256(raw_bytes)
            content = _analyze_jsonl(
                raw_bytes,
            expected_source=expected_source,
            expected_schema_version=schema_version,
            schema_descriptors=schema_descriptors,
            )
            expected_raw_path = _relative(companion, capture_root)
            if parsed.get("raw_path") != expected_raw_path:
                errors.append("manifest_raw_path_mismatch")

    checksum = parsed.get("checksum")
    expected_checksum: str | None = None
    declared_bytes: int | None = None
    declared_lines: int | None = None
    if not isinstance(checksum, Mapping):
        errors.append("manifest_checksum_missing")
    else:
        if checksum.get("algorithm") != "sha256":
            errors.append("manifest_checksum_algorithm_is_not_sha256")
        checksum_value = checksum.get("value")
        expected_checksum = checksum_value if _is_sha256(checksum_value) else None
        bytes_value = checksum.get("bytes")
        lines_value = checksum.get("lines")
        declared_bytes = bytes_value if _is_nonnegative_int(bytes_value) else None
        declared_lines = lines_value if _is_nonnegative_int(lines_value) else None
        if expected_checksum is None:
            errors.append("manifest_checksum_value_invalid")
        if declared_bytes is None:
            errors.append("manifest_checksum_bytes_invalid")
        if declared_lines is None:
            errors.append("manifest_checksum_lines_invalid")
        if actual_checksum is not None and expected_checksum != actual_checksum:
            errors.append("raw_checksum_mismatch")
        if (
            companion_available
            and declared_bytes is not None
            and declared_bytes != len(raw_bytes)
        ):
            errors.append("raw_byte_count_mismatch")
        if (
            companion_available
            and
            declared_lines is not None
            and declared_lines != content["line_count"]
        ):
            errors.append("raw_line_count_mismatch")

    counts = _manifest_counts(parsed, errors=errors)
    if (
        counts["attempted_count"]
        != counts["record_count"] + counts["duplicate_count"]
    ):
        errors.append("manifest_attempted_count_inconsistent")
    if counts["record_count"] != content["line_count"]:
        errors.append("manifest_record_count_mismatch")
    raw_count_comparisons = {
        "unique_event_count": "unique_event_count_recomputed",
        "semantic_duplicate_count": "semantic_duplicate_count_observed",
        "time_regression_count": (
            "recorder_global_time_regression_count_recomputed"
        ),
        "sequence_gap_count": "sequence_gap_count_recomputed",
        "sequence_missing_count": "sequence_missing_count_recomputed",
        "sequence_regression_count": "sequence_regression_count_recomputed",
        "schema_drift_count": "schema_drift_count_recomputed",
        "schema_change_count": "schema_change_count_recomputed",
    }
    for manifest_name, content_name in raw_count_comparisons.items():
        if counts[manifest_name] != content[content_name]:
            errors.append(f"manifest_{manifest_name}_mismatch")
    if parsed.get("sequence_gaps") != content["sequence_gaps_recomputed"]:
        errors.append("manifest_sequence_gaps_mismatch")
    if (
        parsed.get("sequence_regressions")
        != content["sequence_regressions_recomputed"]
    ):
        errors.append("manifest_sequence_regressions_mismatch")
    if (
        parsed.get("time_regressions")
        != content["recorder_global_time_regressions_recomputed"]
    ):
        errors.append("manifest_time_regressions_mismatch")
    if parsed.get("time_range") != _recomputed_time_range(content):
        errors.append("manifest_time_range_mismatch")
    if (
        manifest_schema_counts
        != content["schema_fingerprint_counts_recomputed"]
    ):
        errors.append("manifest_schema_fingerprint_counts_mismatch")
    if content["invalid_json_count"]:
        errors.append("raw_contains_invalid_json")
    if content["record_id_duplicate_count"]:
        errors.append("raw_contains_duplicate_record_ids")
    if content["blank_line_count"]:
        errors.append("raw_contains_blank_lines")
    if raw_bytes and not content["trailing_newline"]:
        errors.append("raw_missing_trailing_newline")
    if content["capture_envelope_error_count"]:
        errors.append(
            "raw_capture_envelope_invalid:"
            f"{content['capture_envelope_error_count']}"
        )
    integrity_valid = not errors
    evidence_level, evidence_reason = _classify_finalized_batch(
        source=source,
        content=content,
        integrity_valid=integrity_valid,
    )
    return {
        "source": source,
        "batch_id": batch_id,
        "manifest_path": _relative(manifest_path, project_root),
        "raw_path": raw_path,
        "forward_capture": True,
        "evidence_level": evidence_level,
        "evidence_reason": evidence_reason,
        "integrity_valid": integrity_valid,
        "integrity_errors": sorted(set(errors)),
        "counts": counts,
        "count_provenance": {
            "record_count": "recomputed_from_raw",
            "unique_event_count": "recomputed_from_raw",
            "semantic_duplicate_count": "recomputed_from_raw",
            "time_regression_count": (
                "recorder_global_append_order_recomputed_from_raw"
            ),
            "sequence_gap_count": "recomputed_from_raw",
            "sequence_missing_count": "recomputed_from_raw",
            "sequence_regression_count": "recomputed_from_raw",
            "schema_drift_count": "recomputed_from_raw",
            "schema_change_count": "recomputed_from_raw",
            "attempted_count": "manifest_declared_unverifiable",
            "duplicate_count": "manifest_declared_unverifiable",
        },
        "time_range": parsed.get(
            "time_range",
            {
                "received_at": {"min": None, "max": None},
                "event_at": {"min": None, "max": None},
            },
        ),
        "schema_fingerprints": parsed.get("schema_fingerprints", []),
        "anomalies": {
            "time_regressions": content["time_regressions_recomputed"],
            "ambiguous_time_regressions": content[
                "ambiguous_time_regressions_recomputed"
            ],
            "recorder_global_time_regressions": content[
                "recorder_global_time_regressions_recomputed"
            ],
            "recorder_time_regression_diagnostics": content[
                "recorder_time_regression_diagnostics"
            ],
            "sequence_gaps": content["sequence_gaps_recomputed"],
            "sequence_regressions": content[
                "sequence_regressions_recomputed"
            ],
        },
        "content": content,
        "checksum": {
            "algorithm": "sha256",
            "expected": expected_checksum,
            "actual": actual_checksum,
            "verified": bool(
                actual_checksum is not None
                and expected_checksum is not None
                and actual_checksum == expected_checksum
            ),
            "bytes": len(raw_bytes),
            "declared_bytes": declared_bytes,
            "lines": content["line_count"],
            "declared_lines": declared_lines,
            "manifest_sha256": _sha256(manifest_bytes),
        },
    }


def _standalone_file_entry(
    path: Path,
    *,
    project_root: Path,
    allowed_root: Path,
    evidence_level: str = "L0",
    data: bytes | None = None,
) -> dict[str, Any]:
    content = (
        _read_bytes(path, allowed_root=allowed_root)
        if data is None
        else data
    )
    return {
        "path": _relative(path, project_root),
        "evidence_level": evidence_level,
        "checksum": {
            "algorithm": "sha256",
            "value": _sha256(content),
            "bytes": len(content),
            "lines": len(
                [line for line in content.splitlines() if line.strip()]
            ),
        },
    }


def _partial_entry(
    path: Path, *, project_root: Path, capture_root: Path
) -> dict[str, Any]:
    data = _read_bytes(path, allowed_root=capture_root)
    source = path.parent.name
    content = _analyze_jsonl(
        data,
        expected_source=source,
        expected_schema_version=None,
    )
    content.pop("_time_observations", None)
    potential_level = "L2" if _is_l2_recording(source, content) else (
        "L1" if content["domain_record_count"] else "L0"
    )
    return {
        **_standalone_file_entry(
            path,
            project_root=project_root,
            allowed_root=capture_root,
            data=data,
        ),
        "source": source,
        "forward_capture": True,
        "finalized": False,
        "excluded_from_empirical_evidence": True,
        "evidence_reason": "unfinalized partial has no immutable manifest",
        "potential_evidence_level_after_finalize": potential_level,
        "content": content,
    }


def _partial_rotation_advisory(
    path: Path, *, project_root: Path
) -> dict[str, Any]:
    return {
        "path": _relative(path, project_root),
        "reason": (
            "mutable partial disappeared during audit; "
            "possible recorder rotation"
        ),
        "evidence_level": "L0",
        "excluded_from_empirical_evidence": True,
    }


def _checkpoint_entry(
    path: Path, *, project_root: Path, capture_root: Path
) -> dict[str, Any]:
    data = _read_bytes(path, allowed_root=capture_root)
    entry = _standalone_file_entry(
        path,
        project_root=project_root,
        allowed_root=capture_root,
        data=data,
    )
    document, error = _json_object(data)
    source = path.stem
    valid = bool(
        document is not None
        and document.get("schema_version") == "capture-checkpoint.v1"
        and document.get("source") == source
        and isinstance(document.get("checkpoint"), Mapping)
    )
    checkpoint_payload = (
        document.get("checkpoint")
        if document is not None
        and isinstance(document.get("checkpoint"), Mapping)
        else {}
    )
    capture_runtime = checkpoint_payload.get("capture_runtime")
    last_finalized_batch = (
        capture_runtime.get("last_finalized_batch")
        if isinstance(capture_runtime, Mapping)
        and isinstance(capture_runtime.get("last_finalized_batch"), Mapping)
        else None
    )
    return {
        **entry,
        "source": source,
        "valid": valid,
        "error": error if error else (None if valid else "invalid checkpoint contract"),
        "updated_at": document.get("updated_at") if document else None,
        "checkpoint_schema_version": checkpoint_payload.get(
            "schema_version"
        ),
        "last_finalized_batch": (
            dict(last_finalized_batch)
            if isinstance(last_finalized_batch, Mapping)
            else None
        ),
        "last_finalized_batch_reference_valid": None,
        "evidence_role": "restart_hint_only",
    }


def _link_checkpoint_references(
    checkpoints: Sequence[Mapping[str, Any]],
    finalized: Sequence[Mapping[str, Any]],
    *,
    capture_root: Path,
) -> list[dict[str, Any]]:
    by_source_batch = {
        (str(batch["source"]), str(batch["batch_id"])): batch
        for batch in finalized
    }
    linked: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        entry = dict(checkpoint)
        reference = entry.get("last_finalized_batch")
        if reference is None:
            entry["last_finalized_batch_reference_status"] = "not_present"
            linked.append(entry)
            continue
        valid = False
        status = "invalid"
        if isinstance(reference, Mapping):
            batch_id = reference.get("batch_id")
            source = str(entry["source"])
            batch = (
                by_source_batch.get((source, batch_id))
                if isinstance(batch_id, str)
                else None
            )
            checksum = reference.get("checksum")
            if batch is not None and isinstance(checksum, Mapping):
                expected_raw_path = f"raw/{source}/{batch_id}.jsonl"
                valid = bool(
                    reference.get("raw_path") == expected_raw_path
                    and reference.get("record_count")
                    == batch["counts"]["record_count"]
                    and checksum.get("algorithm") == "sha256"
                    and checksum.get("value")
                    == batch["checksum"]["expected"]
                    and checksum.get("bytes")
                    == batch["checksum"]["declared_bytes"]
                    and checksum.get("lines")
                    == batch["checksum"]["declared_lines"]
                    and batch["integrity_valid"]
                )
                status = "verified" if valid else "mismatch"
            elif isinstance(batch_id, str):
                expected_raw_path = f"raw/{source}/{batch_id}.jsonl"
                raw_path = capture_root / expected_raw_path
                manifest_path = raw_path.with_suffix(".manifest.json")
                if (
                    reference.get("raw_path") == expected_raw_path
                    and os.path.lexists(raw_path)
                    and os.path.lexists(manifest_path)
                ):
                    try:
                        _validate_input_path(
                            raw_path, allowed_root=capture_root
                        )
                        _validate_input_path(
                            manifest_path, allowed_root=capture_root
                        )
                    except UnsafeInputPathError:
                        pass
                    else:
                        valid = None
                        status = "ahead_of_frozen_inventory"
        entry["last_finalized_batch_reference_valid"] = valid
        entry["last_finalized_batch_reference_status"] = status
        linked.append(entry)
    return linked


def _legacy_classification(path: Path, parse_valid: bool) -> tuple[str, str]:
    if not parse_valid:
        return "L0", "unreadable legacy artifact"
    name = path.name.lower()
    if path.suffix.lower() == ".md" or name == "compatibility_audit.json":
        return "L0", "documentation or compatibility diagnostic"
    if any(pattern in name for pattern in _LEGACY_L1_PATTERNS):
        return (
            "L1",
            "legacy public snapshot/history/touch evidence; forward L2 is unproven",
        )
    return "L0", "unclassified legacy artifact; fail-closed at L0"


def _legacy_artifact_kind(path: Path) -> str:
    name = path.name.lower()
    if path.suffix.lower() == ".md":
        return "documentation"
    if name == "compatibility_audit.json":
        return "compatibility_diagnostic"
    if "_observe" in name:
        return "touch_observation_log"
    if "_robust" in name:
        return "robustness_scenario"
    if "_replay" in name:
        return "public_history_replay"
    if "_current" in name:
        return "current_public_snapshot"
    if name.startswith("complete_set_scan"):
        return "complete_set_snapshot_scan"
    if name.startswith("reward_scan"):
        return "reward_config_snapshot_scan"
    return "unclassified"


def _flatten_counters(
    value: Any,
    *,
    prefix: str = "",
    depth: int = 0,
) -> dict[str, int]:
    if depth > 4 or not isinstance(value, Mapping):
        return {}
    counters: dict[str, int] = {}
    for key in sorted(value):
        item = value[key]
        path = f"{prefix}.{key}" if prefix else str(key)
        lowered = str(key).lower()
        is_counter_name = (
            lowered
            in {
                "count",
                "evaluated",
                "events",
                "records",
            }
            or lowered.endswith(
                (
                    "_count",
                    "_evaluated",
                    "_prepared",
                    "_records",
                    "_seen",
                    "_skipped",
                )
            )
        )
        if (
            is_counter_name
            and isinstance(item, int)
            and not isinstance(item, bool)
        ):
            counters[path] = item
        elif isinstance(item, Mapping):
            counters.update(
                _flatten_counters(item, prefix=path, depth=depth + 1)
            )
    return counters


def _legacy_json_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {
            "root_type": "array",
            "root_item_count": len(value),
            "counter_fields": {},
            "condition_ids": [],
            "coverage": {},
        }
    if not isinstance(value, Mapping):
        return {
            "root_type": type(value).__name__,
            "counter_fields": {},
            "condition_ids": [],
            "coverage": {},
        }

    counters = _flatten_counters(value)
    for key in ("samples", "results", "rejected", "positive_markets", "warnings"):
        item = value.get(key)
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            counters[f"{key}_item_count"] = len(item)

    condition_ids: set[str] = set()
    timestamps: list[str] = []
    timestamp_ms: list[int] = []

    def visit(item: Any, *, key: str | None = None) -> None:
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                visit(child, key=str(child_key))
            return
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for child in item:
                visit(child, key=key)
            return
        if key == "condition_id" and isinstance(item, str) and item:
            condition_ids.add(item)
        if key in {
            "observed_at",
            "received_at",
            "event_at",
            "generated_at",
            "timestamp",
        }:
            parsed = _iso_timestamp(item)
            if parsed is not None:
                timestamps.append(parsed)
        if (
            key is not None
            and key.endswith("_ms")
            and isinstance(item, int)
            and not isinstance(item, bool)
            and item > 0
        ):
            timestamp_ms.append(item)

    visit(value)
    sample_observation_keys: list[str] = []
    root_condition = value.get("condition_id")
    samples = value.get("samples")
    if (
        isinstance(root_condition, str)
        and root_condition
        and isinstance(samples, Sequence)
        and not isinstance(samples, (str, bytes, bytearray))
    ):
        for sample in samples:
            if not isinstance(sample, Mapping):
                continue
            timestamp = sample.get("timestamp_ms")
            if (
                isinstance(timestamp, int)
                and not isinstance(timestamp, bool)
                and timestamp > 0
            ):
                sample_observation_keys.append(
                    f"{root_condition}|{timestamp}"
                )
    coverage: dict[str, Any] = {
        "iso_timestamps": _timestamp_range(timestamps),
        "epoch_ms": {
            "min": min(timestamp_ms) if timestamp_ms else None,
            "max": max(timestamp_ms) if timestamp_ms else None,
            "count": len(timestamp_ms),
        },
    }
    return {
        "root_type": "object",
        "top_level_keys": sorted(str(key) for key in value),
        "counter_fields": dict(sorted(counters.items())),
        "condition_ids": sorted(condition_ids),
        "sample_observation_key_count": len(sample_observation_keys),
        "_sample_observation_keys": sample_observation_keys,
        "coverage": coverage,
    }


def _legacy_artifact_entry(
    path: Path, *, project_root: Path, legacy_root: Path
) -> dict[str, Any]:
    data = _read_bytes(path, allowed_root=legacy_root)
    suffix = path.suffix.lower()
    parse_error: str | None = None
    summary: dict[str, Any]
    if suffix == ".json":
        try:
            parsed = json.loads(data)
        except (UnicodeError, json.JSONDecodeError) as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
            summary = {"root_type": "invalid_json"}
        else:
            summary = _legacy_json_summary(parsed)
    elif suffix == ".jsonl":
        content = _analyze_legacy_jsonl(data)
        parse_error = (
            "one or more invalid JSONL records"
            if content["invalid_json_count"]
            else None
        )
        summary = content
    elif suffix == ".md":
        summary = {
            "root_type": "markdown",
            "nonempty_line_count": len(
                [line for line in data.splitlines() if line.strip()]
            ),
        }
    else:
        summary = {"root_type": "opaque"}
    level, reason = _legacy_classification(path, parse_error is None)
    return {
        "path": _relative(path, project_root),
        "artifact_kind": _legacy_artifact_kind(path),
        "origin": "legacy_read_only_research",
        "forward_capture": False,
        "evidence_level": level,
        "maximum_permitted_evidence_level": "L1",
        "evidence_reason": reason,
        "parse_valid": parse_error is None,
        "parse_error": parse_error,
        "provenance_limit": (
            "artifact checksum is available, but no CaptureStore raw manifest "
            "proves the original upstream responses"
        ),
        "content": summary,
        "checksum": {
            "algorithm": "sha256",
            "value": _sha256(data),
            "bytes": len(data),
            "lines": len([line for line in data.splitlines() if line.strip()]),
        },
    }


def _analyze_legacy_jsonl(data: bytes) -> dict[str, Any]:
    invalid: list[dict[str, Any]] = []
    valid_records = 0
    timestamps: list[str] = []
    condition_ids: set[str] = set()
    for line_number, line in enumerate(data.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            invalid.append(
                {
                    "line": line_number,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if not isinstance(row, Mapping):
            invalid.append(
                {"line": line_number, "error": "record is not a JSON object"}
            )
            continue
        valid_records += 1
        for name in ("observed_at", "received_at", "event_at", "generated_at"):
            parsed = _iso_timestamp(row.get(name))
            if parsed is not None:
                timestamps.append(parsed)
        condition = row.get("condition_id")
        if isinstance(condition, str) and condition:
            condition_ids.add(condition)
    return {
        "root_type": "jsonl",
        "record_count": valid_records,
        "invalid_json_count": len(invalid),
        "invalid_lines": invalid,
        "condition_ids": sorted(condition_ids),
        "coverage": {"iso_timestamps": _timestamp_range(timestamps)},
    }


def _highest_level(levels: Iterable[str]) -> str:
    present = list(levels)
    if not present:
        return "L0"
    return max(present, key=lambda item: _LEVEL_ORDER.get(item, -1))


def _merge_time_ranges(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    received_min: list[str] = []
    received_max: list[str] = []
    event_min: list[str] = []
    event_max: list[str] = []
    for entry in entries:
        content = entry.get("content")
        coverage = content.get("coverage") if isinstance(content, Mapping) else None
        if not isinstance(coverage, Mapping):
            continue
        received = coverage.get("received_at")
        event = coverage.get("event_at")
        if isinstance(received, Mapping):
            if isinstance(received.get("min"), str):
                received_min.append(received["min"])
            if isinstance(received.get("max"), str):
                received_max.append(received["max"])
        if isinstance(event, Mapping):
            if isinstance(event.get("min"), str):
                event_min.append(event["min"])
            if isinstance(event.get("max"), str):
                event_max.append(event["max"])
    return {
        "received_at": {
            "min": min(received_min) if received_min else None,
            "max": max(received_max) if received_max else None,
        },
        "event_at": {
            "min": min(event_min) if event_min else None,
            "max": max(event_max) if event_max else None,
        },
    }


def _source_summary(
    finalized: Sequence[Mapping[str, Any]],
    partials: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    partial_counts: Counter[str] = Counter()
    for entry in finalized:
        grouped[str(entry["source"])].append(entry)
    for entry in partials:
        partial_counts[str(entry["source"])] += 1

    all_sources = sorted(set(grouped) | set(partial_counts))
    summary: dict[str, Any] = {}
    for source in all_sources:
        entries = grouped[source]
        levels = [str(entry["evidence_level"]) for entry in entries]
        summary[source] = {
            "finalized_batch_count": len(entries),
            "partial_file_count": partial_counts[source],
            "record_count": sum(
                int(entry["content"]["line_count"]) for entry in entries
            ),
            "domain_record_count": sum(
                int(entry["content"]["domain_record_count"])
                for entry in entries
            ),
            "usable_l2_record_count": sum(
                int(entry["content"]["usable_l2_record_count"])
                for entry in entries
            ),
            "declared_duplicate_count_unverified": sum(
                int(entry["counts"]["duplicate_count"]) for entry in entries
            ),
            "semantic_duplicate_count": sum(
                int(entry["content"]["semantic_duplicate_count_observed"])
                for entry in entries
            ),
            "sequence_gap_count": sum(
                int(entry["content"]["sequence_gap_count_recomputed"])
                for entry in entries
            ),
            "sequence_missing_count": sum(
                int(entry["content"]["sequence_missing_count_recomputed"])
                for entry in entries
            ),
            "checksum_valid_batch_count": sum(
                1 for entry in entries if entry["checksum"]["verified"]
            ),
            "integrity_valid_batch_count": sum(
                1 for entry in entries if entry["integrity_valid"]
            ),
            "highest_evidence_level": _highest_level(levels),
            "coverage": _merge_time_ranges(entries),
            "l2_coverage": _merge_time_ranges(
                [
                    entry
                    for entry in entries
                    if entry["evidence_level"] == "L2"
                ]
            ),
        }
    return summary


def _aggregate_time_order(
    finalized: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    observations: list[Mapping[str, Any]] = []
    diagnostics: list[Mapping[str, Any]] = []
    recorder_global_count = 0
    excluded_non_domain_count = 0
    for entry in finalized:
        content = entry.get("content")
        if not isinstance(content, dict):
            continue
        private_observations = content.pop("_time_observations", [])
        if isinstance(private_observations, list):
            observations.extend(
                item
                for item in private_observations
                if isinstance(item, Mapping)
            )
        batch_diagnostics = content.get(
            "recorder_time_regression_diagnostics",
            [],
        )
        if isinstance(batch_diagnostics, list):
            diagnostics.extend(
                item
                for item in batch_diagnostics
                if isinstance(item, Mapping)
            )
        recorder_global_count += int(
            content.get(
                "recorder_global_time_regression_count_recomputed",
                0,
            )
        )
        excluded_non_domain_count += int(
            content.get("non_domain_time_order_excluded_count", 0)
        )
    confirmed, ambiguous = _classify_time_observations(observations)
    ambiguous_observation_count = sum(
        1
        for item in observations
        if not isinstance(item.get("entity"), Mapping)
    )
    matched_diagnostics, unmatched_diagnostics = (
        _crosscheck_time_diagnostics(confirmed, diagnostics)
    )
    return {
        "ordering_policy": (
            "domain records are ordered by recorder monotonic_ns then "
            "frame_index; clocks are checked across batches by source, "
            "session, connection, event_type, topic, and stable entity"
        ),
        "recorder_global_manifest_order_role": (
            "integrity compatibility only; append-order diagnostics may be "
            "persisted before their triggering data record"
        ),
        "confirmed_regression_count": len(confirmed),
        "confirmed_regressions": confirmed,
        "ambiguous_regression_warning_count": len(ambiguous),
        "ambiguous_regressions": ambiguous,
        "ambiguous_observation_warning_count": (
            ambiguous_observation_count
        ),
        "recorder_global_manifest_regression_count": recorder_global_count,
        "recorder_time_regression_diagnostic_count": len(diagnostics),
        "recorder_time_regression_diagnostics": diagnostics,
        "confirmed_diagnostic_match_count": matched_diagnostics,
        "unmatched_diagnostic_advisory_count": unmatched_diagnostics,
        "non_domain_time_order_excluded_count": (
            excluded_non_domain_count
        ),
    }


def _build_gaps(
    *,
    finalized: Sequence[Mapping[str, Any]],
    partials: Sequence[Mapping[str, Any]],
    partial_rotation_advisories: Sequence[Mapping[str, Any]],
    unpaired_raw: Sequence[Mapping[str, Any]],
    checkpoints: Sequence[Mapping[str, Any]],
    source_summary: Mapping[str, Any],
    legacy: Sequence[Mapping[str, Any]],
    unsafe_inputs: Sequence[Mapping[str, Any]],
    inventory_stable: bool,
    time_order: Mapping[str, Any],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []

    def add(code: str, severity: str, scope: str, detail: str) -> None:
        gaps.append(
            {
                "code": code,
                "severity": severity,
                "scope": scope,
                "detail": detail,
            }
        )

    for batch in finalized:
        scope = str(batch["manifest_path"])
        for error in batch["integrity_errors"]:
            code = (
                "raw_checksum_mismatch"
                if error == "raw_checksum_mismatch"
                else "capture_integrity_failure"
            )
            add(code, "error", scope, str(error))
        if int(batch["content"]["domain_record_count"]) == 0:
            add(
                "finalized_batch_has_no_domain_records",
                "warning",
                scope,
                "lifecycle/diagnostic records do not establish market-data coverage",
            )
        if int(batch["content"]["sequence_gap_count_recomputed"]) > 0:
            add(
                "sequence_gap",
                "error",
                scope,
                (
                    f"{batch['content']['sequence_missing_count_recomputed']} "
                    "sequence values missing"
                ),
            )
        if int(batch["content"]["schema_drift_count_recomputed"]) > 0:
            add(
                "schema_drift",
                "warning",
                scope,
                (
                    f"{batch['content']['schema_drift_count_recomputed']} "
                    "non-baseline records"
                ),
            )
        if int(batch["content"]["invalid_l2_candidate_count"]) > 0:
            add(
                "invalid_l2_candidate",
                "warning",
                scope,
                (
                    f"{batch['content']['invalid_l2_candidate_count']} "
                    "forward records were unusable for L2 replay"
                ),
            )
        if int(batch["counts"]["duplicate_count"]) > 0:
            add(
                "declared_duplicate_attempts_unverifiable",
                "info",
                scope,
                (
                    f"manifest declares {batch['counts']['duplicate_count']} "
                    "deduplicated append attempts, but raw stores no attempt journal"
                ),
            )
        if (
            int(batch["content"]["domain_record_count"]) > 0
            and int(batch["content"]["sequence_present_count"]) == 0
        ):
            add(
                "upstream_sequence_unavailable",
                "info",
                scope,
                "zero sequence gaps cannot prove lossless delivery without upstream sequence",
            )

    confirmed_time_regressions = int(
        time_order["confirmed_regression_count"]
    )
    if confirmed_time_regressions > 0:
        add(
            "time_regression",
            "error",
            "capture/time_order",
            (
                f"{confirmed_time_regressions} same-clock-domain "
                "regressions across finalized batches"
            ),
        )
    ambiguous_time_regressions = int(
        time_order["ambiguous_regression_warning_count"]
    )
    if ambiguous_time_regressions > 0:
        add(
            "ambiguous_time_regression",
            "warning",
            "capture/time_order",
            (
                f"{ambiguous_time_regressions} regressions could not be "
                "assigned to a stable entity"
            ),
        )
    ambiguous_observations = int(
        time_order["ambiguous_observation_warning_count"]
    )
    if ambiguous_observations > 0:
        add(
            "ambiguous_time_entity",
            "warning",
            "capture/time_order",
            (
                f"{ambiguous_observations} domain observations lacked a "
                "reliable monotonic/entity clock key"
            ),
        )
    unmatched_diagnostics = int(
        time_order["unmatched_diagnostic_advisory_count"]
    )
    if unmatched_diagnostics > 0:
        add(
            "cross_event_ordering_advisory",
            "warning",
            "capture/time_order",
            (
                f"{unmatched_diagnostics} recorder time diagnostics did "
                "not exact-match a same-event-type regression"
            ),
        )

    for entry in partials:
        add(
            "unfinalized_partial_excluded",
            "warning",
            str(entry["path"]),
            "partial is mutable and has no immutable manifest",
        )
    for entry in partial_rotation_advisories:
        add(
            "mutable_partial_rotated_during_audit",
            "info",
            str(entry["path"]),
            str(entry["reason"]),
        )
    for entry in unpaired_raw:
        add(
            "raw_without_manifest",
            "error",
            str(entry["path"]),
            "raw file is excluded because its CaptureStore manifest is missing",
        )
    for entry in checkpoints:
        if not entry["valid"]:
            add(
                "invalid_checkpoint",
                "warning",
                str(entry["path"]),
                str(entry["error"]),
            )
        if entry["last_finalized_batch_reference_valid"] is False:
            add(
                "checkpoint_raw_reference_invalid",
                "error",
                str(entry["path"]),
                "last finalized batch reference does not match immutable raw evidence",
            )
        elif (
            entry.get("last_finalized_batch_reference_status")
            == "ahead_of_frozen_inventory"
        ):
            add(
                "checkpoint_ahead_of_frozen_inventory",
                "info",
                str(entry["path"]),
                "mutable checkpoint references a batch finalized after path freeze",
            )
    for entry in legacy:
        if not entry["parse_valid"]:
            add(
                "invalid_legacy_artifact",
                "warning",
                str(entry["path"]),
                str(entry["parse_error"]),
            )
    for entry in unsafe_inputs:
        add(
            "unsafe_input_path",
            "error",
            str(entry["path"]),
            str(entry["reason"]),
        )
    if not inventory_stable:
        add(
            "capture_inventory_changed_during_freeze",
            "warning",
            "data/edge_discovery_2026-07-24",
            "path inventory did not stabilize within five immediate retries",
        )

    for source in _EXPECTED_CAPTURE_SOURCES:
        observed = source_summary.get(source)
        if not isinstance(observed, Mapping):
            add(
                "capture_source_missing",
                "warning",
                source,
                "no finalized batch or partial file observed",
            )
        elif int(observed.get("finalized_batch_count", 0)) == 0:
            add(
                "source_has_no_finalized_batch",
                "warning",
                source,
                "source exists only as mutable partial/checkpoint state",
            )
        elif int(observed.get("domain_record_count", 0)) == 0:
            add(
                "source_has_no_domain_records",
                "warning",
                source,
                "finalized batches contain no usable market observations",
            )
    return sorted(
        gaps,
        key=lambda item: (
            {"error": 0, "warning": 1, "info": 2}.get(item["severity"], 3),
            item["code"],
            item["scope"],
            item["detail"],
        ),
    )


def _cross_source_overlap(
    sources: Mapping[str, Any],
) -> dict[str, Any]:
    ranges: list[tuple[str, str]] = []
    for source in ("clob_market_ws", "rtds_ws"):
        summary = sources.get(source)
        if (
            not isinstance(summary, Mapping)
            or summary.get("highest_evidence_level") != "L2"
        ):
            return {
                "required_sources": ["clob_market_ws", "rtds_ws"],
                "start": None,
                "end": None,
                "has_overlap": False,
            }
        coverage = summary.get("l2_coverage")
        received = coverage.get("received_at") if isinstance(coverage, Mapping) else None
        if (
            not isinstance(received, Mapping)
            or not isinstance(received.get("min"), str)
            or not isinstance(received.get("max"), str)
        ):
            return {
                "required_sources": ["clob_market_ws", "rtds_ws"],
                "start": None,
                "end": None,
                "has_overlap": False,
            }
        ranges.append((received["min"], received["max"]))
    start = max(item[0] for item in ranges)
    end = min(item[1] for item in ranges)
    return {
        "required_sources": ["clob_market_ws", "rtds_ws"],
        "start": start,
        "end": end,
        "has_overlap": start <= end,
    }


def build_data_audit(
    *,
    project_root: str | Path,
    capture_root: str | Path,
    legacy_root: str | Path,
) -> DataAuditBundle:
    """Read current artifacts once and build deterministic audit documents.

    This function performs no filesystem writes and no network operations.
    Output contains no wall-clock build timestamp; unchanged inputs therefore
    yield byte-identical canonical JSON.
    """

    project = Path(project_root).expanduser().resolve()
    capture = _safe_root(
        capture_root, project_root=project, label="capture_root"
    )
    legacy_path = _safe_root(
        legacy_root, project_root=project, label="legacy_root"
    )

    inventory, inventory_stable = _capture_path_inventory(capture)
    manifest_candidates = inventory["manifests"]
    raw_candidates = inventory["raw"]
    partial_candidates = inventory["partials"]
    checkpoint_candidates = inventory["checkpoints"]
    manifest_paths, unsafe_manifests = _partition_input_paths(
        manifest_candidates,
        allowed_root=capture,
        project_root=project,
    )
    raw_paths, unsafe_raw = _partition_input_paths(
        raw_candidates,
        allowed_root=capture,
        project_root=project,
    )
    partial_paths, unsafe_partials = _partition_input_paths(
        partial_candidates,
        allowed_root=capture,
        project_root=project,
    )
    partial_candidates_by_relative = {
        _relative(path, project): path for path in partial_candidates
    }
    partial_rotation_advisories = [
        _partial_rotation_advisory(
            partial_candidates_by_relative[str(entry["path"])],
            project_root=project,
        )
        for entry in unsafe_partials
        if entry["reason"] == "input disappeared during audit"
    ]
    unsafe_partials = [
        entry
        for entry in unsafe_partials
        if entry["reason"] != "input disappeared during audit"
    ]
    checkpoint_paths, unsafe_checkpoints = _partition_input_paths(
        checkpoint_candidates,
        allowed_root=capture,
        project_root=project,
    )
    partials: list[dict[str, Any]] = []
    for path in partial_paths:
        try:
            partial = _partial_entry(
                path,
                project_root=project,
                capture_root=capture,
            )
        except UnsafeInputPathError as exc:
            if str(exc) != "input disappeared during audit":
                raise
            partial_rotation_advisories.append(
                _partial_rotation_advisory(
                    path,
                    project_root=project,
                )
            )
        else:
            partials.append(partial)
    checkpoint_entries = [
        _checkpoint_entry(
            path,
            project_root=project,
            capture_root=capture,
        )
        for path in checkpoint_paths
    ]
    finalized = [
        _batch_entry(
            manifest_path=path,
            capture_root=capture,
            project_root=project,
        )
        for path in manifest_paths
    ]
    time_order = _aggregate_time_order(finalized)
    paired_raw_paths = {
        Path(
            os.path.abspath(
                path.with_name(
                    f"{path.name.removesuffix('.manifest.json')}.jsonl"
                )
            )
        )
        for path in manifest_paths
    }
    unpaired_raw = [
        {
            **_standalone_file_entry(
                path,
                project_root=project,
                allowed_root=capture,
            ),
            "source": path.parent.name,
            "forward_capture": True,
            "excluded_from_empirical_evidence": True,
            "evidence_reason": "raw file has no CaptureStore manifest",
        }
        for path in raw_paths
        if Path(os.path.abspath(path)) not in paired_raw_paths
    ]
    checkpoints = _link_checkpoint_references(
        checkpoint_entries,
        finalized,
        capture_root=capture,
    )
    legacy_candidates = (
        [
            path
            for path in sorted(legacy_path.rglob("*"))
            if (path.is_symlink() or path.is_file())
            and path.name not in _OUTPUT_NAMES
        ]
        if legacy_path.is_dir()
        else []
    )
    legacy_files, unsafe_legacy = _partition_input_paths(
        legacy_candidates,
        allowed_root=legacy_path,
        project_root=project,
    )
    legacy_entries = [
        _legacy_artifact_entry(
            path,
            project_root=project,
            legacy_root=legacy_path,
        )
        for path in legacy_files
    ]
    unsafe_capture = sorted(
        [
            *unsafe_manifests,
            *unsafe_raw,
            *unsafe_partials,
            *unsafe_checkpoints,
        ],
        key=lambda item: (item["path"], item["reason"]),
    )
    unsafe_inputs = sorted(
        [*unsafe_capture, *unsafe_legacy],
        key=lambda item: (item["path"], item["reason"]),
    )
    keyed_legacy_sample_rows = 0
    legacy_sample_observation_keys: set[str] = set()
    for entry in legacy_entries:
        content = entry.get("content")
        if not isinstance(content, dict):
            continue
        keys = content.pop("_sample_observation_keys", [])
        if not isinstance(keys, list):
            continue
        keyed_legacy_sample_rows += len(keys)
        legacy_sample_observation_keys.update(
            key for key in keys if isinstance(key, str)
        )
    sources = _source_summary(finalized, partials)

    capture_record_count = sum(
        int(entry["content"]["line_count"]) for entry in finalized
    )
    capture_domain_count = sum(
        int(entry["content"]["domain_record_count"]) for entry in finalized
    )
    legacy_embedded_samples = sum(
        int(entry["content"].get("counter_fields", {}).get(
            "samples_item_count", 0
        ))
        for entry in legacy_entries
        if isinstance(entry.get("content"), Mapping)
        and isinstance(entry["content"].get("counter_fields"), Mapping)
    )
    legacy_jsonl_records = sum(
        int(entry["content"].get("record_count", 0))
        for entry in legacy_entries
        if isinstance(entry.get("content"), Mapping)
        and entry["content"].get("root_type") == "jsonl"
    )
    legacy_condition_ids = {
        condition_id
        for entry in legacy_entries
        if isinstance(entry.get("content"), Mapping)
        for condition_id in entry["content"].get("condition_ids", [])
        if isinstance(condition_id, str)
    }
    legacy_reused_sample_rows = (
        keyed_legacy_sample_rows - len(legacy_sample_observation_keys)
    )
    totals = {
        "finalized_batch_count": len(finalized),
        "partial_file_count": len(partials),
        "partial_rotation_advisory_count": len(
            partial_rotation_advisories
        ),
        "checkpoint_count": len(checkpoints),
        "raw_without_manifest_count": len(unpaired_raw),
        "unsafe_input_path_count": len(unsafe_inputs),
        "capture_path_inventory_stable": inventory_stable,
        "capture_record_count": capture_record_count,
        "capture_domain_record_count": capture_domain_count,
        "legacy_artifact_count": len(legacy_entries),
        "legacy_l1_artifact_count": sum(
            1 for entry in legacy_entries if entry["evidence_level"] == "L1"
        ),
        "legacy_embedded_sample_count": legacy_embedded_samples,
        "legacy_keyed_sample_row_count": keyed_legacy_sample_rows,
        "legacy_unique_sample_observation_key_count": len(
            legacy_sample_observation_keys
        ),
        "legacy_reused_sample_row_count": legacy_reused_sample_rows,
        "legacy_jsonl_record_count": legacy_jsonl_records,
        "legacy_unique_condition_count": len(legacy_condition_ids),
        "computed_input_checksum_count": (
            sum(
                1 + (1 if entry["raw_path"] is not None else 0)
                for entry in finalized
            )
            + len(partials)
            + len(checkpoints)
            + len(unpaired_raw)
            + len(legacy_entries)
        ),
    }
    manifest_without_hash: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "roots": {
            "project": ".",
            "capture": _relative(capture, project),
            "legacy_read_only": _relative(legacy_path, project),
        },
        "level_policy": {
            "L0": "synthetic, diagnostic, lifecycle-only, partial, or failed integrity",
            "L1": "public snapshot/history/touch evidence; no fill claim",
            "L2": (
                "finalized checksum-valid forward depth/WS/RTDS observations; "
                "still not maker identity or realized payout"
            ),
            "legacy_cap": "L1",
        },
        "capture": {
            "path_inventory_stable": inventory_stable,
            "time_order": time_order,
            "finalized_batches": finalized,
            "partial_files": partials,
            "partial_rotation_advisories": partial_rotation_advisories,
            "checkpoints": checkpoints,
            "raw_without_manifest": unpaired_raw,
            "unsafe_input_paths": unsafe_capture,
            "sources": sources,
        },
        "unsafe_legacy_input_paths": unsafe_legacy,
        "legacy_artifacts": legacy_entries,
        "totals": totals,
    }
    data_manifest_hash = _sha256(_canonical_bytes(manifest_without_hash))
    manifest = {
        **manifest_without_hash,
        "data_manifest_hash": data_manifest_hash,
    }

    gaps = _build_gaps(
        finalized=finalized,
        partials=partials,
        partial_rotation_advisories=partial_rotation_advisories,
        unpaired_raw=unpaired_raw,
        checkpoints=checkpoints,
        source_summary=sources,
        legacy=legacy_entries,
        unsafe_inputs=unsafe_inputs,
        inventory_stable=inventory_stable,
        time_order=time_order,
    )
    batch_levels = [str(entry["evidence_level"]) for entry in finalized]
    legacy_levels = [str(entry["evidence_level"]) for entry in legacy_entries]
    forward_l2_sources = sorted(
        source
        for source, summary in sources.items()
        if summary["highest_evidence_level"] == "L2"
    )
    checksum_mismatches = [
        entry for entry in finalized if not entry["checksum"]["verified"]
    ]
    integrity_invalid = [
        entry for entry in finalized if not entry["integrity_valid"]
    ]
    cross_source_overlap = _cross_source_overlap(sources)
    latency_source_error = any(
        gap["severity"] == "error"
        and any(
            marker in str(gap["scope"])
            for marker in ("/clob_market_ws/", "/rtds_ws/")
        )
        for gap in gaps
    )
    latency_source_error = latency_source_error or any(
        isinstance(regression.get("entity"), Mapping)
        and regression["entity"].get("source")
        in {"clob_market_ws", "rtds_ws"}
        for regression in time_order["confirmed_regressions"]
    )
    cross_source_inputs_present = bool(
        cross_source_overlap["has_overlap"]
    )
    quality = {
        "schema_version": QUALITY_SCHEMA_VERSION,
        "data_manifest_hash": data_manifest_hash,
        "status": (
            "fail"
            if any(item["severity"] == "error" for item in gaps)
            else ("degraded" if gaps else "pass")
        ),
        "counts": totals,
        "sources": sources,
        "coverage": {
            "forward_finalized": _merge_time_ranges(finalized),
            "cross_source_latency": cross_source_overlap,
        },
        "duplicates": {
            "exact_duplicate_count_recomputed": None,
            "declared_exact_duplicate_count_unverified": sum(
                int(entry["counts"]["duplicate_count"]) for entry in finalized
            ),
            "semantic_duplicate_count": sum(
                int(entry["content"]["semantic_duplicate_count_observed"])
                for entry in finalized
            ),
            "observed_duplicate_record_id_count": sum(
                int(entry["content"]["record_id_duplicate_count"])
                for entry in finalized
            ),
            "legacy_reused_sample_row_count": legacy_reused_sample_rows,
            "legacy_unique_condition_timestamp_key_count": len(
                legacy_sample_observation_keys
            ),
        },
        "sequence": {
            "gap_count": sum(
                int(entry["content"]["sequence_gap_count_recomputed"])
                for entry in finalized
            ),
            "missing_count": sum(
                int(entry["content"]["sequence_missing_count_recomputed"])
                for entry in finalized
            ),
            "regression_count": sum(
                int(entry["content"]["sequence_regression_count_recomputed"])
                for entry in finalized
            ),
        },
        "time": {
            "confirmed_same_clock_domain_regression_count": int(
                time_order["confirmed_regression_count"]
            ),
            "ambiguous_regression_warning_count": int(
                time_order["ambiguous_regression_warning_count"]
            ),
            "ambiguous_observation_warning_count": int(
                time_order["ambiguous_observation_warning_count"]
            ),
            "recorder_global_manifest_regression_count": int(
                time_order["recorder_global_manifest_regression_count"]
            ),
            "recorder_time_regression_diagnostic_count": int(
                time_order["recorder_time_regression_diagnostic_count"]
            ),
            "confirmed_diagnostic_match_count": int(
                time_order["confirmed_diagnostic_match_count"]
            ),
            "unmatched_diagnostic_advisory_count": int(
                time_order["unmatched_diagnostic_advisory_count"]
            ),
            "non_domain_time_order_excluded_count": int(
                time_order["non_domain_time_order_excluded_count"]
            ),
            "policy": (
                "domain records are monotonic/frame ordered and checked "
                "across batches by session, connection, source, event type, "
                "topic, and stable entity; unkeyed regressions are warnings"
            ),
        },
        "checksums": {
            "declared_finalized_batch_count": len(finalized),
            "verified_finalized_batch_count": (
                len(finalized) - len(checksum_mismatches)
            ),
            "mismatch_count": len(checksum_mismatches),
            "integrity_invalid_batch_count": len(integrity_invalid),
        },
        "levels": {
            "highest_observed": _highest_level(batch_levels + legacy_levels),
            "highest_forward_finalized": _highest_level(batch_levels),
            "legacy_maximum": "L1",
            "forward_l2_sources": forward_l2_sources,
            "cross_source_latency_inputs_present": (
                cross_source_inputs_present
            ),
            "cross_source_latency_l2_ready": bool(
                cross_source_inputs_present and not latency_source_error
            ),
            "warning": (
                "Evidence level describes input fidelity, not profitability, "
                "maker fills, reward payout, or live readiness."
            ),
        },
        "gaps": gaps,
    }
    return DataAuditBundle(manifest=manifest, quality=quality)


def _write_new_file(path: Path, data: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_data_audit(
    bundle: DataAuditBundle,
    *,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    """Write canonical JSON artifacts without replacing existing files."""

    directory = Path(output_dir).expanduser().resolve()
    manifest_path = directory / OUTPUT_MANIFEST_NAME
    quality_path = directory / OUTPUT_QUALITY_NAME
    manifest_bytes = _canonical_bytes(bundle.manifest) + b"\n"
    quality_bytes = _canonical_bytes(bundle.quality) + b"\n"
    if manifest_path.exists():
        raise FileExistsError(
            "refusing to overwrite existing DATA_MANIFEST.json or DATA_QUALITY.json"
        )
    directory.mkdir(parents=True, exist_ok=True)
    if quality_path.exists():
        if quality_path.read_bytes() != quality_bytes:
            raise FileExistsError(
                "existing DATA_QUALITY.json is not the identical resumable prepare"
            )
    else:
        _write_new_file(quality_path, quality_bytes)
        _fsync_directory(directory)
    _write_new_file(manifest_path, manifest_bytes)
    _fsync_directory(directory)
    return manifest_path, quality_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build deterministic Edge Lab data manifest and quality JSON"
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--capture-root",
        type=Path,
        default=Path("data/edge_discovery_2026-07-24"),
    )
    parser.add_argument(
        "--legacy-root",
        type=Path,
        default=Path("research/profit_redesign_2026-07-24"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/edge_discovery_2026-07-24"),
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="build and print the deterministic hash without writing files",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project = args.project_root.expanduser().resolve()

    def resolve_from_project(path: Path) -> Path:
        return path if path.is_absolute() else project / path

    bundle = build_data_audit(
        project_root=project,
        capture_root=resolve_from_project(args.capture_root),
        legacy_root=resolve_from_project(args.legacy_root),
    )
    if not args.check_only:
        write_data_audit(
            bundle,
            output_dir=resolve_from_project(args.output_dir),
        )
    print(
        json.dumps(
            {
                "data_manifest_hash": bundle.manifest["data_manifest_hash"],
                "status": bundle.quality["status"],
                "highest_observed": bundle.quality["levels"][
                    "highest_observed"
                ],
                "forward_l2_sources": bundle.quality["levels"][
                    "forward_l2_sources"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
