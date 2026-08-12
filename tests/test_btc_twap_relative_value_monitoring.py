from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.check_btc_twap_relative_value_service as checker
from src.edge_lab.data_store import canonical_json_bytes


def _write_service_config(tmp_path: Path) -> Path:
    preregistration = tmp_path / "PREREGISTRATION.json"
    preregistration.write_text(
        json.dumps(
            {
                "frozen_strategy": {
                    "clock_sync": {
                        "maximum_measurement_uncertainty_ms": 100,
                        "maximum_measurement_age_seconds": 1200,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "SERVICE_CONFIG.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": ("btc-twap-relative-value-continuous-service.v1"),
                "data_root": str(tmp_path / "data"),
                "research_root": str(tmp_path / "research"),
                "preregistration_path": str(preregistration),
                "settlement_grace_seconds": 360,
                "retry_seconds": 30,
                "status_interval_seconds": 15,
                "minimum_free_disk_bytes": 12 * 1024**3,
                "max_history_roots": 1,
                "decision_tau_seconds": [240, 180, 120, 60],
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _write_summary(path: Path) -> Path:
    summary = {
        "paper_only": True,
        "new_orders_disabled": True,
        "classification": "insufficient_data",
        "qualified_net_pnl": None,
        "development_shadow": {
            "qualified_evidence": False,
            "decisions": 3,
            "trades": 2,
            "fills": 11,
            "settled_trades": 2,
            "pending_trades": 0,
            "net_pnl": "9.140645",
            "average_net_pnl": "4.5703225",
            "winning_trades": 2,
            "losing_trades": 0,
            "breakeven_trades": 0,
            "win_rate": "1",
            "profit_factor": None,
        },
        "evidence": {
            "capture_cycles": 4,
            "decision_reports": 16,
            "clob_websocket_errors": 48,
            "clob_reconnects": 48,
            "clob_disconnects": 49,
            "rtds_websocket_errors": 4,
            "rtds_reconnects": 4,
            "rtds_disconnects": 4,
            "recorder_leg_failures": 1,
            "degraded_capture_cycles": 1,
            "minimum_clob_recorder_legs": 2,
            "minimum_rtds_recorder_legs": 2,
            "clock_sync": {
                "required_decisions": 8,
                "valid_decisions": 4,
                "invalid_decisions": 4,
                "maximum_measurement_age_ms": 563_000,
                "maximum_uncertainty_ms": 102,
                "latest_offset_seconds": "0.126386",
            },
            "paper_decision_evaluations": 8,
            "paper_no_trade_decisions": 8,
            "explainable_simulated_trades": 0,
            "explainable_fills": 0,
            "observed_explainable_net_pnl": None,
        },
        "promotion_gaps": {
            "resolved_markets_remaining": 1992,
            "simulated_trades_remaining": 500,
            "explainable_fills_remaining": 500,
        },
    }
    summary["summary_sha256"] = hashlib.sha256(
        canonical_json_bytes(summary)
    ).hexdigest()
    path.write_text(json.dumps(summary), encoding="utf-8")
    return path


def _write_status(path: Path, summary_path: Path) -> None:
    document = {
        "schema_version": "btc-twap-relative-value-service-status.v1",
        "phase": "settlement_wait",
        "heartbeat_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "pid": 123,
        "child_pid": 456,
        "current_expiry_seconds": 1_786_532_400,
        "current_capture_root": "/tmp/capture",
        "completed_report_count": 16,
        "latest_summary_path": str(summary_path),
        "latest_classification": "insufficient_data",
        "last_error": None,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "authenticated_endpoints_used": 0,
        "orders_submitted": 0,
    }
    document["status_sha256"] = hashlib.sha256(
        canonical_json_bytes(document)
    ).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_health_check_surfaces_monitoring_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_service_config(tmp_path)
    summary_path = _write_summary(tmp_path / "summary.json")
    _write_status(tmp_path / "data" / "service" / "status.json", summary_path)
    monkeypatch.setattr(checker, "_process_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        checker,
        "_free_disk_bytes",
        lambda path: 13 * 1024**3,
    )

    exit_code = checker.main(["--config", str(config_path)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["healthy"] is False
    assert "clock_evidence_policy_violated" in payload["failures"]
    assert payload["summary"]["development_shadow"]["net_pnl"] == "9.140645"
    assert payload["summary"]["evidence"]["clob_disconnects"] == 49
    assert payload["summary"]["promotion_gaps"]["resolved_markets_remaining"] == 1992
    assert payload["monitoring"]["connections"] == {
        "clob": {"errors": 48, "reconnects": 48, "disconnects": 49},
        "rtds": {"errors": 4, "reconnects": 4, "disconnects": 4},
        "redundancy": {
            "recorder_leg_failures": 1,
            "degraded_capture_cycles": 1,
            "minimum_clob_legs": 2,
            "minimum_rtds_legs": 2,
        },
    }
    assert payload["monitoring"]["clock"] == {
        "required_decisions": 8,
        "valid_decisions": 4,
        "invalid_decisions": 4,
        "all_required_decisions_valid": False,
        "maximum_measurement_age_ms": 563_000,
        "maximum_uncertainty_ms": 102,
        "latest_offset_seconds": "0.126386",
        "frozen_maximum_measurement_age_ms": 1_200_000,
        "frozen_maximum_uncertainty_ms": 100,
        "policy_passed": False,
        "policy_violations": [
            "invalid_clock_decisions_observed",
            "clock_uncertainty_limit_exceeded",
        ],
    }
    assert payload["monitoring"]["paper"] == {
        "decision_evaluations": 8,
        "no_trade_decisions": 8,
        "explainable_simulated_trades": 0,
        "explainable_fills": 0,
        "observed_explainable_net_pnl": None,
        "qualified_net_pnl": None,
    }
    assert payload["monitoring"]["shadow"]["trades"] == 2
    assert payload["monitoring"]["shadow"]["fills"] == 11
    assert payload["monitoring"]["shadow"]["profit_factor"] is None
    assert payload["monitoring"]["service"]["runtime_healthy"] is True
    assert payload["monitoring"]["service"]["paper_only_guard_valid"] is True
    assert payload["monitoring"]["service"]["status_hash_valid"] is True


def test_health_check_fails_closed_for_tampered_summary_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_service_config(tmp_path)
    summary_path = _write_summary(tmp_path / "summary.json")
    tampered = json.loads(summary_path.read_text(encoding="utf-8"))
    tampered["qualified_net_pnl"] = "1.00"
    summary_path.write_text(json.dumps(tampered), encoding="utf-8")
    _write_status(tmp_path / "data" / "service" / "status.json", summary_path)
    monkeypatch.setattr(checker, "_process_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        checker,
        "_free_disk_bytes",
        lambda path: 13 * 1024**3,
    )

    exit_code = checker.main(["--config", str(config_path)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["healthy"] is False
    assert "validation_summary_invalid" in payload["failures"]
    assert payload["summary"] is None
    assert payload["monitoring"]["connections"] is None
    assert payload["monitoring"]["clock"] is None
