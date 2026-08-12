"""Strict, offline promotion of a frozen public execution replay.

``verify_and_promote_execution_replay`` is the only public entrance.  It does
not fetch data and does not accept derived PnL, counts, reconciliation flags,
or classifications.  It re-opens every bound finalized raw batch, verifies its
manifest and canonical record IDs, rebuilds a strict short-crypto target,
decision-time book and fee state, runs one shared pessimistic ``MakerReplay``,
reconciles settlement, and rebuilds the ``Ledger``.

The first supported vertical slice is intentionally narrow: binary BTC/ETH
5m/15m, immutable maker BUY decisions, a full-book decision prefix, independent
``last_trade_price`` prints, and one terminal settlement.  Narrow support is a
fail-closed property; unsupported evidence is rejected rather than guessed.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from .chainlink_ptb import (
    PriceToBeatRejection,
    extract_chainlink_boundaries_from_finalized_manifests,
)
from .compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from .data_api_trade_identity import data_api_trade_event_key
from .data_store import canonical_json_bytes, canonical_record_id
from .evidence import TradeEvidence
from .execution import (
    BookEvent,
    ExecutionFeeSchedule,
    Ledger,
    MakerOrderRequest,
    MakerReplay,
    OperationAssumption,
    OrderSide,
    QueueScenario,
    TraceRef,
    TradeEvent,
)
from .execution_replay_freeze import (
    CaptureFreezeVerification,
    verify_production_capture_freeze,
)
from .network_safety import high_confidence_secret_findings
from .short_crypto_catalog import (
    CatalogRejection,
    ShortCryptoTarget,
    parse_short_crypto_announcement,
)
from .short_crypto_settlement import (
    ChainlinkBoundaryObservation,
    SettlementRejection,
    reconcile_short_crypto_settlement,
)


REQUEST_SCHEMA = "edge-lab.execution-replay-promotion-request.v1"
RUN_SCHEMA = "edge-lab.execution-replay-run.v1"
REPLAY_SCHEMA = "edge-lab.execution-replay.v1"
QUALITY_SCHEMA = "edge-lab.execution-replay-data-quality.v1"
REPRODUCIBILITY_SCHEMA = "edge-lab.execution-replay-reproducibility.v1"
FIXED_SCENARIO = QueueScenario.PESSIMISTIC
FIXED_HAIRCUT = Decimal("0.5")
PESSIMISTIC_SUBMIT_LATENCY_FLOOR_MS = 250
LIVENESS_TOLERANCE_MS = 30_000
ZERO = Decimal("0")
ONE = Decimal("1")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRANSACTION_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
SENSITIVE_EXACT_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "authorization",
        "cookie",
        "credential",
        "mnemonic",
        "passphrase",
        "password",
        "private_key",
        "proxy_authorization",
        "secret",
        "secret_key",
        "seed_phrase",
        "signature",
    }
)
IGNORED_RESULT_CLAIMS = frozenset(
    {
        "classification",
        "fill_count",
        "pnl",
        "reconciled",
    }
)
ARTIFACT_NAMES = (
    "RUN_MANIFEST.json",
    "EXECUTION_REPLAY.json",
    "TRADES.csv",
    "DATA_QUALITY.json",
    "REPRODUCIBILITY.json",
)
PRODUCTION_MISSING_FROZEN_ROLES = (
    "finalized_full_window_freeze_manifest_id",
    "full_window_capture_close_checkpoint_id",
    "decision_to_trade_l2_closure_id",
    "atomic_settlement_commit_record_id",
    "durable_announcement_record_id",
    "durable_gamma_record_id",
    "durable_chainlink_open_record_id",
    "durable_chainlink_close_record_id",
    "durable_close_pong_before_record_id",
    "durable_close_pong_after_record_id",
    "pessimistic_submit_latency_evidence_id",
    "settlement_operation_cost_evidence_id",
)


class PromotionRejection(ValueError):
    """A frozen input failed a strict promotion invariant."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class PromotionResult:
    """Observable result of the all-or-nothing promotion seam."""

    verified: bool
    promoted: bool
    status: Literal[
        "rejected",
        "fixture_validated",
        "promoted_counterfactual",
    ]
    production_evidence: bool
    classification: str
    run_id: str | None
    output_dir: Path | None
    artifacts: Mapping[str, Path]
    reason_codes: tuple[str, ...] = ()
    required_frozen_ids: tuple[str, ...] = ()
    missing_frozen_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ManifestBinding:
    declared_path: str
    path: Path
    sha256: str
    raw_path: Path
    raw_sha256: str
    source: str
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class _FrozenEvidence:
    bindings: tuple[_ManifestBinding, ...]
    records: tuple[Mapping[str, Any], ...]
    records_by_id: Mapping[str, Mapping[str, Any]]
    locations: Mapping[str, tuple[str, int]]


@dataclass(frozen=True)
class _BookState:
    record_id: str
    received_at_ms: int
    connection_id: str
    monotonic_ns: int
    market_id: str
    token_id: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    state_hash: str


@dataclass(frozen=True)
class _Decision:
    decision_id: str
    experiment_id: str
    strategy_id: str
    config_hash: str
    initial_cash: Decimal
    received_at: str
    received_at_ms: int
    market_id: str
    token_id: str
    price: Decimal
    quantity: Decimal
    visible_queue: Decimal
    tick_size: Decimal
    minimum_order_size: Decimal
    submit_latency_ms: int
    cancel_latency_ms: int
    max_book_age_ms: int
    book: _BookState
    resnapshot_record_id: str
    fee_schedule: ExecutionFeeSchedule
    fee_record_id: str
    settlement_operation_cost: Decimal
    settlement_operation_latency_ms: int
    operation_cost_source: str


@dataclass(frozen=True)
class _PublicTrade:
    record_id: str
    evidence_record_id: str
    event_key: str
    transaction_hash: str
    market_id: str
    observed_token_id: str
    observed_price: Decimal
    observed_taker_side: OrderSide
    token_id: str
    price: Decimal
    quantity: Decimal
    aggressor_side: OrderSide
    match_path: str
    source_timestamp_ms: int
    received_at_ms: int
    replay_timestamp_ms: int


@dataclass(frozen=True)
class _BookUpdate:
    record_id: str
    token_id: str
    received_at_ms: int
    source_timestamp_ms: int
    replay_timestamp_ms: int
    best_bid: Decimal | None
    best_ask: Decimal | None


@dataclass(frozen=True)
class _LivenessProof:
    connection_id: str
    resync_record_id: str
    before_record_id: str
    before_received_at_ms: int
    after_record_id: str
    after_received_at_ms: int


@dataclass(frozen=True)
class _SettlementCommitProof:
    record_id: str
    received_at_ms: int


def _guard_is_active() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise PromotionRejection(
        "new_order_guard_inactive",
        "the repository's hard new-order guard is not active",
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _stable_read(path: Path, *, label: str) -> bytes:
    """Read one regular file through a non-following descriptor.

    The before/after fstat comparison detects in-place mutation while the
    descriptor is being consumed.  A final full-freeze recheck closes the
    longer verify/replay/write interval.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PromotionRejection(
            "stable_read_failed",
            f"unable to open {label} without following symlinks",
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PromotionRejection(
                "stable_read_failed",
                f"{label} is not a regular file",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PromotionRejection(
                "input_changed_during_read",
                f"{label} changed while it was being read",
            )
        value = b"".join(chunks)
        if len(value) != after.st_size:
            raise PromotionRejection(
                "input_changed_during_read",
                f"{label} byte count changed while it was being read",
            )
        return value
    finally:
        os.close(descriptor)


def _decimal(value: Any, *, label: str, positive: bool = False) -> Decimal:
    if isinstance(value, (float, bool)) or not isinstance(
        value, (str, int, Decimal)
    ):
        raise PromotionRejection(
            "decimal_invalid",
            f"{label} must be a decimal string or integer",
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PromotionRejection(
            "decimal_invalid",
            f"{label} must be finite",
        ) from exc
    if not result.is_finite() or (positive and result <= ZERO):
        qualifier = "positive and finite" if positive else "finite"
        raise PromotionRejection(
            "decimal_invalid",
            f"{label} must be {qualifier}",
        )
    return result


def _decimal_text(value: Decimal) -> str:
    if value == ZERO:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _checked_int(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PromotionRejection(
            "integer_invalid",
            f"{label} must be an integer >= {minimum}",
        )
    return value


def _utc(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PromotionRejection(
            "timestamp_invalid",
            f"{label} must be an ISO-8601 timestamp",
        )
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(
            f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError as exc:
        raise PromotionRejection(
            "timestamp_invalid",
            f"{label} must be an ISO-8601 timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PromotionRejection(
            "timestamp_invalid",
            f"{label} must include a timezone",
        )
    return parsed.astimezone(timezone.utc)


def _epoch_ms(value: Any, *, label: str) -> int:
    return int(_utc(value, label=label).timestamp() * 1_000)


def _iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _mapping(value: Any, *, code: str, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PromotionRejection(code, f"{label} must be an object")
    return value


def _sequence(value: Any, *, code: str, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise PromotionRejection(code, f"{label} must be an array")
    return value


def _nonempty(value: Any, *, code: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromotionRejection(code, f"{label} must be a non-empty string")
    return value.strip()


def _relative_parts(value: Any, *, label: str) -> tuple[str, ...]:
    text = _nonempty(value, code="path_invalid", label=label)
    if "\\" in text:
        raise PromotionRejection("path_invalid", f"{label} must use POSIX separators")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PromotionRejection(
            "path_escape",
            f"{label} must be a confined relative path",
        )
    if any(part.endswith(".partial") for part in path.parts):
        raise PromotionRejection(
            "partial_evidence_forbidden",
            f"{label} cannot reference mutable partial evidence",
        )
    return path.parts


def _confined_path(
    base: Path,
    value: Any,
    *,
    label: str,
    must_exist: bool,
) -> Path:
    parts = _relative_parts(value, label=label)
    current = base
    for part in parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise PromotionRejection(
                "symlink_forbidden",
                f"{label} traverses a symlink",
            )
    if must_exist and not current.is_file():
        raise PromotionRejection("path_missing", f"{label} is not a regular file")
    resolved_base = base.resolve(strict=True)
    resolved = current.resolve(strict=must_exist)
    try:
        resolved.relative_to(resolved_base)
    except ValueError as exc:
        raise PromotionRejection("path_escape", f"{label} escaped its root") from exc
    return resolved


def _scan_for_forbidden_material(
    value: Any,
    *,
    location: str,
    scan_secret_values: bool = True,
) -> None:
    if scan_secret_values:
        findings = high_confidence_secret_findings(value, path=location)
        if findings:
            first = findings[0]
            raise PromotionRejection(
                "credential_material_forbidden",
                f"{first['kind']} at {first['path']}",
            )
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in SENSITIVE_EXACT_KEYS:
                raise PromotionRejection(
                    "credential_material_forbidden",
                    f"{location} contains forbidden key {key!r}",
                )
            if normalized in {"orders_submitted", "authenticated_endpoints_used"}:
                if isinstance(child, bool) or not isinstance(child, int) or child != 0:
                    raise PromotionRejection(
                        "unsafe_activity_count",
                        f"{location}.{key} must be the exact integer 0",
                    )
            if normalized in {"actual_fill", "authenticated_fill"} and child is not False:
                raise PromotionRejection(
                    "unsafe_fill_claim",
                    f"{location}.{key} must be the exact boolean false",
                )
            _scan_for_forbidden_material(
                child,
                location=f"{location}.{key}",
                scan_secret_values=False,
            )
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, child in enumerate(value):
            _scan_for_forbidden_material(
                child,
                location=f"{location}[{index}]",
                scan_secret_values=False,
            )


def _read_request(path: Path) -> tuple[Mapping[str, Any], bytes, Path]:
    if path.name.endswith(".partial") or path.is_symlink() or not path.is_file():
        raise PromotionRejection(
            "request_path_invalid",
            "run_manifest_path must be a finalized regular JSON file",
        )
    try:
        raw = _stable_read(path, label="promotion request")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionRejection(
            "request_invalid",
            f"unable to read promotion request: {exc}",
        ) from exc
    request = _mapping(
        value,
        code="request_schema_mismatch",
        label="promotion request",
    )
    if request.get("schema_version") != REQUEST_SCHEMA:
        raise PromotionRejection(
            "request_schema_mismatch",
            f"schema_version must equal {REQUEST_SCHEMA}",
        )
    _scan_for_forbidden_material(request, location="request")
    return request, raw, path.resolve(strict=True).parent


def _raw_sibling(manifest_path: Path) -> Path:
    suffix = ".manifest.json"
    if not manifest_path.name.endswith(suffix):
        raise PromotionRejection(
            "source_manifest_path_invalid",
            "every source path must end in .manifest.json",
        )
    if manifest_path.parent.parent.name != "raw":
        raise PromotionRejection(
            "source_manifest_path_invalid",
            "every source manifest must be under raw/<source>/",
        )
    raw_path = manifest_path.with_name(
        f"{manifest_path.name.removesuffix(suffix)}.jsonl"
    )
    if raw_path.is_symlink() or not raw_path.is_file():
        raise PromotionRejection(
            "source_raw_missing",
            f"finalized raw sibling is missing for {manifest_path}",
        )
    return raw_path


def _load_frozen_evidence(
    request: Mapping[str, Any],
    *,
    base: Path,
) -> _FrozenEvidence:
    entries = _sequence(
        request.get("source_manifests"),
        code="source_manifest_missing",
        label="source_manifests",
    )
    if not entries:
        raise PromotionRejection(
            "source_manifest_missing",
            "at least one source manifest is required",
        )
    seen_paths: set[Path] = set()
    seen_record_ids: set[str] = set()
    bindings: list[_ManifestBinding] = []
    records: list[Mapping[str, Any]] = []
    records_by_id: dict[str, Mapping[str, Any]] = {}
    locations: dict[str, tuple[str, int]] = {}
    for index, raw_entry in enumerate(entries):
        entry = _mapping(
            raw_entry,
            code="source_manifest_binding_invalid",
            label=f"source_manifests[{index}]",
        )
        declared_path = _nonempty(
            entry.get("path"),
            code="source_manifest_binding_invalid",
            label=f"source_manifests[{index}].path",
        )
        manifest_path = _confined_path(
            base,
            declared_path,
            label=f"source_manifests[{index}].path",
            must_exist=True,
        )
        if manifest_path in seen_paths:
            raise PromotionRejection(
                "duplicate_source_manifest",
                f"source manifest is bound twice: {declared_path}",
            )
        seen_paths.add(manifest_path)
        expected_manifest_sha = _nonempty(
            entry.get("sha256"),
            code="source_manifest_binding_invalid",
            label=f"source_manifests[{index}].sha256",
        )
        if SHA256_RE.fullmatch(expected_manifest_sha) is None:
            raise PromotionRejection(
                "source_manifest_binding_invalid",
                "source manifest sha256 must be lowercase hexadecimal",
            )
        manifest_bytes = _stable_read(
            manifest_path,
            label=f"source manifest {declared_path}",
        )
        actual_manifest_sha = _sha256_bytes(manifest_bytes)
        if actual_manifest_sha != expected_manifest_sha:
            raise PromotionRejection(
                "source_manifest_hash_mismatch",
                f"manifest changed after freeze: {declared_path}",
            )
        try:
            manifest_value = json.loads(manifest_bytes)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PromotionRejection(
                "source_manifest_invalid",
                f"invalid source manifest JSON: {declared_path}",
            ) from exc
        manifest = _mapping(
            manifest_value,
            code="source_manifest_invalid",
            label=declared_path,
        )
        if (
            manifest.get("manifest_version") != "capture-manifest.v1"
            or manifest.get("schema_version") != "edge-lab-recorder.raw.v1"
        ):
            raise PromotionRejection(
                "source_manifest_schema_mismatch",
                f"{declared_path} is not a supported finalized capture manifest",
            )
        source = _nonempty(
            manifest.get("source"),
            code="source_manifest_invalid",
            label=f"{declared_path}.source",
        )
        if manifest_path.parent.name != source:
            raise PromotionRejection(
                "source_manifest_source_mismatch",
                f"{declared_path} parent does not match manifest source",
            )
        raw_path = _raw_sibling(manifest_path)
        raw_bytes = _stable_read(
            raw_path,
            label=f"source raw for {declared_path}",
        )
        checksum = _mapping(
            manifest.get("checksum"),
            code="source_manifest_invalid",
            label=f"{declared_path}.checksum",
        )
        if checksum.get("algorithm") != "sha256":
            raise PromotionRejection(
                "source_manifest_invalid",
                f"{declared_path} must use sha256",
            )
        actual_raw_sha = _sha256_bytes(raw_bytes)
        lines = raw_bytes.splitlines()
        record_count = _checked_int(
            manifest.get("record_count"),
            label=f"{declared_path}.record_count",
        )
        if (
            checksum.get("value") != actual_raw_sha
            or checksum.get("bytes") != len(raw_bytes)
            or checksum.get("lines") != len(lines)
            or record_count != len(lines)
            or not raw_bytes.endswith(b"\n")
        ):
            raise PromotionRejection(
                "source_raw_integrity_mismatch",
                f"bytes, lines, or sha256 drifted for {declared_path}",
            )
        declared_raw = manifest.get("raw_path")
        if (
            not isinstance(declared_raw, str)
            or PurePosixPath(declared_raw).name != raw_path.name
            or PurePosixPath(declared_raw).parent.name != source
            or PurePosixPath(declared_raw).parent.parent.name != "raw"
        ):
            raise PromotionRejection(
                "source_manifest_raw_path_mismatch",
                f"{declared_path} does not bind its sibling raw path",
            )
        batch_record_ids: list[str] = []
        for line_number, line in enumerate(lines, start=1):
            try:
                value = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise PromotionRejection(
                    "source_raw_invalid",
                    f"{declared_path} line {line_number} is invalid JSON",
                ) from exc
            record = _mapping(
                value,
                code="source_raw_invalid",
                label=f"{declared_path}:{line_number}",
            )
            if (
                record.get("schema_version") != "edge-lab-recorder.raw.v1"
                or record.get("source") != source
            ):
                raise PromotionRejection(
                    "source_record_schema_mismatch",
                    f"{declared_path}:{line_number} has schema/source drift",
                )
            record_id = record.get("record_id")
            if (
                not isinstance(record_id, str)
                or SHA256_RE.fullmatch(record_id) is None
                or record_id != canonical_record_id(record)
            ):
                raise PromotionRejection(
                    "record_id_mismatch",
                    f"{declared_path}:{line_number} record_id is not canonical",
                )
            if record_id in seen_record_ids:
                raise PromotionRejection(
                    "duplicate_record_id",
                    f"record {record_id} appears more than once in the freeze",
                )
            seen_record_ids.add(record_id)
            _scan_for_forbidden_material(
                record,
                location=f"raw[{record_id}]",
            )
            batch_record_ids.append(record_id)
            records.append(record)
            records_by_id[record_id] = record
            locations[record_id] = (declared_path, line_number)
        bindings.append(
            _ManifestBinding(
                declared_path=declared_path,
                path=manifest_path,
                sha256=actual_manifest_sha,
                raw_path=raw_path,
                raw_sha256=actual_raw_sha,
                source=source,
                record_ids=tuple(batch_record_ids),
            )
        )
    return _FrozenEvidence(
        bindings=tuple(bindings),
        records=tuple(records),
        records_by_id=MappingProxyType(records_by_id),
        locations=MappingProxyType(locations),
    )


def _reverify_frozen_bytes(
    frozen: _FrozenEvidence,
    *,
    request_path: Path,
    request_sha256: str,
) -> None:
    if _sha256_bytes(
        _stable_read(request_path, label="promotion request recheck")
    ) != request_sha256:
        raise PromotionRejection(
            "request_changed_after_verification",
            "promotion request drifted during replay",
        )
    for binding in frozen.bindings:
        if _sha256_bytes(
            _stable_read(
                binding.path,
                label=f"manifest recheck {binding.declared_path}",
            )
        ) != binding.sha256:
            raise PromotionRejection(
                "source_manifest_changed_after_verification",
                f"source manifest drifted during replay: {binding.declared_path}",
            )
        if _sha256_bytes(
            _stable_read(
                binding.raw_path,
                label=f"raw recheck {binding.declared_path}",
            )
        ) != binding.raw_sha256:
            raise PromotionRejection(
                "source_raw_changed_after_verification",
                f"source raw drifted during replay: {binding.declared_path}",
            )


def _inner(record: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(
        record.get("payload"),
        code="source_record_schema_mismatch",
        label=f"record {record.get('record_id')} payload",
    )


def _event_payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    inner = _inner(record)
    return _mapping(
        inner.get("payload"),
        code="source_record_schema_mismatch",
        label=f"record {record.get('record_id')} event payload",
    )


def _strict_target(
    records: Iterable[Mapping[str, Any]],
    *,
    expected_slug: str | None = None,
    expected_condition_id: str | None = None,
) -> ShortCryptoTarget:
    targets: list[ShortCryptoTarget] = []
    for record in records:
        recorder_payload = record.get("payload")
        if (
            record.get("source") != "clob_market_ws"
            or not isinstance(recorder_payload, Mapping)
            or recorder_payload.get("event_type") != "new_market"
        ):
            continue
        try:
            target = parse_short_crypto_announcement(record)
        except CatalogRejection as exc:
            raise PromotionRejection(exc.code, str(exc)) from exc
        if target is not None and (
            expected_slug is None or target.slug == expected_slug
        ):
            targets.append(target)
    if len(targets) != 1:
        raise PromotionRejection(
            "strict_target_count_invalid",
            f"the first tracer requires exactly one strict target, found {len(targets)}",
        )
    selected = targets[0]
    if (
        expected_condition_id is not None
        and selected.condition_id != expected_condition_id
    ):
        raise PromotionRejection(
            "strict_target_identity_mismatch",
            "selected announcement condition differs from the freeze",
        )
    return selected


def _book_state(
    record: Mapping[str, Any],
    *,
    target: ShortCryptoTarget,
) -> _BookState:
    inner = _inner(record)
    payload = _event_payload(record)
    if (
        record.get("source") != "clob_market_ws"
        or inner.get("source") != "clob_market_ws"
        or inner.get("schema_version") != "clob-market-ws.book.v1"
        or inner.get("kind") != "data"
        or inner.get("event_type") != "book"
        or payload.get("event_type") != "book"
    ):
        raise PromotionRejection(
            "l2_schema_mismatch",
            "decision L2 prefix must end in a full CLOB book record",
        )
    market_id = _nonempty(
        payload.get("market"),
        code="l2_identity_mismatch",
        label="book.market",
    )
    token_id = _nonempty(
        payload.get("asset_id"),
        code="l2_identity_mismatch",
        label="book.asset_id",
    )
    if market_id.lower() != target.condition_id or token_id not in {
        target.up_token_id,
        target.down_token_id,
    }:
        raise PromotionRejection(
            "l2_identity_mismatch",
            "book market/token does not match the strict target",
        )

    def levels(name: str) -> tuple[tuple[Decimal, Decimal], ...]:
        rows = _sequence(
            payload.get(name),
            code="l2_schema_mismatch",
            label=f"book.{name}",
        )
        parsed: list[tuple[Decimal, Decimal]] = []
        seen_prices: set[Decimal] = set()
        for index, raw_level in enumerate(rows):
            level = _mapping(
                raw_level,
                code="l2_schema_mismatch",
                label=f"book.{name}[{index}]",
            )
            price = _decimal(
                level.get("price"),
                label=f"book.{name}[{index}].price",
                positive=True,
            )
            size = _decimal(
                level.get("size"),
                label=f"book.{name}[{index}].size",
                positive=True,
            )
            if not ZERO < price < ONE or price in seen_prices:
                raise PromotionRejection(
                    "l2_level_invalid",
                    f"book.{name} contains an invalid or duplicate price",
                )
            seen_prices.add(price)
            parsed.append((price, size))
        return tuple(parsed)

    state_hash = _nonempty(
        payload.get("hash"),
        code="l2_schema_mismatch",
        label="book.hash",
    )
    connection_id = _nonempty(
        inner.get("connection_id"),
        code="l2_schema_mismatch",
        label="book.connection_id",
    )
    return _BookState(
        record_id=str(record["record_id"]),
        received_at_ms=_epoch_ms(record.get("received_at"), label="book.received_at"),
        connection_id=connection_id,
        monotonic_ns=_checked_int(
            inner.get("monotonic_ns"),
            label="book.monotonic_ns",
        ),
        market_id=market_id.lower(),
        token_id=token_id,
        bids=levels("bids"),
        asks=levels("asks"),
        state_hash=state_hash,
    )


def _resnapshot_for_book(
    record: Mapping[str, Any],
    *,
    target: ShortCryptoTarget,
    book: _BookState,
) -> None:
    inner = _inner(record)
    if (
        record.get("source") != "clob_market_ws"
        or inner.get("source") != "clob_market_ws"
        or inner.get("schema_version") != "edge-lab-recorder.lifecycle.v1"
        or inner.get("kind") != "lifecycle"
        or inner.get("event_type") != "resync_complete"
        or inner.get("connection_id") != book.connection_id
    ):
        raise PromotionRejection(
            "resnapshot_proof_invalid",
            "book must follow a durable same-connection resync_complete",
        )
    detail = _mapping(
        inner.get("detail"),
        code="resnapshot_proof_invalid",
        label="resync_complete.detail",
    )
    watermarks = _mapping(
        detail.get("asset_watermarks_ns"),
        code="resnapshot_proof_invalid",
        label="resync_complete.asset_watermarks_ns",
    )
    if set(watermarks) != {target.up_token_id, target.down_token_id}:
        raise PromotionRejection(
            "resnapshot_scope_mismatch",
            "resnapshot must bind both strict-target token IDs",
        )
    if (
        _epoch_ms(record.get("received_at"), label="resync.received_at")
        > book.received_at_ms
        or _checked_int(inner.get("monotonic_ns"), label="resync.monotonic_ns")
        >= book.monotonic_ns
    ):
        raise PromotionRejection(
            "resnapshot_order_invalid",
            "full book must be observed after its resnapshot watermark",
        )


def _candidate_markets(raw_json: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(raw_json, Mapping):
        if "markets" in raw_json:
            rows = _sequence(
                raw_json.get("markets"),
                code="fee_schema_mismatch",
                label="clob market response.markets",
            )
            return tuple(
                _mapping(
                    row,
                    code="fee_schema_mismatch",
                    label="clob market",
                )
                for row in rows
            )
        return (raw_json,)
    if isinstance(raw_json, Sequence) and not isinstance(
        raw_json, (str, bytes, bytearray)
    ):
        return tuple(
            _mapping(row, code="fee_schema_mismatch", label="clob market")
            for row in raw_json
        )
    raise PromotionRejection(
        "fee_schema_mismatch",
        "clob market raw_json must be an object or array",
    )


def _market_condition(market: Mapping[str, Any]) -> str | None:
    value = market.get("c")
    if value is None:
        value = market.get("condition_id", market.get("conditionId"))
    return value.lower() if isinstance(value, str) else None


def _fee_evidence(
    frozen: _FrozenEvidence,
    *,
    target: ShortCryptoTarget,
    decision_at_ms: int,
) -> tuple[ExecutionFeeSchedule, str, Decimal, Decimal]:
    candidates: list[
        tuple[int, str, ExecutionFeeSchedule, Mapping[str, Any]]
    ] = []
    for record in frozen.records:
        if record.get("source") != "clob_http":
            continue
        received_at_ms = _epoch_ms(
            record.get("received_at"),
            label="fee record received_at",
        )
        if received_at_ms > decision_at_ms:
            continue
        inner = _inner(record)
        if (
            inner.get("source") != "clob_http"
            or inner.get("schema_version") != "clob-http.snapshot.v1"
            or inner.get("kind") not in {"data", "snapshot"}
            or inner.get("event_type") not in {"clob_snapshot", "snapshot"}
        ):
            if inner.get("event_type") in {"clob_snapshot", "snapshot"}:
                raise PromotionRejection(
                    "fee_schema_mismatch",
                    "decision-time fee record is outside the exact schema allowlist",
                )
            continue
        payload = _mapping(
            inner.get("payload"),
            code="fee_schema_mismatch",
            label="clob snapshot payload",
        )
        responses = _sequence(
            payload.get("responses"),
            code="fee_schema_mismatch",
            label="clob snapshot responses",
        )
        for raw_response in responses:
            response = _mapping(
                raw_response,
                code="fee_schema_mismatch",
                label="clob snapshot response",
            )
            if response.get("resource") not in {"clob_market", "clob_markets"}:
                continue
            for market in _candidate_markets(response.get("raw_json")):
                if _market_condition(market) != target.condition_id:
                    continue
                token_rows = market.get("t", market.get("tokens"))
                if token_rows is not None:
                    rows = _sequence(
                        token_rows,
                        code="fee_schema_mismatch",
                        label="clob market token mapping",
                    )
                    token_ids = tuple(
                        str(
                            _mapping(
                                row,
                                code="fee_schema_mismatch",
                                label="clob market token",
                            ).get("t")
                            or _mapping(
                                row,
                                code="fee_schema_mismatch",
                                label="clob market token",
                            ).get("token_id")
                        )
                        for row in rows
                    )
                    if token_ids != (target.up_token_id, target.down_token_id):
                        raise PromotionRejection(
                            "fee_token_mapping_mismatch",
                            "fee market token mapping conflicts with announcement",
                        )
                try:
                    schedule = ExecutionFeeSchedule.from_market(market)
                except ValueError as exc:
                    raise PromotionRejection(
                        "decision_fee_missing",
                        f"decision-time market fee evidence is incomplete: {exc}",
                    ) from exc
                candidates.append(
                    (
                        received_at_ms,
                        str(record["record_id"]),
                        schedule,
                        market,
                    )
                )
    if not candidates:
        raise PromotionRejection(
            "decision_fee_missing",
            "no manifest-verified decision-time fee schedule was found",
        )
    latest_time = max(item[0] for item in candidates)
    latest = [item for item in candidates if item[0] == latest_time]
    schedules = {
        (
            item[2].rate,
            item[2].exponent,
            item[2].taker_only,
            item[2].exemption_reason,
        )
        for item in latest
    }
    if len(schedules) != 1:
        raise PromotionRejection(
            "decision_fee_conflict",
            "latest decision-time fee observations disagree",
        )
    selected = sorted(latest, key=lambda item: item[1])[0]
    tick_size = _decimal(
        selected[3].get("mts"),
        label="decision-time market mts",
        positive=True,
    )
    minimum_order_size = _decimal(
        selected[3].get("mos"),
        label="decision-time market mos",
        positive=True,
    )
    return selected[2], selected[1], tick_size, minimum_order_size


def _decision_book_intervening_record_ids(
    frozen: _FrozenEvidence,
    *,
    target: ShortCryptoTarget,
    book: _BookState,
    decision_at_ms: int,
) -> tuple[str, ...]:
    """Return target L2/lifecycle records omitted after the bound full book."""

    invalidating: list[str] = []
    for record in frozen.records:
        if record.get("source") != "clob_market_ws":
            continue
        record_id = str(record["record_id"])
        if record_id == book.record_id:
            continue
        received_at_ms = _epoch_ms(
            record.get("received_at"),
            label="decision-prefix record received_at",
        )
        if not (
            book.received_at_ms <= received_at_ms <= decision_at_ms
        ):
            continue
        inner = _inner(record)
        event_type = inner.get("event_type")
        connection_id = inner.get("connection_id")
        if (
            connection_id == book.connection_id
            and event_type in {"disconnected", "resync_complete"}
        ):
            invalidating.append(record_id)
            continue
        if event_type not in {
            "book",
            "last_trade_price",
            "price_change",
            "tick_size_change",
        }:
            continue
        payload = inner.get("payload")
        if not isinstance(payload, Mapping):
            continue
        market_id = payload.get("market")
        if (
            isinstance(market_id, str)
            and market_id.lower() == target.condition_id
        ):
            invalidating.append(record_id)
    return tuple(sorted(invalidating))


def _decisions(
    frozen: _FrozenEvidence,
    *,
    target: ShortCryptoTarget,
    evidence_mode: Literal["fixture", "captured_public"] = "fixture",
) -> tuple[_Decision, ...]:
    rows = [
        record
        for record in frozen.records
        if record.get("source") == "edge_lab_ghost_decision"
    ]
    if not rows:
        raise PromotionRejection(
            "ghost_decision_missing",
            "no immutable ghost decision is present",
        )
    decisions: list[_Decision] = []
    for record in rows:
        inner = _inner(record)
        payload = _event_payload(record)
        if (
            inner.get("source") != "edge_lab_ghost_decision"
            or inner.get("schema_version") != "edge-lab.ghost-decision.v1"
            or inner.get("kind") != "data"
            or inner.get("event_type") != "ghost_decision"
        ):
            raise PromotionRejection(
                "ghost_decision_schema_mismatch",
                "ghost decision does not match the supported immutable schema",
            )
        condition_value = payload.get("condition_id")
        if (
            not isinstance(condition_value, str)
            or not condition_value.strip()
        ):
            raise PromotionRejection(
                "ghost_decision_target_mismatch",
                "ghost decision condition identity is missing",
            )
        if condition_value.lower() != target.condition_id:
            continue
        received_at = _nonempty(
            record.get("received_at"),
            code="ghost_decision_time_invalid",
            label="decision.received_at",
        )
        received_at_ms = _epoch_ms(received_at, label="decision.received_at")
        if (
            _epoch_ms(
                payload.get("decision_receive_time"),
                label="decision.decision_receive_time",
            )
            != received_at_ms
            or _epoch_ms(
                inner.get("received_at"),
                label="decision inner received_at",
            )
            != received_at_ms
        ):
            raise PromotionRejection(
                "ghost_decision_time_mismatch",
                "decision receive time must equal its immutable envelope time",
            )
        if payload.get("announcement_record_id") != target.announcement_record_id:
            raise PromotionRejection(
                "ghost_decision_target_mismatch",
                "decision does not bind the strict announcement record",
            )
        if (
            str(payload.get("condition_id", "")).lower() != target.condition_id
            or str(payload.get("market_id", "")).lower() != target.condition_id
            or payload.get("token_id")
            not in {target.up_token_id, target.down_token_id}
        ):
            raise PromotionRejection(
                "ghost_decision_target_mismatch",
                "decision market/condition/token identity conflicts with target",
            )
        if payload.get("side") != "BUY" or payload.get("split") != "test":
            raise PromotionRejection(
                "unsupported_decision_policy",
                "v1 promotes only maker BUY decisions with split=test",
            )
        strategy_config = _mapping(
            payload.get("strategy_config"),
            code="strategy_config_invalid",
            label="decision.strategy_config",
        )
        config_hash = _nonempty(
            payload.get("config_hash"),
            code="strategy_config_invalid",
            label="decision.config_hash",
        )
        if (
            SHA256_RE.fullmatch(config_hash) is None
            or _sha256_bytes(canonical_json_bytes(strategy_config)) != config_hash
        ):
            raise PromotionRejection(
                "strategy_config_hash_mismatch",
                "config_hash does not bind the canonical strategy config",
            )
        if (
            strategy_config.get("queue_scenario") != FIXED_SCENARIO.value
            or _decimal(
                strategy_config.get("trade_volume_haircut"),
                label="strategy_config.trade_volume_haircut",
            )
            != FIXED_HAIRCUT
        ):
            raise PromotionRejection(
                "unsupported_decision_policy",
                "promoter requires the fixed pessimistic 50% haircut policy",
            )
        strategy_id = _nonempty(
            strategy_config.get("strategy_id"),
            code="strategy_config_invalid",
            label="strategy_config.strategy_id",
        )
        initial_cash = _decimal(
            strategy_config.get("initial_cash"),
            label="strategy_config.initial_cash",
            positive=True,
        )
        settlement_operation_cost = _decimal(
            strategy_config.get("settlement_operation_cost", "0"),
            label="strategy_config.settlement_operation_cost",
        )
        if (
            settlement_operation_cost < ZERO
            or (
                evidence_mode == "captured_public"
                and settlement_operation_cost <= ZERO
            )
        ):
            raise PromotionRejection(
                "settlement_operation_cost_invalid",
                "captured promotion requires a positive settlement cost bound",
            )
        settlement_operation_latency_ms = _checked_int(
            strategy_config.get(
                "settlement_operation_latency_ms",
                0,
            ),
            label=(
                "strategy_config.settlement_operation_latency_ms"
            ),
        )
        operation_cost_source = str(
            strategy_config.get(
                "operation_cost_source",
                "fixture_zero_cost_only",
            )
        )
        if not operation_cost_source.strip():
            raise PromotionRejection(
                "settlement_operation_cost_invalid",
                "settlement operation cost source must be non-empty",
            )
        if evidence_mode == "captured_public" and (
            "settlement_operation_cost" not in strategy_config
            or "settlement_operation_latency_ms" not in strategy_config
            or settlement_operation_latency_ms <= 0
            or "operation_cost_source" not in strategy_config
            or payload.get("public_trade_side_contract")
            != "data_api_taker_only_required"
        ):
            raise PromotionRejection(
                "settlement_operation_cost_invalid",
                "captured decisions require explicit cost, latency, and "
                "Data API taker-side contracts",
            )
        l2_ids = tuple(
            _nonempty(
                value,
                code="l2_prefix_invalid",
                label="decision.l2_record_ids[]",
            )
            for value in _sequence(
                payload.get("l2_record_ids"),
                code="l2_prefix_invalid",
                label="decision.l2_record_ids",
            )
        )
        if (
            len(l2_ids) != 1
            or payload.get("last_book_record_id") != l2_ids[-1]
            or l2_ids[-1] not in frozen.records_by_id
        ):
            raise PromotionRejection(
                "l2_prefix_invalid",
                "v1 requires one finalized full-book prefix ending at last_book_record_id",
            )
        book = _book_state(
            frozen.records_by_id[l2_ids[-1]],
            target=target,
        )
        if book.received_at_ms > received_at_ms:
            raise PromotionRejection(
                "future_l2_reference",
                "decision references an L2 record received in the future",
            )
        if book.token_id != payload.get("token_id"):
            raise PromotionRejection(
                "l2_identity_mismatch",
                "decision token differs from its L2 book",
            )
        resnapshot_id = _nonempty(
            payload.get("resnapshot_record_id"),
            code="resnapshot_proof_invalid",
            label="decision.resnapshot_record_id",
        )
        if resnapshot_id not in frozen.records_by_id:
            raise PromotionRejection(
                "resnapshot_proof_invalid",
                "decision resnapshot record is absent from the freeze",
            )
        _resnapshot_for_book(
            frozen.records_by_id[resnapshot_id],
            target=target,
            book=book,
        )
        intervening_ids = _decision_book_intervening_record_ids(
            frozen,
            target=target,
            book=book,
            decision_at_ms=received_at_ms,
        )
        if intervening_ids:
            raise PromotionRejection(
                "decision_l2_prefix_incomplete",
                "decision omitted target L2/lifecycle records after its "
                f"bound full book: {','.join(intervening_ids)}",
            )
        price = _decimal(payload.get("price"), label="decision.price", positive=True)
        quantity = _decimal(
            payload.get("quantity"),
            label="decision.quantity",
            positive=True,
        )
        tick_size = _decimal(
            payload.get("tick_size"),
            label="decision.tick_size",
            positive=True,
        )
        minimum_order_size = _decimal(
            payload.get("minimum_order_size"),
            label="decision.minimum_order_size",
            positive=True,
        )
        (
            fee_schedule,
            fee_record_id,
            evidence_tick_size,
            evidence_minimum_order_size,
        ) = _fee_evidence(
            frozen,
            target=target,
            decision_at_ms=received_at_ms,
        )
        if (
            not ZERO < price < ONE
            or price % tick_size != ZERO
            or tick_size != evidence_tick_size
            or minimum_order_size != evidence_minimum_order_size
            or quantity < minimum_order_size
            or payload.get("book_state_hash") != book.state_hash
        ):
            raise PromotionRejection(
                "decision_book_contract_mismatch",
                "decision price/size/tick/minimum/state hash conflicts with L2",
            )
        best_ask = min((level[0] for level in book.asks), default=None)
        if best_ask is None or price >= best_ask:
            raise PromotionRejection(
                "decision_not_passive",
                "maker BUY must remain below the visible best ask",
            )
        visible_queue = sum(
            (size for level_price, size in book.bids if level_price == price),
            ZERO,
        )
        if _decimal(
            payload.get("visible_queue"),
            label="decision.visible_queue",
        ) != visible_queue:
            raise PromotionRejection(
                "visible_queue_mismatch",
                "decision visible queue does not equal the rebuilt L2 queue",
            )
        decisions.append(
            _Decision(
                decision_id=str(record["record_id"]),
                experiment_id=_nonempty(
                    payload.get("experiment_id"),
                    code="ghost_decision_schema_mismatch",
                    label="decision.experiment_id",
                ),
                strategy_id=strategy_id,
                config_hash=config_hash,
                initial_cash=initial_cash,
                received_at=received_at,
                received_at_ms=received_at_ms,
                market_id=target.condition_id,
                token_id=str(payload["token_id"]),
                price=price,
                quantity=quantity,
                visible_queue=visible_queue,
                tick_size=tick_size,
                minimum_order_size=minimum_order_size,
                submit_latency_ms=_checked_int(
                    payload.get("submit_latency_ms"),
                    label="decision.submit_latency_ms",
                ),
                cancel_latency_ms=_checked_int(
                    payload.get("cancel_latency_ms"),
                    label="decision.cancel_latency_ms",
                ),
                max_book_age_ms=_checked_int(
                    payload.get("max_book_age_ms"),
                    label="decision.max_book_age_ms",
                ),
                book=book,
                resnapshot_record_id=resnapshot_id,
                fee_schedule=fee_schedule,
                fee_record_id=fee_record_id,
                settlement_operation_cost=(
                    settlement_operation_cost
                ),
                settlement_operation_latency_ms=(
                    settlement_operation_latency_ms
                ),
                operation_cost_source=operation_cost_source,
            )
        )
    if not decisions:
        raise PromotionRejection(
            "ghost_decision_missing",
            "no immutable ghost decision exists for the frozen target",
        )
    first = decisions[0]
    if any(
        (
            row.strategy_id,
            row.config_hash,
            row.initial_cash,
            row.experiment_id,
        )
        != (
            first.strategy_id,
            first.config_hash,
            first.initial_cash,
            first.experiment_id,
        )
        for row in decisions[1:]
    ):
        raise PromotionRejection(
            "mixed_strategy_run",
            "one promoted run must use one strategy/config/experiment/capital",
        )
    ids = [row.decision_id for row in decisions]
    if len(ids) != len(set(ids)):
        raise PromotionRejection(
            "duplicate_decision_id",
            "decision IDs must be globally unique",
        )
    return tuple(sorted(decisions, key=lambda row: (row.received_at_ms, row.decision_id)))


def _public_trades(
    frozen: _FrozenEvidence,
    *,
    target: ShortCryptoTarget,
    evidence_mode: Literal["fixture", "captured_public"] = "fixture",
) -> tuple[_PublicTrade, ...]:
    """Rebuild independent public trades under an explicit side contract.

    Fixture mode retains the historical Market-WebSocket event only for
    offline invariant tests.  Captured-public promotion never interprets that
    event's undocumented ``side`` field.  It accepts only public Data API
    snapshots explicitly fetched with ``takerOnly=true``, whose documented
    side is the taker/aggressor direction.
    """

    if evidence_mode not in {"fixture", "captured_public"}:
        raise PromotionRejection(
            "evidence_mode_invalid",
            "public trade evidence mode is unsupported",
        )
    grouped: dict[str, list[_PublicTrade]] = {}
    for record in frozen.records:
        if evidence_mode == "captured_public":
            if record.get("source") != "data_api_trades":
                continue
            candidates = _data_api_public_trades(record, target=target)
            for candidate in candidates:
                grouped.setdefault(candidate.event_key, []).append(candidate)
            continue
        if record.get("source") != "clob_market_ws":
            continue
        inner = _inner(record)
        if inner.get("event_type") != "last_trade_price":
            continue
        payload = _event_payload(record)
        if (
            inner.get("source") != "clob_market_ws"
            or inner.get("schema_version")
            != "clob-market-ws.last_trade_price.v1"
            or inner.get("kind") != "data"
            or payload.get("event_type") != "last_trade_price"
        ):
            raise PromotionRejection(
                "public_trade_schema_mismatch",
                "public trade does not match the independent CLOB trade schema",
            )
        market_id = _nonempty(
            payload.get("market"),
            code="public_trade_identity_mismatch",
            label="public trade market",
        ).lower()
        token_id = _nonempty(
            payload.get("asset_id"),
            code="public_trade_identity_mismatch",
            label="public trade asset_id",
        )
        if market_id != target.condition_id or token_id not in {
            target.up_token_id,
            target.down_token_id,
        }:
            continue
        transaction_hash = _nonempty(
            payload.get("transaction_hash"),
            code="public_trade_id_missing",
            label="public trade transaction_hash",
        )
        if TRANSACTION_RE.fullmatch(transaction_hash) is None:
            raise PromotionRejection(
                "public_trade_id_invalid",
                "public trade requires a verifiable transaction hash",
            )
        raw_timestamp = payload.get("timestamp")
        if (
            isinstance(raw_timestamp, bool)
            or not isinstance(raw_timestamp, (str, int))
            or not str(raw_timestamp).isdigit()
        ):
            raise PromotionRejection(
                "public_trade_time_invalid",
                "public trade timestamp must be Unix milliseconds",
            )
        source_timestamp_ms = int(str(raw_timestamp))
        received_at_ms = _epoch_ms(
            record.get("received_at"),
            label="public trade received_at",
        )
        side_text = payload.get("side")
        if side_text not in {"BUY", "SELL"}:
            raise PromotionRejection(
                "public_trade_side_invalid",
                "public trade side must be BUY or SELL",
            )
        candidate = _PublicTrade(
            record_id=str(record["record_id"]),
            evidence_record_id=str(record["record_id"]),
            event_key=transaction_hash.lower(),
            transaction_hash=transaction_hash.lower(),
            market_id=market_id,
            observed_token_id=token_id,
            observed_price=_decimal(
                payload.get("price"),
                label="public trade price",
                positive=True,
            ),
            observed_taker_side=OrderSide(side_text.lower()),
            token_id=token_id,
            price=_decimal(
                payload.get("price"),
                label="public trade price",
                positive=True,
            ),
            quantity=_decimal(
                payload.get("size"),
                label="public trade size",
                positive=True,
            ),
            aggressor_side=OrderSide(side_text.lower()),
            match_path="fixture_ws_side_assumption",
            source_timestamp_ms=source_timestamp_ms,
            received_at_ms=received_at_ms,
            replay_timestamp_ms=max(source_timestamp_ms, received_at_ms),
        )
        if not ZERO < candidate.price < ONE:
            raise PromotionRejection(
                "public_trade_price_invalid",
                "public trade price must be strictly between zero and one",
            )
        grouped.setdefault(candidate.event_key, []).append(candidate)
    unique: list[_PublicTrade] = []
    for event_key, candidates in grouped.items():
        fingerprints = {
            (
                row.market_id,
                row.token_id,
                row.price,
                row.quantity,
                row.aggressor_side,
                row.source_timestamp_ms,
            )
            for row in candidates
        }
        if len(fingerprints) != 1:
            raise PromotionRejection(
                "public_trade_duplicate_conflict",
                f"event {event_key} has conflicting public prints",
            )
        unique.append(
            sorted(
                candidates,
                key=lambda row: (row.received_at_ms, row.record_id),
            )[0]
        )
    return tuple(
        sorted(
            unique,
            key=lambda row: (
                row.replay_timestamp_ms,
                row.transaction_hash,
                row.event_key,
                row.record_id,
            ),
        )
    )


def _data_api_public_trades(
    record: Mapping[str, Any],
    *,
    target: ShortCryptoTarget,
) -> tuple[_PublicTrade, ...]:
    inner = _inner(record)
    payload = _event_payload(record)
    if (
        inner.get("source") != "data_api_trades"
        or inner.get("schema_version")
        != "data-api.trades.taker-only.snapshot.v1"
        or inner.get("kind") != "snapshot"
        or inner.get("event_type") != "data_api_trades"
        or payload.get("schema_version")
        != "edge-lab-data-api-trades-snapshot.v1"
        or payload.get("taker_only") is not True
    ):
        raise PromotionRejection(
            "public_trade_taker_contract_invalid",
            "captured trades require a target-bound takerOnly=true snapshot",
        )
    condition_value = payload.get("condition_id")
    if not isinstance(condition_value, str) or not condition_value:
        raise PromotionRejection(
            "public_trade_identity_mismatch",
            "Data API snapshot condition identity is missing",
        )
    if condition_value.lower() != target.condition_id:
        return ()
    provenance = _mapping(
        payload.get("provenance"),
        code="public_trade_provenance_invalid",
        label="Data API trade provenance",
    )
    request_params = _mapping(
        provenance.get("request_params"),
        code="public_trade_provenance_invalid",
        label="Data API trade request_params",
    )
    url = _nonempty(
        provenance.get("url"),
        code="public_trade_provenance_invalid",
        label="Data API trade URL",
    )
    parsed = urlsplit(url)
    status_code = provenance.get("status_code")
    if (
        provenance.get("source") != "data_api_trades"
        or provenance.get("method") != "GET"
        or parsed.scheme != "https"
        or parsed.hostname != "data-api.polymarket.com"
        or parsed.path != "/trades"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or dict(request_params)
        != {
            "market": target.condition_id,
            "takerOnly": "true",
            "limit": 1_000,
            "offset": 0,
        }
        or isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 200 <= status_code < 300
    ):
        raise PromotionRejection(
            "public_trade_provenance_invalid",
            "Data API response lacks exact unauthenticated GET provenance",
        )
    encoded_body = _nonempty(
        payload.get("raw_body_base64"),
        code="public_trade_body_invalid",
        label="Data API raw_body_base64",
    )
    try:
        raw_body = base64.b64decode(encoded_body, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PromotionRejection(
            "public_trade_body_invalid",
            "Data API raw body is not canonical base64",
        ) from exc
    body_sha256 = provenance.get("body_sha256")
    body_bytes = provenance.get("body_bytes")
    if (
        not isinstance(body_sha256, str)
        or SHA256_RE.fullmatch(body_sha256) is None
        or body_sha256 != _sha256_bytes(raw_body)
        or isinstance(body_bytes, bool)
        or not isinstance(body_bytes, int)
        or body_bytes != len(raw_body)
    ):
        raise PromotionRejection(
            "public_trade_body_invalid",
            "Data API raw body bytes/hash do not match provenance",
        )
    try:
        decoded = json.loads(raw_body, parse_float=str)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionRejection(
            "public_trade_body_invalid",
            "Data API raw body is not valid JSON",
        ) from exc
    raw_json = payload.get("raw_json")
    if (
        not isinstance(decoded, list)
        or not isinstance(raw_json, list)
        or decoded != raw_json
    ):
        raise PromotionRejection(
            "public_trade_body_invalid",
            "Data API parsed rows do not exactly match the preserved body",
        )
    received_at_ms = _epoch_ms(
        record.get("received_at"),
        label="Data API trade received_at",
    )
    snapshot_record_id = str(record["record_id"])
    candidates: list[_PublicTrade] = []
    for index, raw_trade in enumerate(raw_json):
        trade = _mapping(
            raw_trade,
            code="public_trade_schema_mismatch",
            label=f"Data API trade[{index}]",
        )
        market_id = _nonempty(
            trade.get("conditionId"),
            code="public_trade_identity_mismatch",
            label=f"Data API trade[{index}].conditionId",
        ).lower()
        token_id = _nonempty(
            trade.get("asset"),
            code="public_trade_identity_mismatch",
            label=f"Data API trade[{index}].asset",
        )
        if market_id != target.condition_id:
            raise PromotionRejection(
                "public_trade_identity_mismatch",
                "taker-only response contains a different condition",
            )
        if token_id not in {target.up_token_id, target.down_token_id}:
            raise PromotionRejection(
                "public_trade_identity_mismatch",
                "taker-only response contains an unknown outcome token",
            )
        transaction_hash = _nonempty(
            trade.get("transactionHash"),
            code="public_trade_id_missing",
            label=f"Data API trade[{index}].transactionHash",
        )
        if TRANSACTION_RE.fullmatch(transaction_hash) is None:
            raise PromotionRejection(
                "public_trade_id_invalid",
                "Data API trade requires a verifiable transaction hash",
            )
        raw_timestamp = trade.get("timestamp")
        if (
            isinstance(raw_timestamp, bool)
            or not isinstance(raw_timestamp, (str, int))
            or not str(raw_timestamp).isdigit()
        ):
            raise PromotionRejection(
                "public_trade_time_invalid",
                "Data API trade timestamp must be Unix seconds",
            )
        source_timestamp_ms = int(str(raw_timestamp)) * 1_000
        side_text = trade.get("side")
        if side_text not in {"BUY", "SELL"}:
            raise PromotionRejection(
                "public_trade_side_invalid",
                "Data API taker side must be BUY or SELL",
            )
        transaction_hash = transaction_hash.lower()
        try:
            event_key = data_api_trade_event_key(trade)
        except ValueError as exc:
            raise PromotionRejection(
                "public_trade_identity_invalid",
                f"Data API trade[{index}] has no canonical event key",
            ) from exc
        event_record_id = event_key
        observed_price = _decimal(
            trade.get("price"),
            label=f"Data API trade[{index}].price",
            positive=True,
        )
        observed_side = OrderSide(side_text.lower())
        if observed_side is OrderSide.BUY:
            replay_token_id = (
                target.down_token_id
                if token_id == target.up_token_id
                else target.up_token_id
            )
            replay_price = ONE - observed_price
            replay_side = OrderSide.SELL
            match_path = "complementary_buy_mint"
        else:
            replay_token_id = token_id
            replay_price = observed_price
            replay_side = OrderSide.SELL
            match_path = "same_token_sell"
        candidate = _PublicTrade(
            record_id=event_record_id,
            evidence_record_id=snapshot_record_id,
            event_key=event_key,
            transaction_hash=transaction_hash,
            market_id=market_id,
            observed_token_id=token_id,
            observed_price=observed_price,
            observed_taker_side=observed_side,
            token_id=replay_token_id,
            price=replay_price,
            quantity=_decimal(
                trade.get("size"),
                label=f"Data API trade[{index}].size",
                positive=True,
            ),
            aggressor_side=replay_side,
            match_path=match_path,
            source_timestamp_ms=source_timestamp_ms,
            received_at_ms=received_at_ms,
            replay_timestamp_ms=max(source_timestamp_ms, received_at_ms),
        )
        if not ZERO < candidate.price < ONE:
            raise PromotionRejection(
                "public_trade_price_invalid",
                "Data API trade price must be strictly between zero and one",
            )
        candidates.append(candidate)
    return tuple(candidates)


def _book_updates(
    frozen: _FrozenEvidence,
    *,
    target: ShortCryptoTarget,
    after_ms: int,
    connection_id: str,
) -> tuple[_BookUpdate, ...]:
    """Rebuild every post-decision top-of-book observation in the freeze."""

    updates: list[_BookUpdate] = []
    for record in frozen.records:
        if record.get("source") != "clob_market_ws":
            continue
        inner = _inner(record)
        event_type = inner.get("event_type")
        event_connection_id = inner.get("connection_id")
        received_at_ms = _epoch_ms(
            record.get("received_at"),
            label="L2 update received_at",
        )
        if received_at_ms < after_ms:
            continue
        if received_at_ms >= target.closes_at_ms:
            continue
        if (
            inner.get("kind") == "quarantine"
            and event_type
            in {"book", "price_change", "tick_size_change"}
        ):
            raise PromotionRejection(
                "l2_quarantine_in_window",
                "target-window L2 contains quarantined evidence",
            )
        if event_type == "disconnected":
            if event_connection_id == connection_id:
                raise PromotionRejection(
                    "l2_disconnect_in_window",
                    "a post-decision disconnect invalidates queue replay",
                )
            continue
        if event_type == "tick_size_change":
            payload = _event_payload(record)
            if str(payload.get("market", "")).lower() == target.condition_id:
                if event_connection_id != connection_id:
                    raise PromotionRejection(
                        "l2_connection_mismatch",
                        "target-window L2 moved to another connection",
                    )
                raise PromotionRejection(
                    "tick_size_change_in_window",
                    "v1 rejects post-decision tick-size changes",
                )
            continue
        if event_type not in {"book", "price_change"}:
            continue
        if (
            inner.get("source") != "clob_market_ws"
            or inner.get("kind") != "data"
        ):
            raise PromotionRejection(
                "l2_schema_mismatch",
                "post-decision L2 record is outside the strict schema",
            )
        payload = _event_payload(record)
        if str(payload.get("market", "")).lower() != target.condition_id:
            continue
        if event_connection_id != connection_id:
            raise PromotionRejection(
                "l2_connection_mismatch",
                "target-window L2 moved to another connection",
            )
        raw_source_timestamp = payload.get("timestamp")
        if (
            isinstance(raw_source_timestamp, bool)
            or not isinstance(raw_source_timestamp, (str, int))
            or not str(raw_source_timestamp).isdigit()
        ):
            raise PromotionRejection(
                "l2_time_invalid",
                "L2 update timestamp must be Unix milliseconds",
            )
        source_timestamp_ms = int(str(raw_source_timestamp))
        replay_timestamp_ms = max(
            source_timestamp_ms,
            received_at_ms,
        )
        if replay_timestamp_ms >= target.closes_at_ms:
            continue
        if event_type == "book":
            if inner.get("schema_version") != "clob-market-ws.book.v1":
                raise PromotionRejection(
                    "l2_schema_mismatch",
                    "book update schema drifted",
                )
            token_id = str(payload.get("asset_id", ""))
            if token_id not in {
                target.up_token_id,
                target.down_token_id,
            }:
                continue
            bids = _sequence(
                payload.get("bids"),
                code="l2_schema_mismatch",
                label="book update bids",
            )
            asks = _sequence(
                payload.get("asks"),
                code="l2_schema_mismatch",
                label="book update asks",
            )
            bid_prices = [
                _decimal(
                    _mapping(
                        row,
                        code="l2_schema_mismatch",
                        label="book update bid",
                    ).get("price"),
                    label="book update bid price",
                    positive=True,
                )
                for row in bids
            ]
            ask_prices = [
                _decimal(
                    _mapping(
                        row,
                        code="l2_schema_mismatch",
                        label="book update ask",
                    ).get("price"),
                    label="book update ask price",
                    positive=True,
                )
                for row in asks
            ]
            updates.append(
                _BookUpdate(
                    record_id=str(record["record_id"]),
                    token_id=token_id,
                    received_at_ms=received_at_ms,
                    source_timestamp_ms=source_timestamp_ms,
                    replay_timestamp_ms=replay_timestamp_ms,
                    best_bid=max(bid_prices, default=None),
                    best_ask=min(ask_prices, default=None),
                )
            )
            continue
        if (
            inner.get("schema_version")
            != "clob-market-ws.price_change.v1"
        ):
            raise PromotionRejection(
                "l2_schema_mismatch",
                "price_change update schema drifted",
            )
        changes = _sequence(
            payload.get("price_changes"),
            code="l2_schema_mismatch",
            label="price_change.price_changes",
        )
        for raw_change in changes:
            change = _mapping(
                raw_change,
                code="l2_schema_mismatch",
                label="price_change row",
            )
            token_id = str(change.get("asset_id", ""))
            if token_id not in {
                target.up_token_id,
                target.down_token_id,
            }:
                continue
            best_bid = (
                None
                if change.get("best_bid") in {None, ""}
                else _decimal(
                    change.get("best_bid"),
                    label="price_change.best_bid",
                    positive=True,
                )
            )
            best_ask = (
                None
                if change.get("best_ask") in {None, ""}
                else _decimal(
                    change.get("best_ask"),
                    label="price_change.best_ask",
                    positive=True,
                )
            )
            update_id = _sha256_bytes(
                canonical_json_bytes(
                    {
                        "record_id": record["record_id"],
                        "token_id": token_id,
                    }
                )
            )
            updates.append(
                _BookUpdate(
                    record_id=update_id,
                    token_id=token_id,
                    received_at_ms=received_at_ms,
                    source_timestamp_ms=source_timestamp_ms,
                    replay_timestamp_ms=replay_timestamp_ms,
                    best_bid=best_bid,
                    best_ask=best_ask,
                )
            )
    return tuple(
        sorted(
            updates,
            key=lambda row: (
                row.replay_timestamp_ms,
                row.record_id,
            ),
        )
    )


def _liveness_proof(
    frozen: _FrozenEvidence,
    *,
    target: ShortCryptoTarget,
) -> _LivenessProof:
    resyncs: dict[str, list[tuple[int, int, str]]] = {}
    pongs: dict[str, list[tuple[int, int, str]]] = {}
    disconnects: dict[str, list[tuple[int, int]]] = {}
    for record in frozen.records:
        if record.get("source") != "clob_market_ws":
            continue
        inner = _inner(record)
        connection = inner.get("connection_id")
        if not isinstance(connection, str) or not connection:
            continue
        event_type = inner.get("event_type")
        received = _epoch_ms(
            record.get("received_at"),
            label="liveness received_at",
        )
        monotonic = inner.get("monotonic_ns")
        if isinstance(monotonic, bool) or not isinstance(monotonic, int):
            continue
        record_id = str(record["record_id"])
        if (
            event_type == "resync_complete"
            and inner.get("schema_version")
            == "edge-lab-recorder.lifecycle.v1"
        ):
            detail = inner.get("detail")
            if isinstance(detail, Mapping):
                watermarks = detail.get("asset_watermarks_ns")
                if isinstance(watermarks, Mapping) and set(watermarks) == {
                    target.up_token_id,
                    target.down_token_id,
                }:
                    resyncs.setdefault(connection, []).append(
                        (received, monotonic, record_id)
                    )
        elif (
            event_type == "heartbeat_ack"
            and inner.get("schema_version")
            == "edge-lab-recorder.heartbeat-ack.v1"
            and isinstance(inner.get("raw_frame"), str)
            and inner["raw_frame"].strip().upper() == "PONG"
        ):
            pongs.setdefault(connection, []).append(
                (received, monotonic, record_id)
            )
        elif event_type == "disconnected":
            disconnects.setdefault(connection, []).append((received, monotonic))
    proofs: list[_LivenessProof] = []
    for connection in sorted(pongs):
        history = sorted(pongs[connection])
        before = next(
            (row for row in reversed(history) if row[0] <= target.closes_at_ms),
            None,
        )
        after = next(
            (row for row in history if row[0] >= target.closes_at_ms),
            None,
        )
        if before is None or after is None:
            continue
        if (
            target.closes_at_ms - before[0] > LIVENESS_TOLERANCE_MS
            or after[0] - target.closes_at_ms > LIVENESS_TOLERANCE_MS
            or before[2] == after[2]
            or before[1] >= after[1]
            or after[1] - before[1] > 2 * LIVENESS_TOLERANCE_MS * 1_000_000
        ):
            continue
        valid_resyncs = [
            row
            for row in resyncs.get(connection, ())
            if row[0] <= before[0] and row[1] < before[1]
        ]
        if not valid_resyncs:
            continue
        resync = sorted(valid_resyncs)[-1]
        if any(
            resync[0] <= received <= after[0]
            for received, _ in disconnects.get(connection, ())
        ):
            continue
        proofs.append(
            _LivenessProof(
                connection_id=connection,
                resync_record_id=resync[2],
                before_record_id=before[2],
                before_received_at_ms=before[0],
                after_record_id=after[2],
                after_received_at_ms=after[0],
            )
        )
    if len(proofs) != 1:
        raise PromotionRejection(
            "close_liveness_invalid",
            f"expected one same-connection PONG bracket, found {len(proofs)}",
        )
    return proofs[0]


def _settlement(
    frozen: _FrozenEvidence,
    *,
    target: ShortCryptoTarget,
    first_decision: _Decision,
) -> tuple[Any, Any, Any, _LivenessProof]:
    rtds_manifests = [
        binding.path
        for binding in frozen.bindings
        if binding.source == "rtds_ws"
    ]
    rule_url = (
        "https://data.chain.link/streams/"
        f"{target.source_symbol.split('/', 1)[0]}-usd"
    )
    try:
        boundaries = extract_chainlink_boundaries_from_finalized_manifests(
            rtds_manifests,
            target=target,
            rule_url=rule_url,
            first_decision_at=first_decision.received_at,
        )
    except PriceToBeatRejection as exc:
        raise PromotionRejection(exc.code, str(exc)) from exc
    if (
        boundaries.open_boundary.retrospective
        or not boundaries.open_boundary.available_at_first_decision
    ):
        raise PromotionRejection(
            "retrospective_price_to_beat",
            "window-open Chainlink evidence was not available at decision time",
        )
    open_observation = ChainlinkBoundaryObservation(
        record_id=boundaries.open_boundary.record_id,
        symbol=boundaries.open_boundary.source_symbol,
        event_at_ms=boundaries.open_boundary.inner_timestamp_ms,
        received_at_ms=_epoch_ms(
            boundaries.open_boundary.received_at,
            label="Chainlink open received_at",
        ),
        price=boundaries.open_boundary.price,
    )
    close_observation = ChainlinkBoundaryObservation(
        record_id=boundaries.close_boundary.record_id,
        symbol=boundaries.close_boundary.source_symbol,
        event_at_ms=boundaries.close_boundary.inner_timestamp_ms,
        received_at_ms=_epoch_ms(
            boundaries.close_boundary.received_at,
            label="Chainlink close received_at",
        ),
        price=boundaries.close_boundary.price,
    )
    gamma_records = tuple(
        record
        for record in frozen.records
        if record.get("source") == "gamma_http"
    )
    try:
        reconciliation = reconcile_short_crypto_settlement(
            target,
            gamma_records,
            chainlink_open=open_observation,
            chainlink_close=close_observation,
        )
    except SettlementRejection as exc:
        raise PromotionRejection(exc.code, str(exc)) from exc
    if reconciliation.status != "resolved" or reconciliation.label is None:
        reasons = ",".join(reconciliation.reason_codes) or reconciliation.status
        raise PromotionRejection(
            "settlement_not_reconciled",
            f"Gamma/Chainlink settlement did not resolve: {reasons}",
        )
    liveness = _liveness_proof(frozen, target=target)
    return boundaries, reconciliation, reconciliation.label, liveness


def _ledger_trace_mapping(entry: Any) -> dict[str, Any]:
    return {
        "sequence": entry.sequence,
        "action": entry.action,
        "source_event_id": entry.source_event_id,
        "decision_id": entry.decision_id,
        "timestamp_ms": entry.timestamp_ms,
        "market_id": entry.market_id,
        "token_id": entry.token_id,
        "quantity": _decimal_text(entry.quantity),
        "price": None if entry.price is None else _decimal_text(entry.price),
        "cash_delta": _decimal_text(entry.cash_delta),
        "fee": _decimal_text(entry.fee),
        "operation_cost": _decimal_text(entry.operation_cost),
        "details": dict(entry.details),
    }


def _replay_trace_mapping(entry: Any) -> dict[str, Any]:
    return {
        "sequence": entry.sequence,
        "action": entry.action,
        "order_id": entry.order_id,
        "status": entry.status.value,
        "source_event_id": entry.source_event_id,
        "decision_id": entry.decision_id,
        "timestamp_ms": entry.timestamp_ms,
        "details": dict(entry.details),
    }


def _trade_evidence(
    *,
    target: ShortCryptoTarget,
    fill: Mapping[str, Any],
    winning_token_id: str,
    settlement_record_id: str,
    settled_at_ms: int,
    operation_cost: Decimal,
) -> dict[str, Any]:
    quantity = _decimal(fill["quantity"], label="fill.quantity")
    price = _decimal(fill["price"], label="fill.price")
    fee = _decimal(fill["fee"], label="fill.fee")
    notional = quantity * price
    gross_payout = quantity if fill["token_id"] == winning_token_id else ZERO
    spread_pnl = gross_payout - notional
    net_pnl = spread_pnl - fee - operation_cost
    trade_id = _sha256_bytes(
        canonical_json_bytes(
            {
                "decision_id": fill["decision_id"],
                "source_event_id": fill["source_event_id"],
                "quantity": _decimal_text(quantity),
                "price": _decimal_text(price),
                "settlement_record_id": settlement_record_id,
            }
        )
    )
    capacity_notional = (
        _decimal(
            fill["attributable_trade_capacity"],
            label="fill.attributable_trade_capacity",
        )
        * price
    )
    row = TradeEvidence(
        trade_id=trade_id,
        event_id=target.slug,
        independence_group=target.slug,
        market_id=target.condition_id,
        source_event_id=str(fill["source_event_id"]),
        settlement_source_event_id=settlement_record_id,
        decision_id=str(fill["decision_id"]),
        one_leg_episode_id=None,
        filled_at=_iso(int(fill["timestamp_ms"])),
        settled_at=_iso(settled_at_ms),
        settled=True,
        is_real=False,
        is_oos=True,
        leakage_free=True,
        all_costs_included=True,
        fill_explainable=True,
        notional=notional,
        capacity_notional=max(notional, capacity_notional),
        spread_pnl=spread_pnl,
        forecast_pnl=ZERO,
        adverse_selection_pnl=ZERO,
        hedge_pnl=ZERO,
        fees=fee,
        theoretical_rewards=ZERO,
        scoring_rewards=ZERO,
        confirmed_reward_payout=ZERO,
        reward_theoretical_source_event_id=None,
        reward_scoring_source_event_id=None,
        reward_payout_epoch_id=None,
        reward_payout_source_event_id=None,
        other_costs=operation_cost,
        pessimistic_cost=operation_cost,
        claimed_net_pnl=net_pnl,
        one_leg_pnl=None,
    ).to_mapping()
    row.update(
        {
            "counterfactual": True,
            "actual_fill": False,
            "authenticated_fill": False,
            "evidence_kind": "execution_replay_fill",
        }
    )
    return row


def _run_replay(
    frozen: _FrozenEvidence,
    *,
    target: ShortCryptoTarget,
    decisions: tuple[_Decision, ...],
    trades: tuple[_PublicTrade, ...],
    boundaries: Any,
    reconciliation: Any,
    settlement_label: Any,
    liveness: _LivenessProof,
    settlement_commit: _SettlementCommitProof | None,
    ignored_claims: tuple[str, ...],
    evidence_mode: Literal["fixture", "captured_public"],
) -> tuple[dict[str, Any], str]:
    decision_connections = {
        row.book.connection_id for row in decisions
    }
    if len(decision_connections) != 1:
        raise PromotionRejection(
            "decision_l2_connection_mismatch",
            "all ghost decisions must use one continuous L2 connection",
        )
    decision_connection_id = next(iter(decision_connections))
    if liveness.connection_id != decision_connection_id:
        raise PromotionRejection(
            "close_liveness_connection_mismatch",
            "close PONG proof does not bind the decision-book connection",
        )
    first_decision_ms = min(row.received_at_ms for row in decisions)
    decision_floor_ms = max(row.received_at_ms for row in decisions)
    trades = tuple(
        row
        for row in trades
        if (
            row.source_timestamp_ms > decision_floor_ms
            and row.source_timestamp_ms < target.closes_at_ms
            and row.received_at_ms < target.closes_at_ms
        )
    )
    if not trades:
        raise PromotionRejection(
            "public_trade_missing",
            "no causally post-decision, pre-close public trade is present",
        )
    if decision_floor_ms >= min(
        row.replay_timestamp_ms for row in trades
    ):
        raise PromotionRejection(
            "unsupported_interleaved_timeline",
            "v1 requires all ghost decisions before the first public trade",
        )
    ledger = Ledger(decisions[0].initial_cash)
    ledger.register_binary_market(
        target.condition_id,
        yes_token_id=target.up_token_id,
        no_token_id=target.down_token_id,
    )
    replay = MakerReplay(ledger, scenario=FIXED_SCENARIO)

    def submit_decision(decision: _Decision) -> None:
        replay.submit(
            MakerOrderRequest(
                order_id=decision.decision_id,
                market_id=decision.market_id,
                token_id=decision.token_id,
                side=OrderSide.BUY,
                price=decision.price,
                quantity=decision.quantity,
                tick_size=decision.tick_size,
                fee_schedule=decision.fee_schedule,
                capacity=decision.quantity,
                submit_latency_ms=max(
                    decision.submit_latency_ms,
                    PESSIMISTIC_SUBMIT_LATENCY_FLOOR_MS,
                ),
                cancel_latency_ms=decision.cancel_latency_ms,
                minimum_live_ms=0,
                max_book_age_ms=decision.max_book_age_ms,
            ),
            visible_queue_ahead=decision.visible_queue,
            now_ms=decision.received_at_ms,
            book_timestamp_ms=decision.book.received_at_ms,
            trace=TraceRef(
                source_event_id=decision.book.record_id,
                decision_id=decision.decision_id,
                timestamp_ms=decision.received_at_ms,
            ),
        )

    fill_rows: list[dict[str, Any]] = []
    book_updates = _book_updates(
        frozen,
        target=target,
        after_ms=first_decision_ms,
        connection_id=decision_connection_id,
    )
    tick_by_token = {
        row.token_id: row.tick_size for row in decisions
    }
    timeline: list[tuple[int, int, str, Any]] = [
        (
            update.replay_timestamp_ms,
            0,
            update.record_id,
            update,
        )
        for update in book_updates
        if update.token_id in tick_by_token
    ]
    timeline.extend(
        (
            decision.received_at_ms,
            1,
            decision.decision_id,
            decision,
        )
        for decision in decisions
    )
    timeline.extend(
        (
            trade.replay_timestamp_ms,
            3,
            trade.event_key,
            trade,
        )
        for trade in trades
    )
    for decision in decisions:
        cancel_request_at_ms = max(
            decision.received_at_ms,
            target.closes_at_ms - decision.cancel_latency_ms,
        )
        timeline.append(
            (
                cancel_request_at_ms,
                2,
                decision.decision_id,
                decision,
            )
        )
    for _, event_kind, _, event in sorted(timeline):
        if event_kind == 0:
            update = event
            replay.on_book(
                BookEvent(
                    token_id=update.token_id,
                    timestamp_ms=update.replay_timestamp_ms,
                    source_timestamp_ms=update.source_timestamp_ms,
                    best_bid=update.best_bid,
                    best_ask=update.best_ask,
                    tick_size=tick_by_token[update.token_id],
                    source_event_id=update.record_id,
                )
            )
            continue
        if event_kind == 1:
            submit_decision(event)
            continue
        if event_kind == 2:
            decision = event
            replay.request_cancel(
                decision.decision_id,
                now_ms=max(
                    decision.received_at_ms,
                    target.closes_at_ms - decision.cancel_latency_ms,
                ),
                trace=TraceRef(
                    source_event_id=(
                        f"fixed-cancel-before-close:{decision.decision_id}"
                    ),
                    decision_id=decision.decision_id,
                    timestamp_ms=max(
                        decision.received_at_ms,
                        target.closes_at_ms
                        - decision.cancel_latency_ms,
                    ),
                ),
            )
            continue
        trade = event
        if (
            trade.replay_timestamp_ms >= target.closes_at_ms
            or trade.received_at_ms >= target.closes_at_ms
        ):
            continue
        before = {
            order.order_id: order.queue_ahead
            for order in replay.orders
            if order.request.token_id == trade.token_id
        }
        fills = replay.on_trade(
            TradeEvent(
                token_id=trade.token_id,
                timestamp_ms=trade.replay_timestamp_ms,
                price=trade.price,
                quantity=trade.quantity,
                aggressor_side=trade.aggressor_side,
                source_event_id=trade.record_id,
            )
        )
        after = {
            order.order_id: order.queue_ahead
            for order in replay.orders
            if order.request.token_id == trade.token_id
        }
        queue_consumed_total = sum(
            (before[key] - after.get(key, before[key]) for key in before),
            ZERO,
        )
        fill_total = sum((fill.quantity for fill in fills), ZERO)
        attributable_capacity = trade.quantity * FIXED_HAIRCUT
        if queue_consumed_total + fill_total > attributable_capacity:
            raise PromotionRejection(
                "public_trade_volume_double_spent",
                "one public trade consumed more queue/fill than its haircut capacity",
            )
        for fill in fills:
            queue_before = before.get(fill.order_id, ZERO)
            queue_after = after.get(fill.order_id, ZERO)
            fill_rows.append(
                {
                    "order_id": fill.order_id,
                    "decision_id": fill.decision_id,
                    "source_event_id": fill.source_event_id,
                    "public_trade_evidence_record_id": (
                        trade.evidence_record_id
                    ),
                    "public_trade_transaction_hash": trade.transaction_hash,
                    "public_trade_event_key": trade.event_key,
                    "public_trade_observed_token_id": (
                        trade.observed_token_id
                    ),
                    "public_trade_observed_price": _decimal_text(
                        trade.observed_price
                    ),
                    "public_trade_observed_taker_side": (
                        trade.observed_taker_side.value
                    ),
                    "public_trade_match_path": trade.match_path,
                    "market_id": target.condition_id,
                    "token_id": fill.token_id,
                    "side": fill.side.value,
                    "price": _decimal_text(fill.price),
                    "quantity": _decimal_text(fill.quantity),
                    "fee": _decimal_text(fill.fee),
                    "timestamp_ms": fill.timestamp_ms,
                    "queue_before": _decimal_text(queue_before),
                    "queue_consumed": _decimal_text(queue_before - queue_after),
                    "queue_after": _decimal_text(queue_after),
                    "public_trade_volume": _decimal_text(trade.quantity),
                    "trade_volume_haircut": _decimal_text(FIXED_HAIRCUT),
                    "attributable_trade_capacity": _decimal_text(
                        attributable_capacity
                    ),
                    "counterfactual": True,
                    "actual_fill": False,
                    "authenticated_fill": False,
                    "evidence_kind": "execution_replay_fill",
                    "liquidity_role": "maker",
                    "queue_policy": FIXED_SCENARIO.value,
                }
            )
    if not fill_rows:
        raise PromotionRejection(
            "explainable_fill_missing",
            "public trades did not produce a queue-bounded maker fill",
        )
    if any(int(row["timestamp_ms"]) >= target.closes_at_ms for row in fill_rows):
        raise PromotionRejection(
            "fill_after_close",
            "a replay fill occurred at or after market close",
        )
    replay.advance_time(
        target.closes_at_ms,
        source_event_id=boundaries.close_boundary.record_id,
    )
    gamma_record_id = settlement_label.gamma_record_id
    resolve_at_ms = settlement_label.available_at_ms
    settlement_available_ms = max(
        resolve_at_ms,
        liveness.after_received_at_ms,
        _epoch_ms(
            boundaries.close_boundary.received_at,
            label="close boundary received_at",
        ),
    )
    if settlement_commit is not None:
        settlement_available_ms = max(
            settlement_available_ms,
            settlement_commit.received_at_ms,
        )
    settlement_source_event_id = (
        gamma_record_id
        if settlement_commit is None
        else settlement_commit.record_id
    )
    ledger.resolve(
        target.condition_id,
        winning_token_id=settlement_label.winning_token_id,
        trace=TraceRef(
            source_event_id=settlement_source_event_id,
            decision_id=decisions[0].decision_id,
            timestamp_ms=settlement_available_ms,
        ),
    )
    operation_cost = decisions[0].settlement_operation_cost
    operation_latency_ms = decisions[0].settlement_operation_latency_ms
    redeem_completed_at_ms = settlement_available_ms + operation_latency_ms
    ledger.redeem(
        target.condition_id,
        operation=OperationAssumption(
            cost=operation_cost,
            latency_ms=operation_latency_ms,
            confirmed=True,
            source_ref=(
                f"{decisions[0].operation_cost_source}:"
                f"{settlement_source_event_id}:"
                f"{boundaries.close_boundary.record_id}:"
                f"{liveness.after_record_id}"
            ),
        ),
        trace=TraceRef(
            source_event_id=liveness.after_record_id,
            decision_id=decisions[0].decision_id,
            timestamp_ms=redeem_completed_at_ms,
        ),
    )
    snapshot = ledger.accounting_snapshot({})
    if any(quantity != ZERO for quantity in snapshot.positions.values()):
        raise PromotionRejection(
            "ledger_position_not_redeemed",
            "settled replay retained token inventory",
        )
    buy_entries = [entry for entry in ledger.trace if entry.action == "buy"]
    if len(buy_entries) != len(fill_rows):
        raise PromotionRejection(
            "ledger_fill_trace_mismatch",
            "every fill must map to exactly one ledger buy trace",
        )
    settled_pnl = snapshot.marked_equity - decisions[0].initial_cash
    notional = sum(
        (
            _decimal(row["quantity"], label="fill.quantity")
            * _decimal(row["price"], label="fill.price")
            for row in fill_rows
        ),
        ZERO,
    )
    fill_notionals = [
        _decimal(row["quantity"], label="fill.quantity")
        * _decimal(row["price"], label="fill.price")
        for row in fill_rows
    ]
    operation_cost_shares: list[Decimal] = []
    allocated_operation_cost = ZERO
    for index, fill_notional in enumerate(fill_notionals):
        share = (
            operation_cost - allocated_operation_cost
            if index == len(fill_notionals) - 1
            else operation_cost * fill_notional / notional
        )
        operation_cost_shares.append(share)
        allocated_operation_cost += share
    if sum(operation_cost_shares, ZERO) != operation_cost:
        raise PromotionRejection(
            "operation_cost_allocation_mismatch",
            "per-fill operation costs do not reconcile to the ledger",
        )
    trade_evidence = [
        _trade_evidence(
            target=target,
            fill=row,
            winning_token_id=settlement_label.winning_token_id,
            settlement_record_id=settlement_source_event_id,
            settled_at_ms=redeem_completed_at_ms,
            operation_cost=operation_cost_shares[index],
        )
        for index, row in enumerate(fill_rows)
    ]
    reasons = (
        "event_count_below_100",
        "explainable_fill_count_below_100",
    )
    replay_payload = {
        "schema_version": REPLAY_SCHEMA,
        "counterfactual": True,
        "actual_fill": False,
        "authenticated_fill": False,
        "classification": "insufficient_data",
        "classification_reasons": list(reasons),
        "source_claims_ignored": list(ignored_claims),
        "policy": {
            "maker_side": "buy",
            "queue_scenario": FIXED_SCENARIO.value,
            "trade_volume_haircut": _decimal_text(FIXED_HAIRCUT),
            "submit_latency_floor_ms": PESSIMISTIC_SUBMIT_LATENCY_FLOOR_MS,
            "public_trade_capacity_shared_globally": True,
            "complementary_buy_mint_supported": True,
            "book_touch_creates_fill": False,
            "zero_fee_fallback": False,
            "cancel_request_policy": "cancel_latency_before_market_close",
        },
        "target": {
            "slug": target.slug,
            "market_id": target.market_id,
            "condition_id": target.condition_id,
            "up_token_id": target.up_token_id,
            "down_token_id": target.down_token_id,
            "announcement_record_id": target.announcement_record_id,
            "rule_hash": target.rule_hash,
            "opens_at_ms": target.opens_at_ms,
            "closes_at_ms": target.closes_at_ms,
        },
        "decisions": [
            {
                "decision_id": row.decision_id,
                "experiment_id": row.experiment_id,
                "strategy_id": row.strategy_id,
                "config_hash": row.config_hash,
                "market_id": row.market_id,
                "token_id": row.token_id,
                "side": "buy",
                "price": _decimal_text(row.price),
                "quantity": _decimal_text(row.quantity),
                "decision_receive_time": row.received_at,
                "l2_record_ids": [row.book.record_id],
                "last_book_record_id": row.book.record_id,
                "book_state_hash": row.book.state_hash,
                "resnapshot_record_id": row.resnapshot_record_id,
                "visible_queue": _decimal_text(row.visible_queue),
                "tick_size": _decimal_text(row.tick_size),
                "minimum_order_size": _decimal_text(row.minimum_order_size),
                "submit_latency_ms": row.submit_latency_ms,
                "effective_submit_latency_ms": max(
                    row.submit_latency_ms,
                    PESSIMISTIC_SUBMIT_LATENCY_FLOOR_MS,
                ),
                "cancel_latency_ms": row.cancel_latency_ms,
                "cancel_request_at_ms": max(
                    row.received_at_ms,
                    target.closes_at_ms - row.cancel_latency_ms,
                ),
                "fee_record_id": row.fee_record_id,
                "fee_rate": _decimal_text(row.fee_schedule.rate),
                "fee_exponent": _decimal_text(row.fee_schedule.exponent),
                "fee_taker_only": row.fee_schedule.taker_only,
                "split": "test",
            }
            for row in decisions
        ],
        "fills": fill_rows,
        "settlement": {
            "candidate_reconciliation_status": reconciliation.status,
            "strict_terminal_status": (
                "strict_settlement_committed"
                if evidence_mode == "captured_public"
                else "fixture_validation_only"
            ),
            "outcome": settlement_label.outcome,
            "winning_token_id": settlement_label.winning_token_id,
            "gamma_record_id": gamma_record_id,
            "atomic_settlement_commit_record_id": (
                None
                if settlement_commit is None
                else settlement_commit.record_id
            ),
            "settlement_source_event_id": settlement_source_event_id,
            "chainlink_open_record_id": boundaries.open_boundary.record_id,
            "chainlink_close_record_id": boundaries.close_boundary.record_id,
            "chainlink_open_price": _decimal_text(
                boundaries.open_boundary.price
            ),
            "chainlink_close_price": _decimal_text(
                boundaries.close_boundary.price
            ),
            "close_liveness": asdict(liveness),
            "available_at_ms": settlement_available_ms,
            "redeem_completed_at_ms": redeem_completed_at_ms,
            "operation_cost": _decimal_text(operation_cost),
            "operation_latency_ms": operation_latency_ms,
            "operation_cost_source": decisions[0].operation_cost_source,
        },
        "ledger": {
            "initial_cash": _decimal_text(decisions[0].initial_cash),
            "final_cash": _decimal_text(snapshot.cash),
            "final_positions": {
                key: _decimal_text(value)
                for key, value in sorted(snapshot.positions.items())
            },
            "fees_paid": _decimal_text(snapshot.fees_paid),
            "operation_costs_paid": _decimal_text(
                snapshot.operation_costs_paid
            ),
            "operation_cost_semantics": (
                decisions[0].operation_cost_source
            ),
            "final_equity": _decimal_text(snapshot.marked_equity),
            "trace": [_ledger_trace_mapping(entry) for entry in ledger.trace],
            "trace_hash": _sha256_bytes(
                canonical_json_bytes(
                    [_ledger_trace_mapping(entry) for entry in ledger.trace]
                )
            ),
            "reconciled": True,
        },
        "replay_trace": [
            _replay_trace_mapping(entry) for entry in replay.trace
        ],
        "trade_evidence": trade_evidence,
        "counts": {
            "event_count": len(trades),
            "decision_count": len(decisions),
            "explainable_fill_count": len(fill_rows),
            "actual_fill_count": 0,
            "authenticated_fill_count": 0,
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
        },
        "metrics": {
            "initial_capital": _decimal_text(decisions[0].initial_cash),
            "final_equity": _decimal_text(snapshot.marked_equity),
            "fees": _decimal_text(snapshot.fees_paid),
            "operation_costs": _decimal_text(
                snapshot.operation_costs_paid
            ),
            "settled_pnl": _decimal_text(settled_pnl),
            "return": _decimal_text(settled_pnl / decisions[0].initial_cash),
            "turnover": _decimal_text(notional / decisions[0].initial_cash),
            "rewards": "0",
        },
        "safety": {
            "new_orders_disabled": True,
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
            "actual_fill_count": 0,
            "authenticated_fill_count": 0,
        },
    }
    trades_output = io.StringIO(newline="")
    columns = (
        "trade_id",
        "event_id",
        "market_id",
        "decision_id",
        "source_event_id",
        "settlement_source_event_id",
        "filled_at",
        "settled_at",
        "counterfactual",
        "actual_fill",
        "authenticated_fill",
        "evidence_kind",
        "notional",
        "fees",
        "other_costs",
        "pessimistic_cost",
        "claimed_net_pnl",
    )
    writer = csv.DictWriter(
        trades_output,
        fieldnames=columns,
        lineterminator="\n",
    )
    writer.writeheader()
    for row in trade_evidence:
        writer.writerow({name: row.get(name) for name in columns})
    return replay_payload, trades_output.getvalue()


def _validate_capture_freeze_against_replay(
    verification: CaptureFreezeVerification,
    *,
    frozen: _FrozenEvidence,
    target: ShortCryptoTarget,
    decisions: tuple[_Decision, ...],
    trades: tuple[_PublicTrade, ...],
    boundaries: Any,
    settlement_label: Any,
    liveness: _LivenessProof,
) -> _SettlementCommitProof:
    if (
        not verification.verified
        or verification.target is None
        or verification.freeze_id is None
    ):
        raise PromotionRejection(
            "captured_public_freeze_contract_missing",
            "production capture freeze was not verified",
        )
    frozen_paths = tuple(binding.declared_path for binding in frozen.bindings)
    frozen_ids = tuple(
        record_id
        for binding in frozen.bindings
        for record_id in binding.record_ids
    )
    if (
        frozen_paths != verification.source_manifest_paths
        or frozen_ids != verification.record_ids
        or verification.target.slug != target.slug
        or verification.target.condition_id != target.condition_id
        or verification.target.opens_at_ms != target.opens_at_ms
        or verification.target.closes_at_ms != target.closes_at_ms
    ):
        raise PromotionRejection(
            "capture_freeze_replay_binding_mismatch",
            "verified freeze inventory/target differs from replay inputs",
        )
    roles = verification.required_role_ids
    atomic_commit_id = roles["atomic_settlement_commit_record_id"]
    atomic_commit_record = frozen.records_by_id[atomic_commit_id]
    atomic_commit_payload = _event_payload(atomic_commit_record)
    atomic_commit_received_at_ms = _epoch_ms(
        atomic_commit_record.get("received_at"),
        label="atomic settlement commit received_at",
    )
    dependency_role_names = (
        "durable_announcement_record_id",
        "durable_gamma_record_id",
        "durable_chainlink_open_record_id",
        "durable_chainlink_close_record_id",
        "durable_close_pong_before_record_id",
        "durable_close_pong_after_record_id",
    )
    dependency_received_at_ms = tuple(
        _epoch_ms(
            frozen.records_by_id[roles[name]].get("received_at"),
            label=f"{name} received_at",
        )
        for name in dependency_role_names
    )
    if atomic_commit_received_at_ms < max(dependency_received_at_ms):
        raise PromotionRejection(
            "atomic_settlement_commit_not_causal",
            "atomic settlement commit predates a required finalized input",
        )
    gamma_record_ids = _sequence(
        atomic_commit_payload.get("gamma_record_ids"),
        code="atomic_settlement_commit_mismatch",
        label="atomic settlement gamma_record_ids",
    )
    close_liveness = _mapping(
        atomic_commit_payload.get("close_liveness"),
        code="atomic_settlement_commit_mismatch",
        label="atomic settlement close_liveness",
    )
    try:
        committed_open_price = _decimal(
            atomic_commit_payload.get("chainlink_open_price"),
            label="atomic settlement Chainlink open price",
            positive=True,
        )
        committed_close_price = _decimal(
            atomic_commit_payload.get("chainlink_close_price"),
            label="atomic settlement Chainlink close price",
            positive=True,
        )
    except PromotionRejection as exc:
        raise PromotionRejection(
            "atomic_settlement_commit_mismatch",
            "atomic settlement commit contains invalid boundary prices",
        ) from exc
    if (
        roles["durable_gamma_record_id"]
        != settlement_label.gamma_record_id
        or roles["durable_chainlink_open_record_id"]
        != boundaries.open_boundary.record_id
        or roles["durable_chainlink_close_record_id"]
        != boundaries.close_boundary.record_id
        or roles["durable_close_pong_before_record_id"]
        != liveness.before_record_id
        or roles["durable_close_pong_after_record_id"]
        != liveness.after_record_id
        or atomic_commit_payload.get("rule_hash") != target.rule_hash
        or atomic_commit_payload.get("outcome") != settlement_label.outcome
        or atomic_commit_payload.get("winning_token_id")
        != settlement_label.winning_token_id
        or committed_open_price != boundaries.open_boundary.price
        or committed_close_price != boundaries.close_boundary.price
        or settlement_label.gamma_record_id not in gamma_record_ids
        or atomic_commit_payload.get("available_at_ms")
        != settlement_label.available_at_ms
        or atomic_commit_payload.get("chainlink_boundary_status")
        != "committed"
        or close_liveness.get("status") != "verified"
        or close_liveness.get("connection_id") != liveness.connection_id
        or close_liveness.get("before_record_id")
        != liveness.before_record_id
        or close_liveness.get("after_record_id")
        != liveness.after_record_id
        or close_liveness.get("before_received_at_ms")
        != liveness.before_received_at_ms
        or close_liveness.get("after_received_at_ms")
        != liveness.after_received_at_ms
    ):
        raise PromotionRejection(
            "atomic_settlement_commit_mismatch",
            "atomic settlement commit differs from recomputed public evidence",
        )
    submit_payload = _event_payload(
        frozen.records_by_id[
            roles["pessimistic_submit_latency_evidence_id"]
        ]
    )
    cost_payload = _event_payload(
        frozen.records_by_id[
            roles["settlement_operation_cost_evidence_id"]
        ]
    )
    closure_payload = _event_payload(
        frozen.records_by_id[
            roles["decision_to_trade_l2_closure_id"]
        ]
    )
    decision_ids = [row.decision_id for row in decisions]
    if (
        submit_payload.get("decision_record_id")
        not in decision_ids
        or cost_payload.get("decision_record_id") not in decision_ids
        or _checked_int(
            submit_payload.get("latency_ms"),
            label="production submit latency",
        )
        != max(
            PESSIMISTIC_SUBMIT_LATENCY_FLOOR_MS,
            *(row.submit_latency_ms for row in decisions),
        )
        or _decimal(
            cost_payload.get("amount"),
            label="production settlement operation cost",
            positive=True,
        )
        != decisions[0].settlement_operation_cost
        or cost_payload.get("provenance")
        != decisions[0].operation_cost_source
    ):
        raise PromotionRejection(
            "capture_freeze_assumption_mismatch",
            "freeze latency/cost roles differ from immutable decisions",
        )
    closure_decisions = _sequence(
        closure_payload.get("decision_record_ids"),
        code="capture_freeze_assumption_mismatch",
        label="L2 closure decision_record_ids",
    )
    closure_snapshots = _sequence(
        closure_payload.get("public_trade_snapshot_record_ids"),
        code="capture_freeze_assumption_mismatch",
        label="L2 closure public_trade_snapshot_record_ids",
    )
    frozen_trade_snapshot_ids = {
        str(record["record_id"])
        for record in frozen.records
        if record.get("source") == "data_api_trades"
        and isinstance(record.get("payload"), Mapping)
        and isinstance(record["payload"].get("payload"), Mapping)
        and str(
            record["payload"]["payload"].get("condition_id", "")
        ).lower()
        == target.condition_id
    }
    used_trade_snapshot_ids = {
        row.evidence_record_id for row in trades
    }
    if (
        set(closure_decisions) != set(decision_ids)
        or set(closure_snapshots) != frozen_trade_snapshot_ids
        or not used_trade_snapshot_ids.issubset(
            frozen_trade_snapshot_ids
        )
        or _checked_int(
            closure_payload.get("public_trade_count"),
            label="L2 closure public_trade_count",
        )
        < len(trades)
    ):
        raise PromotionRejection(
            "capture_freeze_l2_closure_mismatch",
            "L2 closure does not bind every decision and trade snapshot",
        )
    return _SettlementCommitProof(
        record_id=atomic_commit_id,
        received_at_ms=atomic_commit_received_at_ms,
    )


def _artifact_hash(value: bytes) -> dict[str, Any]:
    return {
        "sha256": _sha256_bytes(value),
        "bytes": len(value),
    }


def _write_bundle(
    *,
    output_root: Path,
    run_id: str,
    payloads: Mapping[str, bytes],
) -> Mapping[str, Path]:
    final_dir = output_root / run_id
    if final_dir.exists():
        if final_dir.is_symlink() or not final_dir.is_dir():
            raise PromotionRejection(
                "existing_content_address_conflict",
                "content-addressed output path is not a regular directory",
            )
        if set(path.name for path in final_dir.iterdir()) != set(payloads):
            raise PromotionRejection(
                "existing_content_address_conflict",
                "existing content-addressed run has a different artifact set",
            )
        for name, expected in payloads.items():
            path = final_dir / name
            if path.is_symlink() or path.read_bytes() != expected:
                raise PromotionRejection(
                    "existing_content_address_conflict",
                    f"existing artifact differs: {name}",
                )
        return MappingProxyType(
            {name: final_dir / name for name in ARTIFACT_NAMES}
        )
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".execution-replay-", dir=output_root)
    )
    try:
        for name in ARTIFACT_NAMES:
            path = temporary / name
            path.write_bytes(payloads[name])
            path.chmod(0o444)
        os.replace(temporary, final_dir)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return MappingProxyType(
        {name: final_dir / name for name in ARTIFACT_NAMES}
    )


def _rejected_result(code: str) -> PromotionResult:
    return PromotionResult(
        verified=False,
        promoted=False,
        status="rejected",
        production_evidence=False,
        classification="insufficient_data",
        run_id=None,
        output_dir=None,
        artifacts=MappingProxyType({}),
        reason_codes=(code,),
        required_frozen_ids=(),
        missing_frozen_roles=(
            PRODUCTION_MISSING_FROZEN_ROLES
            if code == "captured_public_freeze_contract_missing"
            else ()
        ),
    )


def verify_and_promote_execution_replay(
    run_manifest_path: str | Path,
) -> PromotionResult:
    """Verify one frozen run and atomically emit a content-addressed bundle.

    Evidence failures return ``status="rejected"`` with no output directory.
    Programming errors are not hidden.  A valid ``fixture`` request writes a
    deterministic validation bundle but deliberately returns ``promoted=False``;
    only manifest-verified ``captured_public`` evidence can return
    ``promoted_counterfactual``.
    """

    try:
        _guard_is_active()
        request_path = Path(run_manifest_path)
        request, request_bytes, base = _read_request(request_path)
        evidence_mode = request.get("evidence_mode")
        if evidence_mode not in {"fixture", "captured_public"}:
            raise PromotionRejection(
                "evidence_mode_invalid",
                "evidence_mode must be fixture or captured_public",
            )
        freeze_verification: CaptureFreezeVerification | None = None
        if evidence_mode == "captured_public":
            if "production_freeze" not in request:
                raise PromotionRejection(
                    "captured_public_freeze_contract_missing",
                    "captured_public promotion requires a finalized "
                    "full-window capture freeze",
                )
            freeze_verification = verify_production_capture_freeze(
                request_path
            )
            if not freeze_verification.verified:
                raise PromotionRejection(
                    freeze_verification.reason_codes[0],
                    freeze_verification.rejection_detail
                    or "production capture freeze rejected",
                )
        frozen = _load_frozen_evidence(request, base=base)
        target = _strict_target(
            frozen.records,
            expected_slug=(
                None
                if freeze_verification is None
                or freeze_verification.target is None
                else freeze_verification.target.slug
            ),
            expected_condition_id=(
                None
                if freeze_verification is None
                or freeze_verification.target is None
                else freeze_verification.target.condition_id
            ),
        )
        decisions = _decisions(
            frozen,
            target=target,
            evidence_mode=evidence_mode,
        )
        trades = _public_trades(
            frozen,
            target=target,
            evidence_mode=evidence_mode,
        )
        boundaries, reconciliation, settlement_label, liveness = _settlement(
            frozen,
            target=target,
            first_decision=decisions[0],
        )
        settlement_commit: _SettlementCommitProof | None = None
        if freeze_verification is not None:
            settlement_commit = _validate_capture_freeze_against_replay(
                freeze_verification,
                frozen=frozen,
                target=target,
                decisions=decisions,
                trades=trades,
                boundaries=boundaries,
                settlement_label=settlement_label,
                liveness=liveness,
            )
        ignored_claims = tuple(
            sorted(
                {
                    key
                    for key in IGNORED_RESULT_CLAIMS
                    if key in request
                    or any(
                        key in _event_payload(record)
                        for record in frozen.records
                        if record.get("source") == "edge_lab_ghost_decision"
                    )
                }
            )
        )
        replay_payload, trades_csv = _run_replay(
            frozen,
            target=target,
            decisions=decisions,
            trades=trades,
            boundaries=boundaries,
            reconciliation=reconciliation,
            settlement_label=settlement_label,
            liveness=liveness,
            settlement_commit=settlement_commit,
            ignored_claims=ignored_claims,
            evidence_mode=evidence_mode,
        )
        capture_freeze_id = (
            None
            if freeze_verification is None
            else freeze_verification.freeze_id
        )
        replay_payload["capture_freeze_id"] = capture_freeze_id
        request_sha256 = _sha256_bytes(request_bytes)
        _reverify_frozen_bytes(
            frozen,
            request_path=request_path.resolve(strict=True),
            request_sha256=request_sha256,
        )
        source_bindings = [
            {
                "path": binding.declared_path,
                "manifest_sha256": binding.sha256,
                "raw_sha256": binding.raw_sha256,
                "source": binding.source,
                "record_ids": list(binding.record_ids),
            }
            for binding in frozen.bindings
        ]
        quality = {
            "schema_version": QUALITY_SCHEMA,
            "evidence_mode": evidence_mode,
            "fixture_only": evidence_mode == "fixture",
            "production_evidence": evidence_mode == "captured_public",
            "checks": {
                "manifest_hashes": "passed",
                "raw_sha256_bytes_lines": "passed",
                "canonical_record_ids": "passed",
                "path_confinement": "passed",
                "partial_inputs_excluded": "passed",
                "future_l2_references": "passed",
                "visible_queue_rebuilt": "passed",
                "public_trade_volume_shared": "passed",
                "decision_time_fee": "passed",
                "settlement_reconciliation": "passed",
                "ledger_conservation": "passed",
                "new_order_guard": "passed",
                "full_window_capture_inventory": (
                    "passed"
                    if evidence_mode == "captured_public"
                    else "fixture_only_not_production_evidence"
                ),
                "settlement_operation_cost": (
                    "passed"
                    if evidence_mode == "captured_public"
                    else "fixture_only_not_production_evidence"
                ),
            },
            "source_bindings": source_bindings,
            "excluded_claims": list(ignored_claims),
            "classification": "insufficient_data",
            "classification_reasons": replay_payload[
                "classification_reasons"
            ],
            "production_missing_frozen_roles": list(
                ()
                if evidence_mode == "captured_public"
                else PRODUCTION_MISSING_FROZEN_ROLES
            ),
            "capture_freeze_id": capture_freeze_id,
        }
        reproducibility = {
            "schema_version": REPRODUCIBILITY_SCHEMA,
            "input_manifest_sha256": request_sha256,
            "source_bindings": source_bindings,
            "offline_only": True,
            "network_requests": False,
            "capture_freeze_id": capture_freeze_id,
            "writes_outside_content_addressed_output": False,
            "deterministic_policy": {
                "queue_scenario": FIXED_SCENARIO.value,
                "trade_volume_haircut": _decimal_text(FIXED_HAIRCUT),
                "submit_latency_floor_ms": (
                    PESSIMISTIC_SUBMIT_LATENCY_FLOOR_MS
                ),
                "liveness_tolerance_ms": LIVENESS_TOLERANCE_MS,
            },
            "invocation": (
                "verify_and_promote_execution_replay(<RUN_MANIFEST_PATH>)"
            ),
            "byte_identical_replay_expected": True,
        }
        replay_bytes = _json_bytes(replay_payload)
        trades_bytes = trades_csv.encode("utf-8")
        quality_bytes = _json_bytes(quality)
        reproducibility_bytes = _json_bytes(reproducibility)
        addressed = {
            "EXECUTION_REPLAY.json": replay_bytes,
            "TRADES.csv": trades_bytes,
            "DATA_QUALITY.json": quality_bytes,
            "REPRODUCIBILITY.json": reproducibility_bytes,
        }
        address_payload = {
            "schema_version": RUN_SCHEMA,
            "input_manifest_sha256": request_sha256,
            "source_manifest_sha256s": [
                binding.sha256 for binding in frozen.bindings
            ],
            "capture_freeze_id": capture_freeze_id,
            "artifacts": {
                name: _artifact_hash(value)
                for name, value in sorted(addressed.items())
            },
        }
        run_id = _sha256_bytes(canonical_json_bytes(address_payload))
        run_manifest_core = {
            **address_payload,
            "run_id": run_id,
            "evidence_mode": evidence_mode,
            "promotion_status": (
                "fixture_validated"
                if evidence_mode == "fixture"
                else "promoted_counterfactual"
            ),
            "classification": "insufficient_data",
            "counterfactual": True,
            "actual_fill": False,
            "authenticated_fill": False,
            "safety": {
                "new_orders_disabled": True,
                "orders_submitted": 0,
                "authenticated_endpoints_used": 0,
            },
        }
        run_manifest = {
            **run_manifest_core,
            "run_manifest_core_sha256": _sha256_bytes(
                canonical_json_bytes(run_manifest_core)
            ),
        }
        payloads = {
            "RUN_MANIFEST.json": _json_bytes(run_manifest),
            **addressed,
        }
        output_root = _confined_path(
            base,
            request.get("output_root", "phase2_execution_runs"),
            label="output_root",
            must_exist=False,
        )
        if output_root.exists() and output_root.is_symlink():
            raise PromotionRejection(
                "symlink_forbidden",
                "output_root cannot be a symlink",
            )
        artifacts = _write_bundle(
            output_root=output_root,
            run_id=run_id,
            payloads=payloads,
        )
        status: Literal["fixture_validated", "promoted_counterfactual"] = (
            "fixture_validated"
            if evidence_mode == "fixture"
            else "promoted_counterfactual"
        )
        return PromotionResult(
            verified=True,
            promoted=evidence_mode == "captured_public",
            status=status,
            production_evidence=evidence_mode == "captured_public",
            classification="insufficient_data",
            run_id=run_id,
            output_dir=output_root / run_id,
            artifacts=artifacts,
            reason_codes=(
                "event_count_below_100",
                "explainable_fill_count_below_100",
            ),
            required_frozen_ids=tuple(sorted(frozen.records_by_id)),
            missing_frozen_roles=(
                ()
                if evidence_mode == "captured_public"
                else PRODUCTION_MISSING_FROZEN_ROLES
            ),
        )
    except PromotionRejection as exc:
        return _rejected_result(exc.code)
