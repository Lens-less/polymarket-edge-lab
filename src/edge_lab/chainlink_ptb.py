"""Strict decision-vintage Chainlink price-to-beat evidence.

This module consumes immutable RTDS raw envelopes only.  It never fetches a
price and never guesses a window boundary: callers provide the exact market
open timestamp, and only a Chainlink observation at that timestamp can become
price-to-beat evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from typing import Literal

from .data_store import canonical_record_id
from .data_manifest import _batch_entry, _read_bytes
from .short_crypto_catalog import ShortCryptoTarget


_TOPIC = "crypto_prices_chainlink"
_SYMBOLS = frozenset({"btc/usd", "eth/usd"})
_SOURCE_URLS = {
    "btc/usd": "https://data.chain.link/streams/btc-usd",
    "eth/usd": "https://data.chain.link/streams/eth-usd",
}
_DECIMAL_TEXT = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_INTEGER_TEXT = re.compile(r"^[0-9]+$")
_SHA256_TEXT = re.compile(r"^[0-9a-f]{64}$")


class PriceToBeatRejection(ValueError):
    """Frozen public evidence violated a price-to-beat invariant."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ChainlinkPriceToBeat:
    """One lossless Chainlink observation and its local availability vintage."""

    source_topic: str
    source_symbol: str
    source_timestamp_ms: int
    price: Decimal
    display_value: str
    display_encoding: str
    full_accuracy_value: str
    raw_record_id: str
    received_at: str
    first_decision_at: str
    retrospective: bool
    available_at_first_decision: bool


@dataclass(frozen=True)
class FinalizedChainlinkBoundary:
    """One manifest-verified Chainlink boundary with replay provenance."""

    boundary_role: Literal["open", "close"]
    source_topic: str
    source_symbol: str
    rule_url: str
    rule_hash: str
    inner_timestamp_ms: int
    price: Decimal
    display_value: str
    display_encoding: str
    full_accuracy_value: str
    record_id: str
    session_id: str
    connection_id: str
    received_at: str
    source_manifest_path: str
    source_manifest_sha256: str
    raw_path: str
    raw_sha256: str
    line_number: int
    retrospective: bool
    available_at_first_decision: bool


@dataclass(frozen=True)
class FinalizedChainlinkBoundaries:
    """Exact open and close observations for one strict short-crypto target."""

    open_boundary: FinalizedChainlinkBoundary
    close_boundary: FinalizedChainlinkBoundary


@dataclass(frozen=True)
class FinalizedChainlinkBoundaryRequest:
    """One target boundary requested from a shared finalized RTDS scan."""

    target: ShortCryptoTarget
    rule_url: str
    boundary_role: Literal["open", "close"]
    first_decision_at: str


def require_price_to_beat_for_decision(
    price_to_beat: ChainlinkPriceToBeat | FinalizedChainlinkBoundary,
    *,
    decision_at: str,
) -> ChainlinkPriceToBeat | FinalizedChainlinkBoundary:
    """Return evidence only when it was locally available before a decision."""

    if (
        isinstance(price_to_beat, FinalizedChainlinkBoundary)
        and price_to_beat.boundary_role != "open"
    ):
        raise PriceToBeatRejection(
            "boundary_role_not_price_to_beat",
            "only a strict window-open boundary can become price-to-beat",
        )
    if (
        price_to_beat.retrospective
        or not price_to_beat.available_at_first_decision
    ):
        raise PriceToBeatRejection(
            "retrospective_price_to_beat",
            "post-decision boundary evidence cannot become a decision-time PTB",
        )
    decision = _utc_datetime(decision_at, label="decision_at")
    received = _utc_datetime(
        price_to_beat.received_at,
        label="price_to_beat.received_at",
    )
    if decision <= received:
        raise PriceToBeatRejection(
            "price_to_beat_not_yet_received",
            "decision must be strictly later than local PTB receipt",
        )
    return price_to_beat


def _utc_datetime(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PriceToBeatRejection(
            "timestamp_invalid",
            f"{label} must be a non-empty ISO-8601 string",
        )
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(
            f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError as exc:
        raise PriceToBeatRejection(
            "timestamp_invalid",
            f"{label} must be ISO-8601",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PriceToBeatRejection(
            "timestamp_invalid",
            f"{label} must include a timezone",
        )
    return parsed.astimezone(timezone.utc)


def _nested_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _format_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _exact_scaled_integer(value: str) -> int | None:
    whole, separator, fraction = value.partition(".")
    if not separator:
        fraction = ""
    excess = fraction[18:]
    if any(character != "0" for character in excess):
        return None
    fractional_units = int(fraction[:18].ljust(18, "0") or "0")
    return int(whole) * 10**18 + fractional_units


def _decimal_from_scaled_integer(value: str) -> Decimal:
    padded = value.rjust(19, "0")
    return Decimal(f"{padded[:-18]}.{padded[-18:]}")


def extract_chainlink_price_to_beat(
    records: Sequence[Mapping[str, Any]],
    *,
    source_symbol: str,
    opens_at_ms: int,
    first_decision_at: str,
) -> ChainlinkPriceToBeat:
    """Extract the exact Chainlink window-open observation from raw records."""

    if source_symbol not in _SYMBOLS:
        raise PriceToBeatRejection(
            "unsupported_source_symbol",
            "v1 supports only btc/usd and eth/usd Chainlink streams",
        )
    if (
        isinstance(opens_at_ms, bool)
        or not isinstance(opens_at_ms, int)
        or opens_at_ms <= 0
        or opens_at_ms % 1_000 != 0
    ):
        raise PriceToBeatRejection(
            "target_window_invalid",
            "opens_at_ms must be a positive whole-second Unix millisecond value",
        )
    first_decision = _utc_datetime(
        first_decision_at,
        label="first_decision_at",
    )
    candidates: list[Mapping[str, Any]] = []
    for record in records:
        recorder = _nested_mapping(record.get("payload"))
        update = _nested_mapping(recorder.get("payload")) if recorder else None
        price_payload = (
            _nested_mapping(update.get("payload")) if update else None
        )
        if (
            update is not None
            and price_payload is not None
            and update.get("topic") == _TOPIC
            and price_payload.get("symbol") == source_symbol
            and price_payload.get("timestamp") == opens_at_ms
        ):
            candidates.append(record)

    if not candidates:
        raise PriceToBeatRejection(
            "price_to_beat_missing",
            "no exact Chainlink window-open observation was found",
        )
    candidate_ids = [candidate.get("record_id") for candidate in candidates]
    valid_candidate_ids = [
        record_id for record_id in candidate_ids if isinstance(record_id, str)
    ]
    if len(set(valid_candidate_ids)) != len(valid_candidate_ids):
        raise PriceToBeatRejection(
            "duplicate_record_id",
            "the same raw observation appears more than once",
        )
    if len(candidates) > 1:
        observed_prices: set[tuple[Any, Any]] = set()
        for candidate in candidates:
            candidate_recorder = _nested_mapping(candidate.get("payload"))
            candidate_update = (
                _nested_mapping(candidate_recorder.get("payload"))
                if candidate_recorder
                else None
            )
            candidate_price = (
                _nested_mapping(candidate_update.get("payload"))
                if candidate_update
                else None
            )
            observed_prices.add(
                (
                    candidate_price.get("value") if candidate_price else None,
                    (
                        candidate_price.get("full_accuracy_value")
                        if candidate_price
                        else None
                    ),
                )
            )
        if len(observed_prices) > 1:
            raise PriceToBeatRejection(
                "conflicting_price_to_beat",
                "multiple values claim the same symbol and source timestamp",
            )
        raise PriceToBeatRejection(
            "duplicate_price_to_beat",
            "multiple raw observations claim the same PTB value",
        )
    record = candidates[0]
    recorder = _nested_mapping(record.get("payload"))
    assert recorder is not None
    update = _nested_mapping(recorder.get("payload"))
    assert update is not None
    price_payload = _nested_mapping(update.get("payload"))
    assert price_payload is not None
    if (
        record.get("schema_version") != "edge-lab-recorder.raw.v1"
        or record.get("source") != "rtds_ws"
        or recorder.get("schema_version")
        != "rtds.crypto_prices_chainlink.update.v1"
        or recorder.get("source") != "rtds_ws"
        or recorder.get("kind") != "data"
        or recorder.get("event_type")
        != "crypto_prices_chainlink.update"
        or recorder.get("received_at") != record.get("received_at")
        or recorder.get("event_at") != record.get("event_at")
        or recorder.get("sequence") != record.get("sequence")
        or update.get("topic") != _TOPIC
        or update.get("type") != "update"
        or recorder.get("server_timestamp") != update.get("timestamp")
    ):
        raise PriceToBeatRejection(
            "schema_drift",
            "record does not match the supported RTDS Chainlink schema",
        )

    value = price_payload.get("value")
    full_accuracy_value = price_payload.get("full_accuracy_value")
    if (
        not isinstance(value, str)
        or _DECIMAL_TEXT.fullmatch(value) is None
        or not isinstance(full_accuracy_value, str)
        or _INTEGER_TEXT.fullmatch(full_accuracy_value) is None
    ):
        raise PriceToBeatRejection(
            "price_encoding_invalid",
            "value must be plain decimal text and full_accuracy_value an integer",
        )
    try:
        full_value = int(full_accuracy_value)
        display_price = Decimal(value)
        exact_price = _decimal_from_scaled_integer(full_accuracy_value)
    except (InvalidOperation, ValueError) as exc:
        raise PriceToBeatRejection(
            "price_encoding_invalid",
            "Chainlink price fields must be decimal strings",
        ) from exc
    scaled_value = _exact_scaled_integer(value)
    if display_price <= 0 or full_value <= 0 or scaled_value is None:
        raise PriceToBeatRejection(
            "price_encoding_invalid",
            "Chainlink prices must be positive with at most 18 exact decimals",
        )
    if scaled_value == full_value:
        display_encoding = "exact_decimal"
    else:
        try:
            binary64_price = float(exact_price)
        except (OverflowError, ValueError):
            binary64_price = math.inf
        canonical_binary64 = (
            repr(binary64_price) if math.isfinite(binary64_price) else None
        )
        if value != canonical_binary64:
            raise PriceToBeatRejection(
                "price_value_mismatch",
                "value is neither exact nor the canonical binary64 display "
                "of full_accuracy_value / 1e18",
            )
        display_encoding = "canonical_binary64"
    if exact_price <= 0:
        raise PriceToBeatRejection(
            "price_encoding_invalid",
            "full-accuracy Chainlink price must be positive",
        )

    record_id = record.get("record_id")
    if (
        not isinstance(record_id, str)
        or record_id != canonical_record_id(record)
    ):
        raise PriceToBeatRejection(
            "record_id_mismatch",
            "record_id does not match the canonical raw envelope",
        )
    received_at = record.get("received_at")
    received = _utc_datetime(received_at, label="received_at")
    source_timestamp = datetime.fromtimestamp(
        opens_at_ms / 1_000,
        tz=timezone.utc,
    )
    if received < source_timestamp:
        raise PriceToBeatRejection(
            "source_time_conflict",
            "local receipt cannot precede the inner Chainlink timestamp",
        )
    available = received < first_decision
    assert isinstance(received_at, str)
    return ChainlinkPriceToBeat(
        source_topic=_TOPIC,
        source_symbol=source_symbol,
        source_timestamp_ms=opens_at_ms,
        price=exact_price,
        display_value=value,
        display_encoding=display_encoding,
        full_accuracy_value=full_accuracy_value,
        raw_record_id=record_id,
        received_at=received_at,
        first_decision_at=_format_utc(first_decision),
        retrospective=not available,
        available_at_first_decision=available,
    )


def _validate_target_boundary_contract(
    target: ShortCryptoTarget,
    *,
    rule_url: str,
) -> None:
    if target.source_topic != _TOPIC:
        raise PriceToBeatRejection(
            "target_topic_mismatch",
            "strict target must bind the RTDS Chainlink topic",
        )
    if target.source_symbol not in _SYMBOLS:
        raise PriceToBeatRejection(
            "unsupported_source_symbol",
            "v1 supports only btc/usd and eth/usd Chainlink streams",
        )
    expected_rule_url = _SOURCE_URLS[target.source_symbol]
    if rule_url != expected_rule_url:
        raise PriceToBeatRejection(
            "target_rule_url_mismatch",
            f"strict target must bind exactly {expected_rule_url}",
        )
    if (
        not isinstance(target.rule_hash, str)
        or _SHA256_TEXT.fullmatch(target.rule_hash) is None
    ):
        raise PriceToBeatRejection(
            "target_rule_hash_invalid",
            "strict target rule_hash must be a lowercase SHA-256 digest",
        )
    expected_duration = {"5m": 300_000, "15m": 900_000}.get(target.horizon)
    if (
        expected_duration is None
        or target.closes_at_ms - target.opens_at_ms != expected_duration
    ):
        raise PriceToBeatRejection(
            "target_window_invalid",
            "strict target open/close epochs must match its 5m or 15m horizon",
        )


def _manifest_capture_root(manifest_path: Path) -> Path:
    if (
        not manifest_path.name.endswith(".manifest.json")
        or manifest_path.parent.name != "rtds_ws"
        or manifest_path.parent.parent.name != "raw"
    ):
        raise PriceToBeatRejection(
            "source_manifest_path_invalid",
            "manifest must be a finalized raw/rtds_ws/*.manifest.json path",
        )
    return manifest_path.parent.parent.parent


def _verified_manifest_records(
    manifest_path: Path,
) -> tuple[
    list[Mapping[str, Any]],
    dict[str, tuple[int, str, str, str]],
]:
    capture_root = _manifest_capture_root(manifest_path)
    try:
        entry = _batch_entry(
            manifest_path=manifest_path,
            capture_root=capture_root,
            project_root=capture_root,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise PriceToBeatRejection(
            "source_manifest_invalid",
            f"finalized RTDS manifest verification failed: {exc}",
        ) from exc
    if entry.get("source") != "rtds_ws" or not entry.get("integrity_valid"):
        errors = entry.get("integrity_errors")
        raise PriceToBeatRejection(
            "source_manifest_invalid",
            "finalized RTDS manifest failed integrity verification"
            + (f": {errors}" if errors else ""),
        )

    raw_path = manifest_path.with_name(
        f"{manifest_path.name.removesuffix('.manifest.json')}.jsonl"
    )
    try:
        manifest_bytes = _read_bytes(
            manifest_path,
            allowed_root=capture_root,
        )
        raw_bytes = _read_bytes(raw_path, allowed_root=capture_root)
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PriceToBeatRejection(
            "source_manifest_invalid",
            f"unable to freeze verified RTDS inputs: {exc}",
        ) from exc
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != "edge-lab-recorder.raw.v1"
    ):
        raise PriceToBeatRejection(
            "source_manifest_schema_mismatch",
            "RTDS capture manifest must bind edge-lab-recorder.raw.v1",
        )

    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    checksum = entry.get("checksum")
    if (
        not isinstance(checksum, Mapping)
        or checksum.get("verified") is not True
        or checksum.get("actual") != raw_sha256
        or checksum.get("bytes") != len(raw_bytes)
    ):
        raise PriceToBeatRejection(
            "source_manifest_changed_after_verification",
            "RTDS raw bytes drifted after manifest verification",
        )

    records: list[Mapping[str, Any]] = []
    locations: dict[str, tuple[int, str, str, str]] = {}
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    for line_number, line in enumerate(raw_bytes.splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PriceToBeatRejection(
                "source_raw_invalid",
                f"RTDS raw line {line_number} is invalid JSON",
            ) from exc
        if not isinstance(record, Mapping):
            raise PriceToBeatRejection(
                "source_raw_invalid",
                f"RTDS raw line {line_number} must be an object",
            )
        record_id = record.get("record_id")
        if not isinstance(record_id, str):
            raise PriceToBeatRejection(
                "source_raw_invalid",
                f"RTDS raw line {line_number} has no record_id",
            )
        records.append(record)
        locations[record_id] = (
            line_number,
            str(raw_path),
            raw_sha256,
            manifest_sha256,
        )
    return records, locations


def _raw_may_contain_requested_timestamp(
    manifest_path: Path,
    *,
    timestamp_needles: tuple[bytes, ...],
) -> bool:
    """Cheaply reject raw batches that cannot contain an exact boundary.

    A supported boundary timestamp is a native JSON integer.  Its decimal
    ASCII representation must therefore occur verbatim in the raw bytes.
    False positives are harmless because every selected batch still goes
    through the complete manifest, checksum, schema, and record-ID validator.
    """

    capture_root = _manifest_capture_root(manifest_path)
    raw_path = manifest_path.with_name(
        f"{manifest_path.name.removesuffix('.manifest.json')}.jsonl"
    )
    try:
        raw_bytes = _read_bytes(raw_path, allowed_root=capture_root)
    except (OSError, ValueError) as exc:
        raise PriceToBeatRejection(
            "source_manifest_invalid",
            f"unable to inspect finalized RTDS raw bytes: {exc}",
        ) from exc
    return any(needle in raw_bytes for needle in timestamp_needles)


def _finalized_boundary(
    price: ChainlinkPriceToBeat,
    *,
    role: Literal["open", "close"],
    record: Mapping[str, Any],
    manifest_path: Path,
    location: tuple[int, str, str, str],
    rule_url: str,
    rule_hash: str,
) -> FinalizedChainlinkBoundary:
    recorder = _nested_mapping(record.get("payload"))
    assert recorder is not None
    session_id = recorder.get("session_id")
    connection_id = recorder.get("connection_id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(connection_id, str)
        or not connection_id
    ):
        raise PriceToBeatRejection(
            "schema_drift",
            "RTDS boundary must preserve recorder session and connection IDs",
        )
    line_number, raw_path, raw_sha256, manifest_sha256 = location
    return FinalizedChainlinkBoundary(
        boundary_role=role,
        source_topic=price.source_topic,
        source_symbol=price.source_symbol,
        rule_url=rule_url,
        rule_hash=rule_hash,
        inner_timestamp_ms=price.source_timestamp_ms,
        price=price.price,
        display_value=price.display_value,
        display_encoding=price.display_encoding,
        full_accuracy_value=price.full_accuracy_value,
        record_id=price.raw_record_id,
        session_id=session_id,
        connection_id=connection_id,
        received_at=price.received_at,
        source_manifest_path=str(manifest_path),
        source_manifest_sha256=manifest_sha256,
        raw_path=raw_path,
        raw_sha256=raw_sha256,
        line_number=line_number,
        retrospective=price.retrospective,
        available_at_first_decision=price.available_at_first_decision,
    )


def extract_chainlink_boundary_from_finalized_manifests(
    manifest_paths: Sequence[str | Path],
    *,
    target: ShortCryptoTarget,
    rule_url: str,
    boundary_role: Literal["open", "close"],
    first_decision_at: str,
) -> FinalizedChainlinkBoundary:
    """Extract one exact boundary without waiting for the other boundary."""

    _validate_target_boundary_contract(target, rule_url=rule_url)
    if boundary_role not in {"open", "close"}:
        raise PriceToBeatRejection(
            "boundary_role_invalid",
            "boundary_role must be exactly 'open' or 'close'",
        )
    if not manifest_paths:
        raise PriceToBeatRejection(
            "source_manifest_missing",
            "at least one finalized RTDS manifest is required",
        )

    records: list[Mapping[str, Any]] = []
    provenance: dict[str, tuple[Path, tuple[int, str, str, str]]] = {}
    for value in manifest_paths:
        manifest_path = Path(value)
        batch_records, locations = _verified_manifest_records(manifest_path)
        records.extend(batch_records)
        for record_id, location in locations.items():
            provenance[record_id] = (manifest_path, location)

    boundary_timestamp_ms = (
        target.opens_at_ms
        if boundary_role == "open"
        else target.closes_at_ms
    )
    price = extract_chainlink_price_to_beat(
        records,
        source_symbol=target.source_symbol,
        opens_at_ms=boundary_timestamp_ms,
        first_decision_at=first_decision_at,
    )
    by_record_id = {
        record.get("record_id"): record
        for record in records
        if isinstance(record.get("record_id"), str)
    }
    manifest_path, location = provenance[price.raw_record_id]
    return _finalized_boundary(
        price,
        role=boundary_role,
        record=by_record_id[price.raw_record_id],
        manifest_path=manifest_path,
        location=location,
        rule_url=rule_url,
        rule_hash=target.rule_hash,
    )


def extract_chainlink_boundary_batch_from_finalized_manifests(
    manifest_paths: Sequence[str | Path],
    *,
    requests: Mapping[str, FinalizedChainlinkBoundaryRequest],
) -> dict[str, FinalizedChainlinkBoundary | None]:
    """Extract many boundaries with one manifest verification pass."""

    if not requests:
        return {}
    if any(
        not isinstance(request_id, str)
        or not request_id
        or not isinstance(request, FinalizedChainlinkBoundaryRequest)
        for request_id, request in requests.items()
    ):
        raise PriceToBeatRejection(
            "boundary_request_invalid",
            "batch boundary requests must have non-empty string IDs",
        )
    if not manifest_paths:
        raise PriceToBeatRejection(
            "source_manifest_missing",
            "at least one finalized RTDS manifest is required",
        )

    requested_keys: set[tuple[str, int]] = set()
    for request in requests.values():
        _validate_target_boundary_contract(
            request.target,
            rule_url=request.rule_url,
        )
        if request.boundary_role not in {"open", "close"}:
            raise PriceToBeatRejection(
                "boundary_role_invalid",
                "boundary_role must be exactly 'open' or 'close'",
            )
        boundary_timestamp_ms = (
            request.target.opens_at_ms
            if request.boundary_role == "open"
            else request.target.closes_at_ms
        )
        requested_keys.add(
            (request.target.source_symbol, boundary_timestamp_ms)
        )
    timestamp_needles = tuple(
        str(timestamp_ms).encode("ascii")
        for timestamp_ms in sorted(
            {timestamp_ms for _, timestamp_ms in requested_keys}
        )
    )

    candidates: dict[
        tuple[str, int],
        list[Mapping[str, Any]],
    ] = {key: [] for key in requested_keys}
    provenance: dict[
        str,
        tuple[Path, tuple[int, str, str, str]],
    ] = {}
    for value in manifest_paths:
        manifest_path = Path(value)
        if not _raw_may_contain_requested_timestamp(
            manifest_path,
            timestamp_needles=timestamp_needles,
        ):
            continue
        records, locations = _verified_manifest_records(manifest_path)
        for record in records:
            recorder = _nested_mapping(record.get("payload"))
            update = (
                _nested_mapping(recorder.get("payload"))
                if recorder
                else None
            )
            price_payload = (
                _nested_mapping(update.get("payload"))
                if update
                else None
            )
            if (
                update is None
                or price_payload is None
                or update.get("topic") != _TOPIC
            ):
                continue
            symbol = price_payload.get("symbol")
            timestamp_ms = price_payload.get("timestamp")
            if (
                not isinstance(symbol, str)
                or isinstance(timestamp_ms, bool)
                or not isinstance(timestamp_ms, int)
            ):
                continue
            key = (symbol, timestamp_ms)
            if key not in candidates:
                continue
            candidates[key].append(record)
            record_id = record.get("record_id")
            if isinstance(record_id, str) and record_id in locations:
                provenance[record_id] = (
                    manifest_path,
                    locations[record_id],
                )

    results: dict[str, FinalizedChainlinkBoundary | None] = {}
    for request_id, request in requests.items():
        boundary_timestamp_ms = (
            request.target.opens_at_ms
            if request.boundary_role == "open"
            else request.target.closes_at_ms
        )
        try:
            price = extract_chainlink_price_to_beat(
                candidates[
                    (request.target.source_symbol, boundary_timestamp_ms)
                ],
                source_symbol=request.target.source_symbol,
                opens_at_ms=boundary_timestamp_ms,
                first_decision_at=request.first_decision_at,
            )
        except PriceToBeatRejection as exc:
            if exc.code == "price_to_beat_missing":
                results[request_id] = None
                continue
            raise
        manifest_path, location = provenance[price.raw_record_id]
        record = next(
            item
            for item in candidates[
                (request.target.source_symbol, boundary_timestamp_ms)
            ]
            if item.get("record_id") == price.raw_record_id
        )
        results[request_id] = _finalized_boundary(
            price,
            role=request.boundary_role,
            record=record,
            manifest_path=manifest_path,
            location=location,
            rule_url=request.rule_url,
            rule_hash=request.target.rule_hash,
        )
    return results


def extract_chainlink_boundaries_from_finalized_manifests(
    manifest_paths: Sequence[str | Path],
    *,
    target: ShortCryptoTarget,
    rule_url: str,
    first_decision_at: str,
) -> FinalizedChainlinkBoundaries:
    """Extract exact open/close evidence from verified finalized RTDS batches."""

    return FinalizedChainlinkBoundaries(
        open_boundary=extract_chainlink_boundary_from_finalized_manifests(
            manifest_paths,
            target=target,
            rule_url=rule_url,
            boundary_role="open",
            first_decision_at=first_decision_at,
        ),
        close_boundary=extract_chainlink_boundary_from_finalized_manifests(
            manifest_paths,
            target=target,
            rule_url=rule_url,
            boundary_role="close",
            first_decision_at=first_decision_at,
        ),
    )
