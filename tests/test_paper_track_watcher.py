from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.watch_paper_tracks import (
    AlertRecord,
    Publisher,
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

    run_watch_cycle(
        _load_watch_config(config_path),
        publisher=publisher,
        now=now,
    )

    assert "host:cpu_credit_declining" in {
        alert.key for alert in publisher.alerts
    }
