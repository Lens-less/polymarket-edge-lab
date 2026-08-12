"""Read-only conversion of recorder evidence into a receive-time lead/lag study.

This module deliberately stops before strategy backtesting or execution.  It
uses local ``received_at`` plus the recorder's monotonic sequence for causal
ordering, then delegates fixed-grid descriptive scoring to
``edge_lab.latency.lead_lag_study``.  Remote timestamps are retained in the raw
capture but are not treated as cross-source clock alignment evidence.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import decimal
from decimal import (
    Context,
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_HALF_EVEN,
    localcontext,
)
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import platform
import tempfile
from typing import Any, Iterable, Mapping, Optional, Sequence

from .data_manifest import _batch_entry, _read_bytes
from .data_store import DataContractError, canonical_record_id
from .latency import ClobQuoteEvent, SourcePriceEvent, lead_lag_study


SCHEMA_VERSION = "edge-lab-capture-latency-experiment.v1"
ALGORITHM_VERSION = "receive-time-fixed-lag.v2"
DEFAULT_CONDITION_ID = (
    "0x60cb891831b1e0d8f6834f35f150ec058d99075d62b75804ac7ce97b660a4dab"
)
SUPPORTED_QUOTE_EVENTS = frozenset({"book", "price_change", "best_bid_ask"})
RTDS_SOURCE_TOPIC = "crypto_prices"
RTDS_SOURCE_NAME = "polymarket_rtds.crypto_prices"
DECIMAL_PRECISION = 50
DECIMAL_ROUNDING = ROUND_HALF_EVEN
MAX_HEARTBEAT_INTERVAL_SECONDS = Decimal("60")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_OrderKey = tuple[int, int, str, int, str]
_LifecycleSegment = tuple[_OrderKey, _OrderKey]


def _positive_integer(value: int, *, name: str, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class CaptureLatencyConfig:
    """Explicit, credential-free inputs and pre-registered evidence gates."""

    data_root: Path
    condition_id: str = DEFAULT_CONDITION_ID
    source_symbol: str = "btcusdt"
    source_name: str = field(default=RTDS_SOURCE_NAME, init=False)
    lag_grid_ms: tuple[int, ...] = (0, 100, 250, 500, 1_000, 2_000, 5_000)
    max_quote_age_ms: int = 30_000
    min_capture_duration_ms: int = 3_600_000
    min_source_observations: int = 100
    min_lag_samples: int = 100
    min_independent_settled_markets: int = 100
    pinned_raw_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_root", Path(self.data_root))
        for name in ("condition_id", "source_symbol"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(self, "source_symbol", self.source_symbol.lower())
        lags = tuple(self.lag_grid_ms)
        if not lags:
            raise ValueError("lag_grid_ms must be non-empty")
        for lag in lags:
            _positive_integer(lag, name="lag_grid_ms[]", allow_zero=True)
        if len(set(lags)) != len(lags):
            raise ValueError("lag_grid_ms cannot contain duplicates")
        object.__setattr__(self, "lag_grid_ms", lags)
        _positive_integer(self.max_quote_age_ms, name="max_quote_age_ms")
        _positive_integer(
            self.min_capture_duration_ms,
            name="min_capture_duration_ms",
            allow_zero=True,
        )
        _positive_integer(
            self.min_source_observations,
            name="min_source_observations",
        )
        _positive_integer(self.min_lag_samples, name="min_lag_samples")
        _positive_integer(
            self.min_independent_settled_markets,
            name="min_independent_settled_markets",
        )
        pins = tuple(self.pinned_raw_paths)
        for raw_path in pins:
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError(
                    "pinned_raw_paths must be safe relative .jsonl paths"
                )
            path = Path(raw_path)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.suffix != ".jsonl"
            ):
                raise ValueError(
                    "pinned_raw_paths must be safe relative .jsonl paths"
                )
        if len(set(pins)) != len(pins):
            raise ValueError("pinned_raw_paths cannot contain duplicates")
        object.__setattr__(self, "pinned_raw_paths", pins)

    def to_dict(self) -> dict[str, object]:
        return {
            "data_root": str(self.data_root.resolve()),
            "condition_id": self.condition_id,
            "source_symbol": self.source_symbol,
            "source_name": self.source_name,
            "lag_grid_ms": list(self.lag_grid_ms),
            "max_quote_age_ms": self.max_quote_age_ms,
            "min_capture_duration_ms": self.min_capture_duration_ms,
            "min_source_observations": self.min_source_observations,
            "min_lag_samples": self.min_lag_samples,
            "min_independent_settled_markets": (
                self.min_independent_settled_markets
            ),
            "pinned_raw_paths": list(self.pinned_raw_paths),
        }


@dataclass(frozen=True)
class _CapturedRecord:
    source: str
    kind: str
    event_type: str
    received_at: str
    received_ns: int
    monotonic_ns: int
    record_id: str
    session_id: str
    connection_id: Optional[str]
    frame_index: Optional[int]
    replay_eligible: Optional[bool]
    payload: Mapping[str, Any]
    detail: Mapping[str, Any]
    raw_ref: str

    @property
    def received_ms(self) -> int:
        # Availability is rounded up so sub-millisecond data is never made
        # visible before it was actually received.
        return (self.received_ns + 999_999) // 1_000_000

    @property
    def received_floor_ms(self) -> int:
        # Observation horizons are rounded down, the conservative complement
        # to the availability ceiling above.
        return self.received_ns // 1_000_000

    @property
    def order_key(self) -> _OrderKey:
        return (
            self.received_ns,
            self.monotonic_ns,
            self.connection_id or "",
            self.frame_index if self.frame_index is not None else 2**63 - 1,
            self.record_id,
        )


def _parse_utc_ns(value: object) -> int:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("received_at must be a non-empty ISO-8601 string")
    text = value.strip()
    parsed = datetime.fromisoformat(
        f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("received_at must include a timezone")
    delta = parsed.astimezone(timezone.utc) - _EPOCH
    microseconds = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    return microseconds * 1_000


def _finite_decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _discover_capture_files(
    data_root: Path,
    sources: Sequence[str],
    pinned_raw_paths: Sequence[str] = (),
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    if pinned_raw_paths:
        pinned: list[Path] = []
        for raw_path in pinned_raw_paths:
            path = (data_root / raw_path).resolve()
            try:
                relative = path.relative_to(data_root)
            except ValueError as exc:
                raise ValueError(
                    f"pinned raw file is missing or outside data_root: {raw_path}"
                ) from exc
            if (
                len(relative.parts) != 3
                or relative.parts[0] != "raw"
                or relative.parts[1] not in sources
                or path.suffix != ".jsonl"
            ):
                raise ValueError(
                    "pinned raw file must belong to a supported raw source: "
                    f"{raw_path}"
                )
            if not path.is_file():
                raise ValueError(
                    f"pinned raw file is missing or outside data_root: {raw_path}"
                )
            pinned.append(path)
        raw_files = tuple(sorted(pinned))
        # Exact replay is defined solely by its explicit finalized batch pins.
        # Inspecting live partials would add unbound, nondeterministic state to
        # an otherwise immutable report.
        partials: tuple[Path, ...] = ()
    else:
        raw_files = tuple(
            sorted(
                path
                for source in sources
                for path in (data_root / "raw" / source).glob("*.jsonl")
            )
        )
        partials = tuple(
            sorted(
                path
                for source in sources
                for path in (
                    data_root / "raw" / source
                ).glob("*.jsonl.partial")
            )
        )
    return raw_files, partials


def _load_records(
    data_root: Path,
    raw_files: Sequence[Path],
    frozen_raw_bytes: Mapping[Path, bytes],
) -> tuple[list[_CapturedRecord], Counter[str], list[str]]:
    records: list[_CapturedRecord] = []
    rejections: Counter[str] = Counter()
    files: list[str] = []
    seen_record_ids: set[str] = set()
    for path in raw_files:
        source = path.parent.name
        files.append(path.relative_to(data_root).as_posix())
        raw_bytes = frozen_raw_bytes.get(path)
        if raw_bytes is None:
            rejections["unfrozen_raw_file"] += 1
            continue
        try:
            handle = StringIO(raw_bytes.decode("utf-8"))
        except UnicodeError:
            rejections["unreadable_jsonl"] += 1
            continue
        with handle:
            for line_number, line in enumerate(handle, start=1):
                raw_ref = f"{path.resolve()}:{line_number}"
                try:
                    envelope = json.loads(line)
                    recorder = envelope["payload"]
                    kind = recorder["kind"]
                    payload = recorder.get("payload")
                    if payload is None and kind != "data":
                        payload = {}
                    received_at = envelope["received_at"]
                    recorder_received_at = recorder["received_at"]
                    record_id = envelope["record_id"]
                    monotonic_ns = recorder["monotonic_ns"]
                    session_id = recorder["session_id"]
                    connection_id = recorder["connection_id"]
                    event_type = recorder["event_type"]
                    frame_index = recorder.get("frame_index")
                    replay_eligible = recorder.get("replay_eligible")
                    detail = recorder.get("detail")
                    if detail is None:
                        detail = {}
                    if not isinstance(payload, Mapping):
                        raise ValueError("payload must be an object")
                    if not isinstance(detail, Mapping):
                        raise ValueError("detail must be an object")
                    connection_is_valid = (
                        connection_id is None
                        or (
                            isinstance(connection_id, str)
                            and bool(connection_id.strip())
                        )
                    )
                    ws_data_requires_order_identity = (
                        source in {"rtds_ws", "clob_market_ws"}
                        and kind == "data"
                    )
                    if (
                        not isinstance(record_id, str)
                        or not record_id.strip()
                        or not isinstance(session_id, str)
                        or not session_id.strip()
                        or not connection_is_valid
                        or (
                            ws_data_requires_order_identity
                            and (
                                connection_id is None
                                or frame_index is None
                            )
                        )
                        or not isinstance(event_type, str)
                        or not event_type.strip()
                        or not isinstance(kind, str)
                        or not kind.strip()
                        or envelope.get("schema_version")
                        != "edge-lab-recorder.raw.v1"
                        or recorder_received_at != received_at
                        or isinstance(monotonic_ns, bool)
                        or not isinstance(monotonic_ns, int)
                        or monotonic_ns < 0
                        or (
                            frame_index is not None
                            and (
                                isinstance(frame_index, bool)
                                or not isinstance(frame_index, int)
                                or frame_index < 0
                            )
                        )
                        or (
                            replay_eligible is not None
                            and not isinstance(replay_eligible, bool)
                        )
                        or envelope.get("source") != source
                        or recorder.get("source") != source
                    ):
                        raise ValueError("invalid recorder identity/order fields")
                    received_ns = _parse_utc_ns(received_at)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    rejections["malformed_capture_record"] += 1
                    continue
                if record_id in seen_record_ids:
                    rejections["duplicate_record_id"] += 1
                    continue
                try:
                    expected_record_id = canonical_record_id(envelope)
                except (DataContractError, TypeError, ValueError):
                    rejections["record_id_recompute_failed"] += 1
                    continue
                if expected_record_id != record_id:
                    rejections["record_id_mismatch"] += 1
                    continue
                seen_record_ids.add(record_id)
                records.append(
                    _CapturedRecord(
                        source=source,
                        kind=kind,
                        event_type=event_type,
                        received_at=received_at,
                        received_ns=received_ns,
                        monotonic_ns=monotonic_ns,
                        record_id=record_id,
                        session_id=session_id,
                        connection_id=connection_id,
                        frame_index=frame_index,
                        replay_eligible=replay_eligible,
                        payload=payload,
                        detail=detail,
                        raw_ref=raw_ref,
                    )
                )
    return records, rejections, files


def _capture_integrity(
    data_root: Path,
    raw_files: Sequence[Path],
    partials: Sequence[Path],
) -> tuple[dict[str, object], dict[Path, bytes]]:
    missing_manifests: list[str] = []
    invalid_manifests: list[dict[str, object]] = []
    checksum_mismatches: list[dict[str, str]] = []
    pinned_batches: list[dict[str, object]] = []
    frozen_raw_bytes: dict[Path, bytes] = {}
    for raw_path in raw_files:
        relative_raw = raw_path.relative_to(data_root).as_posix()
        manifest_path = raw_path.with_suffix(".manifest.json")
        if not manifest_path.exists():
            missing_manifests.append(relative_raw)
            pinned_batches.append(
                {
                    "raw_path": relative_raw,
                    "manifest_path": (
                        manifest_path.relative_to(data_root).as_posix()
                    ),
                    "checksum": None,
                }
            )
            continue
        relative_manifest = manifest_path.relative_to(data_root).as_posix()
        try:
            entry = _batch_entry(
                manifest_path=manifest_path,
                capture_root=data_root,
                project_root=data_root,
            )
            checksum = entry.get("checksum")
            if not isinstance(checksum, Mapping):
                raise ValueError("strict validator omitted checksum")
            expected = checksum.get("expected")
            declared_bytes = checksum.get("declared_bytes")
            declared_lines = checksum.get("declared_lines")
            if (
                not isinstance(expected, str)
                or not isinstance(declared_bytes, int)
                or isinstance(declared_bytes, bool)
                or not isinstance(declared_lines, int)
                or isinstance(declared_lines, bool)
            ):
                raise ValueError("strict validator returned invalid checksum")
            raw_bytes = _read_bytes(
                raw_path,
                allowed_root=data_root,
            )
            frozen_raw_bytes[raw_path] = raw_bytes
            frozen_sha256 = sha256(raw_bytes).hexdigest()
            frozen_lines = raw_bytes.count(b"\n")
            strict_errors = list(entry.get("integrity_errors") or [])
            manifest = json.loads(
                _read_bytes(
                    manifest_path,
                    allowed_root=data_root,
                ).decode("utf-8")
            )
            if manifest.get("schema_version") != "edge-lab-recorder.raw.v1":
                strict_errors.append("unsupported_capture_schema_version")
            if (
                frozen_sha256 != expected
                or len(raw_bytes) != declared_bytes
                or frozen_lines != declared_lines
            ):
                strict_errors.append("frozen_raw_bytes_changed_after_validation")
            pinned_batches.append(
                {
                    "raw_path": relative_raw,
                    "manifest_path": relative_manifest,
                    "checksum": {
                        "algorithm": "sha256",
                        "value": expected,
                        "bytes": declared_bytes,
                        "lines": declared_lines,
                    },
                }
            )
            if strict_errors:
                invalid_manifests.append(
                    {
                        "manifest_path": relative_manifest,
                        "error_type": "CaptureManifestIntegrityError",
                        "error_codes": sorted(
                            {
                                str(error).split(":", 1)[0]
                                for error in strict_errors
                            }
                        ),
                    }
                )
            if any(
                "checksum" in str(error)
                or "byte_count" in str(error)
                or "line_count" in str(error)
                or "frozen_raw_bytes" in str(error)
                for error in strict_errors
            ):
                checksum_mismatches.append(
                    {
                        "raw_path": relative_raw,
                        "manifest_path": relative_manifest,
                        "expected_sha256": expected,
                        "actual_sha256": frozen_sha256,
                    }
                )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            if not any(
                batch["raw_path"] == relative_raw
                for batch in pinned_batches
            ):
                pinned_batches.append(
                    {
                        "raw_path": relative_raw,
                        "manifest_path": relative_manifest,
                        "checksum": None,
                    }
                )
            invalid_manifests.append(
                {
                    "manifest_path": relative_manifest,
                    "error_type": type(exc).__name__,
                    "error_codes": ["strict_manifest_validation_failed"],
                }
            )
    complete = bool(raw_files) and not (
        missing_manifests or invalid_manifests or checksum_mismatches
    )
    return (
        {
            "strict_validator": "edge_lab.data_manifest._batch_entry",
            "finalized_files_checked": len(raw_files),
            "manifest_complete": complete,
            "missing_manifests": missing_manifests,
            "invalid_manifests": invalid_manifests,
            "checksum_mismatches": checksum_mismatches,
            "pinned_batches": pinned_batches,
            "ignored_partial_files": [
                path.relative_to(data_root).as_posix()
                for path in partials
            ],
        },
        frozen_raw_bytes,
    )


def _append_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _ordered_records(
    records: Iterable[_CapturedRecord],
) -> tuple[list[_CapturedRecord], int, int]:
    ordered = sorted(records, key=lambda record: record.order_key)
    previous_monotonic: dict[str, int] = {}
    inversions = 0
    ties = 0
    for record in ordered:
        previous = previous_monotonic.get(record.session_id)
        if previous is not None and record.monotonic_ns < previous:
            inversions += 1
        elif previous is not None and record.monotonic_ns == previous:
            # One websocket frame can fan out into several immutable records.
            # Such records share both receive and monotonic timestamps; their
            # record IDs provide the deterministic final ordering key.
            ties += 1
        previous_monotonic[record.session_id] = record.monotonic_ns
    return ordered, inversions, ties


def _responses(record: _CapturedRecord) -> tuple[Mapping[str, Any], ...]:
    values = record.payload.get("responses")
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, Mapping))


def _target_metadata(
    records: Iterable[_CapturedRecord],
    condition_id: str,
    rejections: Counter[str],
) -> tuple[
    tuple[tuple[str, str, str], ...],
    int,
    dict[str, object],
]:
    mappings: set[tuple[tuple[str, str, str], ...]] = set()
    snapshots = 0
    matched_records: list[_CapturedRecord] = []
    valid_mapping_record_ids: list[str] = []
    for record in records:
        if record.source != "clob_http":
            continue
        for response in _responses(record):
            raw = response.get("raw_json")
            if not isinstance(raw, Mapping) or raw.get("c") != condition_id:
                continue
            snapshots += 1
            matched_records.append(record)
            tokens = raw.get("t")
            if not isinstance(tokens, list) or len(tokens) != 2:
                rejections["invalid_target_token_metadata"] += 1
                continue
            parsed: list[tuple[str, str, str]] = []
            for token in tokens:
                if not isinstance(token, Mapping):
                    parsed = []
                    break
                token_id = token.get("t")
                label = token.get("o")
                if (
                    not isinstance(token_id, str)
                    or not token_id.strip()
                    or not isinstance(label, str)
                    or not label.strip()
                ):
                    parsed = []
                    break
                normalized = {
                    "up": "YES",
                    "yes": "YES",
                    "down": "NO",
                    "no": "NO",
                }.get(label.strip().casefold())
                if normalized is None:
                    parsed = []
                    break
                parsed.append(
                    (token_id, label, normalized)
                )
            if (
                len(parsed) != 2
                or parsed[0][0] == parsed[1][0]
                or {row[2] for row in parsed} != {"YES", "NO"}
            ):
                rejections["invalid_target_token_metadata"] += 1
                continue
            parsed.sort(key=lambda row: (row[2] != "YES", row[0]))
            mappings.add(tuple(parsed))
            valid_mapping_record_ids.append(record.record_id)
    provenance = {
        "selection_rule": (
            "exact_condition_id_and_received_no_later_than_selected_"
            "ws_overlap_start"
        ),
        "snapshot_record_ids": sorted(
            {record.record_id for record in matched_records}
        ),
        "valid_mapping_record_ids": sorted(set(valid_mapping_record_ids)),
        "snapshot_session_ids": sorted(
            {record.session_id for record in matched_records}
        ),
        "nullable_connection_id_allowed_for_static_snapshot": True,
        "all_snapshots_precede_analysis_window": True,
        "consistent_mapping": len(mappings) == 1,
    }
    if not mappings:
        rejections["missing_target_token_metadata"] += 1
        return (), snapshots, provenance
    if len(mappings) != 1:
        rejections["conflicting_target_token_metadata"] += 1
        return (), snapshots, provenance
    return next(iter(mappings)), snapshots, provenance


def _rule_evidence(
    records: Iterable[_CapturedRecord],
    condition_id: str,
) -> dict[str, object]:
    records = tuple(records)
    target_rules: list[Mapping[str, Any]] = []
    for record in records:
        if record.source != "rules_http":
            continue
        for response in _responses(record):
            raw = response.get("raw_json")
            if (
                isinstance(raw, Mapping)
                and raw.get("conditionId") == condition_id
            ):
                target_rules.append(raw)

    price_fields = (
        "priceToBeat",
        "price_to_beat",
        "referencePrice",
        "reference_price",
    )
    price_values: list[Decimal] = []
    for raw in target_rules:
        for key in price_fields:
            if key not in raw or raw[key] is None:
                continue
            try:
                value = _finite_decimal(raw[key], name=key)
            except ValueError:
                continue
            if value > 0:
                price_values.append(value)
    settled_values: list[str] = []
    for raw in target_rules:
        if raw.get("closed") is not True:
            continue
        for key in ("resolvedOutcome", "winningOutcome", "resolution"):
            value = raw.get(key)
            if not isinstance(value, str):
                continue
            normalized = {
                "up": "YES",
                "yes": "YES",
                "down": "NO",
                "no": "NO",
            }.get(value.strip().casefold())
            if normalized is not None:
                settled_values.append(normalized)
    return {
        "rule_snapshots": len(target_rules),
        "price_to_beat_values": len(price_values),
        "settlement_values": len(settled_values),
        "record_ids": sorted(
            {
                record.record_id
                for record in records
                if record.source == "rules_http"
                and any(
                    isinstance(response.get("raw_json"), Mapping)
                    and response["raw_json"].get("conditionId") == condition_id
                    for response in _responses(record)
                )
            }
        ),
    }


def _parse_source_events(
    records: Sequence[_CapturedRecord],
    ranks: Mapping[str, int],
    config: CaptureLatencyConfig,
    rejections: Counter[str],
) -> list[SourcePriceEvent]:
    events: list[SourcePriceEvent] = []
    for record in records:
        if (
            record.source != "rtds_ws"
            or record.event_type != "crypto_prices.update"
            or not _is_replay_eligible(record)
            or record.payload.get("topic") != RTDS_SOURCE_TOPIC
        ):
            continue
        inner = record.payload.get("payload")
        if not isinstance(inner, Mapping):
            rejections["rtds_missing_payload"] += 1
            continue
        symbol = str(inner.get("symbol", "")).lower()
        if symbol != config.source_symbol:
            continue
        if "full_accuracy_value" not in inner:
            rejections["rtds_missing_full_accuracy_value"] += 1
            continue
        try:
            price = _finite_decimal(
                inner["full_accuracy_value"],
                name="full_accuracy_value",
            )
            events.append(
                SourcePriceEvent(
                    trace_ref=record.record_id,
                    symbol=config.source_symbol.upper(),
                    source=config.source_name,
                    price=price,
                    # Cross-source remote clocks are uncalibrated.  Local
                    # receive time is intentionally the only experiment clock.
                    server_event_time_ms=record.received_ms,
                    received_at_ms=record.received_ms,
                    capture_seq=ranks[record.record_id],
                    raw_ref=record.raw_ref,
                )
            )
        except ValueError:
            rejections["invalid_rtds_price"] += 1
    return events


def _best_level(
    levels: object,
    *,
    side: str,
) -> tuple[Decimal, Optional[Decimal]]:
    if not isinstance(levels, list) or not levels:
        raise ValueError("book side must contain levels")
    parsed: list[tuple[Decimal, Optional[Decimal]]] = []
    for level in levels:
        if not isinstance(level, Mapping) or "price" not in level:
            raise ValueError("invalid book level")
        price = _finite_decimal(level["price"], name=f"{side}.price")
        size = (
            _finite_decimal(level["size"], name=f"{side}.size")
            if level.get("size") is not None
            else None
        )
        if size is not None and size < 0:
            raise ValueError("book size cannot be negative")
        parsed.append((price, size))
    selector = max if side == "bid" else min
    return selector(parsed, key=lambda item: item[0])


def _quote_values(
    event_type: str,
    payload: Mapping[str, Any],
) -> tuple[tuple[str, Decimal, Decimal, Optional[Decimal], Optional[Decimal]], ...]:
    if event_type == "price_change":
        changes = payload.get("price_changes")
        if not isinstance(changes, list):
            raise ValueError("price_change must contain price_changes")
        result = []
        for change in changes:
            if not isinstance(change, Mapping):
                raise ValueError("invalid price_change row")
            asset_id = change.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                raise ValueError("price_change asset_id missing")
            result.append(
                (
                    asset_id,
                    _finite_decimal(change.get("best_bid"), name="best_bid"),
                    _finite_decimal(change.get("best_ask"), name="best_ask"),
                    None,
                    None,
                )
            )
        return tuple(result)

    asset_id = payload.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id:
        raise ValueError(f"{event_type} asset_id missing")
    if event_type == "book":
        best_bid, bid_size = _best_level(payload.get("bids"), side="bid")
        best_ask, ask_size = _best_level(payload.get("asks"), side="ask")
        return ((asset_id, best_bid, best_ask, bid_size, ask_size),)
    if event_type == "best_bid_ask":
        return (
            (
                asset_id,
                _finite_decimal(payload.get("best_bid"), name="best_bid"),
                _finite_decimal(payload.get("best_ask"), name="best_ask"),
                None,
                None,
            ),
        )
    raise ValueError("unsupported quote event")


def _parse_quote_events(
    records: Sequence[_CapturedRecord],
    ranks: Mapping[str, int],
    token_metadata: Sequence[tuple[str, str, str]],
    config: CaptureLatencyConfig,
    rejections: Counter[str],
) -> tuple[list[ClobQuoteEvent], Counter[str], int, int]:
    tokens = {
        token_id: normalized
        for token_id, _label, normalized in token_metadata
    }
    events: list[ClobQuoteEvent] = []
    event_counts: Counter[str] = Counter()
    embedded_last_trade_values = 0
    last_trade_events = 0
    for record in records:
        if record.source != "clob_market_ws":
            continue
        if not _is_replay_eligible(record):
            continue
        if record.payload.get("market") != config.condition_id:
            continue
        if record.event_type == "last_trade_price":
            try:
                asset_id = record.payload.get("asset_id")
                price = _finite_decimal(
                    record.payload.get("price"),
                    name="last_trade_price.price",
                )
                if (
                    asset_id not in tokens
                    or price < 0
                    or price > 1
                ):
                    raise ValueError("invalid target last-trade event")
            except ValueError:
                rejections["invalid_last_trade_price"] += 1
                continue
            last_trade_events += 1
            continue
        if record.event_type not in SUPPORTED_QUOTE_EVENTS:
            continue
        if (
            record.event_type == "book"
            and record.payload.get("last_trade_price") is not None
        ):
            embedded_last_trade_values += 1
        try:
            rows = _quote_values(record.event_type, record.payload)
        except ValueError:
            rejections[f"invalid_{record.event_type}"] += 1
            continue
        for asset_id, bid, ask, bid_size, ask_size in rows:
            if asset_id not in tokens:
                continue
            normalized = tokens[asset_id]
            try:
                event = ClobQuoteEvent(
                    trace_ref=f"{record.record_id}:{asset_id}",
                    market_id=config.condition_id,
                    token_id=asset_id,
                    outcome=normalized,
                    best_bid=bid,
                    best_ask=ask,
                    best_bid_size=bid_size,
                    best_ask_size=ask_size,
                    server_event_time_ms=record.received_ms,
                    received_at_ms=record.received_ms,
                    capture_seq=ranks[record.record_id],
                    raw_ref=record.raw_ref,
                )
            except ValueError:
                rejections[f"invalid_{record.event_type}_quote"] += 1
                continue
            events.append(event)
            event_counts[record.event_type] += 1
    return events, event_counts, embedded_last_trade_values, last_trade_events


def _score_dict(
    score: Any,
    *,
    source_received_ms: Mapping[str, int],
    source_move_windows: Mapping[str, tuple[str, str]],
) -> dict[str, object]:
    quote_pairs = [
        (score.quote_trace_refs[index], score.quote_trace_refs[index + 1])
        for index in range(0, len(score.quote_trace_refs) - 1, 2)
    ]
    unique_quote_pairs = len(set(quote_pairs))
    candidates = sorted(
        (
            (
                source_received_ms[source_ref],
                source_ref,
                source_move_windows[source_ref],
                quote_pair,
            )
            for source_ref, quote_pair in zip(
                score.source_trace_refs,
                quote_pairs,
            )
            if source_ref in source_received_ms
            and source_ref in source_move_windows
        ),
        key=lambda item: (item[0], item[1], item[3]),
    )
    used_source_observations: set[str] = set()
    used_quote_observations: set[str] = set()
    last_horizon_end_ms: Optional[int] = None
    independent_source_refs: list[str] = []
    independent_quote_refs: list[str] = []
    for start_ms, source_ref, source_window, quote_pair in candidates:
        horizon_end_ms = start_ms + score.lag_ms
        if (
            last_horizon_end_ms is not None
            and start_ms <= last_horizon_end_ms
        ):
            continue
        if any(
            trace_ref in used_source_observations
            for trace_ref in source_window
        ):
            continue
        if any(
            trace_ref in used_quote_observations
            for trace_ref in quote_pair
        ):
            continue
        used_source_observations.update(source_window)
        used_quote_observations.update(quote_pair)
        last_horizon_end_ms = horizon_end_ms
        independent_source_refs.append(source_ref)
        independent_quote_refs.extend(quote_pair)
    independent_samples = len(independent_source_refs)
    return {
        "lag_ms": score.lag_ms,
        "samples": score.samples,
        "deduplicated_quote_pair_samples": unique_quote_pairs,
        "independent_samples": independent_samples,
        "duplicate_quote_pair_samples": max(
            0,
            score.samples - unique_quote_pairs,
        ),
        "dependent_samples_excluded": max(
            0,
            score.samples - independent_samples,
        ),
        "correlation": (
            None if score.correlation is None else str(score.correlation)
        ),
        "direction_hit_rate": (
            None
            if score.direction_hit_rate is None
            else str(score.direction_hit_rate)
        ),
        "source_trace_refs": list(score.source_trace_refs),
        "quote_trace_refs": list(score.quote_trace_refs),
        "independent_source_trace_refs": independent_source_refs,
        "independent_quote_trace_refs": independent_quote_refs,
    }


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _method_code_identity() -> dict[str, object]:
    paths = (
        Path(__file__).resolve(),
        Path(__file__).with_name("latency.py").resolve(),
        Path(__file__).with_name("data_store.py").resolve(),
        Path(__file__).with_name("data_manifest.py").resolve(),
    )
    files = [
        {
            "path": f"src/edge_lab/{path.name}",
            "sha256": sha256(path.read_bytes()).hexdigest(),
        }
        for path in paths
    ]
    return {
        "files": files,
        "combined_sha256": sha256(_canonical_bytes(files)).hexdigest(),
    }


def _runtime_identity() -> dict[str, object]:
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "decimal_libmpdec_version": getattr(
            decimal,
            "__libmpdec_version__",
            "unknown",
        ),
        "decimal_precision": DECIMAL_PRECISION,
        "decimal_rounding": DECIMAL_ROUNDING,
    }


def _is_target_source_record(
    record: _CapturedRecord,
    config: CaptureLatencyConfig,
) -> bool:
    if (
        record.source != "rtds_ws"
        or record.event_type != "crypto_prices.update"
        or not _is_replay_eligible(record)
        or record.payload.get("topic") != RTDS_SOURCE_TOPIC
    ):
        return False
    inner = record.payload.get("payload")
    if not (
        isinstance(inner, Mapping)
        and str(inner.get("symbol", "")).lower() == config.source_symbol
        and "full_accuracy_value" in inner
    ):
        return False
    try:
        return (
            _finite_decimal(
                inner["full_accuracy_value"],
                name="full_accuracy_value",
            )
            > 0
        )
    except ValueError:
        return False


def _is_target_quote_record(
    record: _CapturedRecord,
    config: CaptureLatencyConfig,
) -> bool:
    if not (
        record.source == "clob_market_ws"
        and _is_replay_eligible(record)
        and record.payload.get("market") == config.condition_id
        and record.event_type in SUPPORTED_QUOTE_EVENTS
    ):
        return False
    try:
        rows = _quote_values(record.event_type, record.payload)
        return bool(rows) and all(
            Decimal("0") <= bid <= ask <= Decimal("1")
            for _asset_id, bid, ask, _bid_size, _ask_size in rows
        )
    except ValueError:
        return False


def _is_replay_eligible(record: _CapturedRecord) -> bool:
    return (
        record.kind == "data"
        and record.replay_eligible is not False
    )


def _is_valid_clock_probe(record: _CapturedRecord) -> bool:
    if (
        record.event_type not in {"clock_probe", "clock.probe"}
        or not _is_replay_eligible(record)
    ):
        return False
    values = tuple(
        record.payload.get(key)
        for key in (
            "client_sent_at_ms",
            "server_time_ms",
            "client_received_at_ms",
        )
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for value in values
    ):
        return False
    client_sent, _server_time, client_received = values
    return client_received >= client_sent


def _mentions_target_stream(
    record: _CapturedRecord,
    config: CaptureLatencyConfig,
) -> bool:
    if (
        record.source == "rtds_ws"
        and record.event_type == "crypto_prices.update"
        and record.payload.get("topic") == RTDS_SOURCE_TOPIC
    ):
        inner = record.payload.get("payload")
        return (
            isinstance(inner, Mapping)
            and str(inner.get("symbol", "")).lower()
            == config.source_symbol
        )
    return (
        record.source == "clob_market_ws"
        and record.payload.get("market") == config.condition_id
    )


def _select_analysis_session(
    records: Sequence[_CapturedRecord],
    config: CaptureLatencyConfig,
) -> tuple[Optional[str], dict[str, dict[str, object]]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        kind: Optional[str] = None
        if _is_target_source_record(record, config):
            kind = "source_records"
        elif _is_target_quote_record(record, config):
            kind = "quote_records"
        if kind is None:
            continue
        counts[record.session_id][kind] += 1
    eligible: list[tuple[tuple[object, ...], str]] = []
    pair_coverage: dict[str, dict[str, object]] = {}
    for session_id, values in counts.items():
        if not (
            values["source_records"] > 0
            and values["quote_records"] > 0
        ):
            continue
        source_connection, quote_connection, coverage = (
            _select_connection_pair(records, config, session_id)
        )
        pair_coverage[session_id] = coverage
        selected_pair_score = coverage.get("selected_pair_score")
        if (
            source_connection is None
            or quote_connection is None
            or not isinstance(selected_pair_score, list)
        ):
            continue
        eligible.append(
            (
                (
                    *selected_pair_score,
                    session_id,
                ),
                session_id,
            )
        )
    selected = max(eligible)[1] if eligible else None
    summary: dict[str, dict[str, object]] = {
        session_id: {
            "source_records": values["source_records"],
            "quote_records": values["quote_records"],
            "raw_candidate_connection_pairs_with_positive_overlap": int(
                pair_coverage.get(session_id, {}).get(
                    "raw_candidate_pairs_with_positive_overlap",
                    0,
                )
            ),
            "candidate_connection_pairs_with_positive_overlap": (
                int(
                    pair_coverage.get(session_id, {}).get(
                        "candidate_pairs_with_positive_overlap",
                        0,
                    )
                )
            ),
            "lifecycle_validated_candidate_connection_pairs": int(
                pair_coverage.get(session_id, {}).get(
                    "lifecycle_validated_candidate_pairs",
                    0,
                )
            ),
            "lifecycle_candidate_rejections": dict(
                pair_coverage.get(session_id, {}).get(
                    "lifecycle_candidate_rejections",
                    {},
                )
            ),
            "eligible_for_joint_selection": any(
                candidate_session == session_id
                for _score, candidate_session in eligible
            ),
        }
        for session_id, values in sorted(counts.items())
    }
    return selected, summary


def _select_connection_pair(
    records: Sequence[_CapturedRecord],
    config: CaptureLatencyConfig,
    session_id: Optional[str],
) -> tuple[Optional[str], Optional[str], dict[str, object]]:
    source_groups: dict[str, list[_CapturedRecord]] = defaultdict(list)
    quote_groups: dict[str, list[_CapturedRecord]] = defaultdict(list)
    if session_id is not None:
        for record in records:
            if record.session_id != session_id:
                continue
            if _is_target_source_record(record, config):
                source_groups[record.connection_id].append(record)
            elif _is_target_quote_record(record, config):
                quote_groups[record.connection_id].append(record)

    source_lifecycle = {
        connection_id: _connection_lifecycle_segments(
            records,
            source="rtds_ws",
            session_id=session_id,
            connection_id=connection_id,
        )
        for connection_id in source_groups
    }
    quote_lifecycle = {
        connection_id: _connection_lifecycle_segments(
            records,
            source="clob_market_ws",
            session_id=session_id,
            connection_id=connection_id,
        )
        for connection_id in quote_groups
    }
    candidates: list[
        tuple[
            tuple[int, int, int, int, int],
            str,
            str,
            _LifecycleSegment,
        ]
    ] = []
    raw_candidates = 0
    lifecycle_rejections: Counter[str] = Counter()
    for source_connection, source_records in source_groups.items():
        source_first = min(record.received_ns for record in source_records)
        source_last = max(record.received_ns for record in source_records)
        for quote_connection, quote_records in quote_groups.items():
            quote_first = min(record.received_ns for record in quote_records)
            quote_last = max(record.received_ns for record in quote_records)
            overlap_ns = max(
                0,
                min(source_last, quote_last) - max(source_first, quote_first),
            )
            if overlap_ns == 0:
                continue
            raw_candidates += 1
            source_segments, source_trace = source_lifecycle[
                source_connection
            ]
            quote_segments, quote_trace = quote_lifecycle[quote_connection]
            if not source_segments:
                lifecycle_rejections[
                    f"source:{source_trace['reason']}"
                ] += 1
                continue
            if not quote_segments:
                lifecycle_rejections[
                    f"quote:{quote_trace['reason']}"
                ] += 1
                continue
            overlap_segments = _intersect_segments(
                source_segments,
                quote_segments,
            )
            selected_interval = _select_online_analysis_interval(
                overlap_segments,
                source_records,
                quote_records,
            )
            if selected_interval is None:
                lifecycle_rejections[
                    "pair:no_continuous_target_data_overlap"
                ] += 1
                continue
            segment_sources = _records_in_order_interval(
                source_records,
                selected_interval,
            )
            segment_quotes = _records_in_order_interval(
                quote_records,
                selected_interval,
            )
            target_overlap_ns = max(
                0,
                min(
                    max(record.received_ns for record in segment_sources),
                    max(record.received_ns for record in segment_quotes),
                )
                - max(
                    min(record.received_ns for record in segment_sources),
                    min(record.received_ns for record in segment_quotes),
                ),
            )
            score = (
                target_overlap_ns,
                min(len(segment_sources), len(segment_quotes)),
                len(segment_sources) + len(segment_quotes),
                selected_interval[1][0] - selected_interval[0][0],
                min(
                    max(record.received_ns for record in segment_sources),
                    max(record.received_ns for record in segment_quotes),
                ),
            )
            candidates.append(
                (
                    score,
                    source_connection,
                    quote_connection,
                    selected_interval,
                )
            )
    if candidates:
        (
            selected_score,
            selected_source,
            selected_quote,
            selected_interval,
        ) = max(
            candidates,
            key=lambda candidate: (
                candidate[0],
                candidate[1],
                candidate[2],
            ),
        )
    else:
        selected_score = None
        selected_source = None
        selected_quote = None
        selected_interval = None

    def summarize(
        groups: Mapping[str, Sequence[_CapturedRecord]],
    ) -> dict[str, dict[str, int]]:
        return {
            connection_id: {
                "records": len(group),
                "first_received_ns": min(
                    record.received_ns for record in group
                ),
                "last_received_ns": max(
                    record.received_ns for record in group
                ),
            }
            for connection_id, group in sorted(groups.items())
        }

    return (
        selected_source,
        selected_quote,
        {
            "source_connections": summarize(source_groups),
            "quote_connections": summarize(quote_groups),
            "raw_candidate_pairs_with_positive_overlap": raw_candidates,
            "candidate_pairs_with_positive_overlap": len(candidates),
            "lifecycle_validated_candidate_pairs": len(candidates),
            "lifecycle_candidate_rejections": dict(
                sorted(lifecycle_rejections.items())
            ),
            "selected_pair_score": (
                list(selected_score) if selected_score is not None else None
            ),
            "selected_online_interval_ms": (
                None
                if selected_interval is None
                else _segment_to_ms(*selected_interval)
            ),
        },
    )


def _segment_to_ms(
    start_key: _OrderKey,
    end_key: _OrderKey,
) -> dict[str, int]:
    start_ns = start_key[0]
    end_ns = end_key[0]
    start_ms = (start_ns + 999_999) // 1_000_000
    end_ms = end_ns // 1_000_000
    return {
        "start": start_ms,
        "end": end_ms,
        "duration": max(0, end_ms - start_ms),
    }


def _connection_lifecycle_segments(
    records: Sequence[_CapturedRecord],
    *,
    source: str,
    session_id: Optional[str],
    connection_id: Optional[str],
) -> tuple[list[_LifecycleSegment], dict[str, object]]:
    selected = sorted(
        (
            record
            for record in records
            if record.source == source
            and record.session_id == session_id
            and record.connection_id == connection_id
        ),
        key=lambda record: record.order_key,
    )
    liveness = [
        record
        for record in selected
        if record.kind in {"data", "quarantine"}
        or (
            record.kind == "lifecycle"
            and record.event_type
            in {
                "connected",
                "subscribed",
                "heartbeat",
                "heartbeat_ack",
                "disconnected",
            }
        )
    ]
    lifecycle_trace = {
        "liveness_record_count": len(liveness),
        "connected_record_ids": [
            record.record_id
            for record in liveness
            if record.kind == "lifecycle"
            and record.event_type == "connected"
        ],
        "heartbeat_record_ids": [
            record.record_id
            for record in liveness
            if record.kind == "lifecycle"
            and record.event_type in {"heartbeat", "heartbeat_ack"}
        ],
        "disconnected_record_ids": [
            record.record_id
            for record in liveness
            if record.kind == "lifecycle"
            and record.event_type == "disconnected"
        ],
    }
    heartbeat_intervals: list[Decimal] = []
    invalid_heartbeat_intervals = 0
    for record in selected:
        if (
            record.kind != "lifecycle"
            or record.event_type != "heartbeat"
        ):
            continue
        try:
            interval_seconds = _finite_decimal(
                record.detail.get("interval_seconds"),
                name="heartbeat.interval_seconds",
            )
        except ValueError:
            invalid_heartbeat_intervals += 1
            continue
        if not (
            Decimal("0")
            < interval_seconds
            <= MAX_HEARTBEAT_INTERVAL_SECONDS
        ):
            invalid_heartbeat_intervals += 1
            continue
        heartbeat_intervals.append(interval_seconds)
    declared_seconds = sorted(set(heartbeat_intervals))
    declared_intervals = [
        int(
            (interval_seconds * 1_000).to_integral_value(
                rounding=ROUND_CEILING,
            )
        )
        for interval_seconds in declared_seconds
    ]

    def invalid_cadence(reason: str) -> tuple[
        list[_LifecycleSegment],
        dict[str, object],
    ]:
        return [], {
            "source": source,
            "connection_id": connection_id,
            "declared_heartbeat_intervals_seconds": [
                str(value) for value in declared_seconds
            ],
            "declared_heartbeat_intervals_ms": declared_intervals,
            "invalid_heartbeat_interval_records": (
                invalid_heartbeat_intervals
            ),
            "gap_tolerance_ms": None,
            "segments_ms": [],
            "unproven_gap_count": 0,
            "reason": reason,
            **lifecycle_trace,
        }

    if not heartbeat_intervals:
        reason = (
            "invalid_or_implausible_heartbeat_cadence"
            if invalid_heartbeat_intervals
            else "missing_valid_heartbeat_cadence"
        )
        return invalid_cadence(reason)
    if invalid_heartbeat_intervals:
        return invalid_cadence(
            "invalid_or_implausible_heartbeat_cadence"
        )
    if len(declared_seconds) != 1:
        return invalid_cadence("conflicting_heartbeat_cadence")

    gap_tolerance_ms = declared_intervals[0] * 3
    gap_tolerance_ns = gap_tolerance_ms * 1_000_000
    segments: list[_LifecycleSegment] = []
    active = False
    start_key: Optional[_OrderKey] = None
    last_key: Optional[_OrderKey] = None
    unproven_gaps = 0
    for record in liveness:
        record_key = record.order_key
        if record.event_type == "connected" and record.kind == "lifecycle":
            if (
                active
                and start_key is not None
                and last_key is not None
                and last_key > start_key
            ):
                segments.append((start_key, last_key))
            active = True
            start_key = record_key
            last_key = record_key
            continue
        if not active:
            continue
        assert start_key is not None and last_key is not None
        if record.received_ns - last_key[0] > gap_tolerance_ns:
            if last_key > start_key:
                segments.append((start_key, last_key))
            unproven_gaps += 1
            start_key = record_key
        last_key = record_key
        if (
            record.kind == "lifecycle"
            and record.event_type == "disconnected"
        ):
            if last_key > start_key:
                segments.append((start_key, last_key))
            active = False
            start_key = None
            last_key = None
    if (
        active
        and start_key is not None
        and last_key is not None
        and last_key > start_key
    ):
        segments.append((start_key, last_key))
    return segments, {
        "source": source,
        "connection_id": connection_id,
        "declared_heartbeat_intervals_seconds": [
            str(value) for value in declared_seconds
        ],
        "declared_heartbeat_intervals_ms": declared_intervals,
        "invalid_heartbeat_interval_records": invalid_heartbeat_intervals,
        "gap_tolerance_ms": gap_tolerance_ms,
        "segments_ms": [
            _segment_to_ms(start, end) for start, end in segments
        ],
        "unproven_gap_count": unproven_gaps,
        "reason": None if segments else "no_connected_liveness_segment",
        **lifecycle_trace,
    }


def _intersect_segments(
    left: Sequence[_LifecycleSegment],
    right: Sequence[_LifecycleSegment],
) -> list[_LifecycleSegment]:
    intersections: list[_LifecycleSegment] = []
    for left_start, left_end in left:
        for right_start, right_end in right:
            start = max(left_start, right_start)
            end = min(left_end, right_end)
            if end > start:
                intersections.append((start, end))
    return sorted(set(intersections))


def _select_online_analysis_interval(
    overlap_segments: Sequence[_LifecycleSegment],
    source_records: Sequence[_CapturedRecord],
    quote_records: Sequence[_CapturedRecord],
) -> Optional[_LifecycleSegment]:
    candidates: list[
        tuple[tuple[int, int, int, int], _LifecycleSegment]
    ] = []
    for interval in overlap_segments:
        start_key, end_key = interval
        segment_sources = _records_in_order_interval(
            source_records,
            interval,
        )
        segment_quotes = _records_in_order_interval(
            quote_records,
            interval,
        )
        if not segment_sources or not segment_quotes:
            continue
        target_overlap_ns = max(
            0,
            min(
                max(record.received_ns for record in segment_sources),
                max(record.received_ns for record in segment_quotes),
            )
            - max(
                min(record.received_ns for record in segment_sources),
                min(record.received_ns for record in segment_quotes),
            ),
        )
        score = (
            target_overlap_ns,
            min(len(segment_sources), len(segment_quotes)),
            len(segment_sources) + len(segment_quotes),
            end_key[0] - start_key[0],
        )
        candidates.append((score, interval))
    if not candidates:
        return None
    selected_score = max(score for score, _interval in candidates)
    return min(
        interval
        for score, interval in candidates
        if score == selected_score
    )


def _records_in_order_interval(
    records: Sequence[_CapturedRecord],
    interval: _LifecycleSegment,
) -> list[_CapturedRecord]:
    start_key, end_key = interval
    return [
        record
        for record in records
        if start_key <= record.order_key <= end_key
    ]


def _select_static_evidence_records(
    records: Sequence[_CapturedRecord],
    *,
    analysis_start_order_key: Optional[tuple[int, int, str, int, str]],
) -> list[_CapturedRecord]:
    """Select immutable HTTP facts without joining their recorder sessions.

    HTTP snapshots are captured outside websocket connections and legitimately
    have ``connection_id=None``.  They may supply static condition/token facts
    across recorder sessions only when received before the selected websocket
    overlap starts; source/quote time series remain strictly session-bound.
    """

    if analysis_start_order_key is None:
        return []
    return [
        record
        for record in records
        if record.source in {"clob_http", "rules_http"}
        and record.kind in {"snapshot", "data"}
        and record.replay_eligible is not False
        and record.order_key <= analysis_start_order_key
    ]


def _is_target_evidence(
    record: _CapturedRecord,
    config: CaptureLatencyConfig,
    selected_session_id: Optional[str],
    selected_source_connection_id: Optional[str],
    selected_quote_connection_id: Optional[str],
    static_target_record_ids: frozenset[str],
    selected_online_interval: Optional[_LifecycleSegment],
) -> bool:
    inside_analysis_interval = (
        selected_online_interval is not None
        and selected_online_interval[0]
        <= record.order_key
        <= selected_online_interval[1]
    )
    if record.event_type in {"clock_probe", "clock.probe"}:
        return (
            inside_analysis_interval
            and record.session_id == selected_session_id
        )
    if record.source == "rtds_ws":
        return (
            inside_analysis_interval
            and record.session_id == selected_session_id
            and record.connection_id == selected_source_connection_id
            and _is_target_source_record(record, config)
        )
    if record.source == "clob_market_ws":
        return (
            inside_analysis_interval
            and record.session_id == selected_session_id
            and record.connection_id == selected_quote_connection_id
            and record.payload.get("market") == config.condition_id
        )
    if record.source in {"clob_http", "rules_http"}:
        return record.record_id in static_target_record_ids
    return False


def run_capture_latency_experiment(
    config: CaptureLatencyConfig,
) -> dict[str, object]:
    """Run a descriptive study under one fully specified Decimal context."""

    with localcontext(
        Context(
            prec=DECIMAL_PRECISION,
            rounding=DECIMAL_ROUNDING,
        )
    ):
        return _run_capture_latency_experiment(config)


def _run_capture_latency_experiment(
    config: CaptureLatencyConfig,
) -> dict[str, object]:
    """Analyze immutable local capture files without external side effects."""

    code_identity = _method_code_identity()
    runtime_identity = _runtime_identity()
    data_root = config.data_root.resolve()
    if not data_root.is_dir():
        raise ValueError(f"capture data root does not exist: {data_root}")
    capture_sources = (
        "rtds_ws",
        "clob_market_ws",
        "clob_http",
        "rules_http",
    )
    raw_files, partials = _discover_capture_files(
        data_root,
        capture_sources,
        config.pinned_raw_paths,
    )
    integrity, frozen_raw_bytes = _capture_integrity(
        data_root,
        raw_files,
        partials,
    )
    loaded, parser_rejections, files = _load_records(
        data_root,
        raw_files,
        frozen_raw_bytes,
    )
    ordered, _, _ = _ordered_records(loaded)
    selected_session_id, session_coverage = _select_analysis_session(
        ordered,
        config,
    )
    (
        selected_source_connection_id,
        selected_quote_connection_id,
        connection_coverage,
    ) = _select_connection_pair(
        ordered,
        config,
        selected_session_id,
    )
    all_selected_source_records = [
        record
        for record in ordered
        if record.session_id == selected_session_id
        and record.connection_id == selected_source_connection_id
        and _is_target_source_record(record, config)
    ]
    all_selected_quote_records = [
        record
        for record in ordered
        if record.session_id == selected_session_id
        and record.connection_id == selected_quote_connection_id
        and _is_target_quote_record(record, config)
    ]
    source_online_segments, source_lifecycle = (
        _connection_lifecycle_segments(
            ordered,
            source="rtds_ws",
            session_id=selected_session_id,
            connection_id=selected_source_connection_id,
        )
    )
    quote_online_segments, quote_lifecycle = (
        _connection_lifecycle_segments(
            ordered,
            source="clob_market_ws",
            session_id=selected_session_id,
            connection_id=selected_quote_connection_id,
        )
    )
    online_overlap_segments = _intersect_segments(
        source_online_segments,
        quote_online_segments,
    )
    selected_online_interval = _select_online_analysis_interval(
        online_overlap_segments,
        all_selected_source_records,
        all_selected_quote_records,
    )
    temporal_records = [
        record
        for record in ordered
        if selected_online_interval is not None
        and selected_online_interval[0]
        <= record.order_key
        <= selected_online_interval[1]
        and (
            (
                record.source == "rtds_ws"
                and record.session_id == selected_session_id
                and record.connection_id
                == selected_source_connection_id
            )
            or (
                record.source == "clob_market_ws"
                and record.session_id == selected_session_id
                and record.connection_id
                == selected_quote_connection_id
            )
        )
    ]
    selected_source_records = [
        record
        for record in temporal_records
        if _is_target_source_record(record, config)
    ]
    selected_quote_records = [
        record
        for record in temporal_records
        if _is_target_quote_record(record, config)
    ]
    causal_records = [
        *selected_source_records,
        *selected_quote_records,
    ]
    _, monotonic_inversions, monotonic_ties = _ordered_records(causal_records)
    lifecycle_coverage = {
        "source_connection": source_lifecycle,
        "quote_connection": quote_lifecycle,
        "overlap_segments_ms": [
            _segment_to_ms(start, end)
            for start, end in online_overlap_segments
        ],
        "selected_overlap_segment_ms": (
            None
            if selected_online_interval is None
            else _segment_to_ms(*selected_online_interval)
        ),
        "selection_rule": (
            "maximize_target_data_overlap_then_balanced_target_records_"
            "then_total_records_then_continuous_online_duration_then_"
            "earliest_segment"
        ),
    }
    analysis_start_record = (
        max(
            min(selected_source_records, key=lambda record: record.order_key),
            min(selected_quote_records, key=lambda record: record.order_key),
            key=lambda record: record.order_key,
        )
        if selected_source_records and selected_quote_records
        else None
    )
    static_records = _select_static_evidence_records(
        ordered,
        analysis_start_order_key=(
            analysis_start_record.order_key
            if analysis_start_record is not None
            else None
        ),
    )
    analysis_records = sorted(
        [*temporal_records, *static_records],
        key=lambda record: record.order_key,
    )
    ranks = {
        record.record_id: index
        for index, record in enumerate(ordered, start=1)
    }
    (
        token_metadata,
        metadata_snapshots,
        metadata_provenance,
    ) = _target_metadata(
        static_records,
        config.condition_id,
        parser_rejections,
    )
    metadata_provenance["cross_session"] = any(
        session_id != selected_session_id
        for session_id in metadata_provenance["snapshot_session_ids"]
    )
    metadata_provenance["selected_analysis_session_id"] = (
        selected_session_id
    )
    metadata_provenance["analysis_window_start_received_at"] = (
        None
        if analysis_start_record is None
        else analysis_start_record.received_at
    )
    metadata_provenance["analysis_window_start_order_key"] = (
        None
        if analysis_start_record is None
        else list(analysis_start_record.order_key)
    )
    rules = _rule_evidence(static_records, config.condition_id)
    sources = _parse_source_events(
        analysis_records,
        ranks,
        config,
        parser_rejections,
    )
    (
        quotes,
        quote_event_counts,
        embedded_last_trade_values,
        last_trade_events,
    ) = _parse_quote_events(
        analysis_records,
        ranks,
        token_metadata,
        config,
        parser_rejections,
    )

    critical_capture_failures = {
        "unreadable_jsonl",
        "malformed_capture_record",
        "duplicate_record_id",
        "record_id_recompute_failed",
        "record_id_mismatch",
        "unfrozen_raw_file",
    }
    input_integrity_ok = bool(integrity["manifest_complete"]) and not any(
        parser_rejections[reason]
        for reason in critical_capture_failures
    )
    study_rejections: Counter[str] = Counter()
    series: list[dict[str, object]] = []
    received_floor_by_record_id = {
        record.record_id: record.received_floor_ms
        for record in analysis_records
    }
    ordered_sources = sorted(
        sources,
        key=lambda event: (
            event.received_at_ms,
            event.capture_seq is None,
            -1 if event.capture_seq is None else event.capture_seq,
            event.trace_ref,
        ),
    )
    source_received_ms = {
        event.trace_ref: event.received_at_ms for event in ordered_sources
    }
    source_move_windows = {
        current.trace_ref: (previous.trace_ref, current.trace_ref)
        for previous, current in zip(
            ordered_sources,
            ordered_sources[1:],
        )
    }
    for token_id, label, normalized in (
        token_metadata if input_integrity_ok else ()
    ):
        token_quotes = [quote for quote in quotes if quote.token_id == token_id]
        with localcontext(
            Context(
                prec=DECIMAL_PRECISION,
                rounding=DECIMAL_ROUNDING,
            )
        ):
            study = lead_lag_study(
                source_events=sources,
                quote_events=quotes,
                lag_grid_ms=config.lag_grid_ms,
                max_source_age_ms=0,
                max_quote_age_ms=config.max_quote_age_ms,
                symbol=config.source_symbol.upper(),
                source_name=config.source_name,
                market_id=config.condition_id,
                token_id=token_id,
                outcome=normalized,
                observation_end_ms=(
                    max(
                        received_floor_by_record_id[
                            quote.trace_ref.split(":", 1)[0]
                        ]
                        for quote in token_quotes
                    )
                    if token_quotes
                    else None
                ),
            )
            study_rejections.update(study.rejection_counts)
            series.append(
                {
                    "outcome": label,
                    "token_id": token_id,
                    "internal_binary_label": normalized,
                    "lag_scores": [
                        _score_dict(
                            score,
                            source_received_ms=source_received_ms,
                            source_move_windows=source_move_windows,
                        )
                        for score in study.scores
                    ],
                    "best_lag_ms": (
                        None
                        if study.best_lag is None
                        else study.best_lag.lag_ms
                    ),
                }
            )

    source_times = [event.received_at_ms for event in sources]
    quote_times = [event.received_at_ms for event in quotes]
    relevant_times = source_times + quote_times
    first_ms = min(relevant_times) if relevant_times else None
    last_ms = max(relevant_times) if relevant_times else None
    union_capture_duration_ms = (
        0
        if first_ms is None or last_ms is None
        else max(0, last_ms - first_ms)
    )
    overlap_capture_duration_ms = (
        max(
            0,
            min(max(source_times), max(quote_times))
            - max(min(source_times), min(quote_times)),
        )
        if source_times and quote_times
        else 0
    )
    continuous_online_overlap_ms = (
        0
        if selected_online_interval is None
        else _segment_to_ms(*selected_online_interval)["duration"]
    )
    capture_duration_ms = min(
        overlap_capture_duration_ms,
        continuous_online_overlap_ms,
    )
    max_lag_samples = max(
        (
            score["samples"]
            for item in series
            for score in item["lag_scores"]
        ),
        default=0,
    )
    max_deduplicated_quote_pair_samples = max(
        (
            score["deduplicated_quote_pair_samples"]
            for item in series
            for score in item["lag_scores"]
        ),
        default=0,
    )
    max_independent_samples = max(
        (
            score["independent_samples"]
            for item in series
            for score in item["lag_scores"]
        ),
        default=0,
    )
    clock_probe_count = 0
    for record in analysis_records:
        if record.event_type not in {"clock_probe", "clock.probe"}:
            continue
        if _is_valid_clock_probe(record):
            clock_probe_count += 1
        else:
            parser_rejections["invalid_clock_probe"] += 1
    settled_markets = 1 if rules["settlement_values"] else 0

    rejection_reasons: list[str] = []
    if selected_session_id is None:
        _append_once(rejection_reasons, "missing_common_capture_session")
    if (
        selected_source_connection_id is None
        or selected_quote_connection_id is None
    ):
        _append_once(
            rejection_reasons,
            "missing_overlapping_connection_pair",
        )
    if selected_online_interval is None:
        _append_once(
            rejection_reasons,
            "missing_validated_continuous_online_interval",
        )
    if not input_integrity_ok:
        _append_once(
            rejection_reasons,
            "capture_integrity_failed",
        )
    if not token_metadata:
        _append_once(rejection_reasons, "missing_target_token_metadata")
    if not sources:
        _append_once(rejection_reasons, "missing_source_observations")
    if not quotes:
        _append_once(rejection_reasons, "missing_target_quotes")
    if monotonic_inversions:
        _append_once(rejection_reasons, "monotonic_order_inversions")
    if clock_probe_count == 0:
        _append_once(rejection_reasons, "missing_clock_probes")
    if rules["price_to_beat_values"] == 0:
        _append_once(rejection_reasons, "missing_price_to_beat")
    if rules["settlement_values"] == 0:
        _append_once(rejection_reasons, "missing_settlement")
    if last_trade_events == 0:
        _append_once(rejection_reasons, "missing_last_trade_events")
    if capture_duration_ms < config.min_capture_duration_ms:
        _append_once(rejection_reasons, "capture_duration_below_minimum")
    if len(sources) < config.min_source_observations:
        _append_once(rejection_reasons, "source_observations_below_minimum")
    if max_independent_samples < config.min_lag_samples:
        _append_once(
            rejection_reasons,
            "independent_lag_samples_below_minimum",
        )
    if settled_markets < config.min_independent_settled_markets:
        _append_once(
            rejection_reasons,
            "independent_settled_markets_below_minimum",
        )

    fatal = (
        not input_integrity_ok
        or not token_metadata
        or selected_session_id is None
        or selected_source_connection_id is None
        or selected_quote_connection_id is None
        or selected_online_interval is None
        or not sources
        or not quotes
        or max_lag_samples == 0
        or monotonic_inversions > 0
    )
    status = "rejected" if fatal else "insufficient_data"
    static_target_record_ids = frozenset(
        [
            *metadata_provenance["snapshot_record_ids"],
            *rules["record_ids"],
        ]
    )
    relevant_record_ids = sorted(
        record.record_id
        for record in ordered
        if _is_target_evidence(
            record,
            config,
            selected_session_id,
            selected_source_connection_id,
            selected_quote_connection_id,
            static_target_record_ids,
            selected_online_interval,
        )
    )
    relevant_record_id_set = set(relevant_record_ids)
    identity_config = config.to_dict()
    identity_config.pop("data_root")
    identity_config.pop("pinned_raw_paths")
    batch_pins = integrity["pinned_batches"]
    if _method_code_identity() != code_identity:
        raise RuntimeError("method source changed during analysis")
    input_digest = sha256(
        _canonical_bytes(
            {
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
                "config": identity_config,
                "record_ids": relevant_record_ids,
                "pinned_batches": batch_pins,
                "code_identity": code_identity,
                "runtime_identity": runtime_identity,
            }
        )
    ).hexdigest()
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": f"capture-latency-{input_digest[:20]}",
        "input_digest_sha256": input_digest,
        "as_of_received_at": (
            None
            if not relevant_record_ids
            else max(
                (
                    record
                    for record in ordered
                    if record.record_id in relevant_record_id_set
                ),
                key=lambda record: record.order_key,
            ).received_at
        ),
        "status": status,
        "configuration": config.to_dict(),
        "frozen_input": {
            "discovery_mode": (
                "explicit_pinned_batches"
                if config.pinned_raw_paths
                else "all_finalized_batches_at_process_start"
            ),
            "pinned_batches": batch_pins,
            "newer_or_active_batches_ignored": integrity[
                "ignored_partial_files"
            ],
        },
        "safety": {
            "network_requests": False,
            "orders_submitted": False,
            "credentials_accessed": False,
            "fills_simulated": False,
            "backtest_performed": False,
        },
        "method": {
            "study": "fixed_lag_source_return_vs_clob_midpoint_return",
            "algorithm_version": ALGORITHM_VERSION,
            "code_identity": code_identity,
            "runtime_identity": runtime_identity,
            "time_basis": "local_received_at_only",
            "remote_timestamp_used_for_ordering": False,
            "millisecond_conversion": (
                "ceil_event_availability_floor_observation_horizon"
            ),
            "continuous_coverage": (
                "selected_single_lifecycle_heartbeat_validated_overlap_"
                "segment_no_cross_gap_stitching"
            ),
            "descriptive_only": True,
            "reported_correlations": (
                "raw_dependent_samples_only_not_independence_adjusted"
            ),
            "statistical_inference_performed": False,
            "profitability_evidence": False,
        },
        "target": {
            "condition_id": config.condition_id,
            "source_symbol": config.source_symbol,
            "source_binding": {
                "recorder_source": "rtds_ws",
                "topic": RTDS_SOURCE_TOPIC,
                "event_type": "crypto_prices.update",
                "symbol": config.source_symbol,
                "derived_source_name": config.source_name,
            },
            "token_metadata_snapshots": metadata_snapshots,
            "token_metadata_provenance": metadata_provenance,
            "tokens": [
                {
                    "token_id": token_id,
                    "outcome": label,
                    "internal_binary_label": normalized,
                }
                for token_id, label, normalized in token_metadata
            ],
        },
        "coverage": {
            "raw_files": len(files),
            "raw_file_paths": files,
            "loaded_records": len(loaded),
            "non_data_records": sum(
                1 for record in loaded if record.kind != "data"
            ),
            "quarantined_records_excluded": sum(
                1
                for record in loaded
                if _mentions_target_stream(record, config)
                and not _is_replay_eligible(record)
            ),
            "relevant_record_ids": relevant_record_ids,
            "source_observations": len(sources),
            "quote_observations": len(quotes),
            "quote_event_types": dict(sorted(quote_event_counts.items())),
            "first_received_at_ms": first_ms,
            "last_received_at_ms": last_ms,
            "capture_duration_ms": capture_duration_ms,
            "capture_duration_definition": (
                "minimum_of_observed_target_data_overlap_span_and_selected_"
                "continuous_lifecycle_overlap"
            ),
            "observed_target_data_union_span_ms": union_capture_duration_ms,
            "observed_target_data_overlap_span_ms": (
                overlap_capture_duration_ms
            ),
            "selected_continuous_online_overlap_ms": (
                continuous_online_overlap_ms
            ),
            "lifecycle_coverage": lifecycle_coverage,
            "source_received_at_ms": {
                "first": min(source_times) if source_times else None,
                "last": max(source_times) if source_times else None,
            },
            "quote_received_at_ms": {
                "first": min(quote_times) if quote_times else None,
                "last": max(quote_times) if quote_times else None,
            },
            "independent_markets": 1 if quotes else 0,
            "settled_markets": settled_markets,
            "selected_analysis_session_id": selected_session_id,
            "selected_source_connection_id": (
                selected_source_connection_id
            ),
            "selected_quote_connection_id": selected_quote_connection_id,
            "target_session_coverage": session_coverage,
            "connection_coverage": connection_coverage,
        },
        "integrity": integrity,
        "ordering": {
            "basis": [
                "received_at",
                "monotonic_ns",
                "connection_id",
                "frame_index",
                "record_id",
            ],
            "capture_seq_assigned_after_global_sort": True,
            "sessions": len(session_coverage),
            "selected_session_id": selected_session_id,
            "monotonic_inversions": monotonic_inversions,
            "monotonic_ties": monotonic_ties,
        },
        "evidence_availability": {
            "clock_probes": {
                "available": clock_probe_count > 0,
                "count": clock_probe_count,
                "used": False,
                "reason": (
                    None
                    if clock_probe_count
                    else "no clock probes exist in the capture"
                ),
            },
            "price_to_beat": {
                "available": rules["price_to_beat_values"] > 0,
                "observed_values": rules["price_to_beat_values"],
                "rule_snapshots": rules["rule_snapshots"],
                "used": False,
                "reason": (
                    None
                    if rules["price_to_beat_values"]
                    else "rule text is not a numeric decision-vintage price"
                ),
            },
            "settlement": {
                "available": rules["settlement_values"] > 0,
                "observed_values": rules["settlement_values"],
                "used": False,
                "reason": (
                    None
                    if rules["settlement_values"]
                    else "no explicit closed-market winning outcome was captured"
                ),
            },
            "last_trade_events": {
                "available": last_trade_events > 0,
                "count": last_trade_events,
                "embedded_book_values_ignored": embedded_last_trade_values,
                "used": False,
                "reason": (
                    None
                    if last_trade_events
                    else "book fields are not standalone trade events"
                ),
            },
        },
        "series": series,
        "sample_gate": {
            "max_raw_samples_at_any_lag": max_lag_samples,
            "max_deduplicated_quote_pair_samples_at_any_lag": (
                max_deduplicated_quote_pair_samples
            ),
            "max_independent_samples_at_any_lag": (
                max_independent_samples
            ),
            "independent_sample_definition": (
                "greedy_receive_ordered_samples_with_disjoint_source_move_"
                "observations_disjoint_baseline_future_quote_observations_"
                "and_strictly_nonoverlapping_closed_lag_horizons_within_one_"
                "selected_session_connection_pair_market_and_outcome"
            ),
            "binary_outcomes_are_dependent": True,
            "binary_outcomes_combined_for_gate": False,
            "statistical_inference_performed": False,
            "block_bootstrap_or_hac": (
                "not_applicable_no_statistical_inference"
            ),
            "minimum_required": config.min_lag_samples,
        },
        "rejection_counts": {
            "parser": dict(sorted(parser_rejections.items())),
            "lead_lag": dict(sorted(study_rejections.items())),
        },
        "rejection_reasons": rejection_reasons,
        "conclusion": {
            "classification": status,
            "profitable_strategy_validated": False,
            "trades_or_fills_claimed": 0,
            "statement": (
                "Receive-time co-movement is descriptive only; this capture "
                "cannot establish executable or settled profitability."
            ),
        },
    }
    return result


def save_capture_latency_experiment(
    result: Mapping[str, object],
    target: Path,
) -> Path:
    """Atomically create one deterministic standalone experiment JSON."""

    destination = Path(target).resolve()
    configuration = result.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ValueError("result configuration is required for safe output")
    configured_root = configuration.get("data_root")
    if not isinstance(configured_root, str) or not configured_root:
        raise ValueError("result data_root is required for safe output")
    data_root = Path(configured_root).resolve()
    if destination == data_root or data_root in destination.parents:
        raise ValueError("output must remain outside the capture data root")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".partial",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                result,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # A hard-link create is atomic and fails if another writer created the
        # destination after the existence check.  Unlike os.replace, it can
        # never overwrite raw evidence or an earlier report.
        os.link(temporary, destination)
        temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


__all__ = [
    "CaptureLatencyConfig",
    "DEFAULT_CONDITION_ID",
    "SCHEMA_VERSION",
    "run_capture_latency_experiment",
    "save_capture_latency_experiment",
]
