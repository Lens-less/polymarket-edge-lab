from __future__ import annotations

import json
from pathlib import Path

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
            "runtime health, four complete cohorts, real authenticated control-plane "
            "drills, immutable probe preregistration, and current full-depth hedge "
            "evidence are not yet proven"
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


def test_v08_deployment_installs_capture_health_but_no_probe_unit() -> None:
    service = (DEPLOY / "polymm-btc-twap-continuous-rtds.service").read_text(
        encoding="utf-8"
    )
    health = (DEPLOY / "polymm-btc-twap-continuous-rtds-health.service").read_text(
        encoding="utf-8"
    )
    unit_names = {path.name for path in DEPLOY.iterdir() if path.is_file()}

    assert "--validate-only" in service
    assert "run_btc_twap_continuous_rtds.py" in service
    assert "ReadWritePaths=/var/lib/poly-mm-v08" in service
    assert "check_btc_twap_continuous_rtds.py" in health
    assert "IPAddressDeny=any" in health
    assert not any("probe" in name for name in unit_names)
