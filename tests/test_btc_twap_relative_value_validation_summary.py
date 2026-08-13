"""Tests for fail-closed BTC TWAP pilot aggregation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_btc_twap_relative_value_validation_summary import build_summary
from src.edge_lab.data_store import canonical_json_bytes


def _write_report(
    path: Path,
    *,
    model_available: bool = False,
    shadow_net_pnl: str | None = None,
    shadow_trade_count: int | None = None,
    shadow_execution_diagnostics: dict[str, object] | None = None,
    capture_root: str = "/capture/run-1",
    websocket_errors: int = 0,
    rtds_errors: int = 0,
    recorder_leg_failures: int = 0,
    shadow_no_trade_reason: str | None = None,
    clock_sync_valid: bool | None = None,
    decision_tau_seconds: int = 60,
    preregistration_sha256: str = "a" * 64,
) -> None:
    report = {
        "paper_only": True,
        "new_orders_disabled": True,
        "inputs": {
            "capture_root": capture_root,
            "decision_tau_seconds": decision_tau_seconds,
            "preregistration_sha256": preregistration_sha256,
        },
        "integrity": {
            "capture": {
                "checksum_mismatches": [],
                "invalid_manifests": [],
            }
        },
        "observed": {
            "predictor_one_second_samples": 300,
            "book_replay_coverage": {
                "complete_four_token_signal_surface": True,
                "complete_four_token_delayed_execution_surface": True,
            },
            "clob_event_counts": {
                "error": websocket_errors,
                "reconnect_scheduled": websocket_errors,
            },
            "rtds_event_counts": {
                "error": rtds_errors,
                "reconnect_scheduled": rtds_errors,
                "disconnected": rtds_errors,
            },
            "capture_runtime": {
                "recorder_leg_count": 2,
                "recorder_leg_failures": [
                    {"code": "recorder_leg_failed"}
                    for _ in range(recorder_leg_failures)
                ],
                "websocket_redundancy": {
                    "clob_market_ws": 2,
                    "rtds_ws": 2,
                },
                "capture_error": None,
            },
            "clock_sync": (
                {
                    "required": True,
                    "measurement_age_ms": 300_000,
                    "valid_for_decision": clock_sync_valid,
                    "evidence": {
                        "offset_seconds": "0.30",
                        "uncertainty_ms": 52,
                    },
                }
                if clock_sync_valid is not None
                else {
                    "required": False,
                    "measurement_age_ms": None,
                    "valid_for_decision": True,
                    "evidence": None,
                }
            ),
            "latest_rules": {
                "market-5": {
                    "present": True,
                    "rules_match_capture": True,
                    "fee_schedule": {
                        "rate": "0.07",
                        "exponent": 1,
                        "takerOnly": True,
                    },
                    "resolution_event": {"winning_outcome": "Up"},
                    "resolution_event_valid": True,
                },
                "market-15": {
                    "present": True,
                    "rules_match_capture": True,
                    "fee_schedule": {
                        "rate": "0.07",
                        "exponent": 1,
                        "takerOnly": True,
                    },
                    "resolution_event": {"winning_outcome": "Down"},
                    "resolution_event_valid": True,
                },
            },
            "mechanically_labelable_markets": 2,
            "boundaries": {
                "5m": {
                    "market_id": "market-5",
                    "mechanical_outcome": "Up",
                },
                "15m": {
                    "market_id": "market-15",
                    "mechanical_outcome": "Down",
                },
            },
            "resolution_conflicts": [],
        },
        "economic_evidence": {
            "explainable_simulated_trades": 0,
            "explainable_fills": 0,
            "observed_explainable_net_pnl": None,
            "development_shadow_decisions": int(
                shadow_net_pnl is not None or shadow_no_trade_reason is not None
            ),
            "development_shadow_trades": (
                int(shadow_net_pnl is not None)
                if shadow_trade_count is None
                else shadow_trade_count
            ),
            "development_shadow_fills": (
                2 if shadow_net_pnl is not None else 0
            ),
            "development_shadow_net_pnl": shadow_net_pnl,
            "development_shadow_execution_diagnostics": shadow_execution_diagnostics,
            "development_shadow_complete_taker_cost_model": (
                shadow_net_pnl is not None
            ),
            "development_shadow_is_qualified_evidence": False,
        },
        "raw_shadow_model": {
            "available": model_available,
            "reason_codes": (
                [] if model_available else ["causal_predictor_history_missing"]
            ),
        },
        "paper_decision": {
            "evaluated": True,
            "action": "no_trade",
            "reason_codes": ["past_only_isotonic_calibration_not_available"],
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
        },
        "development_shadow_cycle": {
            "available": (
                shadow_net_pnl is not None or shadow_no_trade_reason is not None
            ),
            "track": "development_shadow",
            "decision": (
                {
                    "action": "long_15_up_long_5_down",
                    "decision_at_ms": 1_000_000 - decision_tau_seconds * 1_000,
                    "reason_codes": ["uncalibrated_shadow_only"],
                }
                if shadow_net_pnl is not None
                else {
                    "action": "no_trade",
                    "decision_at_ms": 1_000_000 - decision_tau_seconds * 1_000,
                    "reason_codes": [shadow_no_trade_reason],
                }
                if shadow_no_trade_reason is not None
                else None
            ),
            "execution": (
                {"status": "complete"} if shadow_net_pnl is not None else None
            ),
            "settlement": (
                {
                    "net_pnl": shadow_net_pnl,
                    "explainable": True,
                    "qualified_sample": False,
                }
                if shadow_net_pnl is not None
                else None
            ),
        },
    }
    report["report_sha256"] = hashlib.sha256(
        canonical_json_bytes(report)
    ).hexdigest()
    path.write_text(json.dumps(report), encoding="utf-8")


def test_summary_retains_missing_pnl_as_null(tmp_path: Path) -> None:
    report_path = tmp_path / "pilot.json"
    _write_report(report_path)

    summary = build_summary((report_path,))

    assert summary["classification"] == "insufficient_data"
    assert summary["qualified_net_pnl"] is None
    assert summary["evidence"]["observed_explainable_net_pnl"] is None
    assert summary["evidence"]["officially_resolved_markets"] == 2
    assert summary["evidence"]["market_coverage"] == "1"
    assert summary["promotion_gaps"]["resolved_markets_remaining"] == 1998
    assert "complete_taker_cost_model_missing" in summary["reason_codes"]
    assert summary["evidence"]["paper_decision_evaluations"] == 1
    assert summary["evidence"]["paper_no_trade_decisions"] == 1


def test_summary_rejects_tampered_report(tmp_path: Path) -> None:
    report_path = tmp_path / "pilot.json"
    _write_report(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["paper_only"] = False
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        build_summary((report_path,))


def test_summary_deduplicates_retried_market_evidence(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_report(first, websocket_errors=3, rtds_errors=2, recorder_leg_failures=1)
    _write_report(
        second,
        websocket_errors=3,
        rtds_errors=2,
        recorder_leg_failures=1,
        decision_tau_seconds=120,
    )

    summary = build_summary((first, second))

    assert summary["evidence"]["unique_markets"] == 2
    assert summary["evidence"]["officially_resolved_markets"] == 2
    assert summary["evidence"]["mechanically_labelable_markets"] == 2
    assert summary["evidence"]["market_coverage"] == "1"
    assert summary["evidence"]["capture_cycles"] == 1
    assert summary["evidence"]["decision_reports"] == 2
    assert summary["evidence"]["clob_websocket_errors"] == 3
    assert summary["evidence"]["clob_reconnects"] == 3
    assert summary["evidence"]["rtds_websocket_errors"] == 2
    assert summary["evidence"]["rtds_reconnects"] == 2
    assert summary["evidence"]["recorder_leg_failures"] == 1
    assert summary["evidence"]["degraded_capture_cycles"] == 1
    assert summary["evidence"]["minimum_clob_recorder_legs"] == 2
    assert summary["evidence"]["minimum_rtds_recorder_legs"] == 2


def test_summary_rejects_duplicate_decisions_and_mixed_preregistrations(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    duplicate = tmp_path / "duplicate.json"
    mixed = tmp_path / "mixed.json"
    _write_report(first)
    _write_report(duplicate)
    _write_report(mixed, decision_tau_seconds=120, preregistration_sha256="b" * 64)

    with pytest.raises(ValueError, match="duplicate decision report"):
        build_summary((first, duplicate))
    with pytest.raises(ValueError, match="mixed preregistration"):
        build_summary((first, mixed))


def test_summary_aggregates_settled_shadow_pnl_without_promoting_it(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_report(first, shadow_net_pnl="1.25")
    _write_report(
        second,
        shadow_net_pnl="-0.50",
        decision_tau_seconds=120,
    )

    summary = build_summary((first, second))

    assert summary["qualified_net_pnl"] is None
    assert summary["development_shadow"]["decisions"] == 2
    assert summary["development_shadow"]["trades"] == 2
    assert summary["development_shadow"]["fills"] == 4
    assert summary["development_shadow"]["settled_trades"] == 2
    assert summary["development_shadow"]["net_pnl"] == "0.75"
    assert summary["development_shadow"]["winning_trades"] == 1
    assert summary["development_shadow"]["losing_trades"] == 1
    assert summary["development_shadow"]["qualified_evidence"] is False
    assert "preliminary development-shadow net PnL is 0.75" in summary[
        "conclusion"
    ]


def test_summary_distinguishes_shadow_edge_rejection_from_data_blocker(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "edge-rejected.json"
    _write_report(
        report_path,
        model_available=True,
        shadow_no_trade_reason="net_edge_below_frozen_threshold",
        clock_sync_valid=True,
    )

    summary = build_summary((report_path,))

    assert summary["development_shadow"]["decisions"] == 1
    assert summary["development_shadow"]["no_trade_decisions"] == 1
    assert summary["development_shadow"]["action_counts"] == {"no_trade": 1}
    assert summary["development_shadow"]["decision_reason_counts"] == {
        "net_edge_below_frozen_threshold": 1
    }
    assert summary["evidence"]["clock_sync"] == {
        "required_decisions": 1,
        "valid_decisions": 1,
        "invalid_decisions": 0,
        "maximum_measurement_age_ms": 300_000,
        "maximum_uncertainty_ms": 52,
        "latest_offset_seconds": "0.30",
    }


def test_summary_keeps_economic_attempts_and_execution_diagnostics(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_report(
        first,
        shadow_net_pnl="-1.25",
        shadow_trade_count=1,
        shadow_execution_diagnostics={
            "economic_attempt": True,
            "residual_unhedged_usdc": "0.00001",
            "dust_failed_unhedged": True,
            "material_failed_unhedged": False,
            "transient_naked_exposure_peak_usdc": "5",
            "transient_naked_exposure_duration_ms": 250,
        },
    )
    _write_report(
        second,
        shadow_net_pnl="-2.00",
        shadow_trade_count=1,
        shadow_execution_diagnostics={
            "economic_attempt": True,
            "residual_unhedged_usdc": "2.5",
            "dust_failed_unhedged": False,
            "material_failed_unhedged": True,
            "transient_naked_exposure_peak_usdc": "12.5",
            "transient_naked_exposure_duration_ms": 400,
        },
        decision_tau_seconds=120,
    )

    summary = build_summary((first, second))

    assert summary["development_shadow"]["trades"] == 2
    assert summary["development_shadow"]["settled_trades"] == 2
    assert summary["development_shadow"]["pending_trades"] == 0
    assert summary["development_shadow"]["execution_diagnostics"] == {
        "dust_failed_unhedged_trades": 1,
        "material_failed_unhedged_trades": 1,
        "residual_unhedged_usdc_total": "2.50001",
        "residual_unhedged_usdc_material_total": "2.5",
        "transient_naked_exposure_peak_usdc": "12.5",
        "transient_naked_exposure_total_duration_ms": 650,
        "transient_naked_exposure_trades": 2,
    }
