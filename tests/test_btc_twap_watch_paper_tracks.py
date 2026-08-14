from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.watch_paper_tracks import TrackConfig, WatchConfig, evaluate_watch


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _status_payload(
    *,
    heartbeat_at: str,
    phase: str = "settlement_wait",
    completed_report_count: int = 16,
    latest_summary_path: str | None = None,
    latest_classification: str | None = "insufficient_data",
) -> dict[str, object]:
    return {
        "schema_version": "btc-twap-relative-value-service-status.v1",
        "phase": phase,
        "heartbeat_at": heartbeat_at,
        "pid": 123,
        "child_pid": None,
        "current_expiry_seconds": 1_786_532_400,
        "current_capture_root": "/tmp/capture",
        "completed_report_count": completed_report_count,
        "latest_summary_path": latest_summary_path,
        "latest_classification": latest_classification,
        "last_error": None,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "authenticated_endpoints_used": 0,
        "orders_submitted": 0,
        "status_sha256": "0" * 64,
    }


def _health_payload(
    *,
    checked_at: str,
    healthy: bool = True,
) -> dict[str, object]:
    return {
        "checked_at": checked_at,
        "healthy": healthy,
        "failures": [] if healthy else ["service_phase_unhealthy"],
        "heartbeat_at": checked_at,
        "heartbeat_age_seconds": 0,
        "completed_report_count": 16,
        "free_disk_bytes": 20 * 1024**3,
    }


def _watch_config(tmp_path: Path) -> tuple[WatchConfig, Path, Path, Path]:
    status_path = tmp_path / "track" / "data" / "service" / "status.json"
    health_path = tmp_path / "track" / "monitor" / "health-latest.json"
    capture_summary_path = (
        tmp_path / "track" / "data" / "runs" / "1786532400" / "capture-summary.json"
    )
    config = WatchConfig(
        state_path=tmp_path / "watch-state.json",
        tracks=(
            TrackConfig(
                name="v05",
                kind="paper",
                status_path=status_path,
                health_path=health_path,
                capture_summary_glob=str(
                    tmp_path / "track" / "data" / "runs" / "*" / "capture-summary.json"
                ),
                expected_cycle_seconds=900,
            ),
        ),
        sns_topic_arn=None,
    )
    return config, status_path, health_path, capture_summary_path


def test_watch_pages_when_unhealthy_and_reports_stall(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)
    config, status_path, health_path, capture_summary_path = _watch_config(tmp_path)
    _write_json(
        status_path,
        _status_payload(
            heartbeat_at=(now - timedelta(seconds=20)).isoformat().replace(
                "+00:00", "Z"
            ),
            phase="error_wait",
            completed_report_count=156,
            latest_summary_path=str(tmp_path / "track" / "research" / "VALIDATION_SUMMARY.json"),
        ),
    )
    _write_json(
        health_path,
        _health_payload(
            checked_at=(now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            healthy=False,
        ),
    )
    _write_json(
        capture_summary_path,
        {"generated_at": "2026-08-14T15:20:00Z"},
    )
    previous = {
        "tracks": {
            "v05": {
                "last_completed_report_count": 156,
                "last_report_progress_at": "2026-08-14T15:20:00Z",
                "last_capture_progress_at": "2026-08-14T15:20:00Z",
                "last_phase_healthy_at": "2026-08-14T15:20:00Z",
                "last_quarantine_count": 0,
            }
        }
    }

    result = evaluate_watch(config, now=now, previous_state=previous)

    keys = {(alert.track, alert.kind, alert.severity) for alert in result.alerts}
    assert ("v05", "phase_unhealthy", "page") in keys
    assert ("v05", "report_stalled", "page") in keys
    assert ("v05", "capture_stalled", "page") in keys


def test_watch_emits_recovery_notice_after_progress_returns(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)
    config, status_path, health_path, capture_summary_path = _watch_config(tmp_path)
    _write_json(
        status_path,
        _status_payload(
            heartbeat_at=now.isoformat().replace("+00:00", "Z"),
            phase="cycle_complete",
            completed_report_count=157,
            latest_summary_path=str(tmp_path / "track" / "research" / "VALIDATION_SUMMARY.json"),
        ),
    )
    _write_json(
        health_path,
        _health_payload(
            checked_at=now.isoformat().replace("+00:00", "Z"),
            healthy=True,
        ),
    )
    _write_json(
        capture_summary_path,
        {"generated_at": "2026-08-14T15:59:30Z"},
    )
    previous = {
        "tracks": {
            "v05": {
                "last_completed_report_count": 156,
                "last_report_progress_at": "2026-08-14T15:20:00Z",
                "last_capture_progress_at": "2026-08-14T15:20:00Z",
                "last_phase_healthy_at": None,
                "last_quarantine_count": 0,
                "active_alerts": {
                    "phase_unhealthy": "page",
                    "report_stalled": "page",
                    "capture_stalled": "page",
                },
            }
        }
    }

    result = evaluate_watch(config, now=now, previous_state=previous)

    notices = [
        alert for alert in result.alerts if alert.kind == "recovery" and alert.severity == "notice"
    ]
    assert notices
    track_state = result.state["tracks"]["v05"]
    assert track_state["last_completed_report_count"] == 157
    assert track_state["active_alerts"] == {}


def test_watch_warns_on_quarantine_growth(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 16, 0, tzinfo=timezone.utc)
    config, status_path, health_path, capture_summary_path = _watch_config(tmp_path)
    _write_json(
        status_path,
        _status_payload(
            heartbeat_at=now.isoformat().replace("+00:00", "Z"),
            phase="capturing",
            latest_classification="quarantine",
        ),
    )
    _write_json(
        health_path,
        _health_payload(
            checked_at=now.isoformat().replace("+00:00", "Z"),
            healthy=True,
        ),
    )
    _write_json(capture_summary_path, {"generated_at": "2026-08-14T15:59:30Z"})
    previous = {
        "tracks": {
            "v05": {
                "last_completed_report_count": 16,
                "last_report_progress_at": "2026-08-14T15:59:00Z",
                "last_capture_progress_at": "2026-08-14T15:59:00Z",
                "last_phase_healthy_at": "2026-08-14T15:59:00Z",
                "last_quarantine_count": 0,
            }
        }
    }

    result = evaluate_watch(config, now=now, previous_state=previous)

    warnings = [
        alert
        for alert in result.alerts
        if alert.kind == "quarantine_growth" and alert.severity == "warn"
    ]
    assert len(warnings) == 1
