#!/usr/bin/env python3
"""Progress-oriented watcher for BTC paper and raw-capture tracks."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_btc_regime_candidate_scoreboard import (  # noqa: E402
    build_candidate_scoreboard,
)
from src.edge_lab.btc_twap_relative_value_readiness import (  # noqa: E402
    CaptureCapacityEvidence,
    evaluate_capture_capacity,
)
from src.edge_lab.data_store import canonical_json_bytes  # noqa: E402


def _parse_utc(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_object_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Alert:
    track: str
    kind: str
    severity: str
    detail: str


@dataclass(frozen=True)
class AlertRecord:
    key: str
    severity: str
    subject: str
    body: dict[str, Any]


class Publisher:
    def publish(self, alert: AlertRecord) -> None:
        raise NotImplementedError


class JsonLinePublisher(Publisher):
    def publish(self, alert: AlertRecord) -> None:
        print(
            json.dumps(
                {
                    "key": alert.key,
                    "severity": alert.severity,
                    "subject": alert.subject,
                    "body": alert.body,
                },
                sort_keys=True,
            )
        )


class AwsCliSnsPublisher(Publisher):
    """Use the instance role through AWS CLI without a new Python dependency."""

    def __init__(self, topic_arn: str) -> None:
        if not topic_arn.startswith("arn:aws:sns:"):
            raise ValueError("invalid SNS topic ARN")
        self.topic_arn = topic_arn
        self.region = topic_arn.split(":", 5)[3]
        aws = shutil.which("aws")
        if aws is None:
            raise RuntimeError("AWS CLI is required for SNS publishing")
        self.aws = aws

    def publish(self, alert: AlertRecord) -> None:
        subprocess.run(
            [
                self.aws,
                "sns",
                "publish",
                "--region",
                self.region,
                "--topic-arn",
                self.topic_arn,
                "--subject",
                alert.subject[:100],
                "--message",
                json.dumps(alert.body, sort_keys=True),
            ],
            check=True,
            capture_output=True,
            text=True,
        )


@dataclass(frozen=True)
class TrackState:
    last_report_count: int | None = None
    last_report_change_at: str | None = None
    last_capture_change_at: str | None = None
    active_alerts: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatchState:
    tracks: Mapping[str, TrackState]


@dataclass(frozen=True)
class TrackSpec:
    name: str
    root: Path
    expected_report_period_seconds: int
    expected_capture_period_seconds: int
    kind: str


@dataclass(frozen=True)
class TrackConfig:
    name: str
    status_path: Path
    health_path: Path | None = None
    capacity_evidence_path: Path | None = None
    data_root: Path | None = None
    kind: str = "paper"
    capture_summary_glob: str = ""
    expected_cycle_seconds: int = 900
    maximum_heartbeat_age_seconds: int = 60
    attempt_receipt_glob: str = ""
    started_receipt_stale_seconds: int = 1800
    attempt_receipt_recent_window_count: int = 96
    attempt_receipt_recent_window_hours: int = 24
    expected_regime: str | None = None
    regime_state_path: Path | None = None
    lifecycle: str = "active"

    def __post_init__(self) -> None:
        lifecycle = self.lifecycle.strip().lower() or "active"
        if lifecycle not in {"active", "maintenance", "retired"}:
            raise ValueError(f"unsupported track lifecycle: {lifecycle}")
        object.__setattr__(self, "lifecycle", lifecycle)
        if (
            isinstance(self.maximum_heartbeat_age_seconds, bool)
            or not isinstance(self.maximum_heartbeat_age_seconds, int)
            or self.maximum_heartbeat_age_seconds <= 0
        ):
            raise ValueError("maximum_heartbeat_age_seconds must be positive")
        if (
            isinstance(self.started_receipt_stale_seconds, bool)
            or not isinstance(self.started_receipt_stale_seconds, int)
            or self.started_receipt_stale_seconds <= 0
        ):
            raise ValueError("started_receipt_stale_seconds must be positive")
        for name in (
            "attempt_receipt_recent_window_count",
            "attempt_receipt_recent_window_hours",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be positive")
        object.__setattr__(self, "status_path", self.status_path.expanduser().resolve())
        if self.health_path is not None:
            object.__setattr__(
                self,
                "health_path",
                self.health_path.expanduser().resolve(),
            )
        if self.capacity_evidence_path is not None:
            object.__setattr__(
                self,
                "capacity_evidence_path",
                self.capacity_evidence_path.expanduser().resolve(),
            )
        if self.data_root is not None:
            object.__setattr__(self, "data_root", self.data_root.expanduser().resolve())
        if self.regime_state_path is not None:
            object.__setattr__(
                self,
                "regime_state_path",
                self.regime_state_path.expanduser().resolve(),
            )

    @property
    def monitors_runtime(self) -> bool:
        return self.lifecycle == "active"


@dataclass(frozen=True)
class HostConfig:
    status_path: Path | None = None
    mem_available_threshold_bytes: int = 300 * 1024 * 1024
    cpu_credit_threshold: float = 100.0
    cpu_credit_decrease_seconds: int = 1800
    cpu_credit_region: str | None = None
    cpu_credit_instance_id: str | None = None
    cpu_credit_cloudwatch_timeout_seconds: int = 10
    cpu_credit_cloudwatch_period_seconds: int = 300
    cpu_credit_cloudwatch_lookback_seconds: int = 1800
    cpu_credit_cloudwatch_max_age_seconds: int = 900
    cpu_credit_expected: bool = True
    process_rss_budgets_bytes: Mapping[str, int] | None = None


@dataclass(frozen=True)
class WatchConfig:
    state_path: Path
    tracks: tuple[TrackConfig, ...]
    sns_topic_arn: str | None
    alerts_path: Path | None = None
    host: HostConfig | None = None
    digest_interval_seconds: int = 3600


@dataclass(frozen=True)
class WatchResult:
    state: dict[str, Any]
    alerts: tuple[Alert, ...]


def _mapping_track_state(
    value: TrackState | Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if isinstance(value, TrackState):
        return {
            "last_report_count": value.last_report_count,
            "last_report_change_at": value.last_report_change_at,
            "last_capture_change_at": value.last_capture_change_at,
            "active_alerts": {key: "page" for key in value.active_alerts},
        }
    if isinstance(value, Mapping):
        return {
            "last_report_count": value.get(
                "last_report_count",
                value.get("last_completed_report_count"),
            ),
            "last_report_change_at": value.get(
                "last_report_change_at",
                value.get("last_report_progress_at"),
            ),
            "last_capture_change_at": value.get(
                "last_capture_change_at",
                value.get("last_capture_progress_at"),
            ),
            "active_alerts": value.get("active_alerts", {}),
            "latest_capture_timestamp": value.get("latest_capture_timestamp"),
            "last_quarantine_count": value.get("last_quarantine_count", 0),
        }
    return {}


def evaluate_track(
    *,
    track: TrackSpec,
    status: Mapping[str, Any],
    health: Mapping[str, Any],
    prior_state: TrackState | Mapping[str, Any] | None,
    now_utc: str,
    latest_capture_timestamp: str | None,
) -> tuple[tuple[Alert, ...], TrackState]:
    now = _parse_utc(now_utc)
    if now is None:
        raise ValueError("now_utc must be a UTC timestamp")
    previous = _mapping_track_state(prior_state)
    alerts: list[Alert] = []
    active_alerts: list[str] = []

    phase = status.get("phase")
    if phase in {"error_wait", "stopped"}:
        alerts.append(Alert(track.name, "phase_unhealthy", "page", f"phase={phase}"))
        active_alerts.append("phase_unhealthy")

    completed_report_count = (
        status.get("completed_report_count")
        if isinstance(status.get("completed_report_count"), int)
        else None
    )
    last_report_change_at = previous.get("last_report_change_at")
    if completed_report_count != previous.get("last_report_count"):
        last_report_change_at = now_utc
    else:
        previous_report_change = _parse_utc(last_report_change_at)
        if (
            completed_report_count is not None
            and previous_report_change is not None
            and now - previous_report_change
            >= timedelta(seconds=track.expected_report_period_seconds * 2)
        ):
            alerts.append(
                Alert(
                    track.name,
                    "report_stalled",
                    "page",
                    "completed_report_count did not advance for two cycles",
                )
            )
            active_alerts.append("report_stalled")

    last_capture_change_at = previous.get("last_capture_change_at")
    previous_capture_marker = (
        previous.get("latest_capture_timestamp")
        or previous.get("last_capture_change_at")
    )
    if latest_capture_timestamp != previous_capture_marker:
        last_capture_change_at = latest_capture_timestamp or now_utc
    else:
        previous_capture_change = _parse_utc(last_capture_change_at)
        if (
            latest_capture_timestamp is not None
            and previous_capture_change is not None
            and now - previous_capture_change
            >= timedelta(seconds=track.expected_capture_period_seconds * 2)
        ):
            alerts.append(
                Alert(
                    track.name,
                    "capture_stalled",
                    "page",
                    "latest capture-summary did not advance for two cycles",
                )
            )
            active_alerts.append("capture_stalled")

    if previous.get("active_alerts") and not active_alerts:
        alerts.append(Alert(track.name, "recovery", "notice", "track recovered"))

    return (
        tuple(alerts),
        TrackState(
            last_report_count=completed_report_count,
            last_report_change_at=last_report_change_at,
            last_capture_change_at=last_capture_change_at,
            active_alerts=tuple(active_alerts),
        ),
    )


def _latest_capture_timestamp(pattern: str) -> str | None:
    latest_generated_at: datetime | None = None
    for candidate in sorted(Path(item) for item in glob.glob(pattern)):
        document = _read_object(candidate)
        generated_at = _parse_utc(
            document.get("generated_at") if isinstance(document, Mapping) else None
        )
        if generated_at is None:
            try:
                generated_at = datetime.fromtimestamp(
                    candidate.stat().st_mtime,
                    tz=timezone.utc,
                )
            except OSError:
                continue
        if generated_at is not None and (
            latest_generated_at is None or generated_at > latest_generated_at
        ):
            latest_generated_at = generated_at
    return None if latest_generated_at is None else _utc_text(latest_generated_at)


def _attempt_receipt_paths(track: TrackConfig) -> tuple[Path, ...]:
    if not track.attempt_receipt_glob:
        return ()
    return tuple(
        sorted(
            Path(path).resolve() for path in glob.glob(track.attempt_receipt_glob)
        )
    )


def _receipt_capture_summary_path(
    receipt: Mapping[str, Any],
    *,
    data_root: Path,
) -> Path | None:
    root_value = receipt.get("capture_root")
    if not isinstance(root_value, str) or not root_value:
        return None
    summary_path = Path(root_value).expanduser().resolve() / "capture-summary.json"
    try:
        summary_path.relative_to(data_root)
    except ValueError:
        return None
    return summary_path


def _capture_capacity_snapshot(
    track: TrackConfig,
    *,
    now: datetime,
    host_status: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Evaluate the v0.8 capacity contract from durable public artifacts."""

    if track.kind != "edge_readiness":
        return None
    if track.data_root is None:
        return {
            "passed": False,
            "reason_codes": ["capture_capacity_data_root_unconfigured"],
            "capture_attempt_count": 0,
        }
    data_root = track.data_root.resolve()
    receipt_paths = _attempt_receipt_paths(track)
    if not receipt_paths:
        return {
            "passed": False,
            "reason_codes": [
                "capture_capacity_evidence_unavailable"
                if track.attempt_receipt_glob
                else "capture_attempt_receipts_unconfigured"
            ],
            "capture_attempt_count": 0,
        }
    recent_cutoff = now - timedelta(hours=track.attempt_receipt_recent_window_hours)
    failures = 0
    successes = 0
    pending = 0
    total_duration_ms = 0
    capture_roots: set[Path] = set()
    extra_reasons: list[str] = []
    summary_paths: list[Path] = []
    recent_integrity_reasons: list[str] = []
    historical_integrity_reasons: list[str] = []

    def _receipt_is_recent_by_mtime(path: Path) -> bool:
        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return False
        return modified_at >= recent_cutoff

    def _record_receipt_integrity_issue(code: str, *, recent: bool) -> None:
        if recent:
            recent_integrity_reasons.append(code)
        else:
            historical_integrity_reasons.append(code)

    def _evidence_integrity_document() -> dict[str, Any]:
        return {
            "recent_issue_count": len(recent_integrity_reasons),
            "recent_reason_codes": list(dict.fromkeys(recent_integrity_reasons)),
            "historical_issue_count": len(historical_integrity_reasons),
            "historical_reason_codes": list(dict.fromkeys(historical_integrity_reasons)),
        }

    indexed_receipts: list[tuple[Path, dict[str, Any], datetime]] = []
    for receipt_path in receipt_paths:
        recent_by_mtime = _receipt_is_recent_by_mtime(receipt_path)
        try:
            receipt_path.relative_to(data_root)
        except ValueError:
            _record_receipt_integrity_issue(
                "attempt_receipt_path_out_of_scope",
                recent=recent_by_mtime,
            )
            continue
        document = _read_object(receipt_path)
        if document is None:
            _record_receipt_integrity_issue(
                "attempt_receipt_unreadable",
                recent=recent_by_mtime,
            )
            continue
        created_at = _parse_utc(
            document.get("created_at")
            if isinstance(document.get("created_at"), str)
            else None
        )
        if created_at is None:
            _record_receipt_integrity_issue(
                "attempt_receipt_timestamp_invalid",
                recent=recent_by_mtime,
            )
            continue
        indexed_receipts.append((receipt_path, document, created_at))
    selected_receipts = [
        item for item in indexed_receipts if item[2] >= recent_cutoff
    ]
    selected_receipts.sort(key=lambda item: (item[2], item[0].name), reverse=True)
    selected_receipts = selected_receipts[: track.attempt_receipt_recent_window_count]
    if not selected_receipts:
        evidence_integrity = _evidence_integrity_document()
        return {
            "passed": False,
            "reason_codes": list(
                dict.fromkeys(
                    (
                        *(
                            ("attempt_receipt_evidence_integrity_recent",)
                            if evidence_integrity["recent_issue_count"]
                            else ()
                        ),
                        "capture_capacity_terminal_evidence_unavailable",
                    )
                )
            ),
            "capture_attempt_count": 0,
            "capture_failure_count": 0,
            "successful_attempt_count": 0,
            "pending_attempt_count": 0,
            "receipt_count": len(indexed_receipts),
            "evidence_integrity": evidence_integrity,
            "selection_rule": {
                "created_at_not_before": _utc_text(recent_cutoff),
                "maximum_receipts": track.attempt_receipt_recent_window_count,
            },
        }
    seen_capture_roots: set[Path] = set()
    seen_summary_paths: set[Path] = set()
    seen_attempt_ids: set[str] = set()
    terminal_receipt_paths: list[Path] = []
    pending_receipt_paths: list[Path] = []
    for _receipt_path, document, _created_at in selected_receipts:
        attempt_id = document.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            _record_receipt_integrity_issue(
                "attempt_receipt_id_missing",
                recent=True,
            )
            continue
        if _receipt_path.stem != attempt_id:
            _record_receipt_integrity_issue(
                "attempt_receipt_filename_mismatch",
                recent=True,
            )
            continue
        if attempt_id in seen_attempt_ids:
            _record_receipt_integrity_issue(
                "duplicate_attempt_receipt_id",
                recent=True,
            )
            continue
        seen_attempt_ids.add(attempt_id)
        status = document.get("status")
        if status == "failed":
            terminal_receipt_paths.append(_receipt_path)
            failures += 1
            continue
        if status == "started":
            marker = _parse_utc(
                document.get("updated_at")
                if isinstance(document.get("updated_at"), str)
                else None
            ) or _parse_utc(
                document.get("created_at")
                if isinstance(document.get("created_at"), str)
                else None
            )
            if marker is None:
                _record_receipt_integrity_issue(
                    "attempt_receipt_timestamp_invalid",
                    recent=True,
                )
            elif now - marker >= timedelta(seconds=track.started_receipt_stale_seconds):
                terminal_receipt_paths.append(_receipt_path)
                failures += 1
                extra_reasons.append("stale_started_attempt_receipt")
            else:
                pending_receipt_paths.append(_receipt_path)
                pending += 1
            continue
        if status != "succeeded":
            _record_receipt_integrity_issue(
                "attempt_receipt_status_invalid",
                recent=True,
            )
            continue
        terminal_receipt_paths.append(_receipt_path)
        capture_root_value = document.get("capture_root")
        if not isinstance(capture_root_value, str) or not capture_root_value:
            _record_receipt_integrity_issue(
                "attempt_receipt_capture_root_missing",
                recent=True,
            )
            continue
        capture_root = Path(capture_root_value).expanduser().resolve()
        try:
            capture_root.relative_to(data_root)
        except ValueError:
            _record_receipt_integrity_issue(
                "attempt_receipt_capture_root_out_of_scope",
                recent=True,
            )
            continue
        if capture_root in seen_capture_roots:
            _record_receipt_integrity_issue(
                "duplicate_capture_root_in_recent_window",
                recent=True,
            )
            continue
        summary_path = _receipt_capture_summary_path(document, data_root=data_root)
        if summary_path is None:
            _record_receipt_integrity_issue(
                "attempt_receipt_capture_root_missing",
                recent=True,
            )
            continue
        if summary_path in seen_summary_paths:
            _record_receipt_integrity_issue(
                "duplicate_capture_summary_in_recent_window",
                recent=True,
            )
            continue
        seen_capture_roots.add(capture_root)
        seen_summary_paths.add(summary_path)
        summary_paths.append(summary_path)
    for summary_path in summary_paths:
        document = _read_object(summary_path)
        if document is None:
            _record_receipt_integrity_issue(
                "capture_summary_missing_for_success_receipt",
                recent=True,
            )
            continue
        if (
            document.get("schema_version")
            != "btc-twap-compact-forward-capture-summary.v1"
            or document.get("data_root") != str(summary_path.parent.resolve())
        ):
            _record_receipt_integrity_issue(
                "capture_summary_identity_mismatch",
                recent=True,
            )
            continue
        capture_roots.add(summary_path.parent)
        integrity = document.get("integrity")
        integrity_failed = (
            not isinstance(integrity, Mapping)
            or any(bool(value) for value in integrity.values())
        )
        recorder_failures = document.get("recorder_leg_failures")
        if (
            document.get("capture_error") is not None
            or integrity_failed
            or not isinstance(recorder_failures, list)
            or bool(recorder_failures)
        ):
            failures += 1
            continue
        successes += 1
        try:
            duration = Decimal(str(document.get("duration_seconds")))
        except (InvalidOperation, ValueError):
            duration = Decimal(0)
        if duration.is_finite() and duration > 0:
            total_duration_ms += int(
                (duration * Decimal(1_000)).to_integral_value(
                    rounding=ROUND_CEILING
                )
            )
        else:
            extra_reasons.append("capture_duration_evidence_invalid")
    total_capture_bytes = 0
    for capture_root in capture_roots:
        for path in capture_root.rglob("*"):
            if path.is_symlink():
                extra_reasons.append("capture_tree_contains_symlink")
                continue
            if path.is_file():
                try:
                    total_capture_bytes += path.stat().st_size
                except OSError:
                    extra_reasons.append("capture_tree_size_unreadable")
    projected_daily_bytes = (
        1024**3 + 1
        if total_duration_ms <= 0
        else (
            total_capture_bytes * 86_400_000 + total_duration_ms - 1
        )
        // total_duration_ms
    )
    try:
        free_disk_bytes = shutil.disk_usage(data_root).free
    except OSError:
        free_disk_bytes = 0
        extra_reasons.append("capture_free_disk_unavailable")
    available_memory = host_status.get("mem_available_bytes")
    if (
        isinstance(available_memory, bool)
        or not isinstance(available_memory, int)
        or available_memory < 0
    ):
        available_memory = 0
        extra_reasons.append("capture_memory_telemetry_unavailable")
    cpu_balance = host_status.get("cpu_credit_balance")
    cpu_unavailable = host_status.get("cpu_credit_telemetry_unavailable")
    cpu_expected = host_status.get("cpu_credit_expected", True)
    if cpu_expected is False:
        unavailable_reason = None
        if isinstance(cpu_unavailable, Mapping):
            unavailable_reason = cpu_unavailable.get("reason")
        elif isinstance(cpu_unavailable, str):
            unavailable_reason = cpu_unavailable
        if cpu_unavailable is not None and unavailable_reason != "cpu_credit_not_applicable":
            extra_reasons.append("cpu_credit_telemetry_unavailable")
        cpu_exhausted = False
    elif (
        isinstance(cpu_balance, bool)
        or not isinstance(cpu_balance, (int, float))
        or cpu_unavailable is not None
    ):
        cpu_exhausted = True
        extra_reasons.append("cpu_credit_telemetry_unavailable")
    else:
        cpu_exhausted = float(cpu_balance) <= 0
    terminal_attempt_count = successes + failures
    if terminal_attempt_count == 0:
        evidence_integrity = _evidence_integrity_document()
        return {
            "passed": False,
            "reason_codes": list(
                dict.fromkeys(
                    (
                        *(
                            ("attempt_receipt_evidence_integrity_recent",)
                            if recent_integrity_reasons
                            else ()
                        ),
                        *extra_reasons,
                        "capture_capacity_terminal_evidence_unavailable",
                        *(("capture_attempts_pending",) if pending else ()),
                    )
                )
            ),
            "capture_attempt_count": 0,
            "capture_failure_count": 0,
            "successful_attempt_count": successes,
            "pending_attempt_count": pending,
            "receipt_count": len(indexed_receipts),
            "evidence_integrity": evidence_integrity,
            "selection_rule": {
                "created_at_not_before": _utc_text(recent_cutoff),
                "maximum_receipts": track.attempt_receipt_recent_window_count,
            },
        }
    evidence = CaptureCapacityEvidence(
        capture_attempt_count=terminal_attempt_count,
        capture_failure_count=min(failures, terminal_attempt_count),
        free_disk_bytes=free_disk_bytes,
        projected_daily_capture_bytes=projected_daily_bytes,
        available_memory_bytes=available_memory,
        burstable_cpu_credit_exhausted=cpu_exhausted,
    )
    verdict = evaluate_capture_capacity(evidence)
    evidence_integrity = _evidence_integrity_document()
    reasons = tuple(
        dict.fromkeys(
            (
                *verdict.reason_codes,
                *(
                    ("attempt_receipt_evidence_integrity_recent",)
                    if recent_integrity_reasons
                    else ()
                ),
                *extra_reasons,
                *(("capture_attempts_pending",) if pending else ()),
            )
        )
    )
    unsigned = {
        "schema_version": "btc-twap-capture-capacity-evidence.v1",
        "generated_at": _utc_text(now),
        "track": track.name,
        "attempt_receipts": [
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in terminal_receipt_paths
            if path.is_file()
        ],
        "pending_attempt_receipts": [
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in pending_receipt_paths
            if path.is_file()
        ],
        "host_metrics": {
            "mem_available_bytes": host_status.get("mem_available_bytes"),
            "cpu_credit_balance": (
                None
                if host_status.get("cpu_credit_balance") is None
                else str(host_status.get("cpu_credit_balance"))
            ),
                "cpu_credit_telemetry_unavailable": host_status.get(
                    "cpu_credit_telemetry_unavailable"
                ),
        },
        "evidence_integrity": evidence_integrity,
        "verdict": {
            **verdict.to_document(),
            "passed": (
                verdict.passed
                and not recent_integrity_reasons
                and not extra_reasons
                and pending == 0
            ),
            "reason_codes": list(reasons),
            "capture_attempt_count": evidence.capture_attempt_count,
            "capture_failure_count": evidence.capture_failure_count,
            "successful_attempt_count": successes,
            "pending_attempt_count": pending,
            "receipt_count": len(indexed_receipts),
            "free_disk_bytes": evidence.free_disk_bytes,
            "projected_daily_capture_bytes": evidence.projected_daily_capture_bytes,
            "available_memory_bytes": evidence.available_memory_bytes,
            "burstable_cpu_credit_exhausted": (
                evidence.burstable_cpu_credit_exhausted
            ),
            "selection_rule": {
                "created_at_not_before": _utc_text(recent_cutoff),
                "maximum_receipts": track.attempt_receipt_recent_window_count,
            },
        },
    }
    document = {
        **unsigned["verdict"],
        "schema_version": unsigned["schema_version"],
        "generated_at": unsigned["generated_at"],
        "track": unsigned["track"],
        "attempt_receipts": unsigned["attempt_receipts"],
        "pending_attempt_receipts": unsigned["pending_attempt_receipts"],
        "host_metrics": unsigned["host_metrics"],
        "evidence_integrity": unsigned["evidence_integrity"],
    }
    document["artifact_sha256"] = hashlib.sha256(
        canonical_json_bytes(document)
    ).hexdigest()
    return document


def evaluate_watch(
    config: WatchConfig,
    *,
    now: datetime,
    previous_state: Mapping[str, Any] | None = None,
) -> WatchResult:
    previous_tracks = (
        previous_state.get("tracks", {}) if isinstance(previous_state, Mapping) else {}
    )
    alerts: list[Alert] = []
    state_tracks: dict[str, Any] = {}
    for track in config.tracks:
        status = _read_object(track.status_path) or {}
        health = (
            _read_object(track.health_path) or {}
            if track.health_path is not None
            else {}
        )
        latest_capture_timestamp = _latest_capture_timestamp(track.capture_summary_glob)
        track_alerts, next_state = evaluate_track(
            track=TrackSpec(
                name=track.name,
                root=track.status_path.parent.parent,
                expected_report_period_seconds=track.expected_cycle_seconds,
                expected_capture_period_seconds=track.expected_cycle_seconds,
                kind=track.kind,
            ),
            status=status,
            health=health,
            prior_state=previous_tracks.get(track.name),
            now_utc=_utc_text(now),
            latest_capture_timestamp=latest_capture_timestamp,
        )
        alerts.extend(track_alerts)
        previous_track = _mapping_track_state(previous_tracks.get(track.name))
        quarantine_count = (
            1 if status.get("latest_classification") == "quarantine" else 0
        )
        if quarantine_count > int(previous_track.get("last_quarantine_count", 0) or 0):
            alerts.append(
                Alert(
                    track.name,
                    "quarantine_growth",
                    "warn",
                    "quarantine count increased",
                )
            )
        active_map = {
            alert.kind: alert.severity
            for alert in track_alerts
            if alert.kind != "recovery"
        }
        state_tracks[track.name] = {
            "last_completed_report_count": next_state.last_report_count,
            "last_report_progress_at": next_state.last_report_change_at,
            "last_capture_progress_at": next_state.last_capture_change_at,
            "last_phase_healthy_at": _utc_text(now) if not active_map else None,
            "last_quarantine_count": quarantine_count,
            "active_alerts": active_map,
            "latest_capture_timestamp": latest_capture_timestamp,
        }
    return WatchResult(
        state={"checked_at": _utc_text(now), "tracks": state_tracks},
        alerts=tuple(alerts),
    )


def watch_once(
    config: Mapping[str, Any],
    previous_state: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    watch_config = WatchConfig(
        state_path=Path(str(config["state_path"])),
        tracks=tuple(
            TrackConfig(
                name=str(track["id"]),
                kind=str(track.get("kind", "paper")),
                status_path=Path(str(track["status_path"])),
                health_path=Path(str(track.get("health_path", track["status_path"]))),
                capacity_evidence_path=(
                    Path(str(track["capacity_evidence_path"]))
                    if track.get("capacity_evidence_path")
                    else None
                ),
                capture_summary_glob=str(track.get("capture_summary_glob", "")),
                expected_cycle_seconds=int(track.get("expected_cycle_seconds", 900)),
                maximum_heartbeat_age_seconds=int(
                    track.get("maximum_heartbeat_age_seconds", 60)
                ),
                attempt_receipt_glob=str(track.get("attempt_receipt_glob", "")),
                started_receipt_stale_seconds=int(
                    track.get("started_receipt_stale_seconds", 1800)
                ),
                attempt_receipt_recent_window_count=int(
                    track.get("attempt_receipt_recent_window_count", 96)
                ),
                attempt_receipt_recent_window_hours=int(
                    track.get("attempt_receipt_recent_window_hours", 24)
                ),
            )
            for track in config.get("tracks", [])
            if isinstance(track, Mapping)
        ),
        sns_topic_arn=(
            str(config["sns_topic_arn"])
            if config.get("sns_topic_arn")
            else None
        ),
    )
    result = evaluate_watch(
        watch_config,
        now=datetime.now(timezone.utc),
        previous_state=previous_state,
    )
    state = result.state
    if isinstance(config.get("candidate_scoreboard"), Mapping):
        reports_dir = Path(str(config["candidate_scoreboard"]["reports_dir"]))
        scoreboard = build_candidate_scoreboard(sorted(reports_dir.glob("*.json")))
        state["candidate_scoreboard"] = scoreboard
        output_path = config["candidate_scoreboard"].get("output_path")
        if isinstance(output_path, str) and output_path:
            Path(output_path).write_text(
                json.dumps(scoreboard, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    alerts = {
        "schema_version": "btc-paper-track-watch-alerts.v1",
        "checked_at": state["checked_at"],
        "events": [
            {
                "track": alert.track,
                "kind": alert.kind,
                "severity": alert.severity,
                "detail": alert.detail,
            }
            for alert in result.alerts
        ],
    }
    return state, alerts


def _load_watch_config(path: Path) -> WatchConfig:
    document = _read_object(path)
    if document is None:
        raise ValueError("watch config does not exist")
    tracks = tuple(
        TrackConfig(
            name=str(track["name"]),
            status_path=Path(str(track["status_path"])),
            health_path=(
                Path(str(track["health_path"]))
                if track.get("health_path")
                else None
            ),
            capacity_evidence_path=(
                Path(str(track["capacity_evidence_path"]))
                if track.get("capacity_evidence_path")
                else None
            ),
            data_root=(
                Path(str(track["data_root"]))
                if track.get("data_root")
                else None
            ),
            kind=str(track.get("kind", "paper")),
            capture_summary_glob=str(track.get("capture_summary_glob", "")),
            expected_cycle_seconds=int(track.get("expected_cycle_seconds", 900)),
            maximum_heartbeat_age_seconds=int(
                track.get("maximum_heartbeat_age_seconds", 60)
            ),
            attempt_receipt_glob=str(track.get("attempt_receipt_glob", "")),
            started_receipt_stale_seconds=int(
                track.get("started_receipt_stale_seconds", 1800)
            ),
            attempt_receipt_recent_window_count=int(
                track.get("attempt_receipt_recent_window_count", 96)
            ),
            attempt_receipt_recent_window_hours=int(
                track.get("attempt_receipt_recent_window_hours", 24)
            ),
            expected_regime=(
                str(track["expected_regime"])
                if track.get("expected_regime")
                else None
            ),
            regime_state_path=(
                Path(str(track["regime_state_path"]))
                if track.get("regime_state_path")
                else None
            ),
            lifecycle=str(track.get("lifecycle", "active")),
        )
        for track in document.get("tracks", [])
    )
    host_document = document.get("host")
    host = None
    if isinstance(host_document, Mapping):
        host = HostConfig(
            status_path=(
                Path(str(host_document["status_path"]))
                if host_document.get("status_path")
                else None
            ),
            mem_available_threshold_bytes=int(
                host_document.get("mem_available_threshold_bytes", 300 * 1024 * 1024)
            ),
            cpu_credit_threshold=float(
                host_document.get("cpu_credit_threshold", 100.0)
            ),
            cpu_credit_decrease_seconds=int(
                host_document.get("cpu_credit_decrease_seconds", 1800)
            ),
            cpu_credit_region=(
                str(host_document["cpu_credit_region"])
                if host_document.get("cpu_credit_region")
                else None
            ),
            cpu_credit_instance_id=(
                str(host_document["cpu_credit_instance_id"])
                if host_document.get("cpu_credit_instance_id")
                else None
            ),
            cpu_credit_cloudwatch_timeout_seconds=int(
                host_document.get("cpu_credit_cloudwatch_timeout_seconds", 10)
            ),
            cpu_credit_cloudwatch_period_seconds=int(
                host_document.get("cpu_credit_cloudwatch_period_seconds", 300)
            ),
            cpu_credit_cloudwatch_lookback_seconds=int(
                host_document.get("cpu_credit_cloudwatch_lookback_seconds", 1800)
            ),
            cpu_credit_cloudwatch_max_age_seconds=int(
                host_document.get("cpu_credit_cloudwatch_max_age_seconds", 900)
            ),
            cpu_credit_expected=bool(
                host_document.get("cpu_credit_expected", True)
            ),
            process_rss_budgets_bytes=(
                {
                    str(key): int(value)
                    for key, value in host_document.get(
                        "process_rss_budgets_bytes",
                        {},
                    ).items()
                }
                if isinstance(host_document.get("process_rss_budgets_bytes"), Mapping)
                else None
            ),
        )
    return WatchConfig(
        state_path=Path(str(document["state_path"])),
        tracks=tracks,
        sns_topic_arn=(
            str(document["sns_topic_arn"])
            if document.get("sns_topic_arn")
            else None
        ),
        alerts_path=(
            Path(str(document["alerts_path"]))
            if document.get("alerts_path")
            else None
        ),
        host=host,
        digest_interval_seconds=int(document.get("digest_interval_seconds", 3600)),
    )


def _proc_kib_value(path: Path, field: str) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    prefix = f"{field}:"
    for line in lines:
        if not line.startswith(prefix):
            continue
        parts = line[len(prefix) :].split()
        if not parts or not parts[0].isdigit():
            return None
        return int(parts[0]) * 1024
    return None


def collect_local_host_status(
    tracks: Sequence[TrackConfig],
    *,
    now: datetime,
    proc_root: Path = Path("/proc"),
    previous: Mapping[str, Any] | None = None,
    host: HostConfig | None = None,
    subprocess_run: Any = subprocess.run,
    aws_cli_path: str | None = None,
) -> dict[str, Any]:
    """Collect Linux-local memory and per-track RSS without extra privileges."""

    snapshot: dict[str, Any] = {
        "schema_version": "polymm-local-host-status.v1",
        "collected_at": _utc_text(now),
        "mem_available_bytes": _proc_kib_value(
            proc_root / "meminfo",
            "MemAvailable",
        ),
        "process_rss_bytes": {},
        "cpu_credit_expected": True if host is None else host.cpu_credit_expected,
    }
    process_rss: dict[str, int] = {}
    for track in tracks:
        status = _read_object(track.status_path) or {}
        pids = {
            value
            for field in ("pid", "child_pid")
            if isinstance((value := status.get(field)), int)
            and not isinstance(value, bool)
            and value > 0
        }
        rss_values = [
            _proc_kib_value(proc_root / str(pid) / "status", "VmRSS")
            for pid in pids
        ]
        known = [value for value in rss_values if value is not None]
        if known:
            process_rss[track.name] = sum(known)
    snapshot["process_rss_bytes"] = process_rss
    if host is not None:
        snapshot.update(
            _cloudwatch_cpu_credit_status(
                host=host,
                now=now,
                subprocess_run=subprocess_run,
                aws_cli_path=aws_cli_path,
            )
        )
    return snapshot


def _cloudwatch_cpu_credit_status(
    *,
    host: HostConfig,
    now: datetime,
    subprocess_run: Any,
    aws_cli_path: str | None,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"cpu_credit_refresh_attempted": True}
    if not host.cpu_credit_region or not host.cpu_credit_instance_id:
        snapshot["cpu_credit_telemetry_unavailable"] = {
            "reason": "cpu_credit_config_missing",
            "region": host.cpu_credit_region,
            "instance_id": host.cpu_credit_instance_id,
        }
        return snapshot
    aws = aws_cli_path or shutil.which("aws")
    if aws is None:
        snapshot["cpu_credit_telemetry_unavailable"] = {
            "reason": "aws_cli_missing",
            "region": host.cpu_credit_region,
            "instance_id": host.cpu_credit_instance_id,
        }
        return snapshot
    end_time = now.astimezone(timezone.utc) + timedelta(minutes=1)
    start_time = end_time - timedelta(
        seconds=max(host.cpu_credit_cloudwatch_lookback_seconds, 300)
    )
    try:
        completed = subprocess_run(
            [
                aws,
                "cloudwatch",
                "get-metric-statistics",
                "--region",
                host.cpu_credit_region,
                "--namespace",
                "AWS/EC2",
                "--metric-name",
                "CPUCreditBalance",
                "--dimensions",
                f"Name=InstanceId,Value={host.cpu_credit_instance_id}",
                "--statistics",
                "Average",
                "--period",
                str(host.cpu_credit_cloudwatch_period_seconds),
                "--start-time",
                _utc_text(start_time),
                "--end-time",
                _utc_text(end_time),
                "--output",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=host.cpu_credit_cloudwatch_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        snapshot["cpu_credit_telemetry_unavailable"] = {
            "reason": "cloudwatch_timeout",
            "region": host.cpu_credit_region,
            "instance_id": host.cpu_credit_instance_id,
        }
        return snapshot
    except OSError:
        snapshot["cpu_credit_telemetry_unavailable"] = {
            "reason": "cloudwatch_process_failed",
            "region": host.cpu_credit_region,
            "instance_id": host.cpu_credit_instance_id,
        }
        return snapshot
    except subprocess.CalledProcessError as exc:
        snapshot["cpu_credit_telemetry_unavailable"] = {
            "reason": "cloudwatch_cli_failed",
            "region": host.cpu_credit_region,
            "instance_id": host.cpu_credit_instance_id,
            "stderr": exc.stderr.strip() if isinstance(exc.stderr, str) else "",
        }
        return snapshot
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError:
        snapshot["cpu_credit_telemetry_unavailable"] = {
            "reason": "cloudwatch_bad_json",
            "region": host.cpu_credit_region,
            "instance_id": host.cpu_credit_instance_id,
        }
        return snapshot
    datapoints = document.get("Datapoints")
    if not isinstance(datapoints, list):
        datapoints = []
    points: list[tuple[datetime, float]] = []
    for point in datapoints:
        if not isinstance(point, Mapping):
            continue
        observed_at = _parse_utc(
            point.get("Timestamp") if isinstance(point.get("Timestamp"), str) else None
        )
        average = point.get("Average")
        if observed_at is None or not isinstance(average, (int, float)):
            continue
        points.append((observed_at, float(average)))
    if not points:
        snapshot["cpu_credit_telemetry_unavailable"] = {
            "reason": (
                "cpu_credit_not_applicable"
                if not host.cpu_credit_expected
                else "cloudwatch_metric_missing"
            ),
            "region": host.cpu_credit_region,
            "instance_id": host.cpu_credit_instance_id,
        }
        return snapshot
    observed_at, balance = max(points, key=lambda item: item[0])
    metric_age = now.astimezone(timezone.utc) - observed_at
    if metric_age < timedelta(minutes=-1) or metric_age > timedelta(
        seconds=host.cpu_credit_cloudwatch_max_age_seconds
    ):
        snapshot["cpu_credit_telemetry_unavailable"] = {
            "reason": "cloudwatch_metric_stale",
            "region": host.cpu_credit_region,
            "instance_id": host.cpu_credit_instance_id,
            "observed_at": _utc_text(observed_at),
        }
        return snapshot
    snapshot["cpu_credit_balance"] = balance
    snapshot["cpu_credit_observed_at"] = _utc_text(observed_at)
    return snapshot


def _refresh_local_host_status(
    config: WatchConfig,
    *,
    now: datetime,
    proc_root: Path = Path("/proc"),
) -> None:
    if (
        config.host is None
        or config.host.status_path is None
        or not (proc_root / "meminfo").is_file()
    ):
        return
    previous = _read_object(config.host.status_path)
    _write_object_atomic(
        config.host.status_path,
        collect_local_host_status(
            config.tracks,
            now=now,
            proc_root=proc_root,
            previous=previous,
            host=config.host,
        ),
    )


def _regime_alerts(
    config: WatchConfig,
    *,
    prior_state: Mapping[str, Any],
    result_tracks: dict[str, Any],
) -> dict[str, AlertRecord]:
    alerts: dict[str, AlertRecord] = {}
    prior_tracks = prior_state.get("tracks", {})
    for track in config.tracks:
        if not track.monitors_runtime:
            continue
        if track.regime_state_path is None:
            continue
        regime = _read_object(track.regime_state_path) or {}
        observed = regime.get("observed_regime")
        consecutive = regime.get("consecutive_rejections", 0)
        context = {
            "track": track.name,
            "expected_regime": track.expected_regime,
            "observed_regime": observed,
            "first_seen_at": regime.get("first_seen_at"),
            "last_success_at": regime.get("last_success_at")
            or result_tracks.get(track.name, {}).get("last_success_at"),
            "consecutive_rejections": consecutive,
            "affected_market_slugs": regime.get("affected_market_slugs", []),
        }
        if track.expected_regime and observed and observed != track.expected_regime:
            key = f"{track.name}:regime_mismatch"
            alerts[key] = AlertRecord(
                key=key,
                severity=(
                    "page"
                    if isinstance(consecutive, int) and consecutive >= 3
                    else "warn"
                ),
                subject=f"{track.name} regime mismatch",
                body=context,
            )
        quarantine = regime.get("quarantine_count")
        prior_track = (
            prior_tracks.get(track.name, {})
            if isinstance(prior_tracks, Mapping)
            else {}
        )
        previous_count = (
            prior_track.get("last_quarantine_count", 0)
            if isinstance(prior_track, Mapping)
            else 0
        )
        if isinstance(quarantine, int) and quarantine > int(previous_count or 0):
            key = f"{track.name}:quarantine_growth"
            alerts[key] = AlertRecord(
                key=key,
                severity="warn",
                subject=f"{track.name} quarantine growth",
                body={**context, "quarantine_count": quarantine},
            )
            result_tracks.setdefault(track.name, {})[
                "last_quarantine_count"
            ] = quarantine
    return alerts


def _host_alerts_and_state(
    config: WatchConfig,
    *,
    prior_state: Mapping[str, Any],
    now: datetime,
) -> tuple[dict[str, AlertRecord], dict[str, Any]]:
    if config.host is None or config.host.status_path is None:
        return {}, {}
    alerts: dict[str, AlertRecord] = {}
    host = _read_object(config.host.status_path) or {}
    prior_host = (
        prior_state.get("host", {})
        if isinstance(prior_state.get("host"), Mapping)
        else {}
    )
    host_state = dict(prior_host)
    memory = host.get("mem_available_bytes")
    if isinstance(memory, int):
        host_state["mem_available_bytes"] = memory
        if memory < config.host.mem_available_threshold_bytes:
            key = "host:memory_pressure"
            alerts[key] = AlertRecord(
                key=key,
                severity="warn",
                subject="host memory pressure",
                body={"mem_available_bytes": memory},
            )
    refresh_attempted = bool(host.get("cpu_credit_refresh_attempted"))
    unavailable_value = host.get("cpu_credit_telemetry_unavailable")
    unavailable = (
        dict(unavailable_value) if isinstance(unavailable_value, Mapping) else None
    )
    if refresh_attempted:
        for field in (
            "cpu_credit_balance",
            "cpu_credit_observed_at",
            "cpu_credit_decreasing_since",
        ):
            host_state.pop(field, None)
    if unavailable is not None:
        host_state["cpu_credit_telemetry_unavailable"] = unavailable
        if unavailable.get("reason") != "cpu_credit_not_applicable":
            key = "host:cpu_credit_telemetry_unavailable"
            alerts[key] = AlertRecord(
                key=key,
                severity="warn",
                subject="host CPU credit telemetry unavailable",
                body=unavailable,
            )
    else:
        host_state.pop("cpu_credit_telemetry_unavailable", None)
    credits = host.get("cpu_credit_balance")
    observed_at = _parse_utc(
        host.get("cpu_credit_observed_at")
        if isinstance(host.get("cpu_credit_observed_at"), str)
        else None
    )
    prior_credits = prior_host.get("cpu_credit_balance")
    prior_observed_at = _parse_utc(
        prior_host.get("cpu_credit_observed_at")
        if isinstance(prior_host.get("cpu_credit_observed_at"), str)
        else None
    )
    decreasing_since = _parse_utc(
        prior_host.get("cpu_credit_decreasing_since")
        if isinstance(prior_host.get("cpu_credit_decreasing_since"), str)
        else None
    )
    if isinstance(credits, (int, float)):
        current_credits = float(credits)
        if (
            observed_at is not None
            and (prior_observed_at is None or observed_at > prior_observed_at)
            and isinstance(prior_credits, (int, float))
        ):
            if current_credits < float(prior_credits):
                decreasing_since = decreasing_since or prior_observed_at or observed_at
            else:
                decreasing_since = None
        host_state["cpu_credit_balance"] = current_credits
        if observed_at is not None:
            host_state["cpu_credit_observed_at"] = _utc_text(observed_at)
        host_state.pop("cpu_credit_telemetry_unavailable", None)
        if decreasing_since is None:
            host_state.pop("cpu_credit_decreasing_since", None)
        else:
            host_state["cpu_credit_decreasing_since"] = _utc_text(
                decreasing_since
            )
        if current_credits < config.host.cpu_credit_threshold:
            key = "host:cpu_credit_low"
            alerts[key] = AlertRecord(
                key=key,
                severity="warn",
                subject="host CPU credit low",
                body={"cpu_credit_balance": current_credits},
            )
        if decreasing_since is not None and now - decreasing_since >= timedelta(
            seconds=config.host.cpu_credit_decrease_seconds
        ):
            key = "host:cpu_credit_declining"
            alerts[key] = AlertRecord(
                key=key,
                severity="warn",
                subject="host CPU credit declining",
                body={
                    "cpu_credit_balance": current_credits,
                    "decreasing_since": _utc_text(decreasing_since),
                },
            )
    rss = host.get("process_rss_bytes")
    budgets = config.host.process_rss_budgets_bytes
    if isinstance(rss, Mapping):
        host_state["process_rss_bytes"] = dict(rss)
    if isinstance(rss, Mapping) and isinstance(budgets, Mapping):
        for name, budget in budgets.items():
            usage = rss.get(name)
            if isinstance(usage, int) and usage > budget:
                key = f"host:rss:{name}"
                alerts[key] = AlertRecord(
                    key=key,
                    severity="warn",
                    subject=f"{name} RSS over budget",
                    body={"process_name": name, "rss_bytes": usage},
                )
    return alerts, host_state


def _report_activity(status: Mapping[str, Any]) -> datetime | None:
    raw_path = status.get("latest_summary_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    document = _read_object(path)
    generated_at = _parse_utc(
        document.get("generated_at") if isinstance(document, Mapping) else None
    )
    if generated_at is not None:
        return generated_at
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _track_alert_body(
    track: TrackConfig,
    *,
    status: Mapping[str, Any],
    regime: Mapping[str, Any],
    last_success_at: datetime | None,
) -> dict[str, Any]:
    return {
        "track": track.name,
        "phase": status.get("phase"),
        "expected_regime": track.expected_regime,
        "observed_regime": regime.get("observed_regime"),
        "first_seen_at": regime.get("first_seen_at"),
        "last_success_at": (
            _utc_text(last_success_at) if last_success_at is not None else None
        ),
        "consecutive_rejections": regime.get("consecutive_rejections", 0),
        "affected_market_slugs": regime.get("affected_market_slugs", []),
    }


def run_watch_cycle(
    config: WatchConfig,
    *,
    publisher: Publisher,
    now: datetime,
) -> None:
    state_path = config.state_path
    _refresh_local_host_status(config, now=now)
    host_status = (
        _read_object(config.host.status_path) or {}
        if config.host is not None and config.host.status_path is not None
        else {}
    )
    state = _read_object(state_path) or {
        "schema_version": "polymm-paper-track-watch-state.v1",
        "alerts": {},
        "tracks": {},
        "host": {},
    }
    active_alerts: dict[str, AlertRecord] = {}
    result_tracks: dict[str, Any] = {}
    prior_tracks = (
        state.get("tracks", {})
        if isinstance(state.get("tracks"), Mapping)
        else {}
    )
    for track in config.tracks:
        if track.lifecycle == "retired":
            result_tracks[track.name] = {"lifecycle": "retired"}
            continue
        runtime_monitored = track.monitors_runtime
        status_document = _read_object(track.status_path)
        health_document = (
            _read_object(track.health_path)
            if track.health_path is not None
            else None
        )
        regime_document = (
            _read_object(track.regime_state_path)
            if track.regime_state_path is not None
            else None
        )
        status = status_document or {}
        health = health_document or {}
        regime = regime_document or {}
        capture_capacity = _capture_capacity_snapshot(
            track,
            now=now,
            host_status=host_status,
        )
        if (
            track.kind == "edge_readiness"
            and track.capacity_evidence_path is not None
            and capture_capacity is not None
        ):
            _write_object_atomic(track.capacity_evidence_path, capture_capacity)
        unavailable_sources = []
        for kind, path, document in (
            ("status", track.status_path, status_document),
            ("health", track.health_path, health_document),
            ("regime", track.regime_state_path, regime_document),
        ):
            if track.kind == "edge_readiness" and kind == "health":
                continue
            if path is not None and document is None:
                unavailable_sources.append(
                    {
                        "kind": kind,
                        "path": str(path),
                        "exists": path.exists(),
                    }
                )
        previous = (
            prior_tracks.get(track.name, {})
            if isinstance(prior_tracks.get(track.name), Mapping)
            else {}
        )

        report_activity = _report_activity(status)
        report_count = (
            status.get("completed_report_count")
            if isinstance(status.get("completed_report_count"), int)
            and not isinstance(status.get("completed_report_count"), bool)
            else None
        )
        report_progress_at = _parse_utc(previous.get("last_report_progress_at"))
        if track.kind != "rawcap":
            previous_count = previous.get("last_completed_report_count")
            if previous_count is None:
                report_progress_at = report_activity or now
            elif report_count != previous_count:
                report_progress_at = now
            elif report_progress_at is None:
                report_progress_at = report_activity or now

        capture_marker = _latest_capture_timestamp(track.capture_summary_glob)
        capture_activity = _parse_utc(capture_marker)
        capture_progress_at = _parse_utc(
            previous.get("last_capture_progress_at")
        )
        if "latest_capture_timestamp" not in previous:
            capture_progress_at = capture_activity or now
        elif capture_marker != previous.get("latest_capture_timestamp"):
            capture_progress_at = now
        elif capture_progress_at is None:
            capture_progress_at = capture_activity or now

        success_candidates = [
            value
            for value in (
                report_activity,
                capture_activity,
                report_progress_at,
                capture_progress_at,
            )
            if value is not None
        ]
        last_success = max(success_candidates, default=None)
        body = _track_alert_body(
            track,
            status=status,
            regime=regime,
            last_success_at=last_success,
        )
        if runtime_monitored and unavailable_sources:
            active_alerts[f"{track.name}:telemetry_unavailable"] = AlertRecord(
                key=f"{track.name}:telemetry_unavailable",
                severity="warn",
                subject=f"{track.name} telemetry unavailable",
                body={**body, "unavailable_sources": unavailable_sources},
            )
        heartbeat_at = _parse_utc(status.get("heartbeat_at"))
        if runtime_monitored and status_document is not None and (
            heartbeat_at is None
            or now - heartbeat_at
            > timedelta(seconds=track.maximum_heartbeat_age_seconds)
        ):
            active_alerts[f"{track.name}:heartbeat_stale"] = AlertRecord(
                key=f"{track.name}:heartbeat_stale",
                severity="page",
                subject=f"{track.name} heartbeat stale",
                body=body,
            )
        if runtime_monitored and track.kind != "rawcap" and (
            report_progress_at is not None
            and now - report_progress_at
            >= timedelta(seconds=track.expected_cycle_seconds * 2)
        ):
            active_alerts[f"{track.name}:report_stalled"] = AlertRecord(
                key=f"{track.name}:report_stalled",
                severity="page",
                subject=f"{track.name} reports stalled",
                body={**body, "completed_report_count": report_count},
            )
        if (
            runtime_monitored
            and capture_progress_at is not None
            and now - capture_progress_at
            >= timedelta(seconds=track.expected_cycle_seconds * 2)
        ):
            active_alerts[f"{track.name}:capture_stalled"] = AlertRecord(
                key=f"{track.name}:capture_stalled",
                severity="page",
                subject=f"{track.name} capture stalled",
                body={**body, "latest_capture_timestamp": capture_marker},
            )
        if runtime_monitored and status.get("phase") in {"error_wait", "stopped"} and (
            last_success is None or now - last_success >= timedelta(minutes=10)
        ):
            active_alerts[f"{track.name}:phase_unhealthy"] = AlertRecord(
                key=f"{track.name}:phase_unhealthy",
                severity="page",
                subject=f"{track.name} phase unhealthy",
                body=body,
            )
        health_failures = health.get("failures")
        material_health_failures = (
            [
                str(failure)
                for failure in health_failures
                if str(failure)
                not in {"service_phase_unhealthy", "collector_phase_unhealthy"}
            ]
            if isinstance(health_failures, list)
            else []
        )
        if runtime_monitored and material_health_failures:
            active_alerts[f"{track.name}:health_gate"] = AlertRecord(
                key=f"{track.name}:health_gate",
                severity="warn",
                subject=f"{track.name} health gate failed",
                body={**body, "failures": material_health_failures},
            )
        if (
            runtime_monitored
            and capture_capacity is not None
            and capture_capacity.get("passed") is not True
        ):
            active_alerts[f"{track.name}:capture_capacity"] = AlertRecord(
                key=f"{track.name}:capture_capacity",
                severity="page",
                subject=f"{track.name} capture capacity gate failed",
                body={**body, "capture_capacity": capture_capacity},
            )
        quarantine_count = regime.get("quarantine_count", 0)
        result_tracks[track.name] = {
            "last_completed_report_count": report_count,
            "last_report_progress_at": (
                _utc_text(report_progress_at)
                if report_progress_at is not None
                else None
            ),
            "latest_capture_timestamp": capture_marker,
            "last_capture_progress_at": (
                _utc_text(capture_progress_at)
                if capture_progress_at is not None
                else None
            ),
            "last_success_at": (
                _utc_text(last_success) if last_success is not None else None
            ),
            "last_phase_healthy_at": (
                _utc_text(now)
                if f"{track.name}:phase_unhealthy" not in active_alerts
                else None
            ),
            "last_quarantine_count": (
                quarantine_count if isinstance(quarantine_count, int) else 0
            ),
            "capture_capacity": capture_capacity,
        }
    active_alerts.update(
        _regime_alerts(
            config,
            prior_state=state,
            result_tracks=result_tracks,
        )
    )
    host_alerts, host_state = _host_alerts_and_state(
        config,
        prior_state=state,
        now=now,
    )
    active_alerts.update(host_alerts)
    prior_alerts = (
        dict(state.get("alerts", {}))
        if isinstance(state.get("alerts"), Mapping)
        else {}
    )
    for key, alert in active_alerts.items():
        previous_record = (
            prior_alerts.get(key) if isinstance(prior_alerts, Mapping) else None
        )
        last_published = _parse_utc(
            previous_record.get("last_published_at")
            if isinstance(previous_record, Mapping)
            else None
        )
        if previous_record is None:
            publisher.publish(alert)
            prior_alerts[key] = {"last_published_at": _utc_text(now)}
        elif (
            last_published is not None
            and now - last_published
            >= timedelta(seconds=config.digest_interval_seconds)
        ):
            publisher.publish(
                AlertRecord(
                    key=key,
                    severity="digest",
                    subject=f"{alert.subject} digest",
                    body=alert.body,
                )
            )
            prior_alerts[key] = {"last_published_at": _utc_text(now)}
        else:
            prior_alerts[key] = dict(previous_record)
    for key in list(prior_alerts):
        if key not in active_alerts:
            publisher.publish(
                AlertRecord(
                    key=key,
                    severity="notice",
                    subject=f"{key} recovered",
                    body={"recovered_at": _utc_text(now)},
                )
            )
            del prior_alerts[key]
    state["alerts"] = prior_alerts
    state["tracks"] = result_tracks
    state["host"] = host_state
    state["checked_at"] = _utc_text(now)
    _write_object_atomic(state_path, state)
    if config.alerts_path is not None:
        _write_object_atomic(
            config.alerts_path,
            {
                "schema_version": "polymm-paper-track-alerts.v1",
                "checked_at": _utc_text(now),
                "active_alerts": [
                    {
                        "key": alert.key,
                        "severity": alert.severity,
                        "subject": alert.subject,
                        "body": alert.body,
                    }
                    for alert in sorted(
                        active_alerts.values(),
                        key=lambda item: item.key,
                    )
                ],
            },
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stdout-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw = _read_object(args.config)
    if raw is None:
        raise SystemExit("watch config must be a JSON object")
    config = _load_watch_config(args.config)
    topic_arn = os.environ.get("POLYMM_SNS_TOPIC_ARN") or config.sns_topic_arn
    publisher: Publisher = (
        AwsCliSnsPublisher(topic_arn)
        if topic_arn and not args.stdout_only
        else JsonLinePublisher()
    )
    run_watch_cycle(config, publisher=publisher, now=datetime.now(timezone.utc))
    candidate = raw.get("candidate_scoreboard")
    if isinstance(candidate, Mapping) and candidate.get("reports_dir"):
        reports_dir = Path(str(candidate["reports_dir"]))
        scoreboard = build_candidate_scoreboard(reports_dir.glob("*.json"))
        if candidate.get("output_path"):
            _write_object_atomic(Path(str(candidate["output_path"])), scoreboard)
    print(
        json.dumps(
            {"checked_at": _utc_text(datetime.now(timezone.utc)), "status": "ok"},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
