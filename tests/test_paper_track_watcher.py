from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.edge_lab.btc_twap_relative_value_readiness as readiness
from scripts.watch_paper_tracks import (
    AlertRecord,
    Publisher,
    TrackConfig,
    _capture_capacity_snapshot,
    _load_watch_config,
    collect_local_host_status,
    run_watch_cycle,
)


class CollectingPublisher(Publisher):
    def __init__(self) -> None:
        self.alerts: list[AlertRecord] = []

    def publish(self, alert: AlertRecord) -> None:
        self.alerts.append(alert)


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _touch(path: Path, when: datetime) -> None:
    timestamp = when.timestamp()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
    path.touch()
    path.chmod(0o644)
    import os

    os.utime(path, (timestamp, timestamp))


def test_v08_capacity_snapshot_calls_fail_closed_capacity_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    data_root = tmp_path / "v08" / "data"
    for index in range(21):
        capture_root = data_root / "runs" / str(index) / f"attempt-{index}"
        _write_json(
            capture_root / "capture-summary.json",
            {
                "schema_version": "btc-twap-compact-forward-capture-summary.v1",
                "data_root": str(capture_root.resolve()),
                "duration_seconds": "300",
                "capture_error": (
                    {"code": "capture_failed"} if index == 0 else None
                ),
                "recorder_leg_failures": [],
                "integrity": {
                    "orphan_partials": [],
                    "raw_without_manifest": [],
                    "manifest_without_raw": [],
                    "checksum_mismatches": [],
                    "invalid_manifests": [],
                },
            },
        )
        (capture_root / "payload.bin").write_bytes(b"x" * 100)
        _write_json(
            data_root / "service" / "attempt-receipts" / f"attempt-{index}.json",
            {
                "schema_version": "btc-twap-capture-attempt-receipt.v1",
                "attempt_id": f"attempt-{index}",
                "track_id": "v08",
                "created_at": now.isoformat().replace("+00:00", "Z"),
                "started_at": now.isoformat().replace("+00:00", "Z"),
                "updated_at": now.isoformat().replace("+00:00", "Z"),
                "scheduled_expiry_seconds": index,
                "expiry_seconds": index,
                "status": "failed" if index == 0 else "succeeded",
                "capture_root": str(capture_root),
            },
        )
    track = TrackConfig(
        name="v08",
        kind="edge_readiness",
        status_path=tmp_path / "status.json",
        data_root=data_root,
        capture_summary_glob=str(
            data_root / "runs" / "*" / "*" / "capture-summary.json"
        ),
        attempt_receipt_glob=str(
            data_root / "service" / "attempt-receipts" / "*.json"
        ),
    )
    monkeypatch.setattr(
        "scripts.watch_paper_tracks.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=11 * 1024**3),
    )

    passing = _capture_capacity_snapshot(
        track,
        now=now,
        host_status={
            "mem_available_bytes": 3 * 1024**3,
            "cpu_credit_balance": 50.0,
        },
    )
    missing_cpu = _capture_capacity_snapshot(
        track,
        now=now,
        host_status={"mem_available_bytes": 3 * 1024**3},
    )

    assert passing is not None and passing["passed"] is True
    assert passing["capture_attempt_count"] == 21
    assert passing["capture_failure_count"] == 1
    assert passing["failure_rate"] == str(Decimal(1) / Decimal(21))
    assert passing["schema_version"] == "btc-twap-capture-capacity-evidence.v1"
    assert passing["selection_rule"] == {
        "created_at_not_before": "2026-08-17T12:00:00Z",
        "maximum_receipts": 96,
    }
    assert missing_cpu is not None and missing_cpu["passed"] is False
    assert "cpu_credit_telemetry_unavailable" in missing_cpu["reason_codes"]


def test_v08_capacity_snapshot_uses_recent_window_and_pending_started_are_not_successes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    data_root = tmp_path / "v08" / "data"
    recent_root = data_root / "runs" / "recent" / "attempt-recent"
    old_root = data_root / "runs" / "old" / "attempt-old"
    for capture_root in (recent_root, old_root):
        _write_json(
            capture_root / "capture-summary.json",
            {
                "schema_version": "btc-twap-compact-forward-capture-summary.v1",
                "data_root": str(capture_root.resolve()),
                "duration_seconds": "300",
                "capture_error": None,
                "recorder_leg_failures": [],
                "integrity": {
                    "orphan_partials": [],
                    "raw_without_manifest": [],
                    "manifest_without_raw": [],
                    "checksum_mismatches": [],
                    "invalid_manifests": [],
                },
            },
        )
    _write_json(
        data_root / "service" / "attempt-receipts" / "attempt-old.json",
        {
            "schema_version": "btc-twap-capture-attempt-receipt.v1",
            "attempt_id": "attempt-old",
            "track_id": "v08",
            "created_at": "2026-08-16T11:00:00Z",
            "started_at": "2026-08-16T11:00:00Z",
            "updated_at": "2026-08-16T11:10:00Z",
            "scheduled_expiry_seconds": 1,
            "expiry_seconds": 1,
            "status": "succeeded",
            "capture_root": str(old_root),
        },
    )
    _write_json(
        data_root / "service" / "attempt-receipts" / "attempt-recent.json",
        {
            "schema_version": "btc-twap-capture-attempt-receipt.v1",
            "attempt_id": "attempt-recent",
            "track_id": "v08",
            "created_at": "2026-08-18T11:00:00Z",
            "started_at": "2026-08-18T11:00:00Z",
            "updated_at": "2026-08-18T11:45:00Z",
            "scheduled_expiry_seconds": 2,
            "expiry_seconds": 2,
            "status": "started",
            "capture_root": str(recent_root),
        },
    )
    track = TrackConfig(
        name="v08",
        kind="edge_readiness",
        status_path=tmp_path / "status.json",
        data_root=data_root,
        capture_summary_glob=str(
            data_root / "runs" / "*" / "*" / "capture-summary.json"
        ),
        attempt_receipt_glob=str(
            data_root / "service" / "attempt-receipts" / "*.json"
        ),
    )
    monkeypatch.setattr(
        "scripts.watch_paper_tracks.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=11 * 1024**3),
    )

    result = _capture_capacity_snapshot(
        track,
        now=now,
        host_status={
            "mem_available_bytes": 3 * 1024**3,
            "cpu_credit_balance": 50.0,
        },
    )

    assert result is not None
    assert result["passed"] is False
    assert result["capture_attempt_count"] == 0
    assert result["pending_attempt_count"] == 1
    assert result["receipt_count"] == 2
    assert result["selection_rule"] == {
        "created_at_not_before": "2026-08-17T12:00:00Z",
        "maximum_receipts": 96,
    }
    assert "capture_capacity_terminal_evidence_unavailable" in result["reason_codes"]


def test_watch_cycle_writes_capacity_artifact_without_overwriting_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    status_path = tmp_path / "v08" / "service" / "status.json"
    health_path = tmp_path / "v08" / "monitor" / "edge-readiness-health-latest.json"
    capacity_path = tmp_path / "watch" / "state" / "v08-capture-capacity.json"
    host_status_path = tmp_path / "watch" / "state" / "host-status.json"
    data_root = tmp_path / "v08" / "data"
    capture_root = data_root / "runs" / "1786737600" / "attempt-1"
    _write_json(
        capture_root / "capture-summary.json",
        {
            "schema_version": "btc-twap-compact-forward-capture-summary.v1",
            "data_root": str(capture_root.resolve()),
            "duration_seconds": "300",
            "capture_error": None,
            "recorder_leg_failures": [],
            "integrity": {
                "orphan_partials": [],
                "raw_without_manifest": [],
                "manifest_without_raw": [],
                "checksum_mismatches": [],
                "invalid_manifests": [],
            },
        },
    )
    _write_json(
        data_root / "service" / "attempt-receipts" / "attempt-1.json",
        {
            "schema_version": "btc-twap-capture-attempt-receipt.v1",
            "attempt_id": "attempt-1",
            "track_id": "v08",
            "created_at": "2026-08-18T11:00:00Z",
            "started_at": "2026-08-18T11:00:00Z",
            "updated_at": "2026-08-18T11:10:00Z",
            "terminal_at": "2026-08-18T11:10:00Z",
            "scheduled_expiry_seconds": 1786737600,
            "expiry_seconds": 1786737600,
            "status": "succeeded",
            "capture_root": str(capture_root),
        },
    )
    _write_json(
        status_path,
        {
            "phase": "capturing",
            "heartbeat_at": "2026-08-18T12:00:00Z",
            "completed_report_count": 1,
        },
    )
    _write_json(
        health_path,
        {"schema_version": "btc-twap-service-health-evidence.v1", "sentinel": True},
    )
    _write_json(
        host_status_path,
        {
            "schema_version": "polymm-local-host-status.v1",
            "mem_available_bytes": 3 * 1024**3,
            "cpu_credit_balance": 50.0,
        },
    )
    config_path = _write_json(
        tmp_path / "watch-config.json",
        {
            "schema_version": "polymm-paper-track-watch-config.v1",
            "state_path": str(tmp_path / "watch" / "state" / "watch-state.json"),
            "host": {"status_path": str(host_status_path)},
            "tracks": [
                {
                    "name": "v08",
                    "kind": "edge_readiness",
                    "status_path": str(status_path),
                    "health_path": str(health_path),
                    "capacity_evidence_path": str(capacity_path),
                    "data_root": str(data_root),
                    "capture_summary_glob": str(
                        data_root / "runs" / "*" / "*" / "capture-summary.json"
                    ),
                    "attempt_receipt_glob": str(
                        data_root / "service" / "attempt-receipts" / "*.json"
                    ),
                }
            ],
        },
    )
    monkeypatch.setattr(
        "scripts.watch_paper_tracks.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=11 * 1024**3),
    )

    run_watch_cycle(
        _load_watch_config(config_path),
        publisher=CollectingPublisher(),
        now=now,
    )

    health = json.loads(health_path.read_text(encoding="utf-8"))
    artifact = readiness.load_verified_json_artifact(capacity_path)
    valid, reasons = readiness._validated_capture_capacity_artifact(artifact)

    assert health == {
        "schema_version": "btc-twap-service-health-evidence.v1",
        "sentinel": True,
    }
    assert valid is False
    assert "capture_capacity_artifact_invalid" in reasons


def _watch_config(
    tmp_path: Path,
    *,
    include_regime: bool = False,
    include_host: bool = False,
) -> Path:
    status_path = tmp_path / "v05" / "service" / "status.json"
    health_path = tmp_path / "v05" / "monitor" / "health.json"
    data_root = tmp_path / "v05" / "data"
    regime_path = tmp_path / "v05" / "monitor" / "regime.json"
    host_path = tmp_path / "watch" / "host-status.json"
    config = {
        "schema_version": "polymm-paper-track-watch-config.v1",
        "state_path": str(tmp_path / "watch" / "state.json"),
        "digest_interval_seconds": 3600,
        "tracks": [
            {
                "name": "v05",
                "status_path": str(status_path),
                "health_path": str(health_path),
                "data_root": str(data_root),
                "capture_summary_glob": str(
                    data_root / "runs" / "*" / "capture-summary.json"
                ),
                "expected_regime": "chainlink_twap_30s_5m_and_60s_15m.v1",
                **(
                    {"regime_state_path": str(regime_path)}
                    if include_regime
                    else {}
                ),
            }
        ],
    }
    if include_host:
        config["host"] = {
            "status_path": str(host_path),
            "mem_available_threshold_bytes": 300,
            "cpu_credit_threshold": 100.0,
            "cpu_credit_decrease_seconds": 1800,
            "cpu_credit_region": "us-east-1",
            "cpu_credit_instance_id": "i-1234567890abcdef0",
            "process_rss_budgets_bytes": {"v05": 600},
        }
    return _write_json(tmp_path / "watch-config.json", config)


def test_watch_cycle_pages_digest_and_recovers_on_progress_stall(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    config_path = _watch_config(tmp_path)
    status_path = tmp_path / "v05" / "service" / "status.json"
    health_path = tmp_path / "v05" / "monitor" / "health.json"
    summary_path = tmp_path / "v05" / "research" / "VALIDATION_SUMMARY.json"
    run_root = tmp_path / "v05" / "data" / "runs" / "1786665600"
    run_root.mkdir(parents=True, exist_ok=True)
    capture_summary = _write_json(
        run_root / "capture-summary.json",
        {"generated_at": "2026-08-14T15:15:00Z"},
    )
    _touch(summary_path, now - timedelta(minutes=45))
    _touch(run_root, now - timedelta(minutes=45))
    _touch(capture_summary, now - timedelta(minutes=45))
    _write_json(
        status_path,
        {
            "phase": "error_wait",
            "heartbeat_at": (now - timedelta(seconds=5)).isoformat().replace(
                "+00:00", "Z"
            ),
            "completed_report_count": 156,
            "latest_summary_path": str(summary_path),
            "last_error": {"error_code": "service_cycle_failed"},
        },
    )
    _write_json(
        health_path,
        {
            "free_disk_bytes": 2_000_000,
            "minimum_free_disk_bytes": 1_000_000,
        },
    )
    config = _load_watch_config(config_path)
    publisher = CollectingPublisher()

    run_watch_cycle(config, publisher=publisher, now=now)

    keys = {alert.key for alert in publisher.alerts}
    assert keys == {
        "v05:phase_unhealthy",
        "v05:report_stalled",
        "v05:capture_stalled",
    }
    publisher.alerts.clear()
    _write_json(
        status_path,
        {
            "phase": "error_wait",
            "heartbeat_at": (now + timedelta(minutes=10)).isoformat().replace(
                "+00:00", "Z"
            ),
            "completed_report_count": 156,
            "latest_summary_path": str(summary_path),
            "last_error": {"error_code": "service_cycle_failed"},
        },
    )

    run_watch_cycle(
        config,
        publisher=publisher,
        now=now + timedelta(minutes=10),
    )

    assert publisher.alerts == []
    _write_json(
        status_path,
        {
            "phase": "error_wait",
            "heartbeat_at": (now + timedelta(minutes=61)).isoformat().replace(
                "+00:00", "Z"
            ),
            "completed_report_count": 156,
            "latest_summary_path": str(summary_path),
            "last_error": {"error_code": "service_cycle_failed"},
        },
    )

    run_watch_cycle(
        config,
        publisher=publisher,
        now=now + timedelta(minutes=61),
    )

    assert {alert.severity for alert in publisher.alerts} == {"digest"}
    publisher.alerts.clear()

    _touch(summary_path, now + timedelta(minutes=62))
    _touch(run_root, now + timedelta(minutes=62))
    _write_json(
        capture_summary,
        {"generated_at": "2026-08-14T17:02:00Z"},
    )
    _write_json(
        status_path,
        {
            "phase": "capturing",
            "heartbeat_at": (now + timedelta(minutes=62)).isoformat().replace(
                "+00:00", "Z"
            ),
            "completed_report_count": 160,
            "latest_summary_path": str(summary_path),
            "last_error": None,
        },
    )

    run_watch_cycle(
        config,
        publisher=publisher,
        now=now + timedelta(minutes=62),
    )

    assert {alert.severity for alert in publisher.alerts} == {"notice"}
    assert len(publisher.alerts) == 3


def test_watch_cycle_warns_on_host_pressure_and_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    config_path = _watch_config(tmp_path, include_host=True)
    status_path = tmp_path / "v05" / "service" / "status.json"
    health_path = tmp_path / "v05" / "monitor" / "health.json"
    summary_path = tmp_path / "v05" / "research" / "VALIDATION_SUMMARY.json"
    run_root = tmp_path / "v05" / "data" / "runs" / "1786665600"
    run_root.mkdir(parents=True, exist_ok=True)
    _write_json(
        run_root / "capture-summary.json",
        {"generated_at": now.isoformat().replace("+00:00", "Z")},
    )
    _touch(summary_path, now)
    _touch(run_root, now)
    _write_json(
        status_path,
        {
            "phase": "capturing",
            "heartbeat_at": (now - timedelta(minutes=5)).isoformat().replace(
                "+00:00", "Z"
            ),
            "completed_report_count": 156,
            "latest_summary_path": str(summary_path),
        },
    )
    _write_json(
        health_path,
        {
            "free_disk_bytes": 2_000_000,
            "minimum_free_disk_bytes": 1_000_000,
        },
    )
    _write_json(
        tmp_path / "watch" / "host-status.json",
        {
            "mem_available_bytes": 200,
            "cpu_credit_balance": 90.0,
            "process_rss_bytes": {"v05": 800},
        },
    )
    config = _load_watch_config(config_path)
    publisher = CollectingPublisher()
    monkeypatch.setattr(
        "scripts.watch_paper_tracks._refresh_local_host_status",
        lambda *args, **kwargs: None,
    )

    run_watch_cycle(config, publisher=publisher, now=now)

    keys = {alert.key for alert in publisher.alerts}
    assert "v05:heartbeat_stale" in keys
    assert "host:memory_pressure" in keys
    assert "host:cpu_credit_low" in keys
    assert "host:rss:v05" in keys


def test_unreadable_status_reports_telemetry_gap_not_stale_heartbeat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    config_path = _watch_config(tmp_path)
    status_path = tmp_path / "v05" / "service" / "status.json"
    health_path = tmp_path / "v05" / "monitor" / "health.json"
    _write_json(
        status_path,
        {
            "phase": "capturing",
            "heartbeat_at": now.isoformat().replace("+00:00", "Z"),
            "completed_report_count": 156,
        },
    )
    _write_json(health_path, {"failures": []})
    config = _load_watch_config(config_path)
    original_read_text = Path.read_text

    def deny_status_read(path: Path, *args, **kwargs):
        if path == status_path:
            raise PermissionError(status_path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_status_read)
    publisher = CollectingPublisher()

    run_watch_cycle(config, publisher=publisher, now=now)

    alerts = {alert.key: alert for alert in publisher.alerts}
    assert "v05:telemetry_unavailable" in alerts
    assert alerts["v05:telemetry_unavailable"].severity == "warn"
    assert alerts["v05:telemetry_unavailable"].body["unavailable_sources"] == [
        {
            "exists": True,
            "kind": "status",
            "path": str(status_path),
        }
    ]
    assert "v05:heartbeat_stale" not in alerts


def test_track_specific_heartbeat_lease_avoids_false_pages_for_slow_shadow(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    status_path = _write_json(
        tmp_path / "v07" / "service" / "status.json",
        {
            "phase": "ok",
            "heartbeat_at": (now - timedelta(minutes=20))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )
    config_path = _write_json(
        tmp_path / "watch-config.json",
        {
            "schema_version": "polymm-paper-track-watch-config.v1",
            "state_path": str(tmp_path / "watch" / "state.json"),
            "tracks": [
                {
                    "name": "v07",
                    "kind": "paper",
                    "status_path": str(status_path),
                    "expected_cycle_seconds": 1800,
                    "maximum_heartbeat_age_seconds": 2700,
                }
            ],
        },
    )
    publisher = CollectingPublisher()

    run_watch_cycle(
        _load_watch_config(config_path),
        publisher=publisher,
        now=now,
    )

    assert "v07:heartbeat_stale" not in {alert.key for alert in publisher.alerts}


def test_retired_track_suppresses_runtime_and_telemetry_alerts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    config_path = _watch_config(tmp_path)
    status_path = tmp_path / "v05" / "service" / "status.json"
    _write_json(status_path, {"phase": "error_wait"})
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["tracks"][0]["lifecycle"] = "retired"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    original_read_text = Path.read_text
    original_exists = Path.exists

    def deny_status_read(path: Path, *args, **kwargs):
        if path == status_path:
            raise PermissionError(status_path)
        return original_read_text(path, *args, **kwargs)

    def deny_status_exists(path: Path) -> bool:
        if path == status_path:
            raise PermissionError(status_path)
        return original_exists(path)

    monkeypatch.setattr(Path, "read_text", deny_status_read)
    monkeypatch.setattr(Path, "exists", deny_status_exists)
    publisher = CollectingPublisher()

    run_watch_cycle(
        _load_watch_config(config_path),
        publisher=publisher,
        now=now,
    )

    assert publisher.alerts == []
    state = json.loads(
        (tmp_path / "watch" / "state.json").read_text(encoding="utf-8")
    )
    assert state["tracks"]["v05"] == {"lifecycle": "retired"}


def test_watch_config_rejects_unknown_track_lifecycle(tmp_path: Path) -> None:
    config_path = _watch_config(tmp_path)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["tracks"][0]["lifecycle"] = "retierd"
    config_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported track lifecycle"):
        _load_watch_config(config_path)


def test_watch_cycle_warns_on_regime_mismatch_and_quarantine_growth(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    config_path = _watch_config(tmp_path, include_regime=True)
    status_path = tmp_path / "v05" / "service" / "status.json"
    health_path = tmp_path / "v05" / "monitor" / "health.json"
    summary_path = tmp_path / "v05" / "research" / "VALIDATION_SUMMARY.json"
    run_root = tmp_path / "v05" / "data" / "runs" / "1786665600"
    regime_path = tmp_path / "v05" / "monitor" / "regime.json"
    run_root.mkdir(parents=True, exist_ok=True)
    _write_json(
        run_root / "capture-summary.json",
        {"generated_at": now.isoformat().replace("+00:00", "Z")},
    )
    _touch(summary_path, now)
    _touch(run_root, now)
    _write_json(
        status_path,
        {
            "phase": "capturing",
            "heartbeat_at": now.isoformat().replace("+00:00", "Z"),
            "completed_report_count": 156,
            "latest_summary_path": str(summary_path),
        },
    )
    _write_json(
        health_path,
        {
            "free_disk_bytes": 2_000_000,
            "minimum_free_disk_bytes": 1_000_000,
        },
    )
    _write_json(
        regime_path,
        {
            "observed_regime": "chainlink_twap_60s_5m_and_60s_15m.v1",
            "qualification_status": "quarantine",
            "first_seen_at": now.isoformat().replace("+00:00", "Z"),
            "consecutive_rejections": 3,
            "affected_market_slugs": ["btc-updown-5m-1786666500"],
            "quarantine_count": 2,
        },
    )
    config = _load_watch_config(config_path)
    publisher = CollectingPublisher()

    run_watch_cycle(config, publisher=publisher, now=now)

    keys = {alert.key for alert in publisher.alerts}
    assert "v05:regime_mismatch" in keys
    assert "v05:quarantine_growth" in keys
    mismatch = next(
        alert for alert in publisher.alerts if alert.key == "v05:regime_mismatch"
    )
    assert mismatch.body["expected_regime"] == (
        "chainlink_twap_30s_5m_and_60s_15m.v1"
    )
    assert mismatch.body["observed_regime"] == (
        "chainlink_twap_60s_5m_and_60s_15m.v1"
    )
    assert mismatch.body["consecutive_rejections"] == 3


def test_rawcap_track_does_not_require_report_progress(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    status_path = _write_json(
        tmp_path / "rawcap" / "status.json",
        {
            "phase": "capturing",
            "heartbeat_at": now.isoformat().replace("+00:00", "Z"),
            "completed_capture_count": 4,
        },
    )
    run_root = tmp_path / "rawcap" / "data" / "runs" / "1786665600"
    run_root.mkdir(parents=True)
    _touch(run_root, now)
    config_path = _write_json(
        tmp_path / "watch-config.json",
        {
            "schema_version": "polymm-paper-track-watch-config.v1",
            "state_path": str(tmp_path / "watch" / "state.json"),
            "tracks": [
                {
                    "name": "rawcap",
                    "kind": "rawcap",
                    "status_path": str(status_path),
                    "data_root": str(tmp_path / "rawcap" / "data"),
                    "expected_cycle_seconds": 900,
                }
            ],
        },
    )
    publisher = CollectingPublisher()

    run_watch_cycle(
        _load_watch_config(config_path),
        publisher=publisher,
        now=now,
    )

    assert "rawcap:report_stalled" not in {
        alert.key for alert in publisher.alerts
    }


def test_maintenance_track_suppresses_runtime_alerts(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    status_path = _write_json(
        tmp_path / "rawcap" / "status.json",
        {
            "phase": "stopped",
            "heartbeat_at": (now - timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )
    config_path = _write_json(
        tmp_path / "watch-config.json",
        {
            "schema_version": "polymm-paper-track-watch-config.v1",
            "state_path": str(tmp_path / "watch" / "state.json"),
            "tracks": [
                {
                    "name": "rawcap",
                    "kind": "rawcap",
                    "lifecycle": "maintenance",
                    "status_path": str(status_path),
                    "data_root": str(tmp_path / "rawcap" / "data"),
                    "expected_cycle_seconds": 900,
                }
            ],
        },
    )
    publisher = CollectingPublisher()

    run_watch_cycle(
        _load_watch_config(config_path),
        publisher=publisher,
        now=now,
    )

    assert publisher.alerts == []


def test_watch_cycle_persists_active_alert_snapshot(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    config_path = _watch_config(tmp_path)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    alerts_path = tmp_path / "watch" / "alerts-latest.json"
    document["alerts_path"] = str(alerts_path)
    config_path.write_text(json.dumps(document), encoding="utf-8")
    status_path = tmp_path / "v05" / "service" / "status.json"
    _write_json(
        status_path,
        {
            "phase": "error_wait",
            "heartbeat_at": (now - timedelta(minutes=5))
            .isoformat()
            .replace("+00:00", "Z"),
            "completed_report_count": 156,
        },
    )

    run_watch_cycle(
        _load_watch_config(config_path),
        publisher=CollectingPublisher(),
        now=now,
    )

    snapshot = json.loads(alerts_path.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == "polymm-paper-track-alerts.v1"
    assert any(
        alert["key"] == "v05:heartbeat_stale"
        for alert in snapshot["active_alerts"]
    )


def test_local_host_snapshot_reads_memavailable_and_track_rss(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    (proc_root / "42").mkdir(parents=True)
    (proc_root / "43").mkdir(parents=True)
    (proc_root / "meminfo").write_text(
        "MemTotal:       2000000 kB\nMemAvailable:    850000 kB\n",
        encoding="utf-8",
    )
    (proc_root / "42" / "status").write_text(
        "Name:\tpython\nVmRSS:\t300000 kB\n",
        encoding="utf-8",
    )
    (proc_root / "43" / "status").write_text(
        "Name:\tpython\nVmRSS:\t12000 kB\n",
        encoding="utf-8",
    )
    status_path = _write_json(
        tmp_path / "v05" / "status.json",
        {"pid": 42, "child_pid": 43},
    )
    config_path = _write_json(
        tmp_path / "watch-config.json",
        {
            "schema_version": "polymm-paper-track-watch-config.v1",
            "state_path": str(tmp_path / "watch" / "state.json"),
            "tracks": [
                {
                    "name": "v05",
                    "status_path": str(status_path),
                }
            ],
        },
    )
    config = _load_watch_config(config_path)

    snapshot = collect_local_host_status(
        config.tracks,
        now=datetime(2026, 8, 14, 16, 0, tzinfo=UTC),
        proc_root=proc_root,
    )

    assert snapshot["mem_available_bytes"] == 850000 * 1024
    assert snapshot["process_rss_bytes"] == {
        "v05": (300000 + 12000) * 1024
    }


def test_capture_progress_uses_summary_timestamp_not_run_directory_mtime(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    config_path = _watch_config(tmp_path)
    status_path = tmp_path / "v05" / "service" / "status.json"
    report_summary = tmp_path / "v05" / "research" / "VALIDATION_SUMMARY.json"
    _write_json(report_summary, {"generated_at": "2026-08-14T16:00:00Z"})
    _write_json(
        status_path,
        {
            "phase": "capturing",
            "heartbeat_at": "2026-08-14T16:00:00Z",
            "completed_report_count": 156,
            "latest_summary_path": str(report_summary),
        },
    )
    run_root = tmp_path / "v05" / "data" / "runs" / "1786665600"
    capture_summary = _write_json(
        run_root / "capture-summary.json",
        {"generated_at": "2026-08-14T15:20:00Z"},
    )
    _touch(run_root, now)
    _touch(capture_summary, now)
    _write_json(
        tmp_path / "watch" / "state.json",
        {
            "schema_version": "polymm-paper-track-watch-state.v1",
            "alerts": {},
            "tracks": {
                "v05": {
                    "last_completed_report_count": 156,
                    "last_report_progress_at": "2026-08-14T16:00:00Z",
                    "latest_capture_timestamp": "2026-08-14T15:20:00Z",
                    "last_capture_progress_at": "2026-08-14T15:20:00Z",
                }
            },
        },
    )
    publisher = CollectingPublisher()

    run_watch_cycle(
        _load_watch_config(config_path),
        publisher=publisher,
        now=now,
    )

    assert "v05:capture_stalled" in {alert.key for alert in publisher.alerts}


def test_watch_pages_on_sustained_cpu_credit_decline_above_absolute_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    config_path = _watch_config(tmp_path, include_host=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["host"]["cpu_credit_threshold"] = 100.0
    config["host"]["cpu_credit_decrease_seconds"] = 1800
    config_path.write_text(json.dumps(config), encoding="utf-8")
    _write_json(
        tmp_path / "watch" / "host-status.json",
        {
            "cpu_credit_balance": 110.0,
            "cpu_credit_observed_at": "2026-08-14T16:00:00Z",
        },
    )
    _write_json(
        tmp_path / "watch" / "state.json",
        {
            "schema_version": "polymm-paper-track-watch-state.v1",
            "alerts": {},
            "tracks": {},
            "host": {
                "cpu_credit_balance": 120.0,
                "cpu_credit_observed_at": "2026-08-14T15:30:00Z",
                "cpu_credit_decreasing_since": "2026-08-14T15:20:00Z",
            },
        },
    )
    publisher = CollectingPublisher()
    monkeypatch.setattr(
        "scripts.watch_paper_tracks._refresh_local_host_status",
        lambda *args, **kwargs: None,
    )

    run_watch_cycle(
        _load_watch_config(config_path),
        publisher=publisher,
        now=now,
    )

    assert "host:cpu_credit_declining" in {
        alert.key for alert in publisher.alerts
    }


def test_local_host_snapshot_uses_fresh_cloudwatch_cpu_credit(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    (proc_root / "meminfo").parent.mkdir(parents=True, exist_ok=True)
    (proc_root / "meminfo").write_text(
        "MemAvailable:    850000 kB\n",
        encoding="utf-8",
    )
    config = _load_watch_config(_watch_config(tmp_path, include_host=True))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "Datapoints": [
                        {
                            "Average": 87.5,
                            "Timestamp": "2026-08-14T15:55:00+00:00",
                        }
                    ]
                }
            ),
            stderr="",
        )

    snapshot = collect_local_host_status(
        config.tracks,
        now=datetime(2026, 8, 14, 16, 0, tzinfo=UTC),
        proc_root=proc_root,
        previous={
            "cpu_credit_balance": 244.11,
            "cpu_credit_observed_at": "2026-08-14T03:00:00Z",
        },
        host=config.host,
        subprocess_run=fake_run,
        aws_cli_path="aws",
    )

    assert snapshot["cpu_credit_balance"] == 87.5
    assert snapshot["cpu_credit_observed_at"] == "2026-08-14T15:55:00Z"


def test_local_host_snapshot_rejects_stale_cloudwatch_cpu_credit(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    (proc_root / "meminfo").parent.mkdir(parents=True, exist_ok=True)
    (proc_root / "meminfo").write_text(
        "MemAvailable:    850000 kB\n",
        encoding="utf-8",
    )
    config = _load_watch_config(_watch_config(tmp_path, include_host=True))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "Datapoints": [
                        {
                            "Average": 244.11,
                            "Timestamp": "2026-08-14T15:30:00+00:00",
                        }
                    ]
                }
            ),
            stderr="",
        )

    snapshot = collect_local_host_status(
        config.tracks,
        now=datetime(2026, 8, 14, 16, 0, tzinfo=UTC),
        proc_root=proc_root,
        host=config.host,
        subprocess_run=fake_run,
        aws_cli_path="aws",
    )

    assert "cpu_credit_balance" not in snapshot
    assert snapshot["cpu_credit_telemetry_unavailable"]["reason"] == (
        "cloudwatch_metric_stale"
    )


def test_local_host_snapshot_reports_cloudwatch_process_failure(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    (proc_root / "meminfo").parent.mkdir(parents=True, exist_ok=True)
    (proc_root / "meminfo").write_text(
        "MemAvailable:    850000 kB\n",
        encoding="utf-8",
    )
    config = _load_watch_config(_watch_config(tmp_path, include_host=True))

    def fake_run(*args, **kwargs):
        raise OSError("aws executable unavailable")

    snapshot = collect_local_host_status(
        config.tracks,
        now=datetime(2026, 8, 14, 16, 0, tzinfo=UTC),
        proc_root=proc_root,
        host=config.host,
        subprocess_run=fake_run,
        aws_cli_path="aws",
    )

    assert "cpu_credit_balance" not in snapshot
    assert snapshot["cpu_credit_telemetry_unavailable"]["reason"] == (
        "cloudwatch_process_failed"
    )


def test_watch_replaces_stale_cpu_credit_decline_with_telemetry_unavailable(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=UTC)
    config_path = _watch_config(tmp_path, include_host=True)
    _write_json(
        tmp_path / "watch" / "host-status.json",
        {
            "cpu_credit_refresh_attempted": True,
            "cpu_credit_telemetry_unavailable": {
                "reason": "cloudwatch_timeout",
                "region": "us-east-1",
                "instance_id": "i-1234567890abcdef0",
            },
        },
    )
    _write_json(
        tmp_path / "watch" / "state.json",
        {
            "schema_version": "polymm-paper-track-watch-state.v1",
            "alerts": {},
            "tracks": {},
            "host": {
                "cpu_credit_balance": 120.0,
                "cpu_credit_observed_at": "2026-08-14T15:30:00Z",
                "cpu_credit_decreasing_since": "2026-08-14T15:20:00Z",
            },
        },
    )
    publisher = CollectingPublisher()

    run_watch_cycle(
        _load_watch_config(config_path),
        publisher=publisher,
        now=now,
    )

    keys = {alert.key for alert in publisher.alerts}
    assert "host:cpu_credit_telemetry_unavailable" in keys
    assert "host:cpu_credit_declining" not in keys
    state = json.loads(
        (tmp_path / "watch" / "state.json").read_text(encoding="utf-8")
    )
    assert "cpu_credit_balance" not in state["host"]
