from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from src.profit_system import readiness
from src.profit_system.reports import (
    build_fault_drill_result,
    build_live_probe_result,
)


def test_default_doctor_is_fail_closed_and_complete() -> None:
    report = readiness.evaluate_doctor()

    assert report.gate == readiness.GATE_CANARY
    assert report.status == readiness.STATUS_LIVE_BLOCKED
    assert report.remaining_conditions == readiness.REQUIRED_CANARY_CHECKS
    assert report.context["secret_policy"] == "doctor never reads or prints credential values"


def test_doctor_blocks_forged_all_green_local_json_without_verifiable_attestation(
    tmp_path: Path,
) -> None:
    probe_path = tmp_path / "probe.json"
    drill_path = tmp_path / "drills.json"
    forged_probe = build_live_probe_result(ready=True)
    forged_probe["provenance_attestation"] = {
        "attestation_type": "sigstore-bundle",
        "issuer": "local-test",
        "verifier": "handwritten-json",
        "signature_verified": True,
        "transparency_log_recorded": True,
        "bundle_hash": "a" * 64,
    }
    probe_path.write_text(json.dumps(forged_probe), encoding="utf-8")
    forged_drills = build_fault_drill_result()
    forged_drills["provenance_attestation"] = {
        "attestation_type": "sigstore-bundle",
        "issuer": "local-test",
        "verifier": "handwritten-json",
        "signature_verified": True,
        "transparency_log_recorded": True,
        "bundle_hash": "b" * 64,
    }
    drill_path.write_text(json.dumps(forged_drills), encoding="utf-8")
    config_path = tmp_path / "canary.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": readiness.CANARY_CONFIG_SCHEMA_VERSION,
                "profile_name": "qa-canary",
                "authorization": {
                    "explicit_live_authorization": True,
                    "first_canary_manual_confirmation": True,
                    "plan_id": "plan-001",
                    "approved_by": "desk-operator",
                    "approved_at": "2026-08-30T12:00:00Z",
                },
                "strategy": {
                    "strategy_id": "maker.canary.demo",
                    "market_whitelist": ["market-1"],
                    "qualification": {
                        "research_gate_passed": True,
                        "shadow_gate_passed": True,
                    },
                },
                "risk": {
                    "max_order_notional": "1.00",
                    "max_market_exposure": "2.00",
                    "max_event_exposure": "2.00",
                    "max_strategy_exposure": "2.00",
                    "max_total_exposure": "5.00",
                    "max_daily_loss": "2.00",
                    "max_drawdown": "3.00",
                    "max_open_orders": 2,
                    "max_unmatched_leg_seconds": "15",
                    "max_market_data_age_ms": 1500,
                    "max_user_stream_gap_seconds": 5,
                    "max_order_rate": "1",
                },
                "live_probe_result_path": str(probe_path),
                "fault_drill_result_path": str(drill_path),
            }
        ),
        encoding="utf-8",
    )

    report = readiness.evaluate_doctor(config_path)

    assert report.status == readiness.STATUS_LIVE_BLOCKED
    assert set(report.blocking_check_ids) == {
        *readiness.REQUIRED_LIVE_PROBE_CHECKS,
        "kill_restart_drills",
    }
    assert report.context["live_evidence_policy"] == (
        "unsigned local probe and drill JSON are advisory only until external attestation "
        "verification exists"
    )
    for check in report.checks:
        if check.check_id in set(readiness.REQUIRED_LIVE_PROBE_CHECKS) | {"kill_restart_drills"}:
            assert check.status == readiness.CHECK_BLOCKED
            assert check.evidence["provenance_attestation"] == "present"
            assert check.evidence["doctor_verification"] == "unavailable"


def test_limited_live_gate_is_machine_readable() -> None:
    report = readiness.evaluate_limited_live_gate(
        strategy_id="maker.demo",
        live_days=30,
        independent_fills=100,
        realized_net_pnl=Decimal("5.0"),
        profit_factor=Decimal("1.18"),
        edge_realization_ratio=Decimal("0.72"),
        max_drawdown_within_budget=True,
        positive_windows=2,
        unresolved_reconciliation_count=0,
        unexplained_cash_difference=False,
    )

    assert report.gate == readiness.GATE_LIMITED_LIVE
    assert report.status == readiness.STATUS_LIVE_LIMITED_READY
