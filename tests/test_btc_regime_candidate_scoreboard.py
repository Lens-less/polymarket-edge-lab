from __future__ import annotations

import json
from pathlib import Path

from scripts.build_btc_regime_candidate_scoreboard import build_candidate_scoreboard
from src.edge_lab.data_store import canonical_json_bytes


def _write_report(
    path: Path,
    *,
    q_5_raw: str,
    q_15_raw: str,
    loss_probability: str,
    signal_surface_complete: bool,
    delayed_surface_complete: bool,
    material_second_leg_failure: bool,
    duration_ms: int,
    net_pnl: str,
) -> None:
    report = {
        "development_shadow_cycle": {
            "decision": {
                "action": "long_15_up_long_5_down",
                "q_5_raw": q_5_raw,
                "q_15_raw": q_15_raw,
                "loss_probability": loss_probability,
            },
            "execution": {
                "diagnostics": {
                    "economic_attempt": True,
                    "material_second_leg_failure": material_second_leg_failure,
                    "second_leg_fill_ratio": "1",
                    "transient_naked_exposure_duration_ms": duration_ms,
                    "residual_unhedged_max_loss_usdc": (
                        "0.02" if material_second_leg_failure else "0"
                    ),
                }
            },
            "settlement": {
                "explainable": True,
                "net_pnl": net_pnl,
            },
        },
        "observed": {
            "book_replay_coverage": {
                "complete_four_token_signal_surface": signal_surface_complete,
                "complete_four_token_delayed_execution_surface": delayed_surface_complete,
            }
        },
        "candidate_hypotheses": {
            "available": True,
            "probability": {
                "model_probability_up": {
                    "5m": q_5_raw,
                    "15m": q_15_raw,
                },
                "market_probability_up": {"5m": "0.45", "15m": "0.55"},
                "actual_up": {"5m": net_pnl != "-3.0", "15m": True},
            },
            "depth_buffer": {
                "passes": {
                    "1.25": signal_surface_complete,
                    "1.5": signal_surface_complete,
                    "2": False,
                }
            },
            "existing_canonical_controls": {
                "signal_walks_both_legs_to_full_target": True,
                "timeout_then_immediate_unwind": True,
            },
        },
    }
    report["report_sha256"] = __import__("hashlib").sha256(
        canonical_json_bytes(report)
    ).hexdigest()
    path.write_text(json.dumps(report), encoding="utf-8")


def test_candidate_scoreboard_tracks_kept_and_filtered_pnl(tmp_path: Path) -> None:
    _write_report(
        tmp_path / "baseline-win.json",
        q_5_raw="0.50",
        q_15_raw="0.60",
        loss_probability="0.20",
        signal_surface_complete=True,
        delayed_surface_complete=True,
        material_second_leg_failure=False,
        duration_ms=500,
        net_pnl="5.0",
    )
    _write_report(
        tmp_path / "extreme-dust-win.json",
        q_5_raw="0.01",
        q_15_raw="0.99",
        loss_probability="0.10",
        signal_surface_complete=True,
        delayed_surface_complete=True,
        material_second_leg_failure=False,
        duration_ms=900,
        net_pnl="2.0",
    )
    _write_report(
        tmp_path / "risky-loss.json",
        q_5_raw="0.40",
        q_15_raw="0.55",
        loss_probability="0.60",
        signal_surface_complete=False,
        delayed_surface_complete=False,
        material_second_leg_failure=True,
        duration_ms=1600,
        net_pnl="-3.0",
    )

    scoreboard = build_candidate_scoreboard(tmp_path.glob("*.json"))

    assert scoreboard["reports_considered"] == 3
    assert scoreboard["candidates"]["baseline_shadow"] == {
        "evaluable_trades": 3,
        "filtered_negative_pnl": "0",
        "filtered_positive_pnl": "0",
        "filtered_trades": 0,
        "kept_net_pnl": "4",
        "kept_settled_trades": 3,
        "kept_trades": 3,
        "unavailable_trades": 0,
    }
    assert scoreboard["candidates"]["a1_extreme_probability_veto"]["filtered_positive_pnl"] == "2"
    assert scoreboard["candidates"]["a4_loss_probability_veto"]["filtered_negative_pnl"] == "-3"
    assert scoreboard["candidates"]["b3_signal_depth_buffer_1_5"]["filtered_negative_pnl"] == "-3"
    assert scoreboard["probability_shrinkage"]["grid"]["0.5"]["5m"]["sample_count"] == 3
    assert scoreboard["execution_control_audit"] == {
        "reports_with_candidate_metadata": 3,
        "canonical_signal_walks_both_legs_to_full_target": True,
        "canonical_timeout_then_immediate_unwind": True,
        "material_second_leg_failures": 1,
        "failures_with_residual_max_loss_at_least_0_01_usdc": 1,
        "interpretation": (
            "B1 dual-depth walking and B2 timeout/unwind are existing canonical "
            "controls; evaluate depth buffers and economically material residuals "
            "instead of treating any dust duration as full naked exposure."
        ),
    }
