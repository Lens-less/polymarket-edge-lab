import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_baseline_evidence_pack import (
    PACK_RELATIVE_PATH,
    REBUILT_PACK_RELATIVE_PATH,
    build_pack,
    main,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
METRIC_NAMES = {
    "initial_capital",
    "pnl",
    "return",
    "fees",
    "rewards",
    "max_drawdown",
    "confidence_interval",
    "pnl_concentration",
}


def test_build_pack_separates_synthetic_shadow_and_authenticated_fills():
    pack = build_pack(REPO_ROOT)

    assert pack["schema"] == "polymarket-edge-lab/baseline-evidence-pack-v1"
    assert pack["summary"]["highest_data_level"] == "L1"
    assert pack["summary"]["authenticated_fill_count"] == 0

    evidence = {item["evidence_id"]: item for item in pack["evidence_sets"]}
    legacy = evidence["legacy_mock_crossing_backtest"]
    assert legacy["data_level"] == "L0"
    assert legacy["counts"]["synthetic_fill_count"] == 499
    assert legacy["counts"]["authenticated_fill_count"] == 0
    assert legacy["metrics"]["initial_capital"]["value"] == "1000"
    assert legacy["metrics"]["pnl"]["value"] == "-32.56958870063799670"
    assert (
        legacy["metrics"]["return"]["value"]
        == "-0.0325695887006379967"
    )
    assert (
        legacy["metrics"]["max_drawdown"]["value"]
        == "0.0472630935224204686"
    )
    assert (
        legacy["details"]["legacy_engine_reported_pnl"]
        == "-47.7576649839879622000000000"
    )
    assert legacy["details"]["minimum_position_shares"] == "-330"
    assert legacy["classification"] == "rejected"

    dry_run = evidence["legacy_dry_run_jsonl"]
    assert dry_run["data_level"] == "L0"
    assert dry_run["counts"]["simulated_trade_record_count"] == 330
    assert dry_run["counts"]["authenticated_fill_count"] == 0
    assert dry_run["counts"]["token_id_count"] == 5
    assert dry_run["counts"]["market_count"] is None
    assert dry_run["count_reasons"]["market_count"]
    assert dry_run["metrics"]["pnl"]["value"] is None
    assert "simulated" in dry_run["metrics"]["pnl"]["reason"].lower()

    autoresearch = evidence["legacy_autoresearch_parameter_search"]
    assert autoresearch["data_level"] == "L0"
    assert autoresearch["counts"]["retained_iteration_count"] == 5
    assert autoresearch["counts"]["authenticated_fill_count"] == 0
    assert (
        autoresearch["recomputation_audit"][
            "recorded_sharpe_roundtrip_pass_count"
        ]
        == 5
    )
    assert autoresearch["metrics"]["pnl"]["value"] is None

    replay = evidence["public_replay_corpus"]
    assert replay["data_level"] == "L1"
    assert replay["counts"]["artifact_count"] == 18
    assert replay["counts"]["market_count"] == 7
    assert replay["counts"]["sample_row_count_including_variants"] == 32859
    assert replay["counts"]["unique_condition_timestamp_count"] == 10403
    assert replay["counts"]["canonical_sample_row_count"] == 10403
    assert replay["counts"]["shadow_touch_event_count_including_variants"] == 63
    assert replay["counts"]["authenticated_fill_count"] == 0
    assert replay["metrics"]["pnl"]["value"] is None
    assert "queue" in replay["metrics"]["pnl"]["reason"].lower()


def test_every_financial_metric_is_a_value_with_an_explanation_when_null():
    pack = build_pack(REPO_ROOT)

    for evidence_set in pack["evidence_sets"]:
        metrics = evidence_set["metrics"]
        assert set(metrics) == METRIC_NAMES
        for metric in metrics.values():
            assert set(metric) >= {"value", "reason"}
            if metric["value"] is None:
                assert isinstance(metric["reason"], str)
                assert metric["reason"].strip()

    method_manifest = {
        item["path"]: item for item in pack["method_source_manifest"]
    }
    builder_path = REPO_ROOT / "scripts/build_baseline_evidence_pack.py"
    assert method_manifest["scripts/build_baseline_evidence_pack.py"][
        "sha256"
    ] == hashlib.sha256(builder_path.read_bytes()).hexdigest()
    source_paths = {item["path"] for item in pack["source_artifact_manifest"]}
    assert (
        "research/profit_redesign_2026-07-24/REDESIGN_AND_RESULTS.md"
        in source_paths
    )
    assert (
        "research/profit_redesign_2026-07-24/PRIMARY_SOURCES.md"
        in source_paths
    )


def test_pack_recomputes_retained_derived_arithmetic_without_promoting_proxies():
    pack = build_pack(REPO_ROOT)
    evidence = {item["evidence_id"]: item for item in pack["evidence_sets"]}

    replay_audit = evidence["public_replay_corpus"]["recomputation_audit"]
    assert replay_audit["distribution_block_count"] == 100
    assert replay_audit["distribution_block_tolerance_pass_count"] == 100
    assert replay_audit["integrated_reward_artifact_count"] == 4
    assert replay_audit["integrated_reward_tolerance_pass_count"] == 4
    assert replay_audit["raw_l2_recomputable"] is False

    complete_set_audit = evidence["complete_set_snapshot_scans"][
        "recomputation_audit"
    ]
    complete_set = evidence["complete_set_snapshot_scans"]
    assert complete_set["classification"] == "insufficient_data"
    assert complete_set["counts"]["market_count"] is None
    assert (
        complete_set["counts"]["source_reported_market_evaluation_count"]
        == 1821
    )
    assert complete_set["counts"]["retained_positive_direction_count"] == 0
    assert complete_set_audit["market_size_result_count"] == 800
    assert complete_set_audit["directional_net_edge_check_count"] == 1600
    assert complete_set_audit["directional_net_edge_tolerance_pass_count"] == 1600
    assert complete_set_audit["full_scan_zero_positive_claim_recomputable"] is False

    reward_audit = evidence["reward_market_snapshot_scans"][
        "recomputation_audit"
    ]
    assert reward_audit["quote_direction_count"] == 57
    assert reward_audit["paired_margin_tolerance_pass_count"] == 57
    assert reward_audit["static_reward_tolerance_pass_count"] == 57
    assert reward_audit["raw_book_recomputable"] is False


def test_cli_writes_deterministic_machine_readable_pack(tmp_path):
    output = tmp_path / "baseline.json"

    assert main(["--repo-root", str(REPO_ROOT), "--output", str(output)]) == 0
    first = output.read_bytes()
    parsed = json.loads(first)

    assert parsed == build_pack(REPO_ROOT)
    assert main(["--repo-root", str(REPO_ROOT), "--output", str(output)]) == 0
    assert output.read_bytes() == first


def test_builder_rejects_a_repo_root_that_does_not_own_the_loaded_methods(
    tmp_path,
):
    with pytest.raises(ValueError, match="same checkout"):
        build_pack(tmp_path)


def test_default_rebuild_never_overwrites_the_immutable_seed() -> None:
    assert REBUILT_PACK_RELATIVE_PATH != PACK_RELATIVE_PATH
    with pytest.raises(ValueError, match="immutable baseline seed"):
        main(
            [
                "--repo-root",
                str(REPO_ROOT),
                "--output",
                str(REPO_ROOT / PACK_RELATIVE_PATH),
            ]
        )
