import hashlib

from src.edge_lab.data_store import canonical_json_bytes
from src.profit_system.gates import ResearchGate, ShadowGate


def test_research_gate_emits_immutable_machine_readable_report() -> None:
    report = ResearchGate().evaluate(
        {
            "strategy_id": "track_a.complete_set.arbitrage.v0_2",
            "strategy_version": "0.2.0",
            "preregistration_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "preregistered": True,
            "chronological_oos": True,
            "no_future_leakage": True,
            "baseline_present": True,
            "ablation_present": True,
            "thresholds_frozen": True,
            "stress_cost_multiplier": "1.25",
            "stress_slippage_multiplier": "1.25",
            "stress_fill_rate_multiplier": "0.75",
            "baseline_expected_net_edge": "1.4",
            "ablation_expected_net_edge": "0.2",
            "stressed_expected_net_edge": "0.4",
            "candidate_count": 55,
            "executable_count": 24,
        }
    )

    unsigned = report.to_document(include_hash=False)
    assert report.status == "GO"
    assert report.go is True
    assert report.report_sha256 == hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    assert report.to_document()["preregistration_sha256"] == "a" * 64
    assert report.to_document()["config_sha256"] == "b" * 64


def test_shadow_gate_enforces_fixed_thresholds_and_returns_no_go() -> None:
    report = ShadowGate().evaluate(
        {
            "strategy_id": "track_b.maker.quote.v0_2",
            "strategy_version": "0.2.0",
            "preregistration_sha256": "c" * 64,
            "config_sha256": "d" * 64,
            "observation_days": 13,
            "candidate_count": 49,
            "executable_count": 19,
            "all_decisions_recorded": True,
            "expected_net_edge": "-0.01",
            "data_integrity_ok": True,
            "state_drift_ok": True,
            "thresholds_frozen": True,
        }
    )

    assert report.status == "NO_GO"
    assert report.go is False
    assert set(report.reasons) >= {
        "shadow_days_below_minimum",
        "shadow_candidates_below_minimum",
        "shadow_executable_below_minimum",
        "shadow_expected_edge_non_positive",
    }
