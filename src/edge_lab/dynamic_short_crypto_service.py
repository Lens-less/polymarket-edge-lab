"""Bounded, credential-free dynamic capture for short crypto markets.

The service discovers only finalized public ``new_market`` JSONL batches,
freezes every worker's token scope, and records public CLOB/Gamma evidence.
It intentionally has no trading or authenticated-user integration.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import time
import uuid
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .capture_runtime import (
    CaptureStoreRecorderSink,
    PublicSourcesSnapshotClient,
)
from .chainlink_ptb import (
    FinalizedChainlinkBoundaries,
    FinalizedChainlinkBoundary,
    FinalizedChainlinkBoundaryRequest,
    PriceToBeatRejection,
    extract_chainlink_boundary_batch_from_finalized_manifests,
    require_price_to_beat_for_decision,
)
from .compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from .data_api_trade_identity import data_api_trade_event_key
from .data_store import (
    CaptureStore,
    canonical_json_bytes,
    canonical_record_id,
)
from .dynamic_short_crypto_capture import (
    CaptureDecision,
    CaptureDecisionAction,
    CaptureSupervisorError,
    DynamicShortCryptoCaptureSupervisor,
    TargetLifecycleState,
)
from .dynamic_short_crypto_recovery import (
    CachedRecoveryOutcome,
    RecoveryResult,
    recover_dynamic_short_crypto_runs,
    recover_dynamic_short_crypto_runs_cached,
)
from .lifecycle_cohort_contract import (
    REMEDIATION_COHORT_SCHEMA,
    REMEDIATION_EVENT_TYPE,
    ROOT_COHORT_SCHEMA,
    ROOT_EVENT_TYPE,
    LifecycleRemediationPlan,
)
from .network_safety import safe_error_details, sanitized_response_headers
from .recorder import PublicRecorder, RecorderConfig
from .short_crypto_catalog import ShortCryptoTarget
from .short_crypto_gamma import (
    GammaReconciliationError,
    ShortCryptoGamma,
    is_strict_gamma_preactivation,
    reconcile_short_crypto_gamma,
)
from .short_crypto_registry import (
    build_short_crypto_registry,
    merge_short_crypto_registry_snapshots,
    validate_short_crypto_registry_snapshot,
)
from .short_crypto_settlement import (
    AtomicSettlementGate,
    CloseLivenessProof as StrictCloseLivenessProof,
    ClosePongObservation,
    SettlementCommitReceipt,
    SettlementRejection,
    reconcile_short_crypto_settlement,
)
from .sources import Fetched, PublicSourceError


class DiscoveryInputError(ValueError):
    """A supposedly finalized discovery batch failed immutable validation."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class _FinalizedDiscoveryInput:
    path: Path
    sha256: str
    byte_count: int
    line_count: int
    may_contain_short_crypto_announcement: bool


@dataclass(frozen=True)
class _InboundLivenessObservation:
    session_id: str
    connection_id: str
    received_at_ms: int
    monotonic_ns: int
    record_id: str


@dataclass(frozen=True)
class _CloseLivenessProof:
    connection_id: str
    before: _InboundLivenessObservation
    after: _InboundLivenessObservation


@dataclass(frozen=True)
class _PersistedBookObservation:
    record_id: str
    received_at_ms: int
    monotonic_ns: int
    connection_id: str
    market_id: str
    market_revision: int
    connection_revision: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class _PersistedFeeObservation:
    record_id: str
    received_at_ms: int
    market: Mapping[str, Any]


def _stable_read(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            content = handle.read()
            after = os.fstat(handle.fileno())
        current = path.stat()
    except OSError as exc:
        raise DiscoveryInputError(
            "finalized_input_unreadable",
            f"cannot read finalized discovery artifact: {path}",
        ) from exc
    identities = {
        (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
        )
        for item in (before, after, current)
    }
    if len(identities) != 1 or len(content) != after.st_size:
        raise DiscoveryInputError(
            "finalized_input_changed",
            f"discovery artifact changed during scan: {path}",
        )
    return content


_SHORT_CRYPTO_DESCRIPTOR_KEYS = frozenset(
    {"clob_token_ids", "condition_id", "outcomes", "slug"}
)
_SHORT_CRYPTO_SLUG_MARKERS = (
    b'"slug":"btc-updown-',
    b'"slug":"eth-updown-',
)


def _descriptor_may_contain_short_crypto_announcement(
    value: Any,
) -> bool:
    if isinstance(value, Mapping):
        object_shape = value.get("object")
        if (
            isinstance(object_shape, Mapping)
            and _SHORT_CRYPTO_DESCRIPTOR_KEYS <= set(object_shape)
        ):
            return True
        return any(
            _descriptor_may_contain_short_crypto_announcement(item)
            for item in value.values()
        )
    if isinstance(value, list):
        return any(
            _descriptor_may_contain_short_crypto_announcement(item)
            for item in value
        )
    return False


def _manifest_proves_no_short_crypto_announcement(
    manifest: Mapping[str, Any],
    *,
    raw: bytes,
    line_count: int,
) -> bool:
    if any(marker in raw for marker in _SHORT_CRYPTO_SLUG_MARKERS):
        return False
    fingerprints = manifest.get("schema_fingerprints")
    if line_count == 0:
        return fingerprints in (None, [])
    if not isinstance(fingerprints, list) or not fingerprints:
        return False
    described_line_count = 0
    for fingerprint in fingerprints:
        if not isinstance(fingerprint, Mapping):
            return False
        count = fingerprint.get("count")
        descriptor = fingerprint.get("descriptor")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or not isinstance(descriptor, Mapping)
        ):
            return False
        described_line_count += count
        if _descriptor_may_contain_short_crypto_announcement(descriptor):
            return False
    return described_line_count == line_count


def _validate_finalized_raw(
    path: Path,
    manifest_path: Path,
) -> _FinalizedDiscoveryInput:
    raw = _stable_read(path)
    manifest_bytes = _stable_read(manifest_path)
    try:
        manifest: Any = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscoveryInputError(
            "finalized_manifest_invalid",
            f"capture manifest is not valid UTF-8 JSON: {manifest_path}",
        ) from exc
    checksum = manifest.get("checksum") if isinstance(manifest, dict) else None
    expected_relative = f"raw/{path.parent.name}/{path.name}"
    if (
        not isinstance(manifest, dict)
        or manifest.get("manifest_version") != "capture-manifest.v1"
        or manifest.get("schema_version") != "edge-lab-recorder.raw.v1"
        or manifest.get("source") != path.parent.name
        or manifest.get("batch_id") != path.stem
        or manifest.get("raw_path") != expected_relative
        or not isinstance(checksum, dict)
        or checksum.get("algorithm") != "sha256"
    ):
        raise DiscoveryInputError(
            "finalized_manifest_invalid",
            f"capture manifest identity does not match raw batch: {path}",
        )
    line_count = raw.count(b"\n")
    if raw and not raw.endswith(b"\n"):
        raise DiscoveryInputError(
            "finalized_checksum_mismatch",
            f"finalized JSONL lacks a terminal newline: {path}",
        )
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        checksum.get("value") != raw_sha256
        or checksum.get("bytes") != len(raw)
        or checksum.get("lines") != line_count
        or manifest.get("record_count") != line_count
    ):
        raise DiscoveryInputError(
            "finalized_checksum_mismatch",
            f"capture manifest checksum does not match raw batch: {path}",
        )
    return _FinalizedDiscoveryInput(
        path=path.resolve(),
        sha256=raw_sha256,
        byte_count=len(raw),
        line_count=line_count,
        may_contain_short_crypto_announcement=(
            not _manifest_proves_no_short_crypto_announcement(
                manifest,
                raw=raw,
                line_count=line_count,
            )
        ),
    )


def _unrelated_registry_snapshot(
    inputs: tuple[_FinalizedDiscoveryInput, ...],
) -> dict[str, Any]:
    descriptors = [
        {
            "path": str(item.path),
            "sha256": item.sha256,
            "byte_count": item.byte_count,
            "line_count": item.line_count,
        }
        for item in sorted(inputs, key=lambda candidate: str(candidate.path))
    ]
    line_count = sum(item.line_count for item in inputs)
    snapshot: dict[str, Any] = {
        "schema_version": "edge-lab.short-crypto-registry.v1",
        "inputs": descriptors,
        "targets": [],
        "rejections": [],
        "completeness": {
            "status": "complete",
            "input_file_count": len(inputs),
            "input_line_count": line_count,
            "parsed_object_count": line_count,
            "target_announcement_count": 0,
            "unique_target_count": 0,
            "duplicate_target_announcement_count": 0,
            "unrelated_record_count": line_count,
            "rejected_record_count": 0,
            "rejection_counts": {},
        },
    }
    snapshot["snapshot_sha256"] = hashlib.sha256(
        canonical_json_bytes(snapshot)
    ).hexdigest()
    return snapshot


def _build_registry_delta(
    inputs: tuple[_FinalizedDiscoveryInput, ...],
) -> dict[str, Any]:
    candidates = tuple(
        item
        for item in inputs
        if item.may_contain_short_crypto_announcement
    )
    unrelated = tuple(
        item
        for item in inputs
        if not item.may_contain_short_crypto_announcement
    )
    candidate_snapshot = (
        build_short_crypto_registry(
            tuple(item.path for item in candidates)
        )
        if candidates
        else None
    )
    unrelated_snapshot = (
        _unrelated_registry_snapshot(unrelated)
        if unrelated
        else None
    )
    if candidate_snapshot is None:
        assert unrelated_snapshot is not None
        return unrelated_snapshot
    if unrelated_snapshot is None:
        return candidate_snapshot
    return merge_short_crypto_registry_snapshots(
        candidate_snapshot,
        unrelated_snapshot,
    )


def freeze_finalized_discovery_paths(directory: str | Path) -> tuple[Path, ...]:
    """Return one deterministic, manifest-verified scan of finalized JSONL."""

    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("discovery directory must exist")
    candidates = tuple(
        sorted(
            (
                path
                for path in root.iterdir()
                if path.suffix == ".jsonl"
                and path.is_file()
                and not path.is_symlink()
            ),
            key=lambda item: str(item.resolve()),
        )
    )
    finalized: list[Path] = []
    for path in candidates:
        manifest_path = path.with_suffix(".manifest.json")
        # RawBatchWriter promotes the raw file immediately before publishing
        # its manifest.  Absence therefore means "not yet finalized", not bad
        # data, and this scan simply waits for the next poll.
        if not manifest_path.is_file() or manifest_path.is_symlink():
            continue
        finalized.append(
            _validate_finalized_raw(path, manifest_path).path
        )
    return tuple(finalized)


def _verify_live_order_guard() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("new-order safety guard is not active")


def _require_clean_output_integrity(
    report: Mapping[str, Any],
    *,
    root: Path,
) -> None:
    failures = {
        key: value
        for key, value in report.items()
        if isinstance(value, list) and value
    }
    if failures:
        raise DiscoveryInputError(
            "output_integrity_failed",
            (
                f"capture output is not a clean append-only run root: {root}; "
                f"failure_classes={','.join(sorted(failures))}"
            ),
        )


def _iso_from_epoch_ms(epoch_ms: int) -> str:
    seconds, milliseconds = divmod(epoch_ms, 1_000)
    value = datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
        milliseconds=milliseconds
    )
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _epoch_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000)


def _same_contract(
    first: ShortCryptoTarget,
    second: ShortCryptoTarget,
) -> bool:
    ignored = {"announced_at", "announcement_record_id"}
    return all(
        getattr(first, field) == getattr(second, field)
        for field in first.__dataclass_fields__
        if field not in ignored
    )


@dataclass(frozen=True)
class DynamicShortCryptoServiceConfig:
    """Explicit public-only paths and bounded scheduling parameters."""

    discovery_dir: Path
    output_root: Path
    rtds_manifest_dir: Path | None = None
    subscribe_lead_ms: int = 300_000
    scan_interval_seconds: float = 30.0
    gamma_poll_interval_seconds: float = 300.0
    settlement_timeout_ms: int = 3_600_000
    close_liveness_tolerance_ms: int = 30_000
    clob_snapshot_interval_seconds: float = 30.0
    max_records_per_batch: int = 5_000
    max_assets_per_group: int | None = None
    recovery_run_roots: tuple[Path, ...] = ()
    recovery_snapshot_path: Path | None = None
    recovery_cpu_ratio: float | None = None
    lifecycle_remediation_plan: LifecycleRemediationPlan | None = None

    def __post_init__(self) -> None:
        discovery = Path(self.discovery_dir).expanduser().resolve()
        output = Path(self.output_root).expanduser().resolve()
        if not discovery.is_dir():
            raise ValueError("discovery_dir must be an existing directory")
        if (
            output == discovery
            or output in discovery.parents
            or discovery in output.parents
        ):
            raise ValueError(
                "output_root must be separate from the discovery tree"
            )
        integer_values = {
            "subscribe_lead_ms": self.subscribe_lead_ms,
            "settlement_timeout_ms": self.settlement_timeout_ms,
            "close_liveness_tolerance_ms": (
                self.close_liveness_tolerance_ms
            ),
            "max_records_per_batch": self.max_records_per_batch,
        }
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in integer_values.values()
        ):
            raise ValueError("service integer limits must be positive")
        if (
            self.max_assets_per_group is not None
            and (
                isinstance(self.max_assets_per_group, bool)
                or not isinstance(self.max_assets_per_group, int)
                or self.max_assets_per_group < 2
            )
        ):
            raise ValueError(
                "max_assets_per_group must be None or an integer of at least 2"
            )
        interval_values = {
            "scan_interval_seconds": self.scan_interval_seconds,
            "gamma_poll_interval_seconds": (
                self.gamma_poll_interval_seconds
            ),
            "clob_snapshot_interval_seconds": (
                self.clob_snapshot_interval_seconds
            ),
        }
        if any(
            not math.isfinite(float(value)) or float(value) <= 0
            for value in interval_values.values()
        ):
            raise ValueError("service intervals must be positive and finite")
        object.__setattr__(self, "discovery_dir", discovery)
        object.__setattr__(self, "output_root", output)
        rtds_manifest_dir = (
            None
            if self.rtds_manifest_dir is None
            else Path(self.rtds_manifest_dir).expanduser().resolve()
        )
        if (
            rtds_manifest_dir is not None
            and not rtds_manifest_dir.is_dir()
        ):
            raise ValueError(
                "rtds_manifest_dir must be an existing directory"
            )
        if (
            rtds_manifest_dir is not None
            and (
                output == rtds_manifest_dir
                or output in rtds_manifest_dir.parents
                or rtds_manifest_dir in output.parents
            )
        ):
            raise ValueError(
                "output_root must be separate from the RTDS evidence tree"
            )
        object.__setattr__(
            self,
            "rtds_manifest_dir",
            rtds_manifest_dir,
        )
        recovery_roots = tuple(
            Path(path).expanduser().resolve()
            for path in self.recovery_run_roots
        )
        if len(set(recovery_roots)) != len(recovery_roots):
            raise ValueError("recovery_run_roots must be unique")
        if any(not path.is_dir() for path in recovery_roots):
            raise ValueError(
                "recovery_run_roots must contain existing directories"
            )
        if output in recovery_roots:
            raise ValueError(
                "the active output_root cannot be a recovery input"
            )
        object.__setattr__(
            self,
            "recovery_run_roots",
            recovery_roots,
        )
        recovery_snapshot_path = (
            None
            if self.recovery_snapshot_path is None
            else Path(
                os.path.abspath(
                    Path(self.recovery_snapshot_path).expanduser()
                )
            )
        )
        if (
            recovery_snapshot_path is not None
            and (
                recovery_snapshot_path == output
                or output in recovery_snapshot_path.parents
            )
        ):
            raise ValueError(
                "recovery_snapshot_path must be outside the active run root"
            )
        if self.recovery_cpu_ratio is not None and (
            isinstance(self.recovery_cpu_ratio, bool)
            or not isinstance(self.recovery_cpu_ratio, (int, float))
            or not 0 < float(self.recovery_cpu_ratio) <= 1
        ):
            raise ValueError(
                "recovery_cpu_ratio must be None or in the interval (0, 1]"
            )
        object.__setattr__(
            self,
            "recovery_snapshot_path",
            recovery_snapshot_path,
        )
        object.__setattr__(
            self,
            "recovery_cpu_ratio",
            (
                None
                if self.recovery_cpu_ratio is None
                else float(self.recovery_cpu_ratio)
            ),
        )
        if (
            self.lifecycle_remediation_plan is not None
            and not isinstance(
                self.lifecycle_remediation_plan,
                LifecycleRemediationPlan,
            )
        ):
            raise ValueError(
                "lifecycle_remediation_plan must be a frozen plan"
            )


class _ObservedRecorderSink:
    """Persist first, then expose the initial resnapshot readiness proof."""

    def __init__(
        self,
        sink: CaptureStoreRecorderSink,
        *,
        asset_ids: tuple[str, ...],
    ) -> None:
        self._sink = sink
        self._asset_ids = asset_ids
        self.ready = asyncio.Event()
        self.ready_at_ms: int | None = None
        self.watermark_ns: int | None = None
        self.lifecycle_events: deque[dict[str, Any]] = deque()
        self._inbound_by_connection: dict[
            str,
            deque[_InboundLivenessObservation],
        ] = {}
        self._resync_by_connection: dict[
            str,
            _InboundLivenessObservation,
        ] = {}
        self._gap_start_by_connection: dict[str, int] = {}
        self._liveness_updates_inflight = 0
        self._latest_books: dict[str, _PersistedBookObservation] = {}
        self._latest_fees: dict[str, _PersistedFeeObservation] = {}
        self._l2_revision_by_market: dict[str, int] = {}
        self._lifecycle_revision_by_connection: dict[str, int] = {}
        self._emit_lock = asyncio.Lock()

    @property
    def startup_integrity_report(self) -> dict[str, Any]:
        return self._sink.startup_integrity_report

    def close_liveness_proof(
        self,
        *,
        closes_at_ms: int,
        tolerance_ms: int,
        as_of_ms: int,
    ) -> _CloseLivenessProof | None:
        """Return one persisted same-connection PONG bracket around close."""

        if self._liveness_updates_inflight:
            return None
        for connection_id in sorted(self._inbound_by_connection):
            observations = self._inbound_by_connection[connection_id]
            before = next(
                (
                    item
                    for item in reversed(observations)
                    if item.received_at_ms <= closes_at_ms
                ),
                None,
            )
            after = next(
                (
                    item
                    for item in observations
                    if item.received_at_ms >= closes_at_ms
                ),
                None,
            )
            if (
                before is None
                or after is None
                or closes_at_ms - before.received_at_ms > tolerance_ms
                or after.received_at_ms - closes_at_ms > tolerance_ms
                or after.received_at_ms > as_of_ms
                or before.record_id == after.record_id
                or before.monotonic_ns >= after.monotonic_ns
                or (
                    after.monotonic_ns - before.monotonic_ns
                    > 2 * tolerance_ms * 1_000_000
                )
            ):
                continue
            resync = self._resync_by_connection.get(connection_id)
            if (
                resync is None
                or resync.monotonic_ns > before.monotonic_ns
            ):
                continue
            gap_started_at_ms = self._gap_start_by_connection.get(
                connection_id
            )
            if (
                gap_started_at_ms is not None
                and gap_started_at_ms < closes_at_ms
            ):
                continue
            return _CloseLivenessProof(
                connection_id=connection_id,
                before=before,
                after=after,
            )
        return None

    def ghost_decision_inputs(
        self,
        target: ShortCryptoTarget,
        *,
        now_ms: int,
        max_book_age_ms: int,
    ) -> tuple[
        _PersistedBookObservation,
        _PersistedFeeObservation,
        _InboundLivenessObservation,
    ] | None:
        """Return a durable, decision-vintage L2/fee/resync tuple."""

        book = self._latest_books.get(target.up_token_id)
        fee = self._latest_fees.get(target.condition_id)
        if book is None or fee is None:
            return None
        resync = self._resync_by_connection.get(book.connection_id)
        if (
            resync is None
            or resync.monotonic_ns >= book.monotonic_ns
            or book.received_at_ms > now_ms
            or fee.received_at_ms > now_ms
            or now_ms - book.received_at_ms > max_book_age_ms
            or book.market_id != target.condition_id
            or book.market_revision
            != self._l2_revision_by_market.get(target.condition_id, 0)
            or book.connection_revision
            != self._lifecycle_revision_by_connection.get(
                book.connection_id,
                0,
            )
            or not self.ready.is_set()
        ):
            return None
        return book, fee, resync

    async def emit(self, record: Mapping[str, Any]) -> Any:
        async with self._emit_lock:
            return await self._emit_locked(record)

    async def emit_ghost_decision_if_current(
        self,
        record: Mapping[str, Any],
        *,
        target: ShortCryptoTarget,
        now_ms: int,
        max_book_age_ms: int,
        expected_inputs: tuple[
            _PersistedBookObservation,
            _PersistedFeeObservation,
            _InboundLivenessObservation,
        ],
    ) -> Any | None:
        """Persist a decision only if its durable inputs are still current."""

        async with self._emit_lock:
            if (
                self.ghost_decision_inputs(
                    target,
                    now_ms=now_ms,
                    max_book_age_ms=max_book_age_ms,
                )
                != expected_inputs
            ):
                return None
            return await self._emit_locked(record)

    async def _emit_locked(self, record: Mapping[str, Any]) -> Any:
        source = record.get("source")
        event_type = record.get("event_type")
        tracks_liveness = (
            source == "clob_market_ws"
            and event_type
            in {
                "heartbeat_ack",
                "resync_complete",
                "disconnected",
            }
        )
        if tracks_liveness:
            self._liveness_updates_inflight += 1
        try:
            result = await self._sink.emit(record)
            return await self._observe_persisted_record(record, result)
        finally:
            if tracks_liveness:
                self._liveness_updates_inflight -= 1

    async def _observe_persisted_record(
        self,
        record: Mapping[str, Any],
        result: Any,
    ) -> Any:
        source = record.get("source")
        event_type = record.get("event_type")
        connection_id = record.get("connection_id")
        session_id = record.get("session_id")
        received_at = record.get("received_at")
        monotonic_ns = record.get("monotonic_ns")
        valid_transport_observation = (
            source == "clob_market_ws"
            and isinstance(session_id, str)
            and bool(session_id)
            and isinstance(connection_id, str)
            and bool(connection_id)
            and isinstance(received_at, str)
            and isinstance(monotonic_ns, int)
            and not isinstance(monotonic_ns, bool)
            and monotonic_ns >= 0
        )
        observation = (
            _InboundLivenessObservation(
                session_id=session_id,
                connection_id=connection_id,
                received_at_ms=_epoch_ms(received_at),
                monotonic_ns=monotonic_ns,
                record_id=result.record_id,
            )
            if valid_transport_observation
            else None
        )
        valid_lifecycle = (
            observation is not None
            and record.get("schema_version")
            == "edge-lab-recorder.lifecycle.v1"
            and record.get("kind") == "lifecycle"
            and isinstance(record.get("detail"), Mapping)
        )
        if source == "clob_market_ws":
            payload = record.get("payload")
            market_id = (
                payload.get("market")
                if isinstance(payload, Mapping)
                else None
            )
            if (
                event_type
                in {
                    "book",
                    "last_trade_price",
                    "price_change",
                    "tick_size_change",
                }
                and isinstance(market_id, str)
                and market_id
            ):
                normalized_market_id = market_id.lower()
                self._l2_revision_by_market[normalized_market_id] = (
                    self._l2_revision_by_market.get(
                        normalized_market_id,
                        0,
                    )
                    + 1
                )
            if (
                event_type in {"disconnected", "resync_complete"}
                and isinstance(connection_id, str)
                and connection_id
            ):
                self._lifecycle_revision_by_connection[connection_id] = (
                    self._lifecycle_revision_by_connection.get(
                        connection_id,
                        0,
                    )
                    + 1
                )
        valid_heartbeat_ack = (
            event_type == "heartbeat_ack"
            and observation is not None
            and record.get("schema_version")
            == "edge-lab-recorder.heartbeat-ack.v1"
            and isinstance(record.get("raw_frame"), str)
            and record["raw_frame"].strip().upper() == "PONG"
        )
        if valid_heartbeat_ack:
            history = self._inbound_by_connection.setdefault(
                observation.connection_id,
                deque(maxlen=4_096),
            )
            history.append(observation)
        if (
            source == "clob_market_ws"
            and event_type == "resync_complete"
        ):
            detail = record.get("detail")
            watermark = (
                detail.get("watermark_ns")
                if isinstance(detail, Mapping)
                else None
            )
            asset_watermarks = (
                detail.get("asset_watermarks_ns")
                if isinstance(detail, Mapping)
                else None
            )
            if (
                isinstance(watermark, int)
                and not isinstance(watermark, bool)
                and watermark >= 0
                and isinstance(asset_watermarks, Mapping)
                and set(asset_watermarks) == set(self._asset_ids)
                and all(
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                    for value in asset_watermarks.values()
                )
                and watermark == max(asset_watermarks.values())
                and valid_lifecycle
                and observation is not None
            ):
                checkpoint = {
                    "schema_version": (
                        "edge-lab-dynamic-short-crypto-readiness.v1"
                    ),
                    "connection_id": observation.connection_id,
                    "resync_complete_record_id": result.record_id,
                    "updated_at": record["received_at"],
                }
                await self._sink.checkpoint("clob_http", checkpoint)
                await self._sink.checkpoint(
                    "clob_market_ws",
                    checkpoint,
                )
                self.watermark_ns = watermark
                self.ready_at_ms = observation.received_at_ms
                self.ready.set()
                self._resync_by_connection[
                    observation.connection_id
                ] = observation
                self.lifecycle_events.append(
                    {
                        "event_type": "resync_complete",
                        "received_at_ms": self.ready_at_ms,
                        "monotonic_ns": observation.monotonic_ns,
                        "watermark_ns": watermark,
                        "asset_watermarks_ns": dict(asset_watermarks),
                        "record_id": result.record_id,
                        "connection_id": observation.connection_id,
                    }
                )
        elif (
            source == "clob_market_ws"
            and event_type == "disconnected"
        ):
            if valid_lifecycle and observation is not None:
                history = self._inbound_by_connection.get(
                    observation.connection_id,
                    (),
                )
                gap_evidence = (
                    history[-1]
                    if history
                    else self._resync_by_connection.get(
                        observation.connection_id
                    )
                )
                gap_started_at_ms = (
                    observation.received_at_ms
                    if gap_evidence is None
                    else gap_evidence.received_at_ms
                )
                gap_started_monotonic_ns = (
                    observation.monotonic_ns
                    if gap_evidence is None
                    else gap_evidence.monotonic_ns
                )
                self._gap_start_by_connection[
                    observation.connection_id
                ] = gap_started_at_ms
                self.ready.clear()
                self.ready_at_ms = None
                self.watermark_ns = None
                self.lifecycle_events.append(
                    {
                        "event_type": "disconnected",
                        "received_at_ms": observation.received_at_ms,
                        "monotonic_ns": observation.monotonic_ns,
                        "gap_started_at_ms": gap_started_at_ms,
                        "gap_started_monotonic_ns": (
                            gap_started_monotonic_ns
                        ),
                        "gap_evidence_record_id": (
                            result.record_id
                            if gap_evidence is None
                            else gap_evidence.record_id
                        ),
                        "watermark_ns": None,
                        "record_id": result.record_id,
                        "connection_id": observation.connection_id,
                    }
                )
        if (
            source == "clob_market_ws"
            and event_type == "book"
            and record.get("schema_version") == "clob-market-ws.book.v1"
            and record.get("kind") == "data"
            and isinstance(record.get("payload"), Mapping)
            and isinstance(record.get("connection_id"), str)
            and isinstance(record.get("monotonic_ns"), int)
            and isinstance(record.get("received_at"), str)
        ):
            payload = record["payload"]
            asset_id = payload.get("asset_id")
            market_id = payload.get("market")
            if (
                isinstance(asset_id, str)
                and asset_id in self._asset_ids
                and isinstance(market_id, str)
                and market_id
            ):
                normalized_market_id = market_id.lower()
                self._latest_books[asset_id] = _PersistedBookObservation(
                    record_id=result.record_id,
                    received_at_ms=_epoch_ms(record["received_at"]),
                    monotonic_ns=record["monotonic_ns"],
                    connection_id=record["connection_id"],
                    market_id=normalized_market_id,
                    market_revision=self._l2_revision_by_market.get(
                        normalized_market_id,
                        0,
                    ),
                    connection_revision=(
                        self._lifecycle_revision_by_connection.get(
                            record["connection_id"],
                            0,
                        )
                    ),
                    payload=dict(payload),
                )
        if (
            source == "clob_http"
            and event_type == "clob_snapshot"
            and record.get("schema_version") == "clob-http.snapshot.v1"
            and record.get("kind") in {"snapshot", "data"}
            and isinstance(record.get("payload"), Mapping)
            and isinstance(record.get("received_at"), str)
        ):
            responses = record["payload"].get("responses")
            if isinstance(responses, list):
                for response in responses:
                    if (
                        not isinstance(response, Mapping)
                        or response.get("resource") != "clob_market"
                        or not isinstance(
                            response.get("raw_json"),
                            Mapping,
                        )
                    ):
                        continue
                    market = response["raw_json"]
                    condition_id = market.get(
                        "c",
                        market.get("condition_id"),
                    )
                    if isinstance(condition_id, str):
                        self._latest_fees[
                            condition_id.lower()
                        ] = _PersistedFeeObservation(
                            record_id=result.record_id,
                            received_at_ms=_epoch_ms(
                                record["received_at"]
                            ),
                            market=dict(market),
                        )
        return result

    async def checkpoint(
        self,
        source: str,
        checkpoint: Mapping[str, Any],
    ) -> None:
        await self._sink.checkpoint(source, checkpoint)

    async def close(self) -> tuple[dict[str, Any], ...]:
        return await self._sink.close()


@dataclass
class _WorkerHandle:
    decision: CaptureDecision
    recorder: Any
    sink: _ObservedRecorderSink
    task: asyncio.Task[Any]
    generation: int
    acknowledged: bool = False
    disconnected_at_ms: int | None = None
    disconnect_record_id: str | None = None


@dataclass
class _LiveChainlinkScan:
    manifests: tuple[Path, ...]
    requests: dict[str, FinalizedChainlinkBoundaryRequest]
    plan: tuple[
        tuple[
            tuple[Path, ...],
            dict[str, FinalizedChainlinkBoundaryRequest],
        ],
        ...,
    ]
    deferred_request_ids: set[str]
    next_index: int = 0
    task: asyncio.Task[
        dict[str, FinalizedChainlinkBoundary | None]
    ] | None = None


class DynamicShortCryptoService:
    """Discover, verify, and capture immutable short-crypto target groups."""

    def __init__(
        self,
        config: DynamicShortCryptoServiceConfig,
        *,
        source_client: Any,
        websocket_factory: Any,
        recorder_factory: Any = PublicRecorder,
    ) -> None:
        _verify_live_order_guard()
        self.config = config
        self.source_client = source_client
        self.websocket_factory = websocket_factory
        self.recorder_factory = recorder_factory
        self.supervisor = DynamicShortCryptoCaptureSupervisor(
            subscribe_lead_ms=config.subscribe_lead_ms,
            max_assets_per_group=config.max_assets_per_group,
        )
        self._session_id = uuid.uuid4().hex
        self._processed_paths: set[Path] = set()
        self._targets: dict[str, ShortCryptoTarget] = {}
        self._verified: dict[str, ShortCryptoGamma] = {}
        self._rejected: dict[str, str] = {}
        self._last_gamma_poll_ms: dict[str, int] = {}
        self._gamma_records: dict[str, list[dict[str, Any]]] = {}
        self._committed_gamma_verification_slugs: set[str] = set()
        self._label_status: dict[str, str] = {}
        self._label_tracking_complete: set[str] = set()
        self._last_settlement_fingerprint: dict[str, str] = {}
        self._chainlink_boundaries: dict[
            str,
            dict[str, FinalizedChainlinkBoundary],
        ] = {}
        self._chainlink_commit_record_ids: dict[
            str,
            dict[str, str],
        ] = {}
        self._chainlink_pending_boundaries: dict[
            str,
            FinalizedChainlinkBoundary,
        ] = {}
        self._chainlink_known_request_ids: set[str] = set()
        self._chainlink_scanned_manifest_paths: set[Path] = set()
        self._live_chainlink_scan: _LiveChainlinkScan | None = None
        self._settlement_gates: dict[str, AtomicSettlementGate] = {}
        self._ghost_decision_record_ids: dict[str, str] = {}
        self._ghost_decision_at_ms: dict[str, int] = {}
        self._ghost_decision_exclusions: dict[str, str] = {}
        self._last_data_trade_poll_ms: dict[str, int] = {}
        self._data_api_trade_record_ids: dict[str, list[str]] = {}
        self._data_api_trade_keys: dict[str, set[str]] = {}
        self._production_role_record_ids: dict[
            str,
            dict[str, str],
        ] = {}
        self._data_api_trade_snapshot_count = 0
        self._persisted_decision_ids: set[str] = set()
        self._workers: dict[str, _WorkerHandle] = {}
        self._closed = False
        self._closing = False
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._last_registry_hash: str | None = None
        self._registry_snapshot: dict[str, Any] | None = None
        self._recovery_anchor_payload: dict[str, Any] | None = None
        if not config.recovery_run_roots:
            self._recovery_result: RecoveryResult | None = None
        elif config.recovery_snapshot_path is not None:
            cached_outcome: CachedRecoveryOutcome = (
                recover_dynamic_short_crypto_runs_cached(
                    config.recovery_run_roots,
                    settlement_timeout_ms=config.settlement_timeout_ms,
                    snapshot_path=config.recovery_snapshot_path,
                    cpu_ratio=config.recovery_cpu_ratio,
                )
            )
            self._recovery_result = cached_outcome.result
            self._recovery_anchor_payload = dict(
                cached_outcome.anchor_payload
            )
        else:
            recovery_arguments: dict[str, Any] = {
                "settlement_timeout_ms": config.settlement_timeout_ms,
            }
            if config.recovery_cpu_ratio is not None:
                recovery_arguments["cpu_ratio"] = (
                    config.recovery_cpu_ratio
                )
            self._recovery_result = recover_dynamic_short_crypto_runs(
                config.recovery_run_roots,
                **recovery_arguments,
            )
        self._recovery_initialized = False
        self._lifecycle_cohort_initialized = False
        self._recovered_target_states: dict[str, dict[str, Any]] = {}
        self._recovered_processed_path_count = 0
        self._recovered_settlement_deadlines: dict[str, int] = {}
        self._recovered_worker_action_slugs: set[str] = set()
        self._recovered_worker_liveness: dict[str, dict[str, Any]] = {}
        self._recovery_apply_now_ms: int | None = None
        self._callback_generation = (
            0
            if self._recovery_result is None
            else self._recovery_result.callback_generation
        )
        control_store = CaptureStore(config.output_root / "control")
        self._control_sink = CaptureStoreRecorderSink(
            control_store,
            max_records_per_batch=config.max_records_per_batch,
        )
        self._startup_integrity_report = (
            self._control_sink.startup_integrity_report
        )
        _require_clean_output_integrity(
            self._startup_integrity_report,
            root=control_store.root,
        )

    async def _initialize_recovery(self, *, now_ms: int) -> None:
        """Finalize one recovery decision before network or worker effects."""

        if self._recovery_initialized:
            return
        result = self._recovery_result
        if result is None:
            self._recovery_initialized = True
            return
        event = await self._emit_control(
            now_ms=now_ms,
            event_type="restart_recovery_decision",
            kind="command",
            payload={
                **result.recovery_decision,
                "run_classifications": list(
                    result.run_classifications
                ),
                "gaps": list(result.gaps),
                "exclusions": list(result.exclusions),
                "worker_actions": list(result.worker_actions),
                **(
                    {}
                    if self._recovery_anchor_payload is None
                    else {
                        "recovery_anchor": (
                            self._recovery_anchor_payload
                        )
                    }
                ),
            },
        )
        await self._checkpoint_control_record(
            record_id=event.record_id,
            reason="restart_recovery_durable_before_apply",
            now_ms=now_ms,
        )
        await self._apply_recovery_state(result, now_ms=now_ms)
        self._recovery_initialized = True

    async def _initialize_lifecycle_cohort(self, *, now_ms: int) -> None:
        """Commit the prospective audit cohort before public network effects."""

        if self._lifecycle_cohort_initialized:
            return
        recovered_state = (
            {}
            if self._recovery_result is None
            else self._recovery_result.state
        )
        recovered_journal_value = recovered_state.get(
            "lifecycle_cohort_journal",
            [],
        )
        if (
            not isinstance(recovered_journal_value, list)
            or not all(
                isinstance(item, Mapping)
                and isinstance(item.get("record_id"), str)
                and isinstance(item.get("event_type"), str)
                and isinstance(item.get("payload"), Mapping)
                for item in recovered_journal_value
            )
        ):
            raise DiscoveryInputError(
                "recovered_lifecycle_cohort_journal_invalid",
                "recovered cohort journal has an invalid shape",
            )
        recovered_journal = tuple(recovered_journal_value)
        remediation_plan = self.config.lifecycle_remediation_plan
        if remediation_plan is None and recovered_journal:
            self._lifecycle_cohort_initialized = True
            return
        if remediation_plan is not None:
            if not recovered_journal:
                raise DiscoveryInputError(
                    "lifecycle_remediation_plan_mismatch",
                    "remediation plan requires a recovered chain tip",
                )
            tip = recovered_journal[-1]
            tip_payload = tip["payload"]
            if (
                tip.get("event_type")
                == "lifecycle_remediation_cohort_committed"
                and tip_payload.get("remediation_plan_id")
                == remediation_plan.plan_id
                and tip_payload.get(
                    "predecessor_commitment_record_id"
                )
                == remediation_plan.predecessor_commitment_record_id
            ):
                self._lifecycle_cohort_initialized = True
                return
            if (
                tip.get("record_id")
                != remediation_plan.predecessor_commitment_record_id
            ):
                raise DiscoveryInputError(
                    "lifecycle_remediation_plan_mismatch",
                    "remediation predecessor is not the recovered chain tip",
                )
        grid_ms = 5 * 60 * 1_000
        minimum_lead_ms = (
            self.config.subscribe_lead_ms
            + math.ceil(self.config.scan_interval_seconds * 1_000)
        )
        earliest_open_ms = now_ms + minimum_lead_ms
        eligibility_start_ms = (
            (earliest_open_ms + grid_ms - 1) // grid_ms
        ) * grid_ms
        common_payload: dict[str, Any] = {
            "selection_rule": (
                "first_mature_targets_opening_at_or_after_boundary"
            ),
            "eligibility_start_ms": eligibility_start_ms,
            "sample_size": 20,
            "threshold": "0.8",
            "settlement_timeout_ms": self.config.settlement_timeout_ms,
            "assets": ["BTC", "ETH"],
            "horizons": ["5m", "15m"],
            "selection_order": [
                "opens_at_ms",
                "closes_at_ms",
                "slug",
            ],
            "must_be_durable_before_public_network": True,
            "actual_fill": False,
            "authenticated_fill": False,
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
        }
        if remediation_plan is None:
            event_type = ROOT_EVENT_TYPE
            checkpoint_reason = (
                "lifecycle_cohort_durable_before_public_network"
            )
            commitment_payload = {
                "schema_version": ROOT_COHORT_SCHEMA,
                **common_payload,
                "earliest_valid_commitment_wins": True,
            }
        else:
            event_type = REMEDIATION_EVENT_TYPE
            checkpoint_reason = (
                "lifecycle_remediation_cohort_durable_before_public_network"
            )
            commitment_payload = {
                "schema_version": REMEDIATION_COHORT_SCHEMA,
                **common_payload,
                "predecessor_commitment_record_id": (
                    remediation_plan.predecessor_commitment_record_id
                ),
                "remediation_reason_code": (
                    remediation_plan.remediation_reason_code
                ),
                "remediation_plan_id": remediation_plan.plan_id,
                "append_only": True,
                "prior_failure_retained": True,
            }
        event = await self._emit_control(
            now_ms=now_ms,
            event_type=event_type,
            kind="command",
            payload=commitment_payload,
        )
        await self._checkpoint_control_record(
            record_id=event.record_id,
            reason=checkpoint_reason,
            now_ms=now_ms,
        )
        self._lifecycle_cohort_initialized = True

    def _revalidate_recovered_discovery_descriptors(
        self,
        descriptors: Any,
    ) -> tuple[_FinalizedDiscoveryInput, ...]:
        if not isinstance(descriptors, list):
            raise DiscoveryInputError(
                "recovered_discovery_descriptors_invalid",
                "recovered discovery descriptors must be a list",
            )
        verified: list[_FinalizedDiscoveryInput] = []
        seen_paths: set[Path] = set()
        for descriptor in descriptors:
            if (
                not isinstance(descriptor, Mapping)
                or set(descriptor)
                != {"path", "sha256", "byte_count", "line_count"}
                or not isinstance(descriptor.get("path"), str)
            ):
                raise DiscoveryInputError(
                    "recovered_discovery_descriptor_invalid",
                    "recovered discovery descriptor has an invalid shape",
                )
            unresolved = Path(descriptor["path"]).expanduser()
            if (
                not unresolved.is_absolute()
                or unresolved.is_symlink()
            ):
                raise DiscoveryInputError(
                    "recovered_discovery_path_unsafe",
                    f"recovered discovery path is unsafe: {unresolved}",
                )
            path = unresolved.resolve()
            manifest_path = path.with_suffix(".manifest.json")
            if (
                path.parent != self.config.discovery_dir
                or manifest_path.is_symlink()
            ):
                raise DiscoveryInputError(
                    "recovered_discovery_path_outside_root",
                    f"recovered discovery path is outside the pinned root: {path}",
                )
            if path in seen_paths:
                raise DiscoveryInputError(
                    "recovered_discovery_descriptor_duplicate",
                    f"recovered discovery path occurs more than once: {path}",
                )
            item = _validate_finalized_raw(path, manifest_path)
            expected = {
                "path": str(item.path),
                "sha256": item.sha256,
                "byte_count": item.byte_count,
                "line_count": item.line_count,
            }
            if dict(descriptor) != expected:
                raise DiscoveryInputError(
                    "recovered_discovery_descriptor_mismatch",
                    f"recovered discovery bytes changed: {path}",
                )
            seen_paths.add(path)
            verified.append(item)
        return tuple(sorted(verified, key=lambda item: str(item.path)))

    @staticmethod
    def _recovered_target(
        slug: str,
        value: Mapping[str, Any],
    ) -> ShortCryptoTarget:
        identity = value.get("identity")
        if not isinstance(identity, Mapping):
            raise DiscoveryInputError(
                "recovered_target_identity_invalid",
                f"recovered target {slug} has no identity",
            )
        payload = {
            key: item
            for key, item in identity.items()
            if key != "provenance"
        }
        if (
            set(payload) != set(ShortCryptoTarget.__dataclass_fields__)
            or payload.get("slug") != slug
        ):
            raise DiscoveryInputError(
                "recovered_target_identity_invalid",
                f"recovered target {slug} has incompatible fields",
            )
        try:
            return ShortCryptoTarget(**payload)
        except TypeError as exc:
            raise DiscoveryInputError(
                "recovered_target_identity_invalid",
                f"recovered target {slug} cannot be reconstructed",
            ) from exc

    @staticmethod
    def _rehydrate_gamma_record(
        target: ShortCryptoTarget,
        raw_record: Any,
    ) -> tuple[ShortCryptoGamma, dict[str, Any], int]:
        if (
            not isinstance(raw_record, Mapping)
            or raw_record.get("schema_version")
            != "edge-lab-recorder.raw.v1"
            or raw_record.get("source") != "gamma_http"
            or not isinstance(raw_record.get("record_id"), str)
            or raw_record["record_id"] != canonical_record_id(raw_record)
        ):
            raise DiscoveryInputError(
                "recovered_gamma_evidence_invalid",
                f"recovered Gamma envelope is invalid for {target.slug}",
            )
        event = raw_record.get("payload")
        payload = event.get("payload") if isinstance(event, Mapping) else None
        responses = (
            payload.get("responses")
            if isinstance(payload, Mapping)
            else None
        )
        if (
            not isinstance(event, Mapping)
            or event.get("schema_version")
            != "gamma-http.target-snapshot.v1"
            or event.get("source") != "gamma_http"
            or event.get("event_type") != "gamma_market"
            or not isinstance(responses, list)
            or len(responses) != 1
            or not isinstance(responses[0], Mapping)
            or responses[0].get("resource") != "gamma_market"
            or responses[0].get("request_key") != target.market_id
            or not isinstance(
                responses[0].get("raw_body_base64"), str
            )
            or not isinstance(responses[0].get("provenance"), Mapping)
        ):
            raise DiscoveryInputError(
                "recovered_gamma_evidence_invalid",
                f"recovered Gamma payload is invalid for {target.slug}",
            )
        try:
            raw_body = base64.b64decode(
                responses[0]["raw_body_base64"],
                validate=True,
            )
        except (ValueError, TypeError) as exc:
            raise DiscoveryInputError(
                "recovered_gamma_evidence_invalid",
                f"recovered Gamma bytes are invalid for {target.slug}",
            ) from exc
        provenance = responses[0]["provenance"]
        if (
            provenance.get("body_sha256")
            != hashlib.sha256(raw_body).hexdigest()
            or provenance.get("body_bytes") != len(raw_body)
        ):
            raise DiscoveryInputError(
                "recovered_gamma_evidence_invalid",
                f"recovered Gamma bytes do not match provenance for {target.slug}",
            )
        try:
            gamma = reconcile_short_crypto_gamma(target, raw_body)
            received_at_ms = _epoch_ms(str(event["received_at"]))
        except (
            GammaReconciliationError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise DiscoveryInputError(
                "recovered_gamma_evidence_invalid",
                f"recovered Gamma contract is invalid for {target.slug}",
            ) from exc
        return gamma, dict(raw_record), received_at_ms

    @staticmethod
    def _rehydrate_chainlink_boundary(
        target: ShortCryptoTarget,
        *,
        role: str,
        evidence: Mapping[str, Any],
    ) -> tuple[FinalizedChainlinkBoundary, str]:
        raw_record = evidence.get("evidence_record")
        evidence_record_id = evidence.get("evidence_record_id")
        if (
            role not in {"open", "close"}
            or not isinstance(raw_record, Mapping)
            or not isinstance(evidence_record_id, str)
            or raw_record.get("schema_version")
            != "edge-lab-recorder.raw.v1"
            or raw_record.get("source") != "rtds_ws"
            or raw_record.get("record_id") != evidence_record_id
            or evidence_record_id != canonical_record_id(raw_record)
        ):
            raise DiscoveryInputError(
                "recovered_chainlink_evidence_invalid",
                f"recovered Chainlink envelope is invalid for {target.slug}",
            )
        event = raw_record.get("payload")
        payload = event.get("payload") if isinstance(event, Mapping) else None
        expected_timestamp = (
            target.opens_at_ms if role == "open" else target.closes_at_ms
        )
        if (
            not isinstance(event, Mapping)
            or event.get("schema_version")
            != "edge-lab-dynamic-short-crypto-service.record.v1"
            or event.get("source") != "rtds_ws"
            or event.get("event_type") != "chainlink_boundary_evidence"
            or not isinstance(payload, Mapping)
            or payload.get("schema_version")
            != "edge-lab-dynamic-short-crypto-chainlink-boundary.v1"
            or payload.get("slug") != target.slug
            or payload.get("condition_id") != target.condition_id
            or payload.get("boundary_role") != role
            or payload.get("source_topic") != target.source_topic
            or payload.get("source_symbol") != target.source_symbol
            or payload.get("rule_url")
            != (
                "https://data.chain.link/streams/"
                f"{target.source_symbol.split('/', 1)[0]}-usd"
            )
            or payload.get("rule_hash") != target.rule_hash
            or payload.get("inner_timestamp_ms") != expected_timestamp
        ):
            raise DiscoveryInputError(
                "recovered_chainlink_evidence_invalid",
                f"recovered Chainlink payload is invalid for {target.slug}",
            )
        try:
            price = Decimal(str(payload["price"]))
            full_accuracy_value = str(payload["full_accuracy_value"])
            if price != Decimal(full_accuracy_value) / Decimal(10**18):
                raise ValueError("full-accuracy price mismatch")
            display_value = str(payload["display_value"])
            display_price = Decimal(display_value)
            display_encoding = str(payload["display_encoding"])
            expected_display_encoding = (
                "exact_decimal"
                if display_price == price
                else (
                    "canonical_binary64"
                    if display_value == repr(float(price))
                    else None
                )
            )
            if display_encoding != expected_display_encoding:
                raise ValueError("display price mismatch")
            boundary = FinalizedChainlinkBoundary(
                boundary_role=role,
                source_topic=str(payload["source_topic"]),
                source_symbol=str(payload["source_symbol"]),
                rule_url=str(payload["rule_url"]),
                rule_hash=str(payload["rule_hash"]),
                inner_timestamp_ms=int(payload["inner_timestamp_ms"]),
                price=price,
                display_value=display_value,
                display_encoding=display_encoding,
                full_accuracy_value=full_accuracy_value,
                record_id=str(payload["source_record_id"]),
                session_id=str(payload["source_session_id"]),
                connection_id=str(payload["source_connection_id"]),
                received_at=str(payload["source_received_at"]),
                source_manifest_path=str(
                    payload["source_manifest_path"]
                ),
                source_manifest_sha256=str(
                    payload["source_manifest_sha256"]
                ),
                raw_path=str(payload["source_raw_path"]),
                raw_sha256=str(payload["source_raw_sha256"]),
                line_number=int(payload["source_line_number"]),
                retrospective=payload["retrospective"],
                available_at_first_decision=payload[
                    "available_at_first_decision"
                ],
            )
        except (
            InvalidOperation,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise DiscoveryInputError(
                "recovered_chainlink_evidence_invalid",
                f"recovered Chainlink fields are invalid for {target.slug}",
            ) from exc
        if (
            not isinstance(boundary.retrospective, bool)
            or not isinstance(
                boundary.available_at_first_decision, bool
            )
        ):
            raise DiscoveryInputError(
                "recovered_chainlink_evidence_invalid",
                f"recovered Chainlink flags are invalid for {target.slug}",
            )
        return boundary, evidence_record_id

    async def _apply_recovery_state(
        self,
        result: RecoveryResult,
        *,
        now_ms: int,
    ) -> None:
        """Apply only manifest-revalidated state after the durable decision."""

        state = result.state
        if not isinstance(state, Mapping):
            raise DiscoveryInputError(
                "recovered_state_invalid",
                "restart recovery state must be an object",
            )
        if self._recovery_apply_now_ms is None:
            self._recovery_apply_now_ms = now_ms
        apply_now_ms = self._recovery_apply_now_ms
        descriptors = await asyncio.to_thread(
            self._revalidate_recovered_discovery_descriptors,
            state.get("processed_finalized_discovery_descriptors"),
        )
        recovered_targets = state.get("targets")
        if not isinstance(recovered_targets, Mapping):
            raise DiscoveryInputError(
                "recovered_targets_invalid",
                "restart recovery targets must be an object",
            )
        parsed_targets: dict[str, ShortCryptoTarget] = {}
        parsed_states: dict[str, dict[str, Any]] = {}
        for slug, value in recovered_targets.items():
            if not isinstance(slug, str) or not isinstance(value, Mapping):
                raise DiscoveryInputError(
                    "recovered_target_state_invalid",
                    "recovered target state has an invalid shape",
                )
            target = self._recovered_target(slug, value)
            state_name = value.get("state")
            if state_name not in {
                "announced",
                "gamma_verified",
                "settled",
                "excluded",
                "reacquire_required",
            }:
                raise DiscoveryInputError(
                    "recovered_target_state_invalid",
                    f"recovered target {slug} has unknown state {state_name!r}",
                )
            deadline = value.get("pending_settlement_deadline_ms")
            if deadline is not None and (
                isinstance(deadline, bool)
                or not isinstance(deadline, int)
                or deadline < target.closes_at_ms
            ):
                raise DiscoveryInputError(
                    "recovered_settlement_deadline_invalid",
                    f"recovered target {slug} has an invalid deadline",
                )
            parsed_targets[slug] = target
            parsed_states[slug] = dict(value)

        registry = state.get("registry")
        if not isinstance(registry, Mapping):
            raise DiscoveryInputError(
                "recovered_registry_invalid",
                "restart recovery registry must be an object",
            )
        recovered_registry_snapshot = registry.get("snapshot")
        if recovered_registry_snapshot is not None:
            if not isinstance(recovered_registry_snapshot, Mapping):
                raise DiscoveryInputError(
                    "recovered_registry_invalid",
                    "recovered registry snapshot must be an object",
                )
            try:
                validated_snapshot = (
                    validate_short_crypto_registry_snapshot(
                        recovered_registry_snapshot
                    )
                )
            except ValueError as exc:
                raise DiscoveryInputError(
                    "recovered_registry_invalid",
                    "recovered registry snapshot failed content validation",
                ) from exc
            expected_descriptors = [
                {
                    "path": str(item.path),
                    "sha256": item.sha256,
                    "byte_count": item.byte_count,
                    "line_count": item.line_count,
                }
                for item in descriptors
            ]
            recovered_identities = {
                slug: parsed_states[slug]["identity"]
                for slug in sorted(parsed_states)
            }
            snapshot_identities = {
                item["slug"]: item
                for item in validated_snapshot["targets"]
                if isinstance(item, Mapping)
                and isinstance(item.get("slug"), str)
            }
            if (
                validated_snapshot["inputs"] != expected_descriptors
                or snapshot_identities != recovered_identities
                or registry.get("snapshot_sha256")
                != validated_snapshot["snapshot_sha256"]
            ):
                raise DiscoveryInputError(
                    "recovered_registry_state_mismatch",
                    "recovered registry snapshot disagrees with applied state",
                )
        else:
            validated_snapshot = None

        action_slugs = {
            slug
            for action in result.worker_actions
            if isinstance(action, Mapping)
            for slug in action.get("target_slugs", [])
            if isinstance(slug, str)
        }
        if not action_slugs <= set(parsed_targets):
            raise DiscoveryInputError(
                "recovered_worker_action_target_unknown",
                "a recovered worker action references an unknown target",
            )
        self.supervisor.restore_decision_sequence(
            max(0, result.callback_generation - 1)
        )
        decisions = state.get("subscription_decisions")
        if not isinstance(decisions, Mapping):
            raise DiscoveryInputError(
                "recovered_subscription_decisions_invalid",
                "recovered subscription decisions must be an object",
            )
        self._persisted_decision_ids.update(
            decision_id
            for decision_id in decisions
            if isinstance(decision_id, str)
        )
        latest_gamma_records = state.get("latest_gamma_records", {})
        gamma_evidence = state.get("gamma_evidence")
        chainlink_evidence = state.get("chainlink_evidence")
        worker_liveness = state.get("worker_liveness")
        if (
            not isinstance(latest_gamma_records, Mapping)
            or not isinstance(gamma_evidence, Mapping)
            or not isinstance(chainlink_evidence, Mapping)
            or not isinstance(worker_liveness, Mapping)
        ):
            raise DiscoveryInputError(
                "recovered_evidence_state_invalid",
                "recovered evidence collections must be objects",
            )
        self._recovered_worker_liveness = {
            str(group_id): dict(value)
            for group_id, value in worker_liveness.items()
            if isinstance(group_id, str) and isinstance(value, Mapping)
        }

        self._processed_paths.update(item.path for item in descriptors)
        self._recovered_processed_path_count = len(descriptors)
        self._recovered_target_states = parsed_states
        self._recovered_worker_action_slugs = action_slugs
        for slug, target in parsed_targets.items():
            existing = self._targets.get(slug)
            if existing is not None and existing != target:
                raise DiscoveryInputError(
                    "recovered_target_conflict",
                    f"recovered target conflicts with live state: {slug}",
                )
            self._targets[slug] = target
            recovered = parsed_states[slug]
            deadline = recovered.get("pending_settlement_deadline_ms")
            if isinstance(deadline, int):
                self._recovered_settlement_deadlines[slug] = deadline
            state_name = recovered["state"]
            if state_name == "settled":
                record_id = recovered.get("settlement_record_id")
                settlement = recovered.get("settlement")
                available_at_ms = (
                    settlement.get("available_at_ms")
                    if isinstance(settlement, Mapping)
                    else None
                )
                if not isinstance(record_id, str):
                    raise DiscoveryInputError(
                        "recovered_settlement_invalid",
                        f"recovered target {slug} has no settlement record",
                    )
                observed_at_ms = (
                    available_at_ms
                    if isinstance(available_at_ms, int)
                    and not isinstance(available_at_ms, bool)
                    else target.closes_at_ms
                )
                self.supervisor.restore_finalized_terminal(
                    target,
                    state=TargetLifecycleState.SETTLED,
                    evidence_record_id=record_id,
                    exclusion_reason=None,
                    observed_at_ms=max(
                        target.closes_at_ms,
                        observed_at_ms,
                    ),
                )
                self._label_status[slug] = "strict_settled"
                self._label_tracking_complete.add(slug)
            elif state_name == "excluded":
                reason = recovered.get("exclusion_reason")
                record_id = recovered.get("exclusion_record_id")
                if not isinstance(reason, str) or not isinstance(
                    record_id, str
                ):
                    raise DiscoveryInputError(
                        "recovered_exclusion_invalid",
                        f"recovered target {slug} has incomplete exclusion evidence",
                    )
                observed_at_ms = recovered.get(
                    "exclusion_observed_at_ms",
                    apply_now_ms,
                )
                if (
                    isinstance(observed_at_ms, bool)
                    or not isinstance(observed_at_ms, int)
                ):
                    raise DiscoveryInputError(
                        "recovered_exclusion_invalid",
                        f"recovered target {slug} has an invalid exclusion time",
                    )
                self.supervisor.restore_finalized_terminal(
                    target,
                    state=TargetLifecycleState.EXCLUDED,
                    evidence_record_id=record_id,
                    exclusion_reason=reason,
                    observed_at_ms=observed_at_ms,
                )
                self._rejected[slug] = reason
                self._label_status[slug] = "recovered_excluded"
                self._label_tracking_complete.add(slug)
            elif (
                state_name == "gamma_verified"
                and slug not in action_slugs
            ):
                raw_record = latest_gamma_records.get(slug)
                if raw_record is not None:
                    evidence_items = gamma_evidence.get(slug)
                    if not isinstance(evidence_items, list) or not any(
                        isinstance(item, Mapping)
                        and item.get("gamma_record_id")
                        == raw_record.get("record_id")
                        for item in evidence_items
                    ):
                        raise DiscoveryInputError(
                            "recovered_gamma_evidence_invalid",
                            f"recovered Gamma record is uncommitted for {slug}",
                        )
                    gamma, record, received_at_ms = (
                        self._rehydrate_gamma_record(
                            target,
                            raw_record,
                        )
                    )
                    if received_at_ms > apply_now_ms:
                        raise DiscoveryInputError(
                            "recovered_gamma_future_timestamp",
                            f"recovered Gamma evidence is in the future for {slug}",
                        )
                    self._verified[slug] = gamma
                    self._gamma_records[slug] = [record]
                    self._last_gamma_poll_ms[slug] = received_at_ms
                    self._committed_gamma_verification_slugs.add(slug)
                    self.supervisor.announce(target)

            recovered_boundaries = chainlink_evidence.get(slug)
            if isinstance(recovered_boundaries, Mapping):
                for role in ("open", "close"):
                    entries = recovered_boundaries.get(role)
                    if not isinstance(entries, list):
                        continue
                    recovered_boundary: FinalizedChainlinkBoundary | None = (
                        None
                    )
                    recovered_commit_id: str | None = None
                    for evidence in entries:
                        if (
                            not isinstance(evidence, Mapping)
                            or "evidence_record" not in evidence
                        ):
                            continue
                        boundary, commit_id = (
                            self._rehydrate_chainlink_boundary(
                                target,
                                role=role,
                                evidence=evidence,
                            )
                        )
                        try:
                            boundary_received_at_ms = _epoch_ms(
                                boundary.received_at
                            )
                        except (TypeError, ValueError) as exc:
                            raise DiscoveryInputError(
                                "recovered_chainlink_evidence_invalid",
                                f"recovered Chainlink time is invalid for {slug}",
                            ) from exc
                        if boundary_received_at_ms > apply_now_ms:
                            raise DiscoveryInputError(
                                "recovered_chainlink_future_timestamp",
                                f"recovered Chainlink evidence is in the future for {slug}",
                            )
                        if (
                            recovered_boundary is not None
                            and recovered_boundary != boundary
                        ):
                            raise DiscoveryInputError(
                                "recovered_chainlink_evidence_conflict",
                                f"recovered Chainlink {role} conflicts for {slug}",
                            )
                        recovered_boundary = boundary
                        recovered_commit_id = commit_id
                    if recovered_boundary is not None:
                        self._chainlink_boundaries.setdefault(
                            slug,
                            {},
                        )[role] = recovered_boundary
                        assert recovered_commit_id is not None
                        self._chainlink_commit_record_ids.setdefault(
                            slug,
                            {},
                        )[role] = recovered_commit_id

        recovered_targets = result.state.get("targets")
        if isinstance(recovered_targets, Mapping):
            self._recovered_target_states = {
                str(slug): dict(value)
                for slug, value in recovered_targets.items()
                if isinstance(slug, str) and isinstance(value, Mapping)
            }
        snapshot_hash = registry.get("snapshot_sha256")
        if isinstance(snapshot_hash, str):
            self._last_registry_hash = snapshot_hash
        if validated_snapshot is not None:
            self._registry_snapshot = validated_snapshot

    async def _emit_control(
        self,
        *,
        now_ms: int,
        event_type: str,
        kind: str,
        payload: Mapping[str, Any],
        source: str = "dynamic_short_crypto_service",
    ) -> Any:
        emitted_at_ms = int(time.time() * 1_000)
        return await self._control_sink.emit(
            {
                "schema_version": (
                    "edge-lab-dynamic-short-crypto-service.record.v1"
                ),
                "source": source,
                "kind": kind,
                "event_type": event_type,
                "session_id": self._session_id,
                "connection_id": None,
                "received_at": _iso_from_epoch_ms(emitted_at_ms),
                "event_at": None,
                "sequence": None,
                "monotonic_ns": time.monotonic_ns(),
                "payload": {
                    "scheduler_now_ms": now_ms,
                    **dict(payload),
                },
            }
        )

    async def _checkpoint_control_record(
        self,
        *,
        record_id: str,
        reason: str,
        now_ms: int,
    ) -> None:
        await self._control_sink.checkpoint(
            "dynamic_short_crypto_service",
            {
                "schema_version": (
                    "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
                ),
                "record_id": record_id,
                "reason": reason,
                "updated_at": _iso_from_epoch_ms(now_ms),
            },
        )

    async def _checkpoint_gamma_record(
        self,
        *,
        target: ShortCryptoTarget,
        record_id: str,
        reason: str,
        now_ms: int,
    ) -> None:
        await self._control_sink.checkpoint(
            "gamma_http",
            {
                "schema_version": (
                    "edge-lab-dynamic-short-crypto-gamma-checkpoint.v1"
                ),
                "slug": target.slug,
                "record_id": record_id,
                "reason": reason,
                "updated_at": _iso_from_epoch_ms(now_ms),
            },
        )

    @staticmethod
    def _decision_payload(
        decision: CaptureDecision,
    ) -> dict[str, Any]:
        return {
            "schema_version": decision.schema_version,
            "decision_id": decision.decision_id,
            "sequence": decision.sequence,
            "action": decision.action.value,
            "group_id": decision.group_id,
            "asset_ids": list(decision.asset_ids),
            "targets": [
                {
                    "slug": target.slug,
                    "announcement_record_id": (
                        target.announcement_record_id
                    ),
                    "rule_hash": target.rule_hash,
                    "up_token_id": target.up_token_id,
                    "down_token_id": target.down_token_id,
                }
                for target in decision.targets
            ],
            "emitted_at_ms": decision.emitted_at_ms,
            "reason": decision.reason,
            "minimum_snapshot_watermark_ns": (
                decision.minimum_snapshot_watermark_ns
            ),
        }

    async def _persist_decision(
        self,
        decision: CaptureDecision,
        *,
        now_ms: int,
    ) -> None:
        if decision.decision_id in self._persisted_decision_ids:
            return
        event = await self._emit_control(
            now_ms=now_ms,
            event_type="capture_decision",
            kind="command",
            payload=self._decision_payload(decision),
        )
        await self._checkpoint_control_record(
            record_id=event.record_id,
            reason="capture_decision_durable_before_apply",
            now_ms=now_ms,
        )
        self._persisted_decision_ids.add(decision.decision_id)

    def _new_finalized_paths(
        self,
    ) -> tuple[_FinalizedDiscoveryInput, ...]:
        candidates: list[Path] = []
        with os.scandir(self.config.discovery_dir) as entries:
            for entry in entries:
                if (
                    not entry.name.endswith(".jsonl")
                    or not entry.is_file(follow_symlinks=False)
                    or entry.is_symlink()
                ):
                    continue
                path = self.config.discovery_dir / entry.name
                if path in self._processed_paths:
                    continue
                candidates.append(path)
        finalized: list[_FinalizedDiscoveryInput] = []
        for path in sorted(candidates, key=str):
            manifest = path.with_suffix(".manifest.json")
            if (
                not manifest.is_file()
                or manifest.is_symlink()
            ):
                continue
            finalized.append(_validate_finalized_raw(path, manifest))
        return tuple(finalized)

    @staticmethod
    def _stop_requested(
        stop_event: asyncio.Event | None,
        deadline_monotonic: float | None,
    ) -> bool:
        return (
            stop_event is not None
            and stop_event.is_set()
        ) or (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        )

    async def _ingest_registry(
        self,
        *,
        now_ms: int,
        live_clock: bool = False,
        stop_event: asyncio.Event | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        new_inputs = await asyncio.to_thread(self._new_finalized_paths)
        if not new_inputs:
            return
        build_inputs = new_inputs
        if self._registry_snapshot is None and self._processed_paths:
            build_inputs = tuple(
                _validate_finalized_raw(
                    path,
                    path.with_suffix(".manifest.json"),
                )
                for path in sorted(
                    (
                        *self._processed_paths,
                        *(item.path for item in new_inputs),
                    ),
                    key=str,
                )
            )
        delta = await asyncio.to_thread(
            _build_registry_delta,
            build_inputs,
        )
        expected_inputs = [
            {
                "path": str(item.path),
                "sha256": item.sha256,
                "byte_count": item.byte_count,
                "line_count": item.line_count,
            }
            for item in build_inputs
        ]
        if (
            not isinstance(delta, Mapping)
            or delta.get("inputs") != expected_inputs
        ):
            raise DiscoveryInputError(
                "registry_input_changed_after_manifest_validation",
                "registry parsed bytes that differ from the verified manifests",
            )
        snapshot = (
            dict(delta)
            if self._registry_snapshot is None
            else merge_short_crypto_registry_snapshots(
                self._registry_snapshot,
                delta,
            )
        )
        if self._stop_requested(stop_event, deadline_monotonic):
            return
        await self._emit_control(
            now_ms=now_ms,
            event_type="registry_revision",
            kind="data",
            payload={
                "schema_version": (
                    "edge-lab-short-crypto-registry-revision.v2"
                ),
                "delta_snapshot": delta,
                "cumulative_snapshot_sha256": snapshot[
                    "snapshot_sha256"
                ],
                "cumulative_input_file_count": len(snapshot["inputs"]),
                "cumulative_target_count": len(snapshot["targets"]),
            },
        )
        for item in snapshot["targets"]:
            if self._stop_requested(stop_event, deadline_monotonic):
                return
            if not isinstance(item, Mapping):
                continue
            target_payload = {
                key: value
                for key, value in item.items()
                if key != "provenance"
            }
            target = ShortCryptoTarget(**target_payload)
            existing = self._targets.get(target.slug)
            if existing is not None:
                if not _same_contract(existing, target):
                    raise DiscoveryInputError(
                        "target_contract_conflict",
                        f"target identity changed across revisions: {target.slug}",
                    )
                continue
            self._targets[target.slug] = target
            poll_now_ms = (
                int(time.time() * 1_000) if live_clock else now_ms
            )
            if not self._defer_live_gamma_until_preverification_horizon(
                target,
                now_ms=poll_now_ms,
                live_clock=live_clock,
            ):
                await self._poll_gamma_target(
                    target,
                    now_ms=poll_now_ms,
                    initial=True,
                )
            # A registry delta can contain many new targets and each public
            # Gamma request is deliberately serial.  Keep the live scheduler
            # moving between initial verifications so a long delta cannot
            # starve a target whose subscription boundary arrives mid-sweep.
            if live_clock:
                scheduling_now_ms = int(time.time() * 1_000)
                await self._acknowledge_ready_workers(
                    now_ms=scheduling_now_ms
                )
                await self._apply_decisions(
                    self.supervisor.advance(scheduling_now_ms),
                    now_ms=scheduling_now_ms,
                )
        if self._stop_requested(stop_event, deadline_monotonic):
            return
        self._processed_paths.update(item.path for item in new_inputs)
        self._registry_snapshot = dict(snapshot)
        self._last_registry_hash = str(snapshot["snapshot_sha256"])

    @staticmethod
    def _gamma_public_response(
        target: ShortCryptoTarget,
        fetched: Fetched[Any],
    ) -> tuple[dict[str, Any], str]:
        raw = fetched.raw
        metadata = raw.metadata
        parsed = urlsplit(metadata.url)
        if (
            metadata.source != "gamma"
            or metadata.method != "GET"
            or parsed.scheme != "https"
            or parsed.hostname != "gamma-api.polymarket.com"
            or parsed.path != f"/markets/{target.market_id}"
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
            or metadata.request_params
            or not 200 <= metadata.status_code < 300
            or raw.text.encode("utf-8") != raw.body
        ):
            raise DiscoveryInputError(
                "unsafe_gamma_response",
                "Gamma target response lacks exact public provenance",
            )
        try:
            raw_json = json.loads(raw.text, parse_float=str)
        except json.JSONDecodeError as exc:
            raise DiscoveryInputError(
                "gamma_json_invalid",
                "Gamma target response is not valid JSON",
            ) from exc
        received_at_ms = int(metadata.received_at * 1_000)
        record = {
            "schema_version": "gamma-http.target-snapshot.v1",
            "source": "gamma_http",
            "kind": "snapshot",
            "event_type": "gamma_market",
            "session_id": None,
            "connection_id": None,
            "received_at": _iso_from_epoch_ms(received_at_ms),
            "event_at": None,
            "sequence": None,
            "monotonic_ns": time.monotonic_ns(),
            "payload": {
                "schema_version": "edge-lab-public-snapshot.v1",
                "snapshot_kind": "gamma_target",
                "requested_asset_ids": [],
                "responses": [
                    {
                        "resource": "gamma_market",
                        "request_key": target.market_id,
                        "raw_json": raw_json,
                        "raw_body_base64": base64.b64encode(
                            raw.body
                        ).decode("ascii"),
                        "provenance": {
                            "source": metadata.source,
                            "method": metadata.method,
                            "url": metadata.url,
                            "request_params": {},
                            "status_code": metadata.status_code,
                            "requested_at_epoch_seconds": repr(
                                metadata.requested_at
                            ),
                            "received_at_epoch_seconds": repr(
                                metadata.received_at
                            ),
                            "attempt": metadata.attempt,
                            "response_headers": sanitized_response_headers(
                                metadata.response_headers
                            ),
                            "body_sha256": hashlib.sha256(
                                raw.body
                            ).hexdigest(),
                            "body_bytes": len(raw.body),
                        },
                    }
                ],
                "truncated_resources": [],
            },
        }
        return record, _iso_from_epoch_ms(received_at_ms)

    async def _poll_gamma_target(
        self,
        target: ShortCryptoTarget,
        *,
        now_ms: int,
        initial: bool = False,
    ) -> None:
        if target.slug in self._rejected:
            return
        if target.slug in self._label_tracking_complete:
            return
        target_known = self._target_known_to_supervisor(target.slug)
        if (
            not target_known
            and now_ms >= target.opens_at_ms
        ):
            self._rejected[target.slug] = "gamma_unverified_before_open"
            await self._emit_control(
                now_ms=now_ms,
                event_type="gamma_target_rejected",
                kind="diagnostic",
                payload={
                    "slug": target.slug,
                    "evidence_record_id": (
                        target.announcement_record_id
                    ),
                    "error_type": "EligibilityDeadline",
                    "error_code": "gamma_unverified_before_open",
                },
            )
            return
        if target_known:
            state = self.supervisor.target_snapshot(target.slug).state
            if state is TargetLifecycleState.SETTLED:
                return
        if not initial:
            last_poll = self._last_gamma_poll_ms.get(target.slug)
            interval_ms = int(
                self.config.gamma_poll_interval_seconds * 1_000
            )
            if last_poll is not None and now_ms - last_poll < interval_ms:
                return
        self._last_gamma_poll_ms[target.slug] = now_ms
        try:
            fetched = await asyncio.to_thread(
                self.source_client.gamma_market,
                target.market_id,
            )
            record, received_at = self._gamma_public_response(target, fetched)
            record["session_id"] = self._session_id
            result = await self._control_sink.emit(record)
            outer = {
                "schema_version": "edge-lab-recorder.raw.v1",
                "source": "gamma_http",
                "received_at": received_at,
                "event_at": None,
                "sequence": None,
                "payload": record,
                "record_id": result.record_id,
            }
            self._gamma_records.setdefault(target.slug, []).append(outer)
            reconciled = reconcile_short_crypto_gamma(
                target,
                fetched.raw.body,
            )
        except PublicSourceError as exc:
            details = safe_error_details(
                exc,
                code=exc.code or "gamma_poll_failed",
            )
            await self._emit_control(
                now_ms=now_ms,
                event_type="gamma_poll_failed",
                kind="diagnostic",
                payload={"slug": target.slug, **details},
            )
            return
        except GammaReconciliationError as exc:
            details = safe_error_details(exc, code=exc.code)
            if (
                now_ms < target.opens_at_ms
                and exc.code
                in {
                    "gamma_schema_mismatch",
                    "gamma_status_schema_mismatch",
                }
                and is_strict_gamma_preactivation(
                    target,
                    fetched.raw.body,
                )
            ):
                await self._checkpoint_gamma_record(
                    target=target,
                    record_id=result.record_id,
                    reason="gamma_preactivation_evidence",
                    now_ms=now_ms,
                )
                await self._emit_control(
                    now_ms=now_ms,
                    event_type="gamma_poll_deferred",
                    kind="diagnostic",
                    payload={
                        "slug": target.slug,
                        "evidence_record_id": result.record_id,
                        "error_type": "GammaPreactivation",
                        "error_code": "gamma_not_activated_yet",
                    },
                )
                return
            self._rejected[target.slug] = str(details["error_code"])
            evidence_record_id = (
                result.record_id
                if "result" in locals()
                else target.announcement_record_id
            )
            if "result" in locals():
                await self._checkpoint_gamma_record(
                    target=target,
                    record_id=result.record_id,
                    reason="gamma_rejection_evidence",
                    now_ms=now_ms,
                )
            await self._emit_control(
                now_ms=now_ms,
                event_type="gamma_target_rejected",
                kind="diagnostic",
                payload={
                    "slug": target.slug,
                    "evidence_record_id": evidence_record_id,
                    **details,
                },
            )
            if self._target_known_to_supervisor(target.slug):
                state = self.supervisor.target_snapshot(target.slug).state
                if state not in {
                    TargetLifecycleState.SETTLED,
                    TargetLifecycleState.EXCLUDED,
                }:
                    self.supervisor.exclude(
                        target.slug,
                        reason=f"gamma_contract_rejected:{exc.code}",
                        evidence_record_id=evidence_record_id,
                        observed_at_ms=now_ms,
                    )
            self._gamma_records.pop(target.slug, None)
            return
        self._verified[target.slug] = reconciled
        if not self._target_known_to_supervisor(target.slug):
            self.supervisor.announce(target)
        if target.slug in self._committed_gamma_verification_slugs:
            return
        await self._checkpoint_gamma_record(
            target=target,
            record_id=result.record_id,
            reason="gamma_verification_evidence",
            now_ms=now_ms,
        )
        await self._emit_control(
            now_ms=now_ms,
            event_type="gamma_target_verified",
            kind="data",
            payload={
                "slug": target.slug,
                "market_id": target.market_id,
                "gamma_body_sha256": reconciled.evidence.sha256,
                "gamma_record_id": result.record_id,
                "active": reconciled.status.active,
                "closed": reconciled.status.closed,
                "accepting_orders": reconciled.status.accepting_orders,
            },
        )
        self._committed_gamma_verification_slugs.add(target.slug)

    def _target_known_to_supervisor(self, slug: str) -> bool:
        try:
            self.supervisor.target_snapshot(slug)
        except KeyError:
            return False
        return True

    def _defer_live_gamma_until_preverification_horizon(
        self,
        target: ShortCryptoTarget,
        *,
        now_ms: int,
        live_clock: bool,
    ) -> bool:
        return (
            live_clock
            and now_ms
            < target.opens_at_ms - (2 * self.config.subscribe_lead_ms)
        )

    def _settlement_deadline_ms(
        self,
        target: ShortCryptoTarget,
    ) -> int:
        return self._recovered_settlement_deadlines.get(
            target.slug,
            target.closes_at_ms + self.config.settlement_timeout_ms,
        )

    @staticmethod
    def _chainlink_rule_url(target: ShortCryptoTarget) -> str:
        base_symbol = target.source_symbol.split("/", 1)[0]
        return f"https://data.chain.link/streams/{base_symbol}-usd"

    def _finalized_rtds_manifests(self) -> tuple[Path, ...]:
        directory = self.config.rtds_manifest_dir
        if directory is None:
            return ()
        return tuple(
            sorted(
                (
                    path
                    for path in directory.iterdir()
                    if path.name.endswith(".manifest.json")
                    and path.is_file()
                    and not path.is_symlink()
                ),
                key=str,
            )
        )

    @staticmethod
    def _boundary_payload(
        target: ShortCryptoTarget,
        boundary: FinalizedChainlinkBoundary,
    ) -> dict[str, Any]:
        return {
            "schema_version": (
                "edge-lab-dynamic-short-crypto-chainlink-boundary.v1"
            ),
            "slug": target.slug,
            "condition_id": target.condition_id,
            "boundary_role": boundary.boundary_role,
            "source_topic": boundary.source_topic,
            "source_symbol": boundary.source_symbol,
            "rule_url": boundary.rule_url,
            "rule_hash": boundary.rule_hash,
            "inner_timestamp_ms": boundary.inner_timestamp_ms,
            "price": str(boundary.price),
            "display_value": boundary.display_value,
            "display_encoding": boundary.display_encoding,
            "full_accuracy_value": boundary.full_accuracy_value,
            "source_record_id": boundary.record_id,
            "source_session_id": boundary.session_id,
            "source_connection_id": boundary.connection_id,
            "source_received_at": boundary.received_at,
            "source_manifest_path": boundary.source_manifest_path,
            "source_manifest_sha256": (
                boundary.source_manifest_sha256
            ),
            "source_raw_path": boundary.raw_path,
            "source_raw_sha256": boundary.raw_sha256,
            "source_line_number": boundary.line_number,
            "retrospective": boundary.retrospective,
            "available_at_first_decision": (
                boundary.available_at_first_decision
            ),
        }

    async def _commit_chainlink_boundary(
        self,
        target: ShortCryptoTarget,
        *,
        boundary_role: str,
        boundary: FinalizedChainlinkBoundary,
        now_ms: int,
    ) -> FinalizedChainlinkBoundary:
        by_role = self._chainlink_boundaries.setdefault(
            target.slug,
            {},
        )
        existing = by_role.get(boundary_role)
        if existing is not None:
            return existing
        mirrored = await self._emit_control(
            now_ms=now_ms,
            event_type="chainlink_boundary_evidence",
            kind="data",
            payload=self._boundary_payload(target, boundary),
            source="rtds_ws",
        )
        await self._control_sink.checkpoint(
            "rtds_ws",
            {
                "schema_version": (
                    "edge-lab-dynamic-short-crypto-chainlink-checkpoint.v1"
                ),
                "slug": target.slug,
                "boundary_role": boundary_role,
                "record_id": mirrored.record_id,
                "source_record_id": boundary.record_id,
                "updated_at": _iso_from_epoch_ms(now_ms),
            },
        )
        committed = await self._emit_control(
            now_ms=now_ms,
            event_type="chainlink_boundary_evidence_committed",
            kind="lifecycle",
            payload={
                "slug": target.slug,
                "boundary_role": boundary_role,
                "evidence_record_id": mirrored.record_id,
                "source_record_id": boundary.record_id,
                "source_manifest_sha256": (
                    boundary.source_manifest_sha256
                ),
            },
        )
        await self._checkpoint_control_record(
            record_id=committed.record_id,
            reason="chainlink_boundary_evidence_committed",
            now_ms=now_ms,
        )
        by_role[boundary_role] = boundary
        self._chainlink_commit_record_ids.setdefault(
            target.slug,
            {},
        )[boundary_role] = mirrored.record_id
        return boundary

    def _merge_chainlink_scan_result(
        self,
        *,
        requests: Mapping[str, FinalizedChainlinkBoundaryRequest],
        scan_requests: Mapping[
            str,
            FinalizedChainlinkBoundaryRequest,
        ],
        boundaries: Mapping[
            str,
            FinalizedChainlinkBoundary | None,
        ],
    ) -> set[str]:
        if set(boundaries) != set(scan_requests):
            raise DiscoveryInputError(
                "chainlink_boundary_batch_invalid",
                "batch extractor returned an incompatible request set",
            )
        deferred_request_ids: set[str] = set()
        for request_id, boundary in boundaries.items():
            if boundary is None:
                continue
            request = requests[request_id]
            if (
                request.boundary_role == "open"
                and (
                    boundary.retrospective
                    or not boundary.available_at_first_decision
                )
            ):
                deferred_request_ids.add(request_id)
                continue
            existing_boundary = self._chainlink_pending_boundaries.get(
                request_id
            )
            if existing_boundary is not None:
                if existing_boundary.record_id == boundary.record_id:
                    code = "duplicate_record_id"
                elif (
                    existing_boundary.price != boundary.price
                    or existing_boundary.full_accuracy_value
                    != boundary.full_accuracy_value
                ):
                    code = "conflicting_price_to_beat"
                else:
                    code = "duplicate_price_to_beat"
                raise PriceToBeatRejection(
                    code,
                    "incremental finalized manifests introduced "
                    "a second boundary observation",
                )
            self._chainlink_pending_boundaries[request_id] = boundary
        return deferred_request_ids

    async def _finalize_chainlink_scan(
        self,
        *,
        requests: Mapping[str, FinalizedChainlinkBoundaryRequest],
        manifests: tuple[Path, ...],
        deferred_request_ids: set[str],
        now_ms: int,
    ) -> None:
        self._chainlink_known_request_ids.update(
            set(requests).difference(deferred_request_ids)
        )
        self._chainlink_known_request_ids.difference_update(
            deferred_request_ids
        )
        self._chainlink_scanned_manifest_paths.update(manifests)
        for request_id, request in requests.items():
            boundary_ms = (
                request.target.opens_at_ms
                if request.boundary_role == "open"
                else request.target.closes_at_ms
            )
            if now_ms < boundary_ms:
                continue
            boundary = self._chainlink_pending_boundaries.get(request_id)
            if boundary is None:
                continue
            await self._commit_chainlink_boundary(
                request.target,
                boundary_role=request.boundary_role,
                boundary=boundary,
                now_ms=now_ms,
            )
            self._chainlink_pending_boundaries.pop(request_id, None)

    @staticmethod
    def _start_live_chainlink_scan_batch(
        state: _LiveChainlinkScan,
    ) -> None:
        manifest_inventory, scan_requests = state.plan[state.next_index]
        state.task = asyncio.create_task(
            asyncio.to_thread(
                extract_chainlink_boundary_batch_from_finalized_manifests,
                manifest_inventory,
                requests=scan_requests,
            ),
            name="dynamic-short-crypto-chainlink-backfill",
        )

    async def _capture_due_chainlink_boundaries(
        self,
        *,
        now_ms: int,
        live_clock: bool = False,
    ) -> None:
        live_scan = self._live_chainlink_scan
        if live_clock and live_scan is not None:
            task = live_scan.task
            if task is None or not task.done():
                return
            manifest_inventory, scan_requests = live_scan.plan[
                live_scan.next_index
            ]
            try:
                boundaries = task.result()
            except PriceToBeatRejection as exc:
                self._live_chainlink_scan = None
                if exc.code != "source_time_conflict":
                    raise
                await self._emit_control(
                    now_ms=now_ms,
                    event_type="chainlink_boundary_scan_rejected",
                    kind="diagnostic",
                    payload={
                        "manifest_count": len(manifest_inventory),
                        "request_count": len(scan_requests),
                        **safe_error_details(exc, code=exc.code),
                    },
                )
                await self._finalize_chainlink_scan(
                    requests=live_scan.requests,
                    manifests=live_scan.manifests,
                    deferred_request_ids=(
                        live_scan.deferred_request_ids
                    ),
                    now_ms=now_ms,
                )
                return
            except BaseException:
                self._live_chainlink_scan = None
                raise
            if not isinstance(boundaries, Mapping):
                self._live_chainlink_scan = None
                raise DiscoveryInputError(
                    "chainlink_boundary_batch_invalid",
                    "batch extractor returned a non-object result",
                )
            live_scan.deferred_request_ids.update(
                self._merge_chainlink_scan_result(
                    requests=live_scan.requests,
                    scan_requests=scan_requests,
                    boundaries=boundaries,
                )
            )
            live_scan.next_index += 1
            live_scan.task = None
            if live_scan.next_index < len(live_scan.plan):
                self._start_live_chainlink_scan_batch(live_scan)
                return
            self._live_chainlink_scan = None
            await self._finalize_chainlink_scan(
                requests=live_scan.requests,
                manifests=live_scan.manifests,
                deferred_request_ids=live_scan.deferred_request_ids,
                now_ms=now_ms,
            )
            return

        if self.config.rtds_manifest_dir is None:
            return
        requests: dict[str, FinalizedChainlinkBoundaryRequest] = {}
        for slug in tuple(sorted(self._verified)):
            if slug in self._label_tracking_complete:
                continue
            target = self._targets[slug]
            existing = self._chainlink_boundaries.get(slug, {})
            for boundary_role, boundary_ms in (
                ("open", target.opens_at_ms),
                ("close", target.closes_at_ms),
            ):
                if boundary_role in existing:
                    continue
                request_id = f"{slug}:{boundary_role}"
                requests[request_id] = FinalizedChainlinkBoundaryRequest(
                    target=target,
                    rule_url=self._chainlink_rule_url(target),
                    boundary_role=boundary_role,
                    first_decision_at=_iso_from_epoch_ms(now_ms),
                )
        if not requests:
            return
        manifests = self._finalized_rtds_manifests()
        if not manifests:
            return

        known_before = self._chainlink_known_request_ids
        new_request_ids = set(requests).difference(known_before)
        historical_request_ids = {
            request_id
            for request_id in new_request_ids
            if (
                requests[request_id].target.opens_at_ms
                if requests[request_id].boundary_role == "open"
                else requests[request_id].target.closes_at_ms
            )
            <= now_ms
        }
        unseen_manifests = tuple(
            path
            for path in manifests
            if path not in self._chainlink_scanned_manifest_paths
        )
        scan_plan: list[
            tuple[
                tuple[Path, ...],
                dict[str, FinalizedChainlinkBoundaryRequest],
            ]
        ] = []
        if not self._chainlink_scanned_manifest_paths:
            scan_plan.append((manifests, requests))
        else:
            if historical_request_ids:
                scan_plan.append(
                    (
                        manifests,
                        {
                            request_id: requests[request_id]
                            for request_id in sorted(
                                historical_request_ids
                            )
                        },
                    )
                )
            incremental_request_ids = set(requests).difference(
                historical_request_ids
            )
            if unseen_manifests and incremental_request_ids:
                scan_plan.append(
                    (
                        unseen_manifests,
                        {
                            request_id: requests[request_id]
                            for request_id in sorted(
                                incremental_request_ids
                            )
                        },
                    )
                )

        if live_clock and scan_plan:
            state = _LiveChainlinkScan(
                manifests=manifests,
                requests=requests,
                plan=tuple(scan_plan),
                deferred_request_ids=set(),
            )
            self._live_chainlink_scan = state
            self._start_live_chainlink_scan_batch(state)
            return

        deferred_request_ids: set[str] = set()
        for manifest_inventory, scan_requests in scan_plan:
            boundaries = await asyncio.to_thread(
                extract_chainlink_boundary_batch_from_finalized_manifests,
                manifest_inventory,
                requests=scan_requests,
            )
            if not isinstance(boundaries, Mapping):
                raise DiscoveryInputError(
                    "chainlink_boundary_batch_invalid",
                    "batch extractor returned a non-object result",
                )
            deferred_request_ids.update(
                self._merge_chainlink_scan_result(
                    requests=requests,
                    scan_requests=scan_requests,
                    boundaries=boundaries,
                )
            )
        await self._finalize_chainlink_scan(
            requests=requests,
            manifests=manifests,
            deferred_request_ids=deferred_request_ids,
            now_ms=now_ms,
        )

    async def _emit_due_ghost_decisions(
        self,
        *,
        now_ms: int,
    ) -> None:
        """Freeze one fixed-policy maker BUY decision from live evidence."""

        max_book_age_ms = 5_000
        strategy_config = {
            "strategy_id": "phase2-pessimistic-maker-buy-v1",
            "initial_cash": "100",
            "queue_scenario": "pessimistic",
            "trade_volume_haircut": "0.5",
            "settlement_operation_cost": "1",
            "settlement_operation_latency_ms": 120_000,
            "operation_cost_source": (
                "public_capture_pessimistic_upper_bound"
            ),
        }
        config_hash = hashlib.sha256(
            canonical_json_bytes(strategy_config)
        ).hexdigest()
        for slug in tuple(sorted(self._verified)):
            if (
                slug in self._ghost_decision_record_ids
                or slug in self._ghost_decision_exclusions
            ):
                continue
            target = self._targets[slug]
            if now_ms < target.opens_at_ms:
                continue
            if now_ms >= target.closes_at_ms:
                self._ghost_decision_exclusions[slug] = (
                    "decision_inputs_missing_before_close"
                )
                continue
            lifecycle = self.supervisor.target_snapshot(slug)
            if (
                lifecycle.state is not TargetLifecycleState.OPEN
                or lifecycle.group_id is None
            ):
                continue
            handle = self._workers.get(lifecycle.group_id)
            if handle is None or not handle.acknowledged:
                continue
            open_boundary = self._chainlink_boundaries.get(
                slug,
                {},
            ).get("open")
            if open_boundary is None:
                continue
            try:
                require_price_to_beat_for_decision(
                    open_boundary,
                    decision_at=_iso_from_epoch_ms(now_ms),
                )
            except PriceToBeatRejection as exc:
                self._ghost_decision_exclusions[slug] = exc.code
                continue
            inputs = handle.sink.ghost_decision_inputs(
                target,
                now_ms=now_ms,
                max_book_age_ms=max_book_age_ms,
            )
            if inputs is None:
                continue
            book, fee, resync = inputs
            bids = book.payload.get("bids")
            asks = book.payload.get("asks")
            fee_fields = fee.market.get("fd")
            try:
                if (
                    not isinstance(bids, list)
                    or not isinstance(asks, list)
                    or not bids
                    or not asks
                    or not isinstance(fee_fields, Mapping)
                    or set(fee_fields) != {"r", "e", "to"}
                    or not isinstance(fee_fields.get("to"), bool)
                ):
                    continue
                bid_levels = [
                    (
                        Decimal(str(level["price"])),
                        Decimal(str(level["size"])),
                    )
                    for level in bids
                    if isinstance(level, Mapping)
                ]
                ask_levels = [
                    (
                        Decimal(str(level["price"])),
                        Decimal(str(level["size"])),
                    )
                    for level in asks
                    if isinstance(level, Mapping)
                ]
                tick_size = Decimal(str(fee.market["mts"]))
                minimum_size = Decimal(str(fee.market["mos"]))
                best_bid = max(price for price, _ in bid_levels)
                best_ask = min(price for price, _ in ask_levels)
                visible_queue = sum(
                    (
                        size
                        for price, size in bid_levels
                        if price == best_bid
                    ),
                    Decimal("0"),
                )
            except (
                InvalidOperation,
                KeyError,
                TypeError,
                ValueError,
            ):
                continue
            if (
                not Decimal("0") < best_bid < best_ask < Decimal("1")
                or tick_size <= 0
                or minimum_size <= 0
                or best_bid % tick_size != 0
                or visible_queue <= 0
            ):
                continue
            decision_time = _iso_from_epoch_ms(now_ms)
            record = {
                "schema_version": "edge-lab.ghost-decision.v1",
                "source": "edge_lab_ghost_decision",
                "kind": "data",
                "event_type": "ghost_decision",
                "session_id": self._session_id,
                "connection_id": book.connection_id,
                "received_at": decision_time,
                "event_at": decision_time,
                "sequence": None,
                "monotonic_ns": time.monotonic_ns(),
                "payload": {
                    "experiment_id": (
                        "phase2-dynamic-short-crypto-tracer-v1"
                    ),
                    "strategy_config": strategy_config,
                    "config_hash": config_hash,
                    "announcement_record_id": (
                        target.announcement_record_id
                    ),
                    "condition_id": target.condition_id,
                    "market_id": target.condition_id,
                    "token_id": target.up_token_id,
                    "side": "BUY",
                    "price": str(best_bid),
                    "quantity": str(minimum_size),
                    "decision_receive_time": decision_time,
                    "l2_record_ids": [book.record_id],
                    "last_book_record_id": book.record_id,
                    "book_state_hash": book.payload.get("hash"),
                    "resnapshot_record_id": resync.record_id,
                    "visible_queue": str(visible_queue),
                    "best_bid": str(best_bid),
                    "best_ask": str(best_ask),
                    "tick_size": str(tick_size),
                    "minimum_order_size": str(minimum_size),
                    "submit_latency_ms": 250,
                    "cancel_latency_ms": 60_000,
                    "max_book_age_ms": max_book_age_ms,
                    "split": "test",
                    "fee_evidence_record_id": fee.record_id,
                    "ptb_source_record_id": open_boundary.record_id,
                    "ptb_commit_record_id": (
                        self._chainlink_commit_record_ids.get(
                            slug,
                            {},
                        ).get("open")
                    ),
                    "public_trade_side_contract": (
                        "data_api_taker_only_required"
                    ),
                },
            }
            run_at_frontier = getattr(
                handle.recorder,
                "run_at_clob_capture_frontier",
                None,
            )
            if not callable(run_at_frontier):
                self._ghost_decision_exclusions[slug] = (
                    "capture_frontier_unavailable"
                )
                continue

            async def persist_if_prefix_is_current() -> Any | None:
                return await handle.sink.emit_ghost_decision_if_current(
                    record,
                    target=target,
                    now_ms=now_ms,
                    max_book_age_ms=max_book_age_ms,
                    expected_inputs=inputs,
                )

            result = await run_at_frontier(
                persist_if_prefix_is_current
            )
            if result is None:
                continue
            await handle.sink.checkpoint(
                "edge_lab_ghost_decision",
                {
                    "schema_version": (
                        "edge-lab-ghost-decision-checkpoint.v1"
                    ),
                    "slug": slug,
                    "record_id": result.record_id,
                    "updated_at": decision_time,
                },
            )
            committed = await self._emit_control(
                now_ms=now_ms,
                event_type="ghost_decision_committed",
                kind="lifecycle",
                payload={
                    "slug": slug,
                    "decision_record_id": result.record_id,
                    "group_id": lifecycle.group_id,
                    "l2_record_id": book.record_id,
                    "fee_record_id": fee.record_id,
                    "ptb_source_record_id": open_boundary.record_id,
                },
            )
            await self._checkpoint_control_record(
                record_id=committed.record_id,
                reason="ghost_decision_committed",
                now_ms=now_ms,
            )
            submit_latency = await self._emit_control(
                now_ms=now_ms,
                event_type="pessimistic_submit_latency",
                kind="data",
                payload={
                    "schema_version": (
                        "edge-lab.execution-replay-submit-latency.v1"
                    ),
                    "evidence_role": "pessimistic_submit_latency",
                    "slug": slug,
                    "condition_id": target.condition_id,
                    "opens_at_ms": target.opens_at_ms,
                    "closes_at_ms": target.closes_at_ms,
                    "decision_record_id": result.record_id,
                    "latency_ms": 250,
                    "provenance": (
                        "public_capture_pessimistic_floor"
                    ),
                },
            )
            await self._checkpoint_control_record(
                record_id=submit_latency.record_id,
                reason="pessimistic_submit_latency_evidence",
                now_ms=now_ms,
            )
            settlement_cost = await self._emit_control(
                now_ms=now_ms,
                event_type="settlement_operation_cost",
                kind="data",
                payload={
                    "schema_version": (
                        "edge-lab.execution-replay-settlement-cost.v1"
                    ),
                    "evidence_role": "settlement_operation_cost",
                    "slug": slug,
                    "condition_id": target.condition_id,
                    "opens_at_ms": target.opens_at_ms,
                    "closes_at_ms": target.closes_at_ms,
                    "decision_record_id": result.record_id,
                    "amount": "1",
                    "currency": "USDC",
                    "provenance": (
                        "public_capture_pessimistic_upper_bound"
                    ),
                },
            )
            await self._checkpoint_control_record(
                record_id=settlement_cost.record_id,
                reason="settlement_operation_cost_evidence",
                now_ms=now_ms,
            )
            self._production_role_record_ids[slug] = {
                "pessimistic_submit_latency_evidence_id": (
                    submit_latency.record_id
                ),
                "settlement_operation_cost_evidence_id": (
                    settlement_cost.record_id
                ),
            }
            self._ghost_decision_record_ids[slug] = result.record_id
            self._ghost_decision_at_ms[slug] = now_ms

    async def _poll_data_api_trades(self, *, now_ms: int) -> None:
        """Persist public taker-only trades without relying on WS side."""

        fetch = getattr(self.source_client, "data_trades", None)
        if not callable(fetch):
            return
        for slug in tuple(sorted(self._ghost_decision_record_ids)):
            decision_at_ms = self._ghost_decision_at_ms[slug]
            if now_ms <= decision_at_ms:
                continue
            last_poll = self._last_data_trade_poll_ms.get(slug)
            if last_poll is not None and now_ms - last_poll < 30_000:
                continue
            target = self._targets[slug]
            lifecycle = self.supervisor.target_snapshot(slug)
            if lifecycle.group_id is None:
                continue
            handle = self._workers.get(lifecycle.group_id)
            if handle is None:
                continue
            self._last_data_trade_poll_ms[slug] = now_ms
            try:
                fetched = await asyncio.to_thread(
                    fetch,
                    target.condition_id,
                    taker_only=True,
                    limit=1_000,
                    offset=0,
                )
                raw = fetched.raw
                metadata = raw.metadata
                parsed = urlsplit(metadata.url)
                if (
                    metadata.source != "data_api_trades"
                    or metadata.method != "GET"
                    or parsed.scheme != "https"
                    or parsed.hostname
                    != "data-api.polymarket.com"
                    or parsed.path != "/trades"
                    or parsed.query
                    or parsed.fragment
                    or parsed.username is not None
                    or parsed.password is not None
                    or dict(metadata.request_params)
                    != {
                        "market": target.condition_id,
                        "takerOnly": "true",
                        "limit": 1_000,
                        "offset": 0,
                    }
                    or not 200 <= metadata.status_code < 300
                    or raw.text.encode("utf-8") != raw.body
                ):
                    raise DiscoveryInputError(
                        "unsafe_data_api_trade_response",
                        "Data API trade response lacks exact public provenance",
                    )
                raw_json = json.loads(raw.text, parse_float=str)
                if not isinstance(raw_json, list):
                    raise DiscoveryInputError(
                        "data_api_trade_schema_invalid",
                        "Data API trade response must be an array",
                    )
            except (PublicSourceError, DiscoveryInputError) as exc:
                await self._emit_control(
                    now_ms=now_ms,
                    event_type="data_api_trade_poll_failed",
                    kind="diagnostic",
                    payload={
                        "slug": slug,
                        **safe_error_details(
                            exc,
                            code=(
                                exc.code
                                if isinstance(exc, DiscoveryInputError)
                                else (
                                    exc.code
                                    or "data_api_trade_poll_failed"
                                )
                            ),
                        ),
                    },
                )
                continue
            received_at_ms = int(metadata.received_at * 1_000)
            result = await handle.sink.emit(
                {
                    "schema_version": (
                        "data-api.trades.taker-only.snapshot.v1"
                    ),
                    "source": "data_api_trades",
                    "kind": "snapshot",
                    "event_type": "data_api_trades",
                    "session_id": self._session_id,
                    "connection_id": None,
                    "received_at": _iso_from_epoch_ms(received_at_ms),
                    "event_at": None,
                    "sequence": None,
                    "monotonic_ns": time.monotonic_ns(),
                    "payload": {
                        "schema_version": (
                            "edge-lab-data-api-trades-snapshot.v1"
                        ),
                        "condition_id": target.condition_id,
                        "taker_only": True,
                        "raw_json": raw_json,
                        "raw_body_base64": base64.b64encode(
                            raw.body
                        ).decode("ascii"),
                        "provenance": {
                            "source": metadata.source,
                            "method": metadata.method,
                            "url": metadata.url,
                            "request_params": dict(
                                metadata.request_params
                            ),
                            "status_code": metadata.status_code,
                            "requested_at_epoch_seconds": repr(
                                metadata.requested_at
                            ),
                            "received_at_epoch_seconds": repr(
                                metadata.received_at
                            ),
                            "attempt": metadata.attempt,
                            "response_headers": (
                                sanitized_response_headers(
                                    metadata.response_headers
                                )
                            ),
                            "body_sha256": hashlib.sha256(
                                raw.body
                            ).hexdigest(),
                            "body_bytes": len(raw.body),
                        },
                    },
                }
            )
            self._data_api_trade_snapshot_count += 1
            self._data_api_trade_record_ids.setdefault(
                slug,
                [],
            ).append(result.record_id)
            decision_at_ms = self._ghost_decision_at_ms[slug]
            keys = self._data_api_trade_keys.setdefault(slug, set())
            for trade in raw_json:
                if not isinstance(trade, Mapping):
                    continue
                timestamp = trade.get("timestamp")
                if (
                    isinstance(timestamp, bool)
                    or not isinstance(timestamp, (str, int))
                    or not str(timestamp).isdigit()
                ):
                    continue
                trade_at_ms = int(str(timestamp)) * 1_000
                if (
                    trade_at_ms <= decision_at_ms
                    or trade_at_ms >= target.closes_at_ms
                    or str(
                        trade.get("conditionId", "")
                    ).lower()
                    != target.condition_id
                    or trade.get("asset")
                    not in {
                        target.up_token_id,
                        target.down_token_id,
                    }
                ):
                    continue
                try:
                    keys.add(data_api_trade_event_key(trade))
                except ValueError:
                    continue
            await self._emit_control(
                now_ms=now_ms,
                event_type="data_api_trade_snapshot_persisted",
                kind="data",
                payload={
                    "slug": slug,
                    "record_id": result.record_id,
                    "trade_count": len(raw_json),
                    "taker_only": True,
                },
            )

    async def _reconcile_target_settlement_legacy(
        self,
        target: ShortCryptoTarget,
        *,
        now_ms: int,
    ) -> None:
        snapshot = self.supervisor.target_snapshot(target.slug)
        if (
            snapshot.state is TargetLifecycleState.SETTLED
            or target.slug in self._label_tracking_complete
        ):
            return
        records = self._gamma_records.get(target.slug, [])
        evidence_record_id = (
            records[-1]["record_id"]
            if records
            else target.announcement_record_id
        )
        try:
            settlement = reconcile_short_crypto_settlement(
                target,
                records,
            )
        except SettlementRejection as exc:
            await self._control_sink.checkpoint(
                "gamma_http",
                {
                    "schema_version": (
                        "edge-lab-dynamic-short-crypto-settlement.v1"
                    ),
                    "slug": target.slug,
                    "reason": "settlement_evidence_rejected",
                    "updated_at": _iso_from_epoch_ms(now_ms),
                },
            )
            rejection_event = await self._emit_control(
                now_ms=now_ms,
                event_type="settlement_evidence_rejected",
                kind="diagnostic",
                payload={
                    "slug": target.slug,
                    "evidence_record_id": evidence_record_id,
                    **safe_error_details(exc, code=exc.code),
                },
            )
            await self._control_sink.checkpoint(
                "dynamic_short_crypto_service",
                {
                    "schema_version": (
                        "edge-lab-dynamic-short-crypto-settlement.v1"
                    ),
                    "slug": target.slug,
                    "event_record_id": rejection_event.record_id,
                    "updated_at": _iso_from_epoch_ms(now_ms),
                },
            )
            if snapshot.state is not TargetLifecycleState.EXCLUDED:
                self.supervisor.exclude(
                    target.slug,
                    reason=f"settlement_evidence_rejected:{exc.code}",
                    evidence_record_id=rejection_event.record_id,
                    observed_at_ms=now_ms,
                )
            self._label_status[target.slug] = "rejected"
            self._label_tracking_complete.add(target.slug)
            self._gamma_records.pop(target.slug, None)
            return

        timed_out = (
            now_ms
            >= self._settlement_deadline_ms(target)
        )
        chainlink_boundary_status = (
            "verified"
            if set(
                self._chainlink_boundaries.get(target.slug, {})
            )
            >= {"open", "close"}
            else "missing"
        )
        liveness_proof: _CloseLivenessProof | None = None
        if snapshot.group_id is not None:
            handle = self._workers.get(snapshot.group_id)
            if handle is not None:
                liveness_proof = handle.sink.close_liveness_proof(
                    closes_at_ms=target.closes_at_ms,
                    tolerance_ms=(
                        self.config.close_liveness_tolerance_ms
                    ),
                    as_of_ms=now_ms,
                )

        action = "pending"
        exclusion_reason: str | None = None
        if settlement.status == "conflict":
            action = "capture_excluded_settlement_conflict"
            exclusion_reason = (
                "settlement_conflict:"
                + ",".join(settlement.reason_codes)
            )
            self._label_status[target.slug] = "conflict"
        elif settlement.status == "resolved" and settlement.label is not None:
            # Gamma's one-hot winner is only a candidate label.  An excluded
            # capture remains excluded even when exact Chainlink boundaries
            # were committed later.
            self._label_status[target.slug] = (
                "gamma_resolved_chainlink_pending"
            )
            action = (
                "gamma_label_capture_excluded"
                if snapshot.state is TargetLifecycleState.EXCLUDED
                else "gamma_label_pending_chainlink"
            )
            if timed_out:
                if liveness_proof is None:
                    exclusion_reason = "clob_close_liveness_timeout"
                else:
                    exclusion_reason = (
                        "chainlink_boundary_evidence_missing_after_timeout"
                    )
                action = "capture_excluded_validation_timeout"
        elif timed_out:
            exclusion_reason = (
                f"settlement_{settlement.status}_after_timeout"
            )
            action = "capture_excluded_settlement_timeout"

        proof_payload = {
            "status": "missing"
            if liveness_proof is None
            else "verified",
            "connection_id": (
                None
                if liveness_proof is None
                else liveness_proof.connection_id
            ),
            "before_record_id": (
                None
                if liveness_proof is None
                else liveness_proof.before.record_id
            ),
            "before_received_at_ms": (
                None
                if liveness_proof is None
                else liveness_proof.before.received_at_ms
            ),
            "after_record_id": (
                None
                if liveness_proof is None
                else liveness_proof.after.record_id
            ),
            "after_received_at_ms": (
                None
                if liveness_proof is None
                else liveness_proof.after.received_at_ms
            ),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "status": settlement.status,
                    "action": action,
                    "reason_codes": settlement.reason_codes,
                    "gamma_record_ids": settlement.gamma_record_ids,
                    "capture_state": snapshot.state.value,
                    "proof": proof_payload,
                    "exclusion_reason": exclusion_reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if (
            exclusion_reason is None
            and self._last_settlement_fingerprint.get(target.slug)
            == fingerprint
        ):
            return
        self._last_settlement_fingerprint[target.slug] = fingerprint

        if exclusion_reason is not None:
            await self._control_sink.checkpoint(
                "gamma_http",
                {
                    "schema_version": (
                        "edge-lab-dynamic-short-crypto-settlement.v1"
                    ),
                    "slug": target.slug,
                    "reason": exclusion_reason,
                    "updated_at": _iso_from_epoch_ms(now_ms),
                },
            )
            if snapshot.group_id is not None:
                handle = self._workers.get(snapshot.group_id)
                if handle is not None:
                    await handle.sink.checkpoint(
                        "clob_market_ws",
                        {
                            "schema_version": (
                                "edge-lab-dynamic-short-crypto-"
                                "close-liveness.v1"
                            ),
                            "slug": target.slug,
                            "updated_at": _iso_from_epoch_ms(now_ms),
                        },
                    )
                    liveness_proof = handle.sink.close_liveness_proof(
                        closes_at_ms=target.closes_at_ms,
                        tolerance_ms=(
                            self.config.close_liveness_tolerance_ms
                        ),
                        as_of_ms=now_ms,
                    )
                    proof_payload = {
                        "status": (
                            "missing"
                            if liveness_proof is None
                            else "verified"
                        ),
                        "connection_id": (
                            None
                            if liveness_proof is None
                            else liveness_proof.connection_id
                        ),
                        "before_record_id": (
                            None
                            if liveness_proof is None
                            else liveness_proof.before.record_id
                        ),
                        "before_received_at_ms": (
                            None
                            if liveness_proof is None
                            else liveness_proof.before.received_at_ms
                        ),
                        "after_record_id": (
                            None
                            if liveness_proof is None
                            else liveness_proof.after.record_id
                        ),
                        "after_received_at_ms": (
                            None
                            if liveness_proof is None
                            else liveness_proof.after.received_at_ms
                        ),
                    }
                    if (
                        settlement.status == "resolved"
                        and settlement.label is not None
                    ):
                        exclusion_reason = (
                            "clob_close_liveness_timeout"
                            if liveness_proof is None
                            else (
                                "chainlink_boundary_evidence_"
                                "missing_after_timeout"
                            )
                        )

        settlement_event = await self._emit_control(
            now_ms=now_ms,
            event_type="settlement_reconciled",
            kind="diagnostic",
            payload={
                "slug": target.slug,
                "status": settlement.status,
                "action": action,
                "reason_codes": list(settlement.reason_codes),
                "gamma_record_ids": list(settlement.gamma_record_ids),
                "evidence_record_id": evidence_record_id,
                "outcome": (
                    None
                    if settlement.label is None
                    else settlement.label.outcome
                ),
                "available_at_ms": (
                    None
                    if settlement.label is None
                    else settlement.label.available_at_ms
                ),
                "chainlink_boundary_status": chainlink_boundary_status,
                "close_liveness": proof_payload,
                "exclusion_reason": exclusion_reason,
            },
        )
        if exclusion_reason is not None:
            await self._control_sink.checkpoint(
                "dynamic_short_crypto_service",
                {
                    "schema_version": (
                        "edge-lab-dynamic-short-crypto-settlement.v1"
                    ),
                    "slug": target.slug,
                    "event_record_id": settlement_event.record_id,
                    "updated_at": _iso_from_epoch_ms(now_ms),
                },
            )
            if snapshot.state is not TargetLifecycleState.EXCLUDED:
                self.supervisor.exclude(
                    target.slug,
                    reason=exclusion_reason,
                    evidence_record_id=settlement_event.record_id,
                    observed_at_ms=now_ms,
                )
            self._label_tracking_complete.add(target.slug)
            self._gamma_records.pop(target.slug, None)

    async def _reconcile_target_settlement(
        self,
        target: ShortCryptoTarget,
        *,
        now_ms: int,
    ) -> None:
        """Apply the finalized Chainlink/Gamma/PONG atomic settlement gate."""

        lifecycle = self.supervisor.target_snapshot(target.slug)
        if (
            lifecycle.state is TargetLifecycleState.SETTLED
            or target.slug in self._label_tracking_complete
        ):
            return
        if lifecycle.state is TargetLifecycleState.EXCLUDED:
            await self._reconcile_target_settlement_legacy(
                target,
                now_ms=now_ms,
            )
            return
        records = self._gamma_records.get(target.slug, [])
        try:
            reconciliation = reconcile_short_crypto_settlement(
                target,
                records,
            )
        except SettlementRejection as exc:
            rejection = await self._emit_control(
                now_ms=now_ms,
                event_type="settlement_evidence_rejected",
                kind="diagnostic",
                payload={
                    "slug": target.slug,
                    **safe_error_details(exc, code=exc.code),
                },
            )
            await self._checkpoint_control_record(
                record_id=rejection.record_id,
                reason="settlement_evidence_rejected",
                now_ms=now_ms,
            )
            self.supervisor.exclude(
                target.slug,
                reason=f"settlement_evidence_rejected:{exc.code}",
                evidence_record_id=rejection.record_id,
                observed_at_ms=now_ms,
            )
            self._label_status[target.slug] = "rejected"
            self._label_tracking_complete.add(target.slug)
            self._gamma_records.pop(target.slug, None)
            return

        handle: _WorkerHandle | None = None
        private_liveness: _CloseLivenessProof | None = None
        if lifecycle.group_id is not None:
            handle = self._workers.get(lifecycle.group_id)
            if handle is not None:
                private_liveness = handle.sink.close_liveness_proof(
                    closes_at_ms=target.closes_at_ms,
                    tolerance_ms=(
                        self.config.close_liveness_tolerance_ms
                    ),
                    as_of_ms=now_ms,
                )
        strict_liveness = (
            None
            if private_liveness is None
            else StrictCloseLivenessProof(
                before=ClosePongObservation(
                    record_id=private_liveness.before.record_id,
                    session_id=private_liveness.before.session_id,
                    connection_id=(
                        private_liveness.before.connection_id
                    ),
                    received_at_ms=(
                        private_liveness.before.received_at_ms
                    ),
                ),
                after=ClosePongObservation(
                    record_id=private_liveness.after.record_id,
                    session_id=private_liveness.after.session_id,
                    connection_id=(
                        private_liveness.after.connection_id
                    ),
                    received_at_ms=(
                        private_liveness.after.received_at_ms
                    ),
                ),
                tolerance_ms=self.config.close_liveness_tolerance_ms,
            )
        )
        by_role = self._chainlink_boundaries.get(target.slug, {})
        boundaries = (
            FinalizedChainlinkBoundaries(
                open_boundary=by_role["open"],
                close_boundary=by_role["close"],
            )
            if set(by_role) >= {"open", "close"}
            else None
        )
        proof_payload = {
            "status": (
                "missing"
                if private_liveness is None
                else "verified"
            ),
            "session_id": (
                None
                if private_liveness is None
                else private_liveness.before.session_id
            ),
            "connection_id": (
                None
                if private_liveness is None
                else private_liveness.connection_id
            ),
            "before_record_id": (
                None
                if private_liveness is None
                else private_liveness.before.record_id
            ),
            "before_received_at_ms": (
                None
                if private_liveness is None
                else private_liveness.before.received_at_ms
            ),
            "after_record_id": (
                None
                if private_liveness is None
                else private_liveness.after.record_id
            ),
            "after_received_at_ms": (
                None
                if private_liveness is None
                else private_liveness.after.received_at_ms
            ),
        }

        if reconciliation.status == "conflict":
            conflict = await self._emit_control(
                now_ms=now_ms,
                event_type="settlement_reconciled",
                kind="diagnostic",
                payload={
                    "slug": target.slug,
                    "status": "conflict",
                    "action": "capture_excluded_settlement_conflict",
                    "reason_codes": list(reconciliation.reason_codes),
                    "gamma_record_ids": list(
                        reconciliation.gamma_record_ids
                    ),
                    "outcome": None,
                    "available_at_ms": None,
                    "chainlink_boundary_status": (
                        "verified" if boundaries is not None else "missing"
                    ),
                    "close_liveness": proof_payload,
                    "exclusion_reason": (
                        "settlement_conflict:"
                        + ",".join(reconciliation.reason_codes)
                    ),
                },
            )
            await self._checkpoint_control_record(
                record_id=conflict.record_id,
                reason="settlement_conflict",
                now_ms=now_ms,
            )
            self.supervisor.exclude(
                target.slug,
                reason=(
                    "settlement_conflict:"
                    + ",".join(reconciliation.reason_codes)
                ),
                evidence_record_id=conflict.record_id,
                observed_at_ms=now_ms,
            )
            self._label_status[target.slug] = "settlement_conflict"
            self._label_tracking_complete.add(target.slug)
            self._gamma_records.pop(target.slug, None)
            return

        gate = self._settlement_gates.setdefault(
            target.slug,
            AtomicSettlementGate(target),
        )

        async def persist_settlement(
            commit_payload: dict[str, object],
        ) -> SettlementCommitReceipt:
            if handle is None or strict_liveness is None:
                raise SettlementRejection(
                    "settlement_commit_liveness_missing",
                    "CLOB liveness vanished before finalization",
                )
            await handle.sink.checkpoint(
                "clob_market_ws",
                {
                    "schema_version": (
                        "edge-lab-dynamic-short-crypto-close-liveness.v1"
                    ),
                    "slug": target.slug,
                    "before_record_id": (
                        strict_liveness.before.record_id
                    ),
                    "after_record_id": strict_liveness.after.record_id,
                    "updated_at": _iso_from_epoch_ms(now_ms),
                },
            )
            if target.slug in self._last_data_trade_poll_ms:
                await handle.sink.checkpoint(
                    "data_api_trades",
                    {
                        "schema_version": (
                            "edge-lab-data-api-trades-checkpoint.v1"
                        ),
                        "slug": target.slug,
                        "updated_at": _iso_from_epoch_ms(now_ms),
                    },
                )
            await self._control_sink.checkpoint(
                "gamma_http",
                {
                    "schema_version": (
                        "edge-lab-dynamic-short-crypto-settlement.v1"
                    ),
                    "slug": target.slug,
                    "gamma_record_ids": list(
                        reconciliation.gamma_record_ids
                    ),
                    "updated_at": _iso_from_epoch_ms(now_ms),
                },
            )
            close_checkpoint = await self._emit_control(
                now_ms=now_ms,
                event_type="full_window_capture_close_checkpoint",
                kind="data",
                payload={
                    "schema_version": (
                        "edge-lab.execution-replay-close-checkpoint.v1"
                    ),
                    "evidence_role": (
                        "full_window_capture_close_checkpoint"
                    ),
                    "slug": target.slug,
                    "condition_id": target.condition_id,
                    "opens_at_ms": target.opens_at_ms,
                    "closes_at_ms": target.closes_at_ms,
                    "group_id": lifecycle.group_id,
                    "finalized_through_ms": now_ms,
                    "checkpoint_sources": [
                        "clob_market_ws",
                        "data_api_trades",
                        "gamma_http",
                    ],
                },
            )
            await self._checkpoint_control_record(
                record_id=close_checkpoint.record_id,
                reason="full_window_capture_close_checkpoint",
                now_ms=now_ms,
            )
            l2_closure = await self._emit_control(
                now_ms=now_ms,
                event_type="decision_to_trade_l2_closure",
                kind="data",
                payload={
                    "schema_version": (
                        "edge-lab.execution-replay-l2-closure.v1"
                    ),
                    "evidence_role": "decision_to_trade_l2_closure",
                    "slug": target.slug,
                    "condition_id": target.condition_id,
                    "opens_at_ms": target.opens_at_ms,
                    "closes_at_ms": target.closes_at_ms,
                    "group_id": lifecycle.group_id,
                    "decision_count": (
                        1
                        if target.slug
                        in self._ghost_decision_record_ids
                        else 0
                    ),
                    "decision_record_ids": (
                        []
                        if target.slug
                        not in self._ghost_decision_record_ids
                        else [
                            self._ghost_decision_record_ids[
                                target.slug
                            ]
                        ]
                    ),
                    "public_trade_count": len(
                        self._data_api_trade_keys.get(
                            target.slug,
                            set(),
                        )
                    ),
                    "public_trade_snapshot_record_ids": list(
                        self._data_api_trade_record_ids.get(
                            target.slug,
                            [],
                        )
                    ),
                    "closed_through_ms": target.closes_at_ms,
                    "inventory_semantics": (
                        "all_finalized_group_batches_at_close_checkpoint"
                    ),
                },
            )
            await self._checkpoint_control_record(
                record_id=l2_closure.record_id,
                reason="decision_to_trade_l2_closure",
                now_ms=now_ms,
            )
            self._production_role_record_ids.setdefault(
                target.slug,
                {},
            ).update(
                {
                    "full_window_capture_close_checkpoint_id": (
                        close_checkpoint.record_id
                    ),
                    "decision_to_trade_l2_closure_id": (
                        l2_closure.record_id
                    ),
                }
            )
            settlement_event = await self._emit_control(
                now_ms=now_ms,
                event_type="settlement_reconciled",
                kind="data",
                payload={
                    **commit_payload,
                    "status": "resolved",
                    "action": "strict_settlement_committed",
                    "reason_codes": [],
                    "gamma_record_ids": list(
                        reconciliation.gamma_record_ids
                    ),
                    "outcome": (
                        None
                        if reconciliation.label is None
                        else reconciliation.label.outcome
                    ),
                    "available_at_ms": (
                        None
                        if reconciliation.label is None
                        else reconciliation.label.available_at_ms
                    ),
                    "chainlink_boundary_status": "committed",
                    "close_liveness": proof_payload,
                    "exclusion_reason": None,
                },
            )
            await self._checkpoint_control_record(
                record_id=settlement_event.record_id,
                reason="strict_settlement_commit",
                now_ms=now_ms,
            )
            required = commit_payload.get("required_record_ids")
            required_ids = (
                required
                if isinstance(required, list)
                else []
            )
            durable_ids = {
                item
                for item in required_ids
                if isinstance(item, str)
            }
            durable_ids.add(settlement_event.record_id)
            durable_ids.update(
                self._chainlink_commit_record_ids.get(
                    target.slug,
                    {},
                ).values()
            )
            return SettlementCommitReceipt(
                record_id=settlement_event.record_id,
                durable_finalized_record_ids=frozenset(durable_ids),
            )

        strict = await gate.reconcile_and_commit(
            reconciliation=reconciliation,
            boundaries=boundaries,
            close_liveness=strict_liveness,
            now_ms=now_ms,
            settlement_deadline_ms=self._settlement_deadline_ms(target),
            persist=persist_settlement,
        )
        if strict.state == "SETTLED":
            assert strict.commit_record_id is not None
            self.supervisor.mark_settled(
                target.slug,
                settlement_record_id=strict.commit_record_id,
                observed_at_ms=now_ms,
            )
            self._label_status[target.slug] = "strict_settled"
            self._label_tracking_complete.add(target.slug)
            self._gamma_records.pop(target.slug, None)
            return

        if strict.state in {"EXCLUDED", "settlement_conflict"}:
            if strict.state == "settlement_conflict":
                exclusion_reason = (
                    "settlement_conflict:"
                    + ",".join(strict.reason_codes)
                )
            elif strict_liveness is None:
                exclusion_reason = "clob_close_liveness_timeout"
            elif boundaries is None:
                exclusion_reason = (
                    "chainlink_boundary_evidence_missing_after_timeout"
                )
            else:
                exclusion_reason = (
                    "settlement_validation_timeout:"
                    + ",".join(strict.reason_codes)
                )
            terminal = await self._emit_control(
                now_ms=now_ms,
                event_type="settlement_reconciled",
                kind="diagnostic",
                payload={
                    "slug": target.slug,
                    "status": reconciliation.status,
                    "action": "capture_excluded_validation_timeout",
                    "reason_codes": list(strict.reason_codes),
                    "gamma_record_ids": list(
                        reconciliation.gamma_record_ids
                    ),
                    "outcome": (
                        None
                        if reconciliation.label is None
                        else reconciliation.label.outcome
                    ),
                    "available_at_ms": (
                        None
                        if reconciliation.label is None
                        else reconciliation.label.available_at_ms
                    ),
                    "chainlink_boundary_status": (
                        "verified" if boundaries is not None else "missing"
                    ),
                    "close_liveness": proof_payload,
                    "exclusion_reason": exclusion_reason,
                },
            )
            await self._checkpoint_control_record(
                record_id=terminal.record_id,
                reason="strict_settlement_terminal_exclusion",
                now_ms=now_ms,
            )
            self.supervisor.exclude(
                target.slug,
                reason=exclusion_reason,
                evidence_record_id=terminal.record_id,
                observed_at_ms=now_ms,
            )
            self._label_status[target.slug] = (
                "settlement_conflict"
                if strict.state == "settlement_conflict"
                else "excluded"
            )
            self._label_tracking_complete.add(target.slug)
            self._gamma_records.pop(target.slug, None)
            return

        self._label_status[target.slug] = (
            "gamma_resolved_chainlink_pending"
            if reconciliation.status == "resolved"
            else reconciliation.status
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "state": strict.state,
                    "reason_codes": strict.reason_codes,
                    "gamma_record_ids": (
                        reconciliation.gamma_record_ids
                    ),
                    "boundary_record_ids": (
                        []
                        if boundaries is None
                        else [
                            boundaries.open_boundary.record_id,
                            boundaries.close_boundary.record_id,
                        ]
                    ),
                    "close_liveness": proof_payload,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if self._last_settlement_fingerprint.get(target.slug) == fingerprint:
            return
        self._last_settlement_fingerprint[target.slug] = fingerprint
        await self._emit_control(
            now_ms=now_ms,
            event_type="settlement_reconciled",
            kind="diagnostic",
            payload={
                "slug": target.slug,
                "status": reconciliation.status,
                "action": "strict_settlement_pending",
                "reason_codes": list(strict.reason_codes),
                "gamma_record_ids": list(
                    reconciliation.gamma_record_ids
                ),
                "outcome": (
                    None
                    if reconciliation.label is None
                    else reconciliation.label.outcome
                ),
                "available_at_ms": (
                    None
                    if reconciliation.label is None
                    else reconciliation.label.available_at_ms
                ),
                "chainlink_boundary_status": (
                    "verified" if boundaries is not None else "missing"
                ),
                "close_liveness": proof_payload,
                "exclusion_reason": None,
            },
        )

    async def _enforce_settlement_timeouts(self, *, now_ms: int) -> None:
        for slug in tuple(sorted(self._verified)):
            target = self._targets[slug]
            if (
                now_ms < target.closes_at_ms
                or not self._target_known_to_supervisor(slug)
                or slug in self._label_tracking_complete
            ):
                continue
            state = self.supervisor.target_snapshot(slug).state
            if state is TargetLifecycleState.SETTLED:
                continue
            await self._reconcile_target_settlement(
                target,
                now_ms=now_ms,
            )

    async def _start_worker(
        self,
        decision: CaptureDecision,
        *,
        now_ms: int,
    ) -> None:
        _verify_live_order_guard()
        if decision.group_id in self._workers:
            return
        targets = [self._targets[item.slug] for item in decision.targets]
        group_root = self.config.output_root / "groups" / decision.group_id
        sink = _ObservedRecorderSink(
            CaptureStoreRecorderSink(
                CaptureStore(group_root),
                max_records_per_batch=self.config.max_records_per_batch,
            ),
            asset_ids=decision.asset_ids,
        )
        _require_clean_output_integrity(
            sink.startup_integrity_report,
            root=group_root,
        )
        snapshots = PublicSourcesSnapshotClient(
            self.source_client,
            condition_ids=tuple(target.condition_id for target in targets),
        )
        recorder = self.recorder_factory(
            config=RecorderConfig(
                clob_asset_ids=decision.asset_ids,
                rtds_enabled=False,
                rtds_subscriptions=(),
                initial_clob_resnapshot=True,
                snapshot_intervals={
                    "clob": self.config.clob_snapshot_interval_seconds
                },
                checkpoint_every_records=(
                    self.config.max_records_per_batch
                ),
                sink_timeout_seconds=60.0,
                checkpoint_timeout_seconds=60.0,
            ),
            sink=sink,
            snapshot_client=snapshots,
            websocket_factory=self.websocket_factory,
        )
        task = asyncio.create_task(
            recorder.run(),
            name=f"dynamic-short-crypto-{decision.group_id[:12]}",
        )
        self._workers[decision.group_id] = _WorkerHandle(
            decision=decision,
            recorder=recorder,
            sink=sink,
            task=task,
            generation=self._callback_generation,
        )
        receipt = await self._emit_control(
            now_ms=now_ms,
            event_type="worker_start_applied",
            kind="lifecycle",
            payload={
                "decision_id": decision.decision_id,
                "group_id": decision.group_id,
                "asset_ids": list(decision.asset_ids),
                "target_slugs": [
                    target.slug for target in decision.targets
                ],
            },
        )
        await self._checkpoint_control_record(
            record_id=receipt.record_id,
            reason="worker_start_receipt",
            now_ms=now_ms,
        )
        await asyncio.sleep(0)

    async def _process_worker_lifecycle(self, *, now_ms: int) -> None:
        for group_id, handle in tuple(self._workers.items()):
            if handle.generation != self._callback_generation:
                handle.sink.lifecycle_events.clear()
                continue
            while handle.sink.lifecycle_events:
                event = handle.sink.lifecycle_events.popleft()
                event_type = event["event_type"]
                observed_at_ms = event["received_at_ms"]
                evidence_record_id = event["record_id"]
                if event_type == "disconnected":
                    await handle.sink.checkpoint(
                        "clob_market_ws",
                        {
                            "schema_version": (
                                "edge-lab-dynamic-short-crypto-"
                                "disconnect.v1"
                            ),
                            "group_id": group_id,
                            "disconnect_record_id": evidence_record_id,
                            "updated_at": _iso_from_epoch_ms(
                                observed_at_ms
                            ),
                        },
                    )
                    gap_started_at_ms = event["gap_started_at_ms"]
                    if not handle.acknowledged:
                        await self._emit_control(
                            now_ms=now_ms,
                            event_type="worker_initial_readiness_cleared",
                            kind="diagnostic",
                            payload={
                                "group_id": group_id,
                                "evidence_record_id": evidence_record_id,
                            },
                        )
                        continue
                    for binding in handle.decision.targets:
                        target = self._targets[binding.slug]
                        snapshot = self.supervisor.target_snapshot(
                            binding.slug
                        )
                        if (
                            gap_started_at_ms < target.closes_at_ms
                            and observed_at_ms >= target.opens_at_ms
                            and snapshot.state
                            not in {
                                TargetLifecycleState.SETTLED,
                                TargetLifecycleState.EXCLUDED,
                            }
                        ):
                            self.supervisor.exclude(
                                binding.slug,
                                reason=(
                                    "websocket_disconnect_during_market"
                                ),
                                evidence_record_id=evidence_record_id,
                                observed_at_ms=observed_at_ms,
                            )
                    remaining = [
                        binding
                        for binding in handle.decision.targets
                        if self.supervisor.target_snapshot(
                            binding.slug
                        ).state
                        not in {
                            TargetLifecycleState.SETTLED,
                            TargetLifecycleState.EXCLUDED,
                        }
                    ]
                    if not remaining:
                        await self._emit_control(
                            now_ms=now_ms,
                            event_type="worker_capture_gap_terminal",
                            kind="diagnostic",
                            payload={
                                "group_id": group_id,
                                "evidence_record_id": evidence_record_id,
                            },
                        )
                        continue
                    handle.disconnected_at_ms = (
                        gap_started_at_ms
                        if handle.disconnected_at_ms is None
                        else min(
                            handle.disconnected_at_ms,
                            gap_started_at_ms,
                        )
                    )
                    handle.disconnect_record_id = evidence_record_id
                    try:
                        requirement = self.supervisor.on_disconnect(
                            group_id,
                            disconnected_at_ns=event[
                                "gap_started_monotonic_ns"
                            ],
                            observed_at_ms=observed_at_ms,
                        )
                    except CaptureSupervisorError as exc:
                        await self._emit_control(
                            now_ms=now_ms,
                            event_type="worker_disconnect_rejected",
                            kind="diagnostic",
                            payload={
                                "group_id": group_id,
                                **safe_error_details(
                                    exc,
                                    code="worker_disconnect_rejected",
                                ),
                            },
                        )
                        continue
                    await self._persist_decision(
                        requirement,
                        now_ms=now_ms,
                    )
                    await self._emit_control(
                        now_ms=now_ms,
                        event_type="worker_resnapshot_required",
                        kind="lifecycle",
                        payload={
                            "group_id": group_id,
                            "decision_id": requirement.decision_id,
                            "evidence_record_id": evidence_record_id,
                            "gap_evidence_record_id": event[
                                "gap_evidence_record_id"
                            ],
                            "gap_started_at_ms": gap_started_at_ms,
                            "disconnect_detected_at_ms": observed_at_ms,
                            "minimum_snapshot_watermark_ns": (
                                requirement.minimum_snapshot_watermark_ns
                            ),
                        },
                    )
                    continue

                if not handle.acknowledged:
                    continue
                group = self.supervisor.group_snapshot(group_id)
                if not group.resync_required:
                    continue
                watermark_ns = event["watermark_ns"]
                completion_monotonic_ns = event["monotonic_ns"]
                disconnected_at_ms = handle.disconnected_at_ms
                self.supervisor.complete_resnapshot(
                    group_id,
                    snapshot_watermark_ns=completion_monotonic_ns,
                    completed_at_ms=observed_at_ms,
                )
                for binding in handle.decision.targets:
                    target = self._targets[binding.slug]
                    snapshot = self.supervisor.target_snapshot(
                        binding.slug
                    )
                    if (
                        disconnected_at_ms is not None
                        and disconnected_at_ms < target.closes_at_ms
                        and observed_at_ms >= target.opens_at_ms
                        and snapshot.state
                        not in {
                            TargetLifecycleState.SETTLED,
                            TargetLifecycleState.EXCLUDED,
                        }
                    ):
                        self.supervisor.exclude(
                            binding.slug,
                            reason="resnapshot_not_ready_before_open",
                            evidence_record_id=evidence_record_id,
                            observed_at_ms=observed_at_ms,
                        )
                handle.disconnected_at_ms = None
                handle.disconnect_record_id = None
                await self._emit_control(
                    now_ms=now_ms,
                    event_type="worker_resnapshot_complete",
                    kind="lifecycle",
                    payload={
                        "group_id": group_id,
                        "evidence_record_id": evidence_record_id,
                        "completion_monotonic_ns": (
                            completion_monotonic_ns
                        ),
                        "server_watermark_ns": watermark_ns,
                        "asset_server_watermarks_ns": event[
                            "asset_watermarks_ns"
                        ],
                        "gap_started_at_ms": disconnected_at_ms,
                        "gap_completed_at_ms": observed_at_ms,
                    },
                )

    async def _enforce_resync_gaps(self, *, now_ms: int) -> None:
        for group_id, handle in tuple(self._workers.items()):
            if (
                not handle.acknowledged
                or handle.disconnected_at_ms is None
            ):
                continue
            group = self.supervisor.group_snapshot(group_id)
            if not group.resync_required:
                continue
            evidence_record_id = (
                handle.disconnect_record_id
                or handle.decision.targets[0].announcement_record_id
            )
            for binding in handle.decision.targets:
                target = self._targets[binding.slug]
                snapshot = self.supervisor.target_snapshot(binding.slug)
                if (
                    handle.disconnected_at_ms < target.closes_at_ms
                    and now_ms >= target.opens_at_ms
                    and snapshot.state
                    not in {
                        TargetLifecycleState.SETTLED,
                        TargetLifecycleState.EXCLUDED,
                    }
                ):
                    self.supervisor.exclude(
                        binding.slug,
                        reason="resnapshot_not_ready_before_open",
                        evidence_record_id=evidence_record_id,
                        observed_at_ms=now_ms,
                    )
            if not all(
                self.supervisor.target_snapshot(binding.slug).state
                in {
                    TargetLifecycleState.SETTLED,
                    TargetLifecycleState.EXCLUDED,
                }
                for binding in handle.decision.targets
            ):
                continue
            self.supervisor.abandon_resnapshot(
                group_id,
                observed_at_ms=now_ms,
            )
            handle.disconnected_at_ms = None
            handle.disconnect_record_id = None
            await self._emit_control(
                now_ms=now_ms,
                event_type="worker_resnapshot_abandoned",
                kind="diagnostic",
                payload={
                    "group_id": group_id,
                    "evidence_record_id": evidence_record_id,
                    "reason": "all_targets_terminal",
                },
            )

    async def _acknowledge_ready_workers(self, *, now_ms: int) -> None:
        for group_id, handle in tuple(self._workers.items()):
            if handle.acknowledged or not handle.sink.ready.is_set():
                continue
            if (
                handle.sink.ready_at_ms is None
                or handle.sink.watermark_ns is None
            ):
                continue
            try:
                self.supervisor.acknowledge(
                    handle.decision.decision_id,
                    applied_at_ms=handle.sink.ready_at_ms,
                    initial_snapshot_watermark_ns=handle.sink.watermark_ns,
                )
            except CaptureSupervisorError as exc:
                await self._emit_control(
                    now_ms=now_ms,
                    event_type="worker_readiness_rejected",
                    kind="diagnostic",
                    payload={
                        "group_id": group_id,
                        **safe_error_details(
                            exc,
                            code="worker_readiness_rejected",
                        ),
                    },
                )
                await self._stop_worker(group_id, now_ms=now_ms, acknowledge=False)
                continue
            handle.acknowledged = True
            await self._emit_control(
                now_ms=now_ms,
                event_type="worker_ready",
                kind="lifecycle",
                payload={
                    "decision_id": handle.decision.decision_id,
                    "group_id": group_id,
                    "snapshot_watermark_ns": handle.sink.watermark_ns,
                },
            )

    async def _handle_worker_failures(self, *, now_ms: int) -> None:
        for group_id, handle in tuple(self._workers.items()):
            if not handle.task.done():
                continue
            if handle.task.cancelled():
                error: BaseException = asyncio.CancelledError()
                code = "worker_cancelled_unexpectedly"
            else:
                error = handle.task.exception() or RuntimeError(
                    "capture worker ended before an unsubscribe decision"
                )
                code = "worker_failed"
            failure_event = await self._emit_control(
                now_ms=now_ms,
                event_type="worker_failed",
                kind="diagnostic",
                payload={
                    "group_id": group_id,
                    **safe_error_details(error, code=code),
                },
            )
            await self._checkpoint_control_record(
                record_id=failure_event.record_id,
                reason="worker_failure_evidence",
                now_ms=now_ms,
            )
            for binding in handle.decision.targets:
                snapshot = self.supervisor.target_snapshot(binding.slug)
                if snapshot.state in {
                    TargetLifecycleState.SETTLED,
                    TargetLifecycleState.EXCLUDED,
                }:
                    continue
                self.supervisor.exclude(
                    binding.slug,
                    reason=code,
                    evidence_record_id=failure_event.record_id,
                    observed_at_ms=now_ms,
                )
            await self._stop_worker(
                group_id,
                now_ms=now_ms,
                acknowledge=False,
                task_failure_handled=True,
            )

    async def _stop_inactive_terminal_workers(self, *, now_ms: int) -> None:
        for group_id, handle in tuple(self._workers.items()):
            group = self.supervisor.group_snapshot(group_id)
            if group.active or not all(
                self.supervisor.target_snapshot(binding.slug).state
                in {
                    TargetLifecycleState.SETTLED,
                    TargetLifecycleState.EXCLUDED,
                }
                for binding in handle.decision.targets
            ):
                continue
            await self._stop_worker(
                group_id,
                now_ms=now_ms,
                acknowledge=False,
            )

    async def _stop_worker(
        self,
        group_id: str,
        *,
        now_ms: int,
        acknowledge: bool,
        decision: CaptureDecision | None = None,
        task_failure_handled: bool = False,
    ) -> None:
        handle = self._workers.get(group_id)
        if handle is None:
            if acknowledge and decision is not None:
                receipt = await self._emit_control(
                    now_ms=now_ms,
                    event_type="worker_stopped",
                    kind="lifecycle",
                    payload={
                        "group_id": group_id,
                        "manifest_count": 0,
                        "acknowledged": True,
                        "worker_already_absent": True,
                        "decision_id": decision.decision_id,
                    },
                )
                await self._checkpoint_control_record(
                    record_id=receipt.record_id,
                    reason="worker_stop_receipt",
                    now_ms=now_ms,
                )
                self.supervisor.acknowledge(
                    decision.decision_id,
                    applied_at_ms=now_ms,
                )
            return
        await handle.recorder.stop()
        task_results = await asyncio.gather(
            handle.task,
            return_exceptions=True,
        )
        manifests = await handle.sink.close()
        task_error = next(
            (
                result
                for result in task_results
                if isinstance(result, BaseException)
            ),
            None,
        )
        self._workers.pop(group_id, None)
        if task_error is not None and not task_failure_handled:
            await self._emit_control(
                now_ms=now_ms,
                event_type="worker_stop_failed",
                kind="diagnostic",
                payload={
                    "group_id": group_id,
                    "manifest_count": len(manifests),
                    **safe_error_details(
                        task_error,
                        code="worker_stop_failed",
                    ),
                },
            )
            raise RuntimeError(
                f"capture worker {group_id} failed during shutdown"
            ) from task_error
        receipt = await self._emit_control(
            now_ms=now_ms,
            event_type="worker_stopped",
            kind="lifecycle",
            payload={
                "group_id": group_id,
                "manifest_count": len(manifests),
                "acknowledged": acknowledge,
                "decision_id": (
                    None if decision is None else decision.decision_id
                ),
            },
        )
        if decision is not None:
            await self._checkpoint_control_record(
                record_id=receipt.record_id,
                reason="worker_stop_receipt",
                now_ms=now_ms,
            )
            if acknowledge:
                self.supervisor.acknowledge(
                    decision.decision_id,
                    applied_at_ms=now_ms,
                )

    async def _apply_decisions(
        self,
        decisions: tuple[CaptureDecision, ...],
        *,
        now_ms: int,
    ) -> None:
        for decision in decisions:
            await self._persist_decision(
                decision,
                now_ms=now_ms,
            )
            if decision.action is CaptureDecisionAction.SUBSCRIBE:
                await self._start_worker(decision, now_ms=now_ms)
            elif decision.action is CaptureDecisionAction.UNSUBSCRIBE:
                await self._stop_worker(
                    decision.group_id,
                    now_ms=now_ms,
                    acknowledge=True,
                    decision=decision,
                )

    async def scan_once(
        self,
        *,
        now_ms: int | None = None,
        stop_event: asyncio.Event | None = None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        """Run one bounded discovery, verification, and scheduling pass."""

        if self._closing or self._closed:
            raise RuntimeError(
                "dynamic short-crypto service is closing or closed"
            )
        async with self._lifecycle_lock:
            if self._closing or self._closed:
                raise RuntimeError(
                    "dynamic short-crypto service is closing or closed"
                )
            return await self._scan_once_locked(
                now_ms=now_ms,
                stop_event=stop_event,
                deadline_monotonic=deadline_monotonic,
            )

    async def _scan_once_locked(
        self,
        *,
        now_ms: int | None,
        stop_event: asyncio.Event | None,
        deadline_monotonic: float | None,
    ) -> dict[str, Any]:
        _verify_live_order_guard()
        live_clock = now_ms is None
        actual_now_ms = (
            int(time.time() * 1_000) if live_clock else now_ms
        )
        if (
            isinstance(actual_now_ms, bool)
            or not isinstance(actual_now_ms, int)
            or actual_now_ms < 0
        ):
            raise ValueError("now_ms must be a non-negative integer")
        await self._initialize_recovery(now_ms=actual_now_ms)
        await self._initialize_lifecycle_cohort(now_ms=actual_now_ms)
        await self._ingest_registry(
            now_ms=actual_now_ms,
            live_clock=live_clock,
            stop_event=stop_event,
            deadline_monotonic=deadline_monotonic,
        )
        if live_clock:
            actual_now_ms = int(time.time() * 1_000)
        if self._stop_requested(stop_event, deadline_monotonic):
            return self.snapshot()
        await self._handle_worker_failures(now_ms=actual_now_ms)
        await self._process_worker_lifecycle(now_ms=actual_now_ms)
        await self._acknowledge_ready_workers(now_ms=actual_now_ms)
        await self._enforce_resync_gaps(now_ms=actual_now_ms)
        await self._apply_decisions(
            self.supervisor.advance(actual_now_ms),
            now_ms=actual_now_ms,
        )
        scheduling_now_ms = actual_now_ms
        for slug in tuple(sorted(self._targets)):
            if self._stop_requested(stop_event, deadline_monotonic):
                break
            if live_clock:
                scheduling_now_ms = int(time.time() * 1_000)
                await self._acknowledge_ready_workers(
                    now_ms=scheduling_now_ms
                )
                await self._apply_decisions(
                    self.supervisor.advance(scheduling_now_ms),
                    now_ms=scheduling_now_ms,
                )
            target = self._targets[slug]
            if not self._defer_live_gamma_until_preverification_horizon(
                target,
                now_ms=scheduling_now_ms,
                live_clock=live_clock,
            ):
                await self._poll_gamma_target(
                    target,
                    now_ms=scheduling_now_ms,
                )
            # Public Gamma calls are deliberately serial and may span a
            # subscription boundary.  A live scan must therefore advance the
            # scheduler after every call; an explicit ``now_ms`` remains a
            # fully deterministic offline replay clock.
            if live_clock:
                scheduling_now_ms = int(time.time() * 1_000)
                await self._acknowledge_ready_workers(
                    now_ms=scheduling_now_ms
                )
                await self._apply_decisions(
                    self.supervisor.advance(scheduling_now_ms),
                    now_ms=scheduling_now_ms,
                )
        await self._capture_due_chainlink_boundaries(
            now_ms=scheduling_now_ms,
            live_clock=live_clock,
        )
        # A newly-started recorder persists its initial snapshot on a worker
        # thread.  Gamma polling above normally gives that thread enough time
        # to publish the readiness watermark, so consume it again in this
        # same scheduling pass instead of needlessly waiting a full scan
        # interval.
        await self._handle_worker_failures(now_ms=scheduling_now_ms)
        await self._process_worker_lifecycle(now_ms=scheduling_now_ms)
        await self._acknowledge_ready_workers(now_ms=scheduling_now_ms)
        await self._enforce_resync_gaps(now_ms=scheduling_now_ms)
        if live_clock:
            scheduling_now_ms = int(time.time() * 1_000)
        await self._emit_due_ghost_decisions(now_ms=scheduling_now_ms)
        await self._poll_data_api_trades(now_ms=scheduling_now_ms)
        # Reconcile only after a second lifecycle drain.  A disconnect that
        # arrived while Gamma was in flight must invalidate the close gate
        # before any terminal settlement action is considered.
        await self._enforce_settlement_timeouts(
            now_ms=scheduling_now_ms
        )
        await self._apply_decisions(
            self.supervisor.advance(scheduling_now_ms),
            now_ms=scheduling_now_ms,
        )
        await self._stop_inactive_terminal_workers(
            now_ms=scheduling_now_ms
        )
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        states = Counter(
            self.supervisor.target_snapshot(slug).state.value
            for slug in sorted(self._targets)
            if self._target_known_to_supervisor(slug)
        )
        return {
            "schema_version": (
                "edge-lab-dynamic-short-crypto-service-status.v1"
            ),
            "target_count": len(self._targets),
            "verified_target_count": len(self._verified),
            "rejected_target_count": len(self._rejected),
            "active_worker_count": len(self._workers),
            "target_states": dict(sorted(states.items())),
            "label_states": dict(
                sorted(Counter(self._label_status.values()).items())
            ),
            "label_tracking_complete_count": len(
                self._label_tracking_complete
            ),
            "ghost_decision_count": len(
                self._ghost_decision_record_ids
            ),
            "ghost_decision_exclusion_count": len(
                self._ghost_decision_exclusions
            ),
            "data_api_trade_snapshot_count": (
                self._data_api_trade_snapshot_count
            ),
            "processed_discovery_file_count": len(self._processed_paths),
            "registry_snapshot_sha256": self._last_registry_hash,
            "recovered_state_application": {
                "processed_discovery_descriptor_count": (
                    self._recovered_processed_path_count
                ),
                "target_state_count": len(
                    self._recovered_target_states
                ),
                "settlement_deadline_count": len(
                    self._recovered_settlement_deadlines
                ),
                "worker_action_target_count": len(
                    self._recovered_worker_action_slugs
                ),
                "worker_liveness_group_count": len(
                    self._recovered_worker_liveness
                ),
            },
            "new_orders_disabled": True,
            "authenticated_endpoints_used": 0,
            "orders_submitted": 0,
            "restart_recovery": (
                {
                    "status": "fresh_no_prior_runs",
                    "recovered_from_run_ids": [],
                    "replayed_record_count": 0,
                    "state_hash": None,
                    "gap_count": 0,
                    "exclusion_count": 0,
                    "callback_generation": self._callback_generation,
                }
                if self._recovery_result is None
                else {
                    "status": "replayed_finalized_only",
                    "recovered_from_run_ids": list(
                        self._recovery_result.recovered_from_run_ids
                    ),
                    "replayed_record_count": (
                        self._recovery_result.replayed_record_count
                    ),
                    "state_hash": self._recovery_result.state_hash,
                    "gap_count": len(self._recovery_result.gaps),
                    "exclusion_count": len(
                        self._recovery_result.exclusions
                    ),
                    "callback_generation": self._callback_generation,
                }
            ),
            "startup_integrity": dict(self._startup_integrity_report),
        }

    async def run(
        self,
        *,
        duration_seconds: float | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """Run continuously or for a caller-bounded duration."""

        if duration_seconds is not None and (
            not math.isfinite(float(duration_seconds))
            or duration_seconds <= 0
        ):
            raise ValueError("duration_seconds must be positive and finite")
        started = time.monotonic()
        deadline_monotonic = (
            None
            if duration_seconds is None
            else started + duration_seconds
        )
        try:
            while True:
                if self._stop_requested(stop_event, deadline_monotonic):
                    break
                await self.scan_once(
                    stop_event=stop_event,
                    deadline_monotonic=deadline_monotonic,
                )
                if self._stop_requested(stop_event, deadline_monotonic):
                    break
                delay = self.config.scan_interval_seconds
                if duration_seconds is not None:
                    remaining = duration_seconds - (
                        time.monotonic() - started
                    )
                    if remaining <= 0:
                        break
                    delay = min(delay, remaining)
                if stop_event is None:
                    await asyncio.sleep(delay)
                    continue
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=delay,
                    )
                except TimeoutError:
                    continue
        finally:
            await asyncio.shield(self.close())
        return self.snapshot()

    async def close(self) -> None:
        """Finalize every group before the control journal."""

        close_task = self._close_task
        if close_task is None:
            self._closing = True
            close_task = asyncio.create_task(
                self._close_once(),
                name="dynamic-short-crypto-service-close",
            )
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _close_once(self) -> None:
        async with self._lifecycle_lock:
            await self._close_impl()

    async def _close_impl(self) -> None:
        self._closed = True
        now_ms = int(time.time() * 1_000)
        failures: list[Exception] = []
        live_scan = self._live_chainlink_scan
        self._live_chainlink_scan = None
        if live_scan is not None and live_scan.task is not None:
            if not live_scan.task.done():
                live_scan.task.cancel()
            try:
                await live_scan.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                failures.append(exc)
        for group_id in tuple(self._workers):
            handle = self._workers.get(group_id)
            if handle is None:
                continue
            # Give a worker whose failure is already runnable one scheduling
            # turn before classifying the shutdown as an ordinary capture
            # interruption.
            await asyncio.sleep(0)
            if handle.task.done():
                try:
                    await self._handle_worker_failures(now_ms=now_ms)
                except Exception as exc:
                    failures.append(exc)
                if group_id not in self._workers:
                    continue
                handle = self._workers[group_id]
            for binding in handle.decision.targets:
                snapshot = self.supervisor.target_snapshot(binding.slug)
                if snapshot.state in {
                    TargetLifecycleState.SETTLED,
                    TargetLifecycleState.EXCLUDED,
                }:
                    continue
                target = self._targets[binding.slug]
                if now_ms < target.opens_at_ms:
                    reason = "capture_stopped_before_open"
                elif now_ms < target.closes_at_ms:
                    reason = "capture_interrupted_during_market"
                else:
                    reason = "settlement_tracking_interrupted"
                try:
                    interruption_event = await self._emit_control(
                        now_ms=now_ms,
                        event_type="target_capture_interrupted",
                        kind="diagnostic",
                        payload={
                            "group_id": group_id,
                            "slug": binding.slug,
                            "reason": reason,
                            "announcement_record_id": (
                                binding.announcement_record_id
                            ),
                        },
                    )
                except Exception as exc:
                    failures.append(exc)
                    continue
                try:
                    await self._checkpoint_control_record(
                        record_id=interruption_event.record_id,
                        reason="target_capture_interruption_evidence",
                        now_ms=now_ms,
                    )
                except Exception as exc:
                    failures.append(exc)
                    continue
                if handle.task.done():
                    try:
                        await self._handle_worker_failures(now_ms=now_ms)
                    except Exception as exc:
                        failures.append(exc)
                    break
                self.supervisor.exclude(
                    binding.slug,
                    reason=reason,
                    evidence_record_id=interruption_event.record_id,
                    observed_at_ms=now_ms,
                )
            if group_id not in self._workers:
                continue
            try:
                await self._stop_worker(
                    group_id,
                    now_ms=now_ms,
                    acknowledge=False,
                )
            except Exception as exc:
                failures.append(exc)
        try:
            await self._control_sink.close()
        except Exception as exc:
            failures.append(exc)
        if failures:
            raise RuntimeError(
                f"dynamic capture close had {len(failures)} failure(s)"
            ) from failures[0]
