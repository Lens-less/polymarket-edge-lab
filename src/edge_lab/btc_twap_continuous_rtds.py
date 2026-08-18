"""Continuous public-only BTC TWAP 60s RTDS capture."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .btc_twap_relative_value_service import write_json_document
from .capture_runtime import CaptureStoreRecorderSink
from .compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from .data_store import CaptureStore, canonical_json_bytes
from .network_safety import safe_error_details
from .recorder import (
    DefaultWebSocketFactory,
    PublicRecorder,
    RecorderClock,
    RecorderConfig,
    RecorderSink,
    WebSocketFactory,
)

STATUS_SCHEMA_VERSION = "btc-twap-continuous-rtds-status.v1"
SUMMARY_SCHEMA_VERSION = "btc-twap-continuous-rtds-summary.v1"
VALIDATION_SCHEMA_VERSION = "btc-twap-continuous-rtds-validation.v1"
BTC_TWAP_TOPIC = "crypto_prices_twap_sixty"
BTC_TWAP_SYMBOL = "btc/usd"
MAXIMUM_HEALTHY_OBSERVATION_GAP_MS = 2_000
MAXIMUM_HEALTHY_STATUS_AGE_SECONDS = 15


class ContinuousCaptureFinalizationError(RuntimeError):
    """A capture failed after writing a final status and summary."""

    def __init__(self, summary: Mapping[str, Any]) -> None:
        self.summary = dict(summary)
        super().__init__("continuous RTDS capture failed; final summary is available")


@dataclass(frozen=True)
class ContinuousBtcTwapRtdsConfig:
    data_root: Path
    status_path: Path | None = None
    checkpoint_every_records: int = 100
    max_records_per_batch: int = 10_000
    reconnect_initial_seconds: float = 0.5
    reconnect_max_seconds: float = 30.0
    rtds_heartbeat_seconds: float = 5.0
    rtds_receive_idle_timeout_seconds: float = 90.0
    sink_timeout_seconds: float = 60.0
    checkpoint_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        resolved_root = self.data_root.expanduser().resolve()
        resolved_status = (
            resolved_root / "service" / "status.json"
            if self.status_path is None
            else self.status_path.expanduser().resolve()
        )
        object.__setattr__(self, "data_root", resolved_root)
        object.__setattr__(self, "status_path", resolved_status)
        for name in ("checkpoint_every_records", "max_records_per_batch"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "reconnect_max_seconds",
            "rtds_heartbeat_seconds",
            "rtds_receive_idle_timeout_seconds",
            "sink_timeout_seconds",
            "checkpoint_timeout_seconds",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if (
            not isinstance(self.reconnect_initial_seconds, (int, float))
            or self.reconnect_initial_seconds < 0
        ):
            raise ValueError("reconnect_initial_seconds must be non-negative")
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError(
                "reconnect_max_seconds cannot be below reconnect_initial_seconds"
            )


class _NoSnapshotClient:
    async def fetch_snapshot(self, kind: str, *, asset_ids: Sequence[str] = ()) -> Any:
        raise RuntimeError(f"unexpected snapshot request: {kind}")


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _btc_twap_subscription() -> dict[str, str]:
    return {
        "topic": BTC_TWAP_TOPIC,
        "type": "update",
        "filters": json.dumps({"symbol": BTC_TWAP_SYMBOL}, separators=(",", ":")),
    }


def build_btc_twap_recorder_config(
    *,
    checkpoint_every_records: int = 100,
    reconnect_initial_seconds: float = 0.5,
    reconnect_max_seconds: float = 30.0,
    rtds_heartbeat_seconds: float = 5.0,
    rtds_receive_idle_timeout_seconds: float = 90.0,
    sink_timeout_seconds: float = 60.0,
    checkpoint_timeout_seconds: float = 60.0,
) -> RecorderConfig:
    return RecorderConfig(
        clob_enabled=False,
        clob_asset_ids=(),
        rtds_subscriptions=(_btc_twap_subscription(),),
        snapshot_intervals={},
        checkpoint_every_records=checkpoint_every_records,
        reconnect_initial_seconds=reconnect_initial_seconds,
        reconnect_max_seconds=reconnect_max_seconds,
        rtds_heartbeat_seconds=rtds_heartbeat_seconds,
        rtds_receive_idle_timeout_seconds=rtds_receive_idle_timeout_seconds,
        sink_timeout_seconds=sink_timeout_seconds,
        checkpoint_timeout_seconds=checkpoint_timeout_seconds,
    )


def _parse_iso_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return int(parsed.timestamp() * 1_000)


def _status_sha256(document: Mapping[str, Any]) -> str:
    unsigned = dict(document)
    unsigned.pop("status_sha256", None)
    return hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()


def _official_observation(message: Any) -> tuple[int, str] | None:
    if not isinstance(message, Mapping):
        return None
    payload = message.get("payload")
    if (
        message.get("topic") != BTC_TWAP_TOPIC
        or message.get("type") != "update"
        or not isinstance(payload, Mapping)
        or payload.get("symbol") != BTC_TWAP_SYMBOL
    ):
        return None
    timestamp = payload.get("timestamp")
    envelope_timestamp = message.get("timestamp")
    full_accuracy_value = payload.get("full_accuracy_value")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp < 0
        or isinstance(envelope_timestamp, bool)
        or not isinstance(envelope_timestamp, int)
        or envelope_timestamp != timestamp
        or not isinstance(full_accuracy_value, str)
        or not full_accuracy_value.isdigit()
    ):
        return None
    return timestamp, full_accuracy_value


def evaluate_continuous_rtds_health(
    status: Mapping[str, Any] | None,
    *,
    now: datetime,
    maximum_age_seconds: int = MAXIMUM_HEALTHY_STATUS_AGE_SECONDS,
    maximum_gap_ms: int = MAXIMUM_HEALTHY_OBSERVATION_GAP_MS,
) -> dict[str, Any]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    failures: list[str] = []
    heartbeat_age_seconds: float | None = None
    if status is None:
        failures.append("status_missing")
    else:
        expected_hash = _status_sha256(status)
        if status.get("status_sha256") != expected_hash:
            failures.append("status_hash_mismatch")
        heartbeat_at = _parse_iso_ms(status.get("heartbeat_at"))
        if heartbeat_at is None:
            failures.append("heartbeat_invalid")
        else:
            heartbeat_age_seconds = now.timestamp() - heartbeat_at / 1_000
            if (
                heartbeat_age_seconds < -1
                or heartbeat_age_seconds > maximum_age_seconds
            ):
                failures.append("status_stale")
        if status.get("phase") != "capturing":
            failures.append("capture_not_running")
        if (
            status.get("topic") != BTC_TWAP_TOPIC
            or status.get("symbol") != BTC_TWAP_SYMBOL
        ):
            failures.append("official_topic_or_symbol_mismatch")
        data_record_count = status.get("data_record_count")
        if (
            isinstance(data_record_count, bool)
            or not isinstance(data_record_count, int)
            or data_record_count <= 0
        ):
            failures.append("no_valid_official_observations")
        invalid_count = status.get("invalid_data_record_count")
        if isinstance(invalid_count, bool) or invalid_count != 0:
            failures.append("invalid_official_observation_present")
        disconnect_count = status.get("disconnect_count")
        error_count = status.get("error_count")
        if (
            isinstance(disconnect_count, bool)
            or not isinstance(disconnect_count, int)
            or disconnect_count != 0
            or isinstance(error_count, bool)
            or not isinstance(error_count, int)
            or error_count != 0
        ):
            failures.append("connection_gap_or_error_present")
        gap = status.get("max_observation_gap_ms")
        if (
            isinstance(gap, bool)
            or not isinstance(gap, int)
            or gap < 0
            or gap > maximum_gap_ms
        ):
            failures.append("official_observation_gap_exceeded")
        for key, expected in (
            ("paper_only", True),
            ("public_only", True),
            ("new_orders_disabled", True),
            ("authenticated_endpoints_used", 0),
            ("orders_submitted", 0),
        ):
            if status.get(key) != expected:
                failures.append(f"safety_guard_invalid:{key}")
    return {
        "schema_version": "btc-twap-continuous-rtds-health.v1",
        "healthy": not failures,
        "failures": failures,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "status": None if status is None else dict(status),
    }


class _ContinuousStatusTracker:
    def __init__(self, *, data_root: Path, status_path: Path) -> None:
        self.data_root = data_root
        self.status_path = status_path
        self.phase = "starting"
        self.heartbeat_at = _utc_now()
        self.connection_epoch = 0
        self.disconnect_count = 0
        self.reconnect_count = 0
        self.error_count = 0
        self.invalid_data_record_count = 0
        self.data_record_count = 0
        self.last_source_observed_at: str | None = None
        self.last_source_received_at: str | None = None
        self.last_full_accuracy_value: str | None = None
        self.max_observation_gap_ms = 0
        self._last_observed_at_ms: int | None = None
        self.checkpoint_reasons: Counter[str] = Counter()
        self.capture_integrity: Mapping[str, Any] | None = None
        self.startup_integrity: Mapping[str, Any] | None = None
        self.last_error: Mapping[str, Any] | None = None

    def observe_record(self, record: Mapping[str, Any]) -> None:
        self.phase = "capturing"
        received_at = record.get("received_at")
        if isinstance(received_at, str) and received_at:
            self.heartbeat_at = received_at
        if record.get("source") != "rtds_ws":
            return
        if record.get("kind") == "lifecycle":
            self._observe_lifecycle(record)
            return
        if record.get("kind") != "data":
            return
        message = record.get("payload")
        observation = _official_observation(message)
        if observation is None:
            self.invalid_data_record_count += 1
            self.error_count += 1
            self.last_error = {
                "error_type": "DataContractError",
                "error_code": "invalid_official_rtds_observation",
            }
            return
        observed_at_ms, full_accuracy_value = observation
        self.data_record_count += 1
        if isinstance(received_at, str) and received_at:
            self.last_source_received_at = received_at
        observed_at = (
            datetime.fromtimestamp(
                observed_at_ms / 1_000,
                tz=timezone.utc,
            )
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        if isinstance(observed_at, str) and observed_at:
            self.last_source_observed_at = observed_at
            observed_at_ms = _parse_iso_ms(observed_at)
            if (
                observed_at_ms is not None
                and self._last_observed_at_ms is not None
                and observed_at_ms >= self._last_observed_at_ms
            ):
                self.max_observation_gap_ms = max(
                    self.max_observation_gap_ms,
                    observed_at_ms - self._last_observed_at_ms,
                )
            if observed_at_ms is not None:
                self._last_observed_at_ms = observed_at_ms
        self.last_full_accuracy_value = full_accuracy_value

    def _observe_lifecycle(self, record: Mapping[str, Any]) -> None:
        event_type = record.get("event_type")
        detail = record.get("detail")
        detail_mapping = detail if isinstance(detail, Mapping) else {}
        if event_type == "connected":
            connection_number = detail_mapping.get("connection_number")
            if isinstance(connection_number, int) and connection_number > 0:
                self.connection_epoch = max(self.connection_epoch, connection_number)
            else:
                self.connection_epoch += 1
        elif event_type == "disconnected":
            self.disconnect_count += 1
        elif event_type == "reconnect_scheduled":
            self.reconnect_count += 1
        elif event_type == "error":
            self.error_count += 1
            self.last_error = dict(detail_mapping)

    def observe_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        self.phase = "capturing"
        updated_at = checkpoint.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            self.heartbeat_at = updated_at
        reason = checkpoint.get("reason")
        if isinstance(reason, str) and reason:
            self.checkpoint_reasons[reason] += 1

    def finalize(
        self,
        *,
        capture_integrity: Mapping[str, Any],
        startup_integrity: Mapping[str, Any],
        capture_error: BaseException | None,
    ) -> None:
        self.capture_integrity = copy.deepcopy(dict(capture_integrity))
        self.startup_integrity = copy.deepcopy(dict(startup_integrity))
        self.heartbeat_at = _utc_now()
        if capture_error is None:
            self.phase = "stopped"
            return
        self.phase = "failed"
        self.last_error = safe_error_details(capture_error, code="capture_failed")

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "phase": self.phase,
            "heartbeat_at": self.heartbeat_at,
            "pid": os.getpid(),
            "data_root": str(self.data_root),
            "status_path": str(self.status_path),
            "topic": BTC_TWAP_TOPIC,
            "symbol": BTC_TWAP_SYMBOL,
            "connection_epoch": self.connection_epoch,
            "disconnect_count": self.disconnect_count,
            "reconnect_count": self.reconnect_count,
            "error_count": self.error_count,
            "invalid_data_record_count": self.invalid_data_record_count,
            "data_record_count": self.data_record_count,
            "last_source_observed_at": self.last_source_observed_at,
            "last_source_received_at": self.last_source_received_at,
            "last_full_accuracy_value": self.last_full_accuracy_value,
            "max_observation_gap_ms": self.max_observation_gap_ms,
            "coverage_healthy": (
                self.data_record_count > 0
                and self.disconnect_count == 0
                and self.error_count == 0
                and self.invalid_data_record_count == 0
                and self.max_observation_gap_ms <= MAXIMUM_HEALTHY_OBSERVATION_GAP_MS
            ),
            "checkpoint_reasons": dict(sorted(self.checkpoint_reasons.items())),
            "capture_integrity": self.capture_integrity,
            "startup_integrity": self.startup_integrity,
            "last_error": self.last_error,
            "paper_only": True,
            "public_only": True,
            "new_orders_disabled": True,
            "authenticated_endpoints_used": 0,
            "orders_submitted": 0,
        }
        document["status_sha256"] = _status_sha256(document)
        return document

    def write(self) -> dict[str, Any]:
        document = self.to_document()
        write_json_document(self.status_path, document)
        return document


class _StatusRecorderSink(RecorderSink):
    def __init__(
        self,
        *,
        backing: CaptureStoreRecorderSink,
        tracker: _ContinuousStatusTracker,
    ) -> None:
        self.backing = backing
        self.tracker = tracker

    @property
    def store(self) -> CaptureStore:
        return self.backing.store

    @property
    def startup_integrity_report(self) -> dict[str, Any]:
        return self.backing.startup_integrity_report

    async def emit(self, record: Mapping[str, Any]) -> Any:
        result = await self.backing.emit(record)
        self.tracker.observe_record(record)
        self.tracker.write()
        return result

    async def checkpoint(self, source: str, checkpoint: Mapping[str, Any]) -> Any:
        result = await self.backing.checkpoint(source, checkpoint)
        if source == "rtds_ws":
            self.tracker.observe_checkpoint(checkpoint)
        self.tracker.write()
        return result

    async def close(self) -> tuple[dict[str, Any], ...]:
        return await self.backing.close()


def _verify_live_order_guard() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("new-order safety guard is not active")


async def run_continuous_capture(
    config: ContinuousBtcTwapRtdsConfig,
    *,
    run_for_seconds: float | None = None,
    websocket_factory: WebSocketFactory | None = None,
    snapshot_client: Any | None = None,
    clock: RecorderClock | None = None,
) -> dict[str, Any]:
    _verify_live_order_guard()
    store = CaptureStore(config.data_root)
    tracker = _ContinuousStatusTracker(
        data_root=config.data_root,
        status_path=config.status_path
        or (config.data_root / "service" / "status.json"),
    )
    sink = _StatusRecorderSink(
        backing=CaptureStoreRecorderSink(
            store,
            max_records_per_batch=config.max_records_per_batch,
        ),
        tracker=tracker,
    )
    tracker.write()
    recorder = PublicRecorder(
        config=build_btc_twap_recorder_config(
            checkpoint_every_records=config.checkpoint_every_records,
            reconnect_initial_seconds=config.reconnect_initial_seconds,
            reconnect_max_seconds=config.reconnect_max_seconds,
            rtds_heartbeat_seconds=config.rtds_heartbeat_seconds,
            rtds_receive_idle_timeout_seconds=(
                config.rtds_receive_idle_timeout_seconds
            ),
            sink_timeout_seconds=config.sink_timeout_seconds,
            checkpoint_timeout_seconds=config.checkpoint_timeout_seconds,
        ),
        sink=sink,
        snapshot_client=snapshot_client or _NoSnapshotClient(),
        websocket_factory=websocket_factory or DefaultWebSocketFactory(),
        clock=clock,
    )
    capture_error: BaseException | None = None
    try:
        await recorder.run(run_for_seconds=run_for_seconds)
    except BaseException as exc:  # noqa: BLE001
        capture_error = exc
    manifests = await sink.close()
    integrity = store.audit_integrity()
    tracker.finalize(
        capture_integrity=integrity,
        startup_integrity=sink.startup_integrity_report,
        capture_error=capture_error,
    )
    status_document = tracker.write()
    summary = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "data_root": str(config.data_root),
        "status_path": str(config.status_path),
        "topic": BTC_TWAP_TOPIC,
        "symbol": BTC_TWAP_SYMBOL,
        "manifest_count": len(manifests),
        "manifest_record_count": sum(
            int(manifest.get("record_count", 0)) for manifest in manifests
        ),
        "integrity": integrity,
        "startup_integrity": sink.startup_integrity_report,
        "status": status_document,
        "new_orders_disabled": True,
        "capture_error": (
            None
            if capture_error is None
            else safe_error_details(capture_error, code="capture_failed")
        ),
    }
    write_json_document(config.data_root / "capture-summary.json", summary)
    if capture_error is not None:
        raise ContinuousCaptureFinalizationError(summary) from capture_error
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run continuous public-only BTC TWAP 60s RTDS capture."
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--status-path", type=Path)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--checkpoint-every-records", type=int, default=100)
    parser.add_argument("--max-records-per-batch", type=int, default=10_000)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = ContinuousBtcTwapRtdsConfig(
        data_root=args.data_root,
        status_path=args.status_path,
        checkpoint_every_records=args.checkpoint_every_records,
        max_records_per_batch=args.max_records_per_batch,
    )
    build_btc_twap_recorder_config(
        checkpoint_every_records=config.checkpoint_every_records,
    )
    _verify_live_order_guard()
    if args.validate_only:
        payload = {
            "schema_version": VALIDATION_SCHEMA_VERSION,
            "valid": True,
            "paper_only": True,
            "public_only": True,
            "new_orders_disabled": True,
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
            "topic": BTC_TWAP_TOPIC,
            "symbol": BTC_TWAP_SYMBOL,
            "status_path": str(config.status_path),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    summary = asyncio.run(
        run_continuous_capture(
            config,
            run_for_seconds=args.duration_seconds,
        )
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0
