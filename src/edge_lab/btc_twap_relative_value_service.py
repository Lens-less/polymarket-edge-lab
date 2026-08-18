"""Continuous, public-only BTC 5m/15m TWAP paper-validation service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

import requests

from .btc_twap_relative_value import (
    SameExpiryPair,
    TwapMarketContract,
)
from .capture_cli import ForwardCaptureConfig, _local_proxy_mapping
from .capture_runtime import (
    CaptureStoreRecorderSink,
    PublicSourcesSnapshotClient,
)
from .compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from .data_store import CaptureStore, canonical_json_bytes
from .network_safety import (
    configure_public_session,
    discard_session_cookies,
    safe_error_details,
)
from .recorder import (
    DefaultWebSocketFactory,
    PublicRecorder,
    RecorderConfig,
    RecorderWorkerError,
)
from .settlement_regime import (
    LEGACY_SETTLEMENT_REGIME_ID,
    SettlementRegimeSpec,
    legacy_settlement_regime,
    load_settlement_regime_registry,
    regime_scope_value,
)
from .sources import GAMMA_BASE, PublicSourceError, PublicSourcesClient

SERVICE_CONFIG_SCHEMA = "btc-twap-relative-value-continuous-service.v1"
SERVICE_STATUS_SCHEMA = "btc-twap-relative-value-service-status.v1"
CAPTURE_CONFIG_SCHEMA = "edge-lab-forward-capture-config.v1"
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LEGACY_EVIDENCE_TRACK_ID = "v04-legacy"
_SNTP_SELECTED = re.compile(
    r"^\s*([+-]\d+(?:\.\d+)?)\s+\+/-\s+(\d+(?:\.\d+)?)\s+" r"time\.apple\.com(?:\s|$)",
    re.MULTILINE,
)
_CHRONYC_VALUE = re.compile(r"^\s*([^:]+?)\s*:\s*(.+?)\s*$")
_CHRONYC_SYSTEM_TIME = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s+seconds\s+(slow|fast)\s+of\s+NTP\s+time\s*$",
    re.IGNORECASE,
)
_CHRONYC_REFERENCE = re.compile(
    r"^(?:[0-9A-Fa-f]+\s+\()?([0-9]{1,3}(?:\.[0-9]{1,3}){3})\)?$"
)
_COMPACT_DATA_IDENTITY_MAX = 50_000
CLOCK_SYNC_SOURCE_SNTP = "SNTP time.apple.com"
CLOCK_SYNC_SOURCE_CHRONY_AMAZON = "Chrony Amazon Time Sync Service 169.254.169.123"
SUPPORTED_CLOCK_SYNC_SOURCES = frozenset(
    (CLOCK_SYNC_SOURCE_SNTP, CLOCK_SYNC_SOURCE_CHRONY_AMAZON)
)


class DiskCapacityError(RuntimeError):
    """A new capture cannot start without consuming reserved free space."""


class CaptureFinalizationError(RuntimeError):
    """A bounded capture failed after producing an auditable final summary."""

    def __init__(self, summary: Mapping[str, Any]) -> None:
        if not isinstance(summary, Mapping):
            raise TypeError("capture finalization summary must be a mapping")
        self.summary = dict(summary)
        super().__init__("capture failed; structured finalization is available")


@dataclass(frozen=True)
class ClockSyncMeasurement:
    """Read-only SNTP evidence frozen before one prospective capture."""

    measured_at_raw_ms: int
    offset_seconds: str
    uncertainty_seconds: str
    source: str = CLOCK_SYNC_SOURCE_SNTP

    def __post_init__(self) -> None:
        if (
            isinstance(self.measured_at_raw_ms, bool)
            or not isinstance(self.measured_at_raw_ms, int)
            or self.measured_at_raw_ms < 0
        ):
            raise ValueError("measured_at_raw_ms must be non-negative")
        if self.source not in SUPPORTED_CLOCK_SYNC_SOURCES:
            raise ValueError("unsupported clock-sync source")
        try:
            offset = Decimal(self.offset_seconds)
            uncertainty = Decimal(self.uncertainty_seconds)
        except (ArithmeticError, ValueError) as exc:
            raise ValueError("clock-sync values must be decimal strings") from exc
        if (
            not offset.is_finite()
            or not uncertainty.is_finite()
            or uncertainty < 0
            or abs(offset) > Decimal("10")
            or uncertainty > Decimal("10")
        ):
            raise ValueError("clock-sync values are outside safe bounds")
        object.__setattr__(self, "offset_seconds", str(offset))
        object.__setattr__(self, "uncertainty_seconds", str(uncertainty))

    @property
    def causal_receipt_offset_ms(self) -> int:
        """Latest plausible true receipt time, rounded outward to milliseconds."""

        latest_seconds = Decimal(self.offset_seconds) + Decimal(
            self.uncertainty_seconds
        )
        return int((latest_seconds * 1_000).to_integral_value(rounding=ROUND_CEILING))

    @property
    def uncertainty_ms(self) -> int:
        return int(
            (Decimal(self.uncertainty_seconds) * 1_000).to_integral_value(
                rounding=ROUND_CEILING
            )
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": "btc-twap-clock-sync.v1",
            "source": self.source,
            "measured_at_raw_ms": self.measured_at_raw_ms,
            "offset_seconds": self.offset_seconds,
            "uncertainty_seconds": self.uncertainty_seconds,
            "causal_receipt_offset_ms": self.causal_receipt_offset_ms,
            "uncertainty_ms": self.uncertainty_ms,
            "system_clock_mutated": False,
        }


def parse_sntp_output(
    output: str,
    *,
    measured_at_raw_ms: int,
) -> ClockSyncMeasurement:
    if not isinstance(output, str):
        raise TypeError("SNTP output must be text")
    matches = _SNTP_SELECTED.findall(output)
    if not matches:
        raise ValueError("SNTP output has no selected time.apple.com measurement")
    offset, uncertainty = matches[-1]
    return ClockSyncMeasurement(
        measured_at_raw_ms=measured_at_raw_ms,
        offset_seconds=offset,
        uncertainty_seconds=uncertainty,
        source=CLOCK_SYNC_SOURCE_SNTP,
    )


def measure_sntp_clock_sync() -> ClockSyncMeasurement:
    """Query public SNTP without setting or slewing the system clock."""

    best_measurement: ClockSyncMeasurement | None = None
    best_uncertainty: Decimal | None = None
    for _ in range(3):
        try:
            completed = subprocess.run(
                ("/usr/bin/sntp", "-d", "-t", "3", "time.apple.com"),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        measured_at_raw_ms = int(datetime.now(timezone.utc).timestamp() * 1_000)
        try:
            measurement = parse_sntp_output(
                f"{completed.stdout}\n{completed.stderr}",
                measured_at_raw_ms=measured_at_raw_ms,
            )
        except ValueError:
            continue
        uncertainty = Decimal(measurement.uncertainty_seconds)
        if (
            best_measurement is None
            or best_uncertainty is None
            or uncertainty < best_uncertainty
            or (
                uncertainty == best_uncertainty
                and measurement.measured_at_raw_ms > best_measurement.measured_at_raw_ms
            )
        ):
            best_measurement = measurement
            best_uncertainty = uncertainty
    if best_measurement is None:
        raise RuntimeError("public SNTP clock measurement failed")
    return best_measurement


def parse_chronyc_tracking_output(
    output: str,
    *,
    measured_at_raw_ms: int,
) -> ClockSyncMeasurement:
    if not isinstance(output, str):
        raise TypeError("chronyc output must be text")
    fields: dict[str, str] = {}
    for line in output.splitlines():
        match = _CHRONYC_VALUE.match(line)
        if match is None:
            continue
        fields[match.group(1).strip().casefold()] = match.group(2).strip()
    reference = fields.get("reference id")
    if reference is None:
        raise ValueError("chronyc output missing reference id")
    reference_match = _CHRONYC_REFERENCE.match(reference)
    if reference_match is None or reference_match.group(1) != "169.254.169.123":
        raise ValueError("chronyc output is not locked to 169.254.169.123")
    leap_status = fields.get("leap status")
    if leap_status != "Normal":
        raise ValueError("chronyc leap status is not Normal")
    system_time = fields.get("system time")
    if system_time is None:
        raise ValueError("chronyc output missing system time")
    system_time_match = _CHRONYC_SYSTEM_TIME.match(system_time)
    if system_time_match is None:
        raise ValueError("chronyc system time is not parseable")
    offset = Decimal(system_time_match.group(1))
    direction = system_time_match.group(2).casefold()
    if direction == "fast":
        offset = -offset
    elif direction != "slow":
        raise ValueError("chronyc system time direction is unsupported")

    def parse_decimal(field: str) -> Decimal:
        value = fields.get(field)
        if value is None:
            raise ValueError(f"chronyc output missing {field}")
        try:
            parsed = Decimal(value.split()[0])
        except (ArithmeticError, IndexError, ValueError) as exc:
            raise ValueError(f"chronyc {field} is not parseable") from exc
        if not parsed.is_finite():
            raise ValueError(f"chronyc {field} is not finite")
        return parsed

    last_offset = parse_decimal("last offset")
    root_delay = parse_decimal("root delay")
    root_dispersion = parse_decimal("root dispersion")
    uncertainty = abs(last_offset) + (abs(root_delay) / 2) + root_dispersion
    return ClockSyncMeasurement(
        measured_at_raw_ms=measured_at_raw_ms,
        offset_seconds=str(offset),
        uncertainty_seconds=str(uncertainty),
        source=CLOCK_SYNC_SOURCE_CHRONY_AMAZON,
    )


def measure_chrony_clock_sync() -> ClockSyncMeasurement:
    """Query Amazon Time Sync Service via chrony without mutating the system clock."""

    best_measurement: ClockSyncMeasurement | None = None
    best_uncertainty: Decimal | None = None
    for _ in range(3):
        try:
            completed = subprocess.run(
                ("/usr/bin/chronyc", "-n", "tracking"),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        measured_at_raw_ms = int(datetime.now(timezone.utc).timestamp() * 1_000)
        try:
            measurement = parse_chronyc_tracking_output(
                completed.stdout,
                measured_at_raw_ms=measured_at_raw_ms,
            )
        except ValueError:
            continue
        uncertainty = Decimal(measurement.uncertainty_seconds)
        if (
            best_measurement is None
            or best_uncertainty is None
            or uncertainty < best_uncertainty
            or (
                uncertainty == best_uncertainty
                and measurement.measured_at_raw_ms > best_measurement.measured_at_raw_ms
            )
        ):
            best_measurement = measurement
            best_uncertainty = uncertainty
    if best_measurement is None:
        raise RuntimeError("chrony clock measurement failed")
    return best_measurement


class CompactRecorderSink:
    """Persist the causal paper-replay surface without unrelated event volume."""

    def __init__(
        self,
        backing: CaptureStoreRecorderSink,
        *,
        allowed_asset_ids: tuple[str, ...],
        reconstructed_book_interval_ms: int = 250,
        semantic_dedup_max_records: int = _COMPACT_DATA_IDENTITY_MAX,
    ) -> None:
        if not isinstance(backing, CaptureStoreRecorderSink):
            raise TypeError("backing must be CaptureStoreRecorderSink")
        if (
            not isinstance(allowed_asset_ids, tuple)
            or not allowed_asset_ids
            or any(not isinstance(item, str) or not item for item in allowed_asset_ids)
        ):
            raise ValueError("allowed_asset_ids must be a non-empty string tuple")
        if (
            isinstance(reconstructed_book_interval_ms, bool)
            or not isinstance(reconstructed_book_interval_ms, int)
            or reconstructed_book_interval_ms <= 0
        ):
            raise ValueError("reconstructed_book_interval_ms must be positive")
        if (
            isinstance(semantic_dedup_max_records, bool)
            or not isinstance(semantic_dedup_max_records, int)
            or semantic_dedup_max_records <= 0
        ):
            raise ValueError("semantic_dedup_max_records must be positive")
        self.backing = backing
        self.allowed_asset_ids = frozenset(allowed_asset_ids)
        self.reconstructed_book_interval_ms = reconstructed_book_interval_ms
        self.semantic_dedup_max_records = semantic_dedup_max_records
        self._depth_by_asset: dict[
            str, tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]
        ] = {}
        self._depth_timestamp_ms: dict[str, int] = {}
        self._last_emitted_book_ms: dict[str, int] = {}
        self._seen_data_identities: OrderedDict[
            tuple[str, str, str | None, str],
            str,
        ] = OrderedDict()
        self._operation_lock = asyncio.Lock()
        self._closed_manifests: tuple[dict[str, Any], ...] | None = None
        self._persistence_error: BaseException | None = None

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = Decimal(str(value))
        except (ArithmeticError, ValueError):
            return None
        return parsed if parsed.is_finite() else None

    @classmethod
    def _depth_levels(cls, value: Any) -> dict[Decimal, Decimal] | None:
        if not isinstance(value, list):
            return None
        levels: dict[Decimal, Decimal] = {}
        for item in value:
            if not isinstance(item, Mapping):
                return None
            price = cls._decimal(item.get("price"))
            size = cls._decimal(item.get("size"))
            if (
                price is None
                or size is None
                or not Decimal("0") <= price <= Decimal("1")
                or size <= 0
            ):
                return None
            levels[price] = size
        return levels

    @staticmethod
    def _source_timestamp_ms(payload: Mapping[str, Any]) -> int | None:
        try:
            value = int(payload.get("timestamp"))
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    @staticmethod
    def _levels_from_depth(
        levels: Mapping[Decimal, Decimal], *, bids: bool
    ) -> list[dict[str, str]]:
        return [
            {"price": str(price), "size": str(levels[price])}
            for price in sorted(levels, reverse=bids)
        ]

    def _remember_full_book(self, payload: Mapping[str, Any]) -> bool:
        token_id = payload.get("asset_id")
        if token_id not in self.allowed_asset_ids:
            return False
        timestamp_ms = self._source_timestamp_ms(payload)
        bids = self._depth_levels(payload.get("bids"))
        asks = self._depth_levels(payload.get("asks"))
        if timestamp_ms is None or not bids or not asks:
            self._depth_by_asset.pop(str(token_id), None)
            self._depth_timestamp_ms.pop(str(token_id), None)
            return False
        token = str(token_id)
        previous_timestamp = self._depth_timestamp_ms.get(token)
        if previous_timestamp is not None and timestamp_ms < previous_timestamp:
            self._depth_by_asset.pop(token, None)
            self._depth_timestamp_ms.pop(token, None)
            return False
        self._depth_by_asset[token] = (bids, asks)
        self._depth_timestamp_ms[token] = timestamp_ms
        self._last_emitted_book_ms[token] = timestamp_ms
        return True

    async def _emit_reconstructed_books(
        self,
        compact: Mapping[str, Any],
    ) -> Any:
        payload = compact.get("payload")
        if not isinstance(payload, Mapping):
            return None
        timestamp_ms = self._source_timestamp_ms(payload)
        changes = payload.get("price_changes")
        if timestamp_ms is None or not isinstance(changes, list):
            return None
        by_token: dict[str, list[Mapping[str, Any]]] = {}
        for item in changes:
            if not isinstance(item, Mapping):
                continue
            token_id = item.get("asset_id")
            if token_id in self.allowed_asset_ids:
                by_token.setdefault(str(token_id), []).append(item)

        emitted = None
        for token_id in sorted(by_token):
            current = self._depth_by_asset.get(token_id)
            previous_timestamp = self._depth_timestamp_ms.get(token_id)
            if (
                current is None
                or previous_timestamp is None
                or timestamp_ms < previous_timestamp
            ):
                self._depth_by_asset.pop(token_id, None)
                self._depth_timestamp_ms.pop(token_id, None)
                continue
            bids, asks = (dict(current[0]), dict(current[1]))
            coherent = True
            expected_bids: set[Decimal] = set()
            expected_asks: set[Decimal] = set()
            for change in by_token[token_id]:
                price = self._decimal(change.get("price"))
                size = self._decimal(change.get("size"))
                best_bid = self._decimal(change.get("best_bid"))
                best_ask = self._decimal(change.get("best_ask"))
                side = change.get("side")
                if (
                    price is None
                    or size is None
                    or best_bid is None
                    or best_ask is None
                    or not Decimal("0") <= price <= Decimal("1")
                    or size < 0
                    or side not in {"BUY", "SELL"}
                ):
                    coherent = False
                    break
                levels = bids if side == "BUY" else asks
                if size == 0:
                    levels.pop(price, None)
                else:
                    levels[price] = size
                expected_bids.add(best_bid)
                expected_asks.add(best_ask)
            if (
                not coherent
                or not bids
                or not asks
                or len(expected_bids) != 1
                or len(expected_asks) != 1
                or max(bids) != next(iter(expected_bids))
                or min(asks) != next(iter(expected_asks))
            ):
                self._depth_by_asset.pop(token_id, None)
                self._depth_timestamp_ms.pop(token_id, None)
                continue
            self._depth_by_asset[token_id] = (bids, asks)
            self._depth_timestamp_ms[token_id] = timestamp_ms
            if (
                timestamp_ms - self._last_emitted_book_ms.get(token_id, -1)
                < self.reconstructed_book_interval_ms
            ):
                continue
            reconstructed_payload = {
                "event_type": "book",
                "market": payload.get("market"),
                "asset_id": token_id,
                "timestamp": str(timestamp_ms),
                "bids": self._levels_from_depth(bids, bids=True),
                "asks": self._levels_from_depth(asks, bids=False),
                "depth_policy": ("full_depth_reconstructed_from_price_change"),
                "source_event_type": "price_change",
            }
            reconstructed = dict(compact)
            reconstructed.update(
                {
                    "schema_version": "clob-market-ws.reconstructed-book.v1",
                    "event_type": "book",
                    "server_timestamp": str(timestamp_ms),
                    "payload_hash": hashlib.sha256(
                        canonical_json_bytes(reconstructed_payload)
                    ).hexdigest(),
                    "payload": reconstructed_payload,
                }
            )
            emitted = await self._persist_record(reconstructed)
            self._last_emitted_book_ms[token_id] = timestamp_ms
        return emitted

    @staticmethod
    def _normalized_levels(value: Any, *, bids: bool) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        parsed: list[tuple[Decimal, dict[str, str]]] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            try:
                price = Decimal(str(item.get("price")))
                size = Decimal(str(item.get("size")))
            except (ArithmeticError, ValueError):
                continue
            if not price.is_finite() or not size.is_finite() or size < 0:
                continue
            parsed.append((price, {"price": str(price), "size": str(size)}))
        parsed.sort(key=lambda item: item[0], reverse=bids)
        return [level for _price, level in parsed]

    def _compact_clob_payload(
        self,
        event_type: Any,
        payload: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(event_type, str) or not isinstance(payload, Mapping):
            return None
        if event_type in {"best_bid_ask", "new_market"}:
            return None
        if event_type == "price_change":
            return None
        asset_id = payload.get("asset_id")
        if event_type in {"book", "last_trade_price", "tick_size_change"}:
            if asset_id not in self.allowed_asset_ids:
                return None
        if event_type == "market_resolved":
            assets = payload.get("assets_ids")
            if not isinstance(assets, list) or not self.allowed_asset_ids.intersection(
                str(item) for item in assets
            ):
                return None
        compact = dict(payload)
        if event_type == "book":
            if not self._remember_full_book(payload):
                return None
            compact["bids"] = self._normalized_levels(payload.get("bids"), bids=True)
            compact["asks"] = self._normalized_levels(payload.get("asks"), bids=False)
            compact["depth_policy"] = "full_depth"
        return compact

    @property
    def startup_integrity_report(self) -> dict[str, Any]:
        return self.backing.startup_integrity_report

    def load_recorder_checkpoint(self, source: str) -> dict[str, Any] | None:
        return self.backing.load_recorder_checkpoint(source)

    @property
    def persistence_error(self) -> BaseException | None:
        return self._persistence_error

    @staticmethod
    def _semantic_data_identity(
        record: Mapping[str, Any],
    ) -> tuple[str, str, str | None, str] | None:
        if record.get("kind") != "data":
            return None
        source = record.get("source")
        event_type = record.get("event_type")
        payload_hash = record.get("payload_hash")
        if (
            not isinstance(source, str)
            or not source
            or not isinstance(event_type, str)
            or not event_type
            or not isinstance(payload_hash, str)
            or not payload_hash
        ):
            return None
        server_timestamp_value = record.get("server_timestamp")
        if source == "rtds_ws" and isinstance(record.get("payload"), Mapping):
            semantic_payload = dict(record["payload"])
            semantic_payload.pop("connection_id", None)
            semantic_payload.pop("timestamp", None)
            observation = semantic_payload.get("payload")
            if (
                isinstance(observation, Mapping)
                and observation.get("timestamp") is not None
            ):
                server_timestamp_value = observation["timestamp"]
            payload_hash = hashlib.sha256(
                canonical_json_bytes(semantic_payload)
            ).hexdigest()
        server_timestamp = (
            None if server_timestamp_value is None else str(server_timestamp_value)
        )
        return (
            source,
            event_type,
            server_timestamp,
            payload_hash,
        )

    def _is_duplicate_data_record(self, record: Mapping[str, Any]) -> bool:
        identity = self._semantic_data_identity(record)
        connection_id = record.get("connection_id")
        if identity is None or not isinstance(connection_id, str):
            return False
        if identity in self._seen_data_identities:
            first_connection_id = self._seen_data_identities[identity]
            self._seen_data_identities.move_to_end(identity)
            return first_connection_id != connection_id
        self._seen_data_identities[identity] = connection_id
        if len(self._seen_data_identities) > self.semantic_dedup_max_records:
            self._seen_data_identities.popitem(last=False)
        return False

    def _remember_persistence_error(self, error: BaseException) -> None:
        if self._persistence_error is None:
            self._persistence_error = error

    async def _persist_record(self, record: Mapping[str, Any]) -> Any:
        try:
            return await self.backing.emit(record)
        except Exception as exc:
            self._remember_persistence_error(exc)
            raise

    async def emit(self, record: Mapping[str, Any]) -> Any:
        async with self._operation_lock:
            # Gamma universe snapshots are unrelated high-volume discovery data.
            # CLOB snapshots are causal execution evidence: they retain the
            # server clock, full four-token L2 anchors, and raw fee/market rules.
            if record.get("source") == "gamma_http":
                return None
            compact = {
                key: record[key]
                for key in (
                    "schema_version",
                    "source",
                    "received_at",
                    "event_at",
                    "event_type",
                    "kind",
                    "monotonic_ns",
                    "sequence",
                    "session_id",
                    "connection_id",
                    "frame_hash",
                    "payload_hash",
                    "server_hash",
                    "server_timestamp",
                    "error_code",
                    "error_type",
                    "detail",
                    "payload",
                )
                if key in record
            }
            if compact.get("source") == "rtds_ws" and compact.get("kind") == "data":
                message = compact.get("payload")
                topic = message.get("topic") if isinstance(message, Mapping) else None
                payload = (
                    message.get("payload") if isinstance(message, Mapping) else None
                )
                if (
                    topic == "crypto_prices"
                    and isinstance(payload, Mapping)
                    and payload.get("symbol") != "btcusdt"
                ):
                    return None
            if self._is_duplicate_data_record(compact):
                return None
            if compact.get("source") == "clob_market_ws":
                if (
                    compact.get("kind") == "data"
                    and compact.get("event_type") == "price_change"
                ):
                    return await self._emit_reconstructed_books(compact)
                compact_payload = self._compact_clob_payload(
                    compact.get("event_type"), compact.get("payload")
                )
                if compact_payload is None and compact.get("kind") == "data":
                    return None
                if compact_payload is not None:
                    compact["payload"] = compact_payload
            return await self._persist_record(compact)

    async def checkpoint(
        self,
        source: str,
        checkpoint: Mapping[str, Any],
    ) -> None:
        async with self._operation_lock:
            try:
                await self.backing.checkpoint(source, checkpoint)
            except Exception as exc:
                self._remember_persistence_error(exc)
                raise

    async def close(self) -> tuple[dict[str, Any], ...]:
        async with self._operation_lock:
            if self._closed_manifests is not None:
                return self._closed_manifests
            try:
                self._closed_manifests = await self.backing.close()
            except Exception as exc:
                self._remember_persistence_error(exc)
                raise
            return self._closed_manifests


class _RecorderSinkView:
    """Delegate shared emits while restricting checkpoint ownership per leg."""

    def __init__(
        self,
        sink: CompactRecorderSink,
        *,
        checkpoint_owner: bool,
    ) -> None:
        self._sink = sink
        self._checkpoint_owner = checkpoint_owner

    async def emit(self, record: Mapping[str, Any]) -> Any:
        return await self._sink.emit(record)

    async def checkpoint(
        self,
        source: str,
        checkpoint: Mapping[str, Any],
    ) -> None:
        if self._checkpoint_owner:
            await self._sink.checkpoint(source, checkpoint)


@dataclass(frozen=True)
class ContinuousServiceConfig:
    """Frozen local orchestration limits for the paper-only service."""

    data_root: Path
    research_root: Path
    preregistration_path: Path
    settlement_grace_seconds: int
    retry_seconds: int
    status_interval_seconds: int
    minimum_free_disk_bytes: int
    max_history_roots: int
    decision_tau_seconds: tuple[int, ...] | int
    evidence_track_id: str = LEGACY_EVIDENCE_TRACK_ID
    clock_sync_source: str = CLOCK_SYNC_SOURCE_SNTP
    seed_report_paths: tuple[Path, ...] = ()
    settlement_regime_id: str = LEGACY_SETTLEMENT_REGIME_ID
    regime_registry_path: Path | None = None

    def __post_init__(self) -> None:
        for name in ("data_root", "research_root", "preregistration_path"):
            value = getattr(self, name)
            if not isinstance(value, Path):
                raise TypeError(f"{name} must be a Path")
            object.__setattr__(self, name, value.expanduser().resolve())
        seed_reports = tuple(
            path.expanduser().resolve() for path in self.seed_report_paths
        )
        if any(not isinstance(path, Path) for path in self.seed_report_paths):
            raise TypeError("seed_report_paths must contain Paths")
        object.__setattr__(self, "seed_report_paths", seed_reports)
        if self.regime_registry_path is not None:
            if not isinstance(self.regime_registry_path, Path):
                raise TypeError("regime_registry_path must be a Path or None")
            object.__setattr__(
                self,
                "regime_registry_path",
                self.regime_registry_path.expanduser().resolve(),
            )
        for name in (
            "settlement_grace_seconds",
            "retry_seconds",
            "status_interval_seconds",
            "minimum_free_disk_bytes",
            "max_history_roots",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        raw_taus = self.decision_tau_seconds
        decision_taus = (raw_taus,) if isinstance(raw_taus, int) else raw_taus
        if (
            not isinstance(decision_taus, tuple)
            or not decision_taus
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 45 <= value <= 240
                for value in decision_taus
            )
            or len(set(decision_taus)) != len(decision_taus)
        ):
            raise ValueError(
                "decision_tau_seconds must be unique integers inside [45, 240]"
            )
        object.__setattr__(self, "decision_tau_seconds", decision_taus)
        if _ATTEMPT_ID.fullmatch(self.evidence_track_id) is None:
            raise ValueError("evidence_track_id must be a non-empty safe token")
        if self.clock_sync_source not in SUPPORTED_CLOCK_SYNC_SOURCES:
            raise ValueError("clock_sync_source must be a supported source")
        if (
            not isinstance(self.settlement_regime_id, str)
            or _ATTEMPT_ID.fullmatch(self.settlement_regime_id) is None
        ):
            raise ValueError("settlement_regime_id must be a non-empty safe token")
        if (
            self.settlement_regime_id != LEGACY_SETTLEMENT_REGIME_ID
            and self.regime_registry_path is None
        ):
            raise ValueError("non-legacy settlement regimes require a registry path")


@dataclass(frozen=True)
class CapturePlan:
    """One immutable same-expiry capture attempt."""

    expiry_seconds: int
    attempt_id: str
    duration_seconds: int
    capture_root: Path
    capture_config_path: Path
    report_path: Path
    capture_config: Mapping[str, Any]


@dataclass(frozen=True)
class ServiceStatus:
    """Observable service health written atomically for monitoring."""

    phase: str
    heartbeat_at: str
    pid: int
    child_pid: int | None
    current_expiry_seconds: int | None
    current_capture_root: str | None
    completed_report_count: int
    latest_summary_path: str | None
    latest_classification: str | None
    last_error: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, str) or not self.phase:
            raise ValueError("phase must be non-empty")
        if not isinstance(self.heartbeat_at, str) or not self.heartbeat_at:
            raise ValueError("heartbeat_at must be non-empty")
        for name in ("pid", "completed_report_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.child_pid is not None and (
            isinstance(self.child_pid, bool)
            or not isinstance(self.child_pid, int)
            or self.child_pid <= 0
        ):
            raise ValueError("child_pid must be a positive integer or None")

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": SERVICE_STATUS_SCHEMA,
            "phase": self.phase,
            "heartbeat_at": self.heartbeat_at,
            "pid": self.pid,
            "child_pid": self.child_pid,
            "current_expiry_seconds": self.current_expiry_seconds,
            "current_capture_root": self.current_capture_root,
            "completed_report_count": self.completed_report_count,
            "latest_summary_path": self.latest_summary_path,
            "latest_classification": self.latest_classification,
            "last_error": self.last_error,
            "paper_only": True,
            "public_only": True,
            "new_orders_disabled": True,
            "authenticated_endpoints_used": 0,
            "orders_submitted": 0,
        }
        document["status_sha256"] = hashlib.sha256(
            canonical_json_bytes(document)
        ).hexdigest()
        return document


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("atomic JSON target must not be a symlink")
    encoded = canonical_json_bytes(document) + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_service_status(path: Path, status: ServiceStatus) -> None:
    if not isinstance(status, ServiceStatus):
        raise TypeError("status must be ServiceStatus")
    _atomic_json(path, status.to_document())


def write_json_document(path: Path, document: Mapping[str, Any]) -> None:
    _atomic_json(path, document)


def evaluate_service_health(
    status: Mapping[str, Any],
    *,
    now: datetime,
    process_alive: bool,
    free_disk_bytes: int,
    minimum_free_disk_bytes: int,
    maximum_heartbeat_age_seconds: int,
) -> dict[str, Any]:
    """Evaluate the durable service status without optimistic defaults."""

    if not isinstance(status, Mapping):
        raise TypeError("status must be a mapping")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    if not isinstance(process_alive, bool):
        raise TypeError("process_alive must be a boolean")
    for name, value in (
        ("free_disk_bytes", free_disk_bytes),
        ("minimum_free_disk_bytes", minimum_free_disk_bytes),
        ("maximum_heartbeat_age_seconds", maximum_heartbeat_age_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    failures: list[str] = []
    unsigned = dict(status)
    claimed_hash = unsigned.pop("status_sha256", None)
    expected_hash = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    hash_valid = claimed_hash == expected_hash
    if not hash_valid:
        failures.append("status_hash_invalid")
    if status.get("schema_version") != SERVICE_STATUS_SCHEMA:
        failures.append("status_schema_invalid")

    guard_valid = (
        status.get("paper_only") is True
        and status.get("public_only") is True
        and status.get("new_orders_disabled") is True
        and status.get("authenticated_endpoints_used") == 0
        and status.get("orders_submitted") == 0
    )
    if not guard_valid:
        failures.append("paper_only_guard_invalid")
    if not process_alive:
        failures.append("process_not_alive")
    if free_disk_bytes < minimum_free_disk_bytes:
        failures.append("disk_capacity_below_reserved_floor")

    heartbeat_age_seconds: float | None = None
    heartbeat = status.get("heartbeat_at")
    try:
        if not isinstance(heartbeat, str):
            raise ValueError("heartbeat is not a string")
        parsed = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("heartbeat is not timezone aware")
        heartbeat_age_seconds = (
            now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)
        ).total_seconds()
    except (TypeError, ValueError):
        failures.append("heartbeat_invalid")
    else:
        if heartbeat_age_seconds > maximum_heartbeat_age_seconds:
            failures.append("heartbeat_stale")
        elif heartbeat_age_seconds < -5:
            failures.append("heartbeat_from_future")

    phase = status.get("phase")
    if phase in {"error_wait", "stopped"} or not isinstance(phase, str):
        failures.append("service_phase_unhealthy")
    return {
        "schema_version": "btc-twap-relative-value-service-health.v1",
        "healthy": not failures,
        "failures": failures,
        "phase": phase,
        "heartbeat_at": heartbeat,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "pid": status.get("pid"),
        "process_alive": process_alive,
        "current_expiry_seconds": status.get("current_expiry_seconds"),
        "current_capture_root": status.get("current_capture_root"),
        "free_disk_bytes": free_disk_bytes,
        "minimum_free_disk_bytes": minimum_free_disk_bytes,
        "status_hash_valid": hash_valid,
        "paper_only_guard_valid": guard_valid,
        "completed_report_count": status.get("completed_report_count"),
        "latest_summary_path": status.get("latest_summary_path"),
        "latest_classification": status.get("latest_classification"),
        "last_error": status.get("last_error"),
    }


def load_service_config(path: Path) -> ContinuousServiceConfig:
    raw = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise TypeError("continuous service config must be an object")
    if raw.get("schema_version") != SERVICE_CONFIG_SCHEMA:
        raise ValueError("unsupported continuous service config schema")
    allowed = {
        "schema_version",
        "data_root",
        "research_root",
        "preregistration_path",
        "settlement_grace_seconds",
        "retry_seconds",
        "status_interval_seconds",
        "minimum_free_disk_bytes",
        "max_history_roots",
        "decision_tau_seconds",
        "evidence_track_id",
        "clock_sync_source",
        "seed_report_paths",
        "settlement_regime_id",
        "regime_registry_path",
    }
    unexpected = set(raw) - allowed
    if unexpected:
        raise ValueError(
            "unsupported continuous service config fields: "
            + ", ".join(sorted(str(item) for item in unexpected))
        )
    for required_path in ("data_root", "research_root", "preregistration_path"):
        value = raw.get(required_path)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{required_path} must be a non-empty path string")
    for key, value in raw.items():
        normalized = str(key).casefold().replace("-", "_")
        if any(
            term in normalized
            for term in (
                "authorization",
                "credential",
                "private_key",
                "secret",
                "signature",
                "wallet",
            )
        ):
            raise ValueError(f"forbidden credential-like config key: {key}")
        if isinstance(value, str) and "@" in value and "://" in value:
            raise ValueError("authenticated URLs are forbidden")
    seed = raw.get("seed_report_paths", [])
    if not isinstance(seed, list) or any(not isinstance(item, str) for item in seed):
        raise TypeError("seed_report_paths must be a string array")
    decision_raw = raw.get("decision_tau_seconds", 60)
    if isinstance(decision_raw, bool) or not isinstance(decision_raw, (int, list)):
        raise TypeError("decision_tau_seconds must be an integer or integer array")
    decision_taus = (
        (decision_raw,) if isinstance(decision_raw, int) else tuple(decision_raw)
    )
    registry_path_raw = raw.get("regime_registry_path")
    if registry_path_raw is not None and (
        not isinstance(registry_path_raw, str) or not registry_path_raw.strip()
    ):
        raise ValueError("regime_registry_path must be a non-empty path string")
    return ContinuousServiceConfig(
        data_root=Path(str(raw.get("data_root", ""))),
        research_root=Path(str(raw.get("research_root", ""))),
        preregistration_path=Path(str(raw.get("preregistration_path", ""))),
        settlement_grace_seconds=int(raw.get("settlement_grace_seconds", 360)),
        retry_seconds=int(raw.get("retry_seconds", 30)),
        status_interval_seconds=int(raw.get("status_interval_seconds", 15)),
        minimum_free_disk_bytes=int(raw.get("minimum_free_disk_bytes", 12 * 1024**3)),
        max_history_roots=int(raw.get("max_history_roots", 1)),
        decision_tau_seconds=decision_taus,
        evidence_track_id=str(raw.get("evidence_track_id", LEGACY_EVIDENCE_TRACK_ID)),
        clock_sync_source=str(raw.get("clock_sync_source", CLOCK_SYNC_SOURCE_SNTP)),
        seed_report_paths=tuple(Path(item) for item in seed),
        settlement_regime_id=str(
            raw.get("settlement_regime_id", LEGACY_SETTLEMENT_REGIME_ID)
        ),
        regime_registry_path=(
            None if registry_path_raw is None else Path(registry_path_raw)
        ),
    )


def _parse_required_utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field} must be a UTC timestamp")
    return parsed.astimezone(timezone.utc)


def validate_preregistration_runtime_identity(
    preregistration: Mapping[str, Any],
    *,
    config: ContinuousServiceConfig,
) -> None:
    if not isinstance(preregistration, Mapping):
        raise TypeError("preregistration must be a mapping")
    scope = preregistration.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("preregistration scope must be an object")
    if scope.get("paper_only") is not True:
        raise ValueError("preregistration scope must keep paper_only=true")
    if scope.get("live_orders_disabled") is not True:
        raise ValueError("preregistration scope must keep live_orders_disabled=true")
    _parse_required_utc_timestamp(
        scope.get("prospective_only_after"),
        field="scope.prospective_only_after",
    )
    scope_track_id = scope.get("evidence_track_id")
    if scope_track_id is not None:
        if (
            not isinstance(scope_track_id, str)
            or _ATTEMPT_ID.fullmatch(scope_track_id) is None
        ):
            raise ValueError("scope.evidence_track_id must be a non-empty safe token")
        if config.evidence_track_id != scope_track_id:
            raise ValueError(
                "service evidence_track_id does not match preregistration scope"
            )
    scope_regime = scope.get("settlement_regime")
    if scope_regime is not None:
        if not isinstance(scope_regime, str) or not scope_regime.strip():
            raise ValueError("scope.settlement_regime must be a non-empty string")
        if regime_scope_value(config.settlement_regime_id) != regime_scope_value(
            scope_regime
        ):
            raise ValueError(
                "service settlement_regime_id does not match preregistration scope"
            )
    if config.settlement_regime_id != LEGACY_SETTLEMENT_REGIME_ID:
        registry_document = preregistration.get("regime_registry")
        if not isinstance(registry_document, Mapping):
            raise ValueError("v0.6 preregistration must freeze the regime registry")
        registry_relpath = registry_document.get("path")
        registry_sha256 = registry_document.get("sha256")
        if not isinstance(registry_relpath, str) or not registry_relpath.strip():
            raise ValueError("regime registry path must be non-empty")
        if (
            not isinstance(registry_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", registry_sha256) is None
        ):
            raise ValueError("regime registry sha256 must be a lowercase digest")
        project_root = config.preregistration_path.expanduser().resolve().parents[2]
        registry_path = (project_root / registry_relpath).resolve()
        if not registry_path.is_relative_to(project_root):
            raise ValueError("regime registry path must stay inside the project")
        if config.regime_registry_path != registry_path:
            raise ValueError("service registry path does not match preregistration")
        actual_registry_hash = hashlib.sha256(registry_path.read_bytes()).hexdigest()
        if actual_registry_hash != registry_sha256:
            raise ValueError("regime registry sha256 does not match the local file")
        registry = load_settlement_regime_registry(registry_path)
        if registry.sha256 != registry_sha256:
            raise ValueError("regime registry sha256 validation disagrees")
        registry.require(config.settlement_regime_id)
    frozen_strategy = preregistration.get("frozen_strategy")
    if not isinstance(frozen_strategy, Mapping):
        raise ValueError("preregistration frozen_strategy must be an object")
    frozen_taus = frozen_strategy.get("decision_tau_seconds")
    if frozen_taus != list(config.decision_tau_seconds):
        raise ValueError("service decision_tau_seconds do not match preregistration")
    frozen_clock = frozen_strategy.get("clock_sync")
    if not isinstance(frozen_clock, Mapping):
        raise ValueError("preregistration frozen_strategy.clock_sync must be an object")
    if frozen_clock.get("source") != config.clock_sync_source:
        raise ValueError("service clock_sync_source does not match preregistration")
    strategy_spec = preregistration.get("strategy_spec")
    if not isinstance(strategy_spec, Mapping):
        raise ValueError("preregistration strategy_spec must be an object")
    strategy_relpath = strategy_spec.get("path")
    expected_hash = strategy_spec.get("sha256")
    if not isinstance(strategy_relpath, str) or not strategy_relpath.strip():
        raise ValueError("strategy_spec.path must be a non-empty string")
    if (
        not isinstance(expected_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
    ):
        raise ValueError("strategy_spec.sha256 must be a lowercase sha256 hex string")
    project_root = config.preregistration_path.expanduser().resolve().parents[2]
    strategy_path = (project_root / strategy_relpath).resolve()
    if not strategy_path.is_relative_to(project_root):
        raise ValueError("strategy_spec.path must stay inside the deployed project")
    actual_hash = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError("strategy_spec sha256 does not match the local strategy file")
    repository_head = preregistration.get("repository_head")
    if preregistration.get("schema_version") in {
        "btc-5m-15m-relative-value-preregistration.v5",
        "btc-5m-15m-relative-value-preregistration.v6",
    } and repository_head is None:
        raise ValueError("v0.5+ preregistration must freeze repository_head")
    if repository_head is not None:
        if (
            not isinstance(repository_head, str)
            or re.fullmatch(r"[0-9a-f]{40}", repository_head) is None
        ):
            raise ValueError("repository_head must be a lowercase commit sha")
        implementation_marker = project_root / ".implementation-revision"
        if implementation_marker.exists():
            deployed_revision = implementation_marker.read_text(
                encoding="utf-8"
            ).strip()
            if deployed_revision != repository_head:
                raise ValueError(
                    "deployed implementation revision does not match preregistration"
                )


def require_disk_capacity(
    config: ContinuousServiceConfig,
    *,
    free_bytes: int | None = None,
) -> None:
    if free_bytes is None:
        probe = config.data_root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free_bytes = shutil.disk_usage(probe).free
    if (
        isinstance(free_bytes, bool)
        or not isinstance(free_bytes, int)
        or free_bytes < 0
    ):
        raise ValueError("free_bytes must be a non-negative integer")
    if free_bytes < config.minimum_free_disk_bytes:
        raise DiskCapacityError(
            "disk_capacity_below_reserved_floor: "
            f"free={free_bytes} reserved={config.minimum_free_disk_bytes}"
        )


def _next_expiry_seconds(now_ms: int) -> int:
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")
    now_seconds = now_ms // 1_000
    return (now_seconds // 900 + 1) * 900


def _capture_expiry_seconds(
    now_ms: int,
    requested_expiry_seconds: int | None,
) -> int:
    next_expiry_seconds = _next_expiry_seconds(now_ms)
    if requested_expiry_seconds is None:
        return next_expiry_seconds
    if (
        isinstance(requested_expiry_seconds, bool)
        or not isinstance(requested_expiry_seconds, int)
        or requested_expiry_seconds % 900 != 0
        or not next_expiry_seconds
        <= requested_expiry_seconds
        <= next_expiry_seconds + 1_800
    ):
        raise ValueError(
            "expiry_seconds must be an aligned expiry no more than two "
            "cycles beyond the next boundary"
        )
    return requested_expiry_seconds


def settlement_regime_for_config(
    config: ContinuousServiceConfig,
) -> SettlementRegimeSpec:
    """Resolve the immutable regime bound to one service configuration."""

    if config.regime_registry_path is None:
        regime = legacy_settlement_regime()
        if config.settlement_regime_id != regime.regime_id:
            raise ValueError("settlement regime requires the frozen registry")
        return regime
    registry = load_settlement_regime_registry(config.regime_registry_path)
    return registry.require(config.settlement_regime_id)


def build_capture_plan(
    *,
    config: ContinuousServiceConfig,
    now_ms: int,
    attempt_id: str,
    clock_sync: ClockSyncMeasurement,
    gamma_5: Mapping[str, Any],
    clob_5: Mapping[str, Any],
    gamma_15: Mapping[str, Any],
    clob_15: Mapping[str, Any],
    expiry_seconds: int | None = None,
) -> CapturePlan:
    """Validate live public metadata and freeze one capture attempt."""

    if _ATTEMPT_ID.fullmatch(attempt_id) is None:
        raise ValueError("attempt_id has an invalid shape")
    if not isinstance(clock_sync, ClockSyncMeasurement):
        raise TypeError("clock_sync must be a ClockSyncMeasurement")
    regime = settlement_regime_for_config(config)
    market_5 = TwapMarketContract.from_public_metadata(
        gamma_5,
        clob_5,
        settlement_regime=regime,
    )
    market_15 = TwapMarketContract.from_public_metadata(
        gamma_15,
        clob_15,
        settlement_regime=regime,
    )
    pair = SameExpiryPair.from_contracts(market_5, market_15)
    expiry_seconds = _capture_expiry_seconds(now_ms, expiry_seconds)
    if pair.expires_at_ms != expiry_seconds * 1_000:
        raise ValueError("public markets do not match the requested expiry boundary")
    capture_root = (
        config.data_root / "runs" / str(expiry_seconds) / attempt_id
    ).resolve()
    capture_config_path = capture_root / "capture-config.json"
    report_path = (
        config.research_root
        / "reports"
        / f"PILOT_REPORT_{expiry_seconds}_{attempt_id}.json"
    ).resolve()
    duration_seconds = max(
        60,
        math.ceil(
            (pair.expires_at_ms + config.settlement_grace_seconds * 1_000 - now_ms)
            / 1_000
        ),
    )
    capture_config: dict[str, Any] = {
        "schema_version": CAPTURE_CONFIG_SCHEMA,
        "data_root": str(capture_root),
        "capture_started_at_ms": now_ms,
        "evidence_track_id": config.evidence_track_id,
        "settlement_regime_id": regime.regime_id,
        "clock_sync": clock_sync.to_document(),
        "asset_ids": [
            market_15.up_token_id,
            market_15.down_token_id,
            market_5.up_token_id,
            market_5.down_token_id,
        ],
        "condition_ids": [
            market_15.condition_id,
            market_5.condition_id,
        ],
        "rule_market_ids": [market_15.market_id, market_5.market_id],
        "rtds_subscriptions": [
            {"topic": "crypto_prices", "type": "update"},
            {
                "topic": "crypto_prices_twap_thirty",
                "type": "update",
                "filters": '{"symbol":"btc/usd"}',
            },
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "filters": '{"symbol":"btc/usd"}',
            },
        ],
        # Pair discovery has already frozen the two targets.  Avoid repeatedly
        # fetching unrelated Gamma pages; spend that budget on causal full-book,
        # trade, fee, rule, and clock evidence instead.
        "snapshot_intervals": {"clob": 5, "trades": 30, "rules": 30},
        # The upstream CLOB stream can exceed 300 events/second even though
        # only the frozen 100-500ms replay surface is retained.  Checkpoint on
        # upstream count infrequently to avoid thousands of tiny manifests;
        # each retained line is still flushed by RawBatchWriter immediately.
        "checkpoint_every_records": 10_000,
        "max_records_per_batch": 5_000,
        "gamma_page_limit": 10,
        "gamma_max_pages": 1,
        "reward_page_limit": 1,
        "reward_max_pages": 1,
        "targets": [
            {
                "horizon": "15m",
                "slug": market_15.slug,
                "market_id": market_15.market_id,
                "condition_id": market_15.condition_id,
                "up_token_id": market_15.up_token_id,
                "down_token_id": market_15.down_token_id,
                "opens_at_ms": market_15.opens_at_ms,
                "closes_at_ms": market_15.closes_at_ms,
                "resolution_source": market_15.resolution_source,
                "source_topic": market_15.source_topic,
                "settlement_regime": market_15.settlement_regime,
                "twap_window_seconds": market_15.twap_window_seconds,
                "tick_size": str(market_15.tick_size),
                "minimum_order_size": str(market_15.minimum_order_size),
                "taker_delay_ms": market_15.taker_delay_ms,
                "accepting_orders": market_15.accepting_orders,
                "fee_schedule": {
                    "rate": str(market_15.fee_schedule.rate),
                    "exponent": str(market_15.fee_schedule.exponent),
                    "taker_only": market_15.fee_schedule.taker_only,
                },
                "rules_text_sha256": hashlib.sha256(
                    str(gamma_15.get("description", "")).encode("utf-8")
                ).hexdigest(),
                "rule_hash": market_15.rule_hash,
            },
            {
                "horizon": "5m",
                "slug": market_5.slug,
                "market_id": market_5.market_id,
                "condition_id": market_5.condition_id,
                "up_token_id": market_5.up_token_id,
                "down_token_id": market_5.down_token_id,
                "opens_at_ms": market_5.opens_at_ms,
                "closes_at_ms": market_5.closes_at_ms,
                "resolution_source": market_5.resolution_source,
                "source_topic": market_5.source_topic,
                "settlement_regime": market_5.settlement_regime,
                "twap_window_seconds": market_5.twap_window_seconds,
                "tick_size": str(market_5.tick_size),
                "minimum_order_size": str(market_5.minimum_order_size),
                "taker_delay_ms": market_5.taker_delay_ms,
                "accepting_orders": market_5.accepting_orders,
                "fee_schedule": {
                    "rate": str(market_5.fee_schedule.rate),
                    "exponent": str(market_5.fee_schedule.exponent),
                    "taker_only": market_5.fee_schedule.taker_only,
                },
                "rules_text_sha256": hashlib.sha256(
                    str(gamma_5.get("description", "")).encode("utf-8")
                ).hexdigest(),
                "rule_hash": market_5.rule_hash,
            },
        ],
    }
    return CapturePlan(
        expiry_seconds=expiry_seconds,
        attempt_id=attempt_id,
        duration_seconds=duration_seconds,
        capture_root=capture_root,
        capture_config_path=capture_config_path,
        report_path=report_path,
        capture_config=capture_config,
    )


def verify_paper_only_guard() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("new-order safety guard is not active")


def public_session(proxy_url: str | None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.proxies.update(_local_proxy_mapping(proxy_url))
    configure_public_session(session)
    return session


def fetch_gamma_market_by_slug(
    session: requests.Session,
    slug: str,
) -> Mapping[str, Any]:
    if not isinstance(slug, str) or not slug:
        raise ValueError("slug must be non-empty")
    try:
        response = session.get(
            f"{GAMMA_BASE}/markets",
            params={"slug": slug},
            timeout=20,
            allow_redirects=False,
        )
    finally:
        discard_session_cookies(session)
    if response.status_code != 200:
        raise PublicSourceError(
            f"Gamma slug lookup returned HTTP {response.status_code}",
            code="gamma_slug_lookup_failed",
            error_type="HTTPStatus",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PublicSourceError(
            "Gamma slug lookup returned invalid JSON",
            code="gamma_slug_json_invalid",
            error_type="JSONDecodeError",
        ) from exc
    matches = (
        [
            item
            for item in payload
            if isinstance(item, Mapping) and item.get("slug") == slug
        ]
        if isinstance(payload, list)
        else []
    )
    if len(matches) != 1:
        raise PublicSourceError(
            f"Gamma slug lookup expected one exact match, got {len(matches)}",
            code="gamma_slug_cardinality_invalid",
            error_type="DataContractError",
        )
    return dict(matches[0])


def discover_next_pair(
    *,
    session: requests.Session,
    source_client: PublicSourcesClient,
    config: ContinuousServiceConfig,
    now_ms: int,
    attempt_id: str,
    clock_sync: ClockSyncMeasurement,
    expiry_seconds: int | None = None,
) -> CapturePlan:
    expiry_seconds = _capture_expiry_seconds(now_ms, expiry_seconds)
    slug_15 = f"btc-updown-15m-{expiry_seconds - 900}"
    slug_5 = f"btc-updown-5m-{expiry_seconds - 300}"
    gamma_15 = fetch_gamma_market_by_slug(session, slug_15)
    gamma_5 = fetch_gamma_market_by_slug(session, slug_5)
    condition_15 = str(gamma_15.get("conditionId", ""))
    condition_5 = str(gamma_5.get("conditionId", ""))
    clob_15 = dict(source_client.clob_market(condition_15).value.raw)
    clob_5 = dict(source_client.clob_market(condition_5).value.raw)
    return build_capture_plan(
        config=config,
        now_ms=now_ms,
        attempt_id=attempt_id,
        clock_sync=clock_sync,
        gamma_5=gamma_5,
        clob_5=clob_5,
        gamma_15=gamma_15,
        clob_15=clob_15,
        expiry_seconds=expiry_seconds,
    )


def _build_redundant_recorder_configs(
    config: ForwardCaptureConfig,
) -> tuple[RecorderConfig, RecorderConfig]:
    common = {
        "clob_asset_ids": config.asset_ids,
        "rtds_subscriptions": config.rtds_subscriptions,
        "checkpoint_every_records": config.checkpoint_every_records,
        "sink_timeout_seconds": 60.0,
        "checkpoint_timeout_seconds": 60.0,
    }
    return (
        RecorderConfig(
            **common,
            snapshot_intervals=dict(config.snapshot_intervals),
        ),
        RecorderConfig(
            **common,
            snapshot_intervals={},
        ),
    )


async def _run_redundant_recorders(
    primary: PublicRecorder,
    secondary: PublicRecorder,
    *,
    duration_seconds: float,
    shared_sink: CompactRecorderSink,
) -> tuple[BaseException, ...]:
    recorders = (primary, secondary)
    task_to_recorder = {
        asyncio.create_task(
            recorder.run(run_for_seconds=duration_seconds),
            name=f"btc-twap-redundant-capture-{index}",
        ): recorder
        for index, recorder in enumerate(recorders, start=1)
    }
    failures: list[BaseException] = []
    pending = set(task_to_recorder)
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures.append(exc)
            if shared_sink.persistence_error is not None and pending:
                await asyncio.gather(
                    *(task_to_recorder[task].stop() for task in pending),
                    return_exceptions=True,
                )
        if shared_sink.persistence_error is not None:
            raise shared_sink.persistence_error
        return tuple(failures)
    finally:
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


def _recorder_failure_details(error: BaseException) -> dict[str, str]:
    if isinstance(error, RecorderWorkerError):
        return {
            "error_type": error.error_type,
            "error_code": error.error_code,
        }
    return safe_error_details(error, code="recorder_leg_failed")


async def run_compact_forward_capture(
    config: ForwardCaptureConfig,
    *,
    duration_seconds: float,
    proxy_url: str | None,
) -> dict[str, Any]:
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive and finite")
    verify_paper_only_guard()
    session = public_session(proxy_url)
    sources = PublicSourcesClient(
        session=session,
        timeout=20,
        retries=2,
        rate_per_second=8,
        burst=2,
        min_interval_seconds=0.05,
    )
    store = CaptureStore(config.data_root)
    backing = CaptureStoreRecorderSink(
        store,
        max_records_per_batch=config.max_records_per_batch,
    )
    sink = CompactRecorderSink(
        backing,
        allowed_asset_ids=tuple(config.asset_ids),
    )
    snapshots = PublicSourcesSnapshotClient(
        sources,
        gamma_page_limit=config.gamma_page_limit,
        gamma_max_pages=config.gamma_max_pages,
        condition_ids=config.condition_ids,
        reward_page_limit=config.reward_page_limit,
        reward_max_pages=config.reward_max_pages,
        rule_market_ids=config.rule_market_ids,
    )
    primary_config, secondary_config = _build_redundant_recorder_configs(config)
    websocket_factory = DefaultWebSocketFactory(proxy_url=proxy_url)
    primary = PublicRecorder(
        config=primary_config,
        sink=_RecorderSinkView(sink, checkpoint_owner=True),
        snapshot_client=snapshots,
        websocket_factory=websocket_factory,
    )
    secondary = PublicRecorder(
        config=secondary_config,
        sink=_RecorderSinkView(sink, checkpoint_owner=False),
        snapshot_client=snapshots,
        websocket_factory=websocket_factory,
    )
    capture_error: BaseException | None = None
    recorder_failures: tuple[BaseException, ...] = ()
    manifests: tuple[dict[str, Any], ...] = ()
    try:
        recorder_failures = await _run_redundant_recorders(
            primary,
            secondary,
            duration_seconds=duration_seconds,
            shared_sink=sink,
        )
        if len(recorder_failures) == 2:
            capture_error = RuntimeError("both redundant public recorder legs failed")
    except BaseException as exc:
        capture_error = exc
    finally:
        manifests = await sink.close()
        session.close()
    integrity = store.audit_integrity()
    summary = {
        "schema_version": "btc-twap-compact-forward-capture-summary.v1",
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "data_root": str(config.data_root),
        "duration_seconds": str(duration_seconds),
        "target_count": len(config.targets),
        "asset_count": len(config.asset_ids),
        "recorder_leg_count": 2,
        "recorder_leg_failures": [
            _recorder_failure_details(error)
            for error in recorder_failures
        ],
        "websocket_redundancy": {
            "clob_market_ws": 2,
            "rtds_ws": 2,
        },
        "manifest_count": len(manifests),
        "manifest_record_count": sum(
            int(manifest.get("record_count", 0)) for manifest in manifests
        ),
        "integrity": integrity,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "authenticated_endpoints_used": 0,
        "orders_submitted": 0,
        "capture_error": (
            None
            if capture_error is None
            else safe_error_details(capture_error, code="capture_failed")
        ),
    }
    if isinstance(capture_error, asyncio.CancelledError):
        raise capture_error
    if capture_error is not None:
        raise CaptureFinalizationError(summary) from capture_error
    return summary
