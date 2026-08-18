from __future__ import annotations

import json
from pathlib import Path

from src.edge_lab.btc_twap_relative_value_service import (
    ContinuousServiceConfig,
    load_service_config,
    validate_preregistration_runtime_identity,
)

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research" / "btc_5m_15m_edge_readiness_v08_2026-08-18"
DEPLOY = ROOT / "deploy" / "aws" / "edge_readiness_v08"


def test_v08_preregistration_is_structural_only_fail_closed_and_no_go() -> None:
    preregistration = json.loads(
        (RESEARCH / "PREREGISTRATION.json").read_text(encoding="utf-8")
    )
    service = json.loads((RESEARCH / "SERVICE_CONFIG.json").read_text(encoding="utf-8"))

    assert preregistration["current_decision"] == {
        "strategy_live": "NO_GO",
        "execution_probe": "NO_GO",
        "reason": (
            "the full-window three-mode Gate 0 report and 200-expiry neutral "
            "double-maker shadow evidence are not available; pUSD, signature, "
            "terminal reconciliation, double-maker venue, and geoblock checks "
            "remain closed"
        ),
    }
    assert preregistration["live_eligible_track"]["edge_basis"] == (
        "structural_floor_only"
    )
    assert (
        preregistration["predictive_research_track"][
            "automatic_fallback_to_live_or_probe"
        ]
        is False
    )
    assert (
        preregistration["gate_0"]["existing_41_attempt_report"][
            "must_fail_on_count_mismatch"
        ]
        is True
    )
    assert (
        preregistration["gate_0"]["existing_41_attempt_report"][
            "scan_ttc_seconds_inclusive"
        ]
        == [300, 0]
    )
    assert preregistration["gate_0"]["decision_execution_mode"] == "maker_maker"
    assert preregistration["execution_probe_gate"]["minimum_neutral_shadow_expiries"] == 200
    assert preregistration["execution_probe_gate"]["maker_submissions_max"] == 2
    assert (
        preregistration["execution_probe_gate"]["capture_capacity_gate_required"]
        ["maximum_failure_rate"]
        == "0.05"
    )
    assert (
        preregistration["rolling_locked_shadow"][
            "dirty_no_trade_no_fill_and_failed_cohorts_remain_in_denominator"
        ]
        is True
    )
    assert service["safety"] == {
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "credentials_allowed": False,
        "authenticated_endpoints_allowed": False,
        "production_probe_adapter_installed": False,
    }
    assert service["pair_capture"]["token_scope"] == "paired_four_tokens_only"
    assert service["pair_capture"]["persist_raw_clob_frames"] is False
    assert service["pair_capture"]["persist_reconstructed_full_depth_frames"] is False
    assert service["pair_capture"]["full_clob_snapshot_interval_seconds"] == 30
    assert service["capacity_gate"]["maximum_capture_failure_rate"] == 0.05
    assert service["capacity_gate"]["minimum_free_disk_bytes"] >= 10 * 1024**3

    runtime = load_service_config(RESEARCH / "SERVICE_CONFIG.json")
    assert runtime.clob_snapshot_interval_seconds == 30
    assert runtime.public_taker_trades_interval_seconds == 5
    assert runtime.rules_interval_seconds == 30
    assert runtime.persist_raw_clob_frames is False
    assert runtime.persist_reconstructed_full_depth_frames is False
    assert runtime.persist_top_of_book_changes is True
    assert runtime.minimum_free_disk_bytes == 10 * 1024**3
    local_runtime = ContinuousServiceConfig(
        **{
            **runtime.__dict__,
            "preregistration_path": RESEARCH / "PREREGISTRATION.json",
            "regime_registry_path": (
                ROOT
                / "research"
                / "settlement_regime_break_2026-08-14"
                / "REGIME_REGISTRY.json"
            ),
        }
    )
    validate_preregistration_runtime_identity(
        preregistration,
        config=local_runtime,
    )


def test_v08_deployment_installs_capture_health_but_no_probe_unit() -> None:
    service = (DEPLOY / "polymm-btc-twap-continuous-rtds.service").read_text(
        encoding="utf-8"
    )
    health = (DEPLOY / "polymm-btc-twap-continuous-rtds-health.service").read_text(
        encoding="utf-8"
    )
    unit_names = {path.name for path in DEPLOY.iterdir() if path.is_file()}
    pair_capture = (
        DEPLOY / "polymm-btc-twap-edge-readiness-v08.service"
    ).read_text(encoding="utf-8")

    assert "--validate-only" in service
    assert "run_btc_twap_continuous_rtds.py" in service
    assert "ReadWritePaths=/var/lib/poly-mm-v08" in service
    assert "check_btc_twap_continuous_rtds.py" in health
    assert "run_btc_twap_relative_value_service.py" in pair_capture
    assert "--validate-only" in pair_capture
    assert "ReadWritePaths=/var/lib/poly-mm-v08" in pair_capture
    assert "IPAddressDeny=any" in health
    assert not any("probe" in name for name in unit_names)
