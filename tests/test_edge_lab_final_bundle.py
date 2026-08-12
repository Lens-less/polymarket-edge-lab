"""Behavioral tests for the conservative final Edge Discovery bundle."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from src.edge_lab.final_bundle import (
    FinalBundleConfig,
    _source_safety_valid,
    build_final_bundle,
)


def test_source_safety_requires_exact_zero_activity_counts() -> None:
    assert _source_safety_valid(
        {
            "safety": {
                "orders_submitted": 0,
                "authenticated_endpoints_used": 0,
            }
        }
    )
    assert not _source_safety_valid(
        {"safety": {"orders_submitted": 1}}
    )
    assert _source_safety_valid(
        {"safety": {"orders_submitted": False}}
    )
    assert not _source_safety_valid(
        {"safety": {"authenticated_endpoints_used": 1}}
    )


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def _complete_fixture(root: Path) -> FinalBundleConfig:
    research = root / "research"
    _write_json(
        research / "BASELINE_EVIDENCE_PACK.json",
        {
            "schema": "edge-baseline-evidence-pack.v1",
            "source_as_of": "2026-07-24T09:00:00Z",
            "evidence_sets": [
                {
                    "evidence_id": "legacy-negative",
                    "classification": "rejected",
                    "data_level": "L0",
                    "counts": {
                        "event_count": 0,
                        "authenticated_fill_count": 0,
                    },
                    "metrics": {
                        "pnl": {
                            "value": "-2",
                            "unit": "pUSD",
                            "reason": "synthetic reconciliation only",
                        }
                    },
                    "disqualifying_reasons": ["synthetic"],
                    "source_paths": ["legacy.json"],
                },
                {
                    "evidence_id": "legacy-missing-ledger",
                    "classification": "rejected",
                    "data_level": "L0",
                    "counts": {"authenticated_fill_count": 0},
                    "metrics": {
                        "pnl": {
                            "value": None,
                            "unit": "pUSD",
                            "reason": "no reconciled ledger",
                        }
                    },
                    "disqualifying_reasons": ["no ledger"],
                    "source_paths": ["legacy-2.json"],
                },
            ],
        },
    )
    _write_json(
        research / "selective-liquidity-rewards-20260724T010000Z.json",
        {
            "schema_version": "edge-lab-liquidity-reward-experiment-v1",
            "experiment_id": "reward-incomplete",
            "started_at": "2026-07-24T01:00:00Z",
            "completed_at": "2026-07-24T01:01:00Z",
            "classification_counts": {"insufficient_data": 1},
            "global_conclusion": "No payout ledger.",
            "markets": [],
            "safety": {"new_orders_disabled": True},
        },
    )
    _write_json(
        research / "selective-liquidity-rewards-20260724T020000Z.json",
        {
            "schema_version": "edge-lab-liquidity-reward-experiment-v1",
            "experiment_id": "reward-canonical",
            "started_at": "2026-07-24T02:00:00Z",
            "completed_at": "2026-07-24T02:01:00Z",
            "classification_counts": {"promising_not_validated": 1},
            "global_conclusion": "Theoretical only.",
            "markets": [
                {
                    "condition_id": "condition-1",
                    "classification": "promising_not_validated",
                    "theoretical_ledger": {
                        "recognized_pnl": "0",
                        "daily_scenarios": [
                            {"pnl": "999", "reward_multiplier": "1"}
                        ],
                    },
                    "scoring_ledger": {"records": [], "recognized_pnl": "0"},
                    "payout_ledger": {"records": [], "recognized_pnl": "0"},
                    # Deliberately hostile fields: these must not become trades.
                    "price_touches": [{"price": "0.49", "size": "100"}],
                    "validation_ceiling": {
                        "classification_ceiling": "promising_not_validated",
                        "missing_evidence": ["multiple_confirmed_payout_epochs"],
                    },
                }
            ],
            "safety": {"new_orders_disabled": True},
        },
    )
    for run_id, accepted in (("constraint-failed", 0), ("constraint-screen", 2)):
        _write_json(
            research / "constraint_experiment_runs" / run_id / "summary.json",
            {
                "schema_version": "edge-lab-constraint-experiment-summary.v1",
                "run_id": run_id,
                "experiment_id": f"exp-{run_id}",
                "started_at_ms": 1_784_888_000_000,
                "finished_at_ms": 1_784_888_001_000,
                "candidate_count": 10,
                "accepted_snapshot_count": accepted,
                "failure_counts": {"missing_chain_mapping": 10 - accepted},
                "profit_claim": "none_snapshot_diagnostics_only",
                "new_orders_disabled": True,
                "public_get_only": True,
            },
        )
    _write_json(
        research / "LATENCY_CAPTURE_EXPERIMENT.json",
        {
            "schema_version": "edge-lab-latency-capture-experiment.v1",
            "experiment_id": "latency-1",
            "as_of_received_at": "2026-07-24T03:00:00Z",
            "status": "completed",
            "coverage": {
                "independent_markets": 1,
                "quote_observations": 452,
            },
            "sample_gate": {"passed": False},
            "conclusion": {
                "classification": "insufficient_data",
                "profitable_strategy_validated": False,
                "trades_or_fills_claimed": 0,
                "statement": "Co-movement only.",
            },
            "series": [{"touches": 500, "idealized_pnl": "123"}],
            "safety": {"new_orders_disabled": True},
        },
    )
    weather = research / "weather_public"
    _write_json(
        weather / "RUN_MANIFEST.json",
        {
            "schema": "weather-acquisition-run/v1",
            "discovered_events": 80,
            "eligible_events": 4,
            "classification": "insufficient_data",
            "data_level": "L1",
            "explainable_fills": 0,
            "experiment_available": False,
            "experiment_error": "need five cities",
            "safety": {
                "orders_submitted": 0,
                "live_trading_enabled": False,
            },
        },
    )
    manifest = {
        "schema_version": "edge-data-manifest.v1",
        "generated_at": "2026-07-24T04:00:00Z",
        "summary": {
            "highest_evidence_level": "L2",
            "integrity_failure_count": 0,
            "forward_record_count": 1177,
        },
    }
    manifest["data_manifest_hash"] = hashlib.sha256(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    _write_json(research / "DATA_MANIFEST.json", manifest)
    return FinalBundleConfig(
        research_dir=research,
        output_dir=research,
        report_date="2026-07-24",
        weather_run_dir=weather,
    )


def test_bundle_keeps_theory_and_touches_out_of_trades_and_profit(tmp_path: Path) -> None:
    result = build_final_bundle(_complete_fixture(tmp_path))

    backtest = json.loads(result.backtest_results.read_text(encoding="utf-8"))
    assert backtest["overall"]["profitable_strategy_validated"] is False
    assert backtest["overall"]["classification"] == "rejected"
    assert backtest["safety"]["live_trading_enabled"] is False
    rewards = next(row for row in backtest["strategies"] if row["family"] == "rewards")
    assert [
        row["experiment_id"]
        for row in backtest["strategies"]
        if row["family"] == "rewards"
    ] == ["reward-canonical"]
    assert rewards["diagnostics"]["canonical_result"] is True
    assert rewards["metrics"]["pnl"]["value"] is None
    assert "theoretical" in rewards["metrics"]["pnl"]["reason"].lower()
    assert rewards["diagnostics"]["theoretical_scenario_count"] == 1
    assert rewards["counts"]["authenticated_fill_count"] == 0

    with result.trades.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == []
    assert "price_touch" not in result.trades.read_text(encoding="utf-8")
    assert "999" not in result.trades.read_text(encoding="utf-8")
    reproduction = result.reproduction.read_text(encoding="utf-8")
    assert "verify_weather_run_manifest" in reproduction
    assert "--replay-manifest" in reproduction
    assert "--replay-source-run" in reproduction
    assert "run_latency_capture_experiment.py --validate-only" in reproduction
    assert "run_edge_capture.py" in reproduction
    assert "--validate-only" in reproduction
    assert "compileall" in reproduction
    assert "pytest -q" in reproduction
    assert "-m network" in reproduction
    assert "git diff --check" in reproduction


def test_registry_contains_every_discovered_run_not_only_best(tmp_path: Path) -> None:
    result = build_final_bundle(_complete_fixture(tmp_path))

    rows = [
        json.loads(line)
        for line in result.experiment_registry.read_text(encoding="utf-8").splitlines()
    ]
    ids = {row["experiment_id"] for row in rows}
    assert {
        "baseline:legacy-negative",
        "baseline:legacy-missing-ledger",
        "reward-incomplete",
        "reward-canonical",
        "constraint-failed",
        "constraint-screen",
        "latency-1",
        "weather-public-acquisition",
        "data-manifest-audit",
    }.issubset(ids)
    assert all(row["live_trading_enabled"] is False for row in rows)
    assert all("source_sha256" in row for row in rows)


def test_missing_inputs_generate_explicit_null_reason_and_disabled_live_readiness(
    tmp_path: Path,
) -> None:
    research = tmp_path / "empty"
    research.mkdir()

    result = build_final_bundle(
        FinalBundleConfig(
            research_dir=research,
            output_dir=research,
            report_date="2026-07-24",
        )
    )

    backtest = json.loads(result.backtest_results.read_text(encoding="utf-8"))
    assert backtest["overall"]["classification"] == "insufficient_data"
    assert len(backtest["strategies"]) == 7
    for row in backtest["strategies"]:
        assert row["classification"] == "insufficient_data"
        assert row["metrics"]["pnl"]["value"] is None
        assert row["metrics"]["pnl"]["reason"]
    readiness = result.live_readiness.read_text(encoding="utf-8")
    assert "DISABLED" in readiness
    assert "不建议实盘交易" in readiness
    quality = result.data_quality.read_text(encoding="utf-8")
    assert "missing" in quality.lower()
    assert "DATA_MANIFEST.json" in quality


def test_bundle_is_byte_reproducible_and_config_hashes_every_input(
    tmp_path: Path,
) -> None:
    config = _complete_fixture(tmp_path)
    first = build_final_bundle(config)
    first_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in first.artifact_paths
    }

    second = build_final_bundle(config)
    second_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in second.artifact_paths
    }

    assert first_hashes == second_hashes
    snapshot = json.loads(second.config_snapshot.read_text(encoding="utf-8"))
    assert snapshot["live_trading_enabled"] is False
    assert snapshot["network_access"] is False
    assert len(snapshot["inputs"]) == 8
    assert all(
        row["sha256"] is None or len(row["sha256"]) == 64
        for row in snapshot["inputs"]
    )


def test_unverified_profitable_claim_is_capped_and_unsafe_source_is_rejected(
    tmp_path: Path,
) -> None:
    config = _complete_fixture(tmp_path)
    canonical = (
        config.research_dir
        / "selective-liquidity-rewards-20260724T020000Z.json"
    )
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    payload["classification_counts"] = {"validated_profitable": 1}
    payload["safety"] = {"new_orders_disabled": False}
    _write_json(canonical, payload)

    result = build_final_bundle(config)

    backtest = json.loads(result.backtest_results.read_text(encoding="utf-8"))
    rewards = next(row for row in backtest["strategies"] if row["family"] == "rewards")
    assert rewards["classification"] == "rejected"
    assert rewards["profitable_strategy_validated"] is False
    assert any("safety boundary" in reason for reason in rewards["limitations"])


def test_strict_frozen_manifest_wins_and_race_snapshot_is_registry_only(
    tmp_path: Path,
) -> None:
    config = _complete_fixture(tmp_path)
    strict = {
        "schema_version": "edge-data-manifest.v1",
        "totals": {
            "capture_record_count": 11561,
            "capture_path_inventory_stable": True,
            "raw_without_manifest_count": 0,
        },
    }
    strict["data_manifest_hash"] = hashlib.sha256(
        json.dumps(
            strict,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    strict_dir = config.research_dir / "capture_freeze_strict"
    _write_json(strict_dir / "DATA_MANIFEST.json", strict)
    _write_json(
        strict_dir / "DATA_QUALITY.json",
        {
            "schema_version": "edge-data-quality.v1",
            "data_manifest_hash": strict["data_manifest_hash"],
            "status": "complete",
        },
    )

    result = build_final_bundle(config)

    snapshot = json.loads(result.config_snapshot.read_text(encoding="utf-8"))
    assert (
        snapshot["canonical_data_manifest"]["path"]
        == "capture_freeze_strict/DATA_MANIFEST.json"
    )
    assert (
        snapshot["canonical_data_manifest"]["matching_data_quality_path"]
        == "capture_freeze_strict/DATA_QUALITY.json"
    )
    registry = [
        json.loads(line)
        for line in result.experiment_registry.read_text(encoding="utf-8").splitlines()
    ]
    manifests = [row for row in registry if row["strategy"] == "data_capture"]
    assert len(manifests) == 2
    assert sum(row["canonical"] is True for row in manifests) == 1
    assert sum(row["superseded"] is True for row in manifests) == 1


def test_unbound_weather_experiment_is_not_ingested(tmp_path: Path) -> None:
    config = _complete_fixture(tmp_path)
    manifest_path = config.weather_run_dir / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema"] = "weather-acquisition-run/v2"
    manifest["experiment_available"] = True
    _write_json(manifest_path, manifest)
    _write_json(
        config.weather_run_dir / "EXPERIMENT.json",
        {
            "schema_version": "weather-experiment/v1",
            "classification": "validated_profitable",
            "trade_rows": [
                {
                    "is_realized": True,
                    "shadow_profit": {"__decimal__": "999"},
                }
            ],
        },
    )

    result = build_final_bundle(config)

    backtest = json.loads(result.backtest_results.read_text(encoding="utf-8"))
    weather = next(row for row in backtest["strategies"] if row["family"] == "weather")
    assert weather["classification"] == "insufficient_data"
    assert weather["source_artifact"].endswith("RUN_MANIFEST.json")
    assert weather["diagnostics"]["manifest_binding_verified"] is False
    assert "verification failed" in weather["diagnostics"]["manifest_binding_error"]
    assert len(result.trades.read_text(encoding="utf-8").splitlines()) == 1


def test_approved_constraint_final_supersedes_predecessor_and_unsafe_newer(
    tmp_path: Path,
) -> None:
    config = _complete_fixture(tmp_path)
    for run_id, finished_at_ms in (
        ("current-20260724-standard-v5-final-provenance", 1_784_888_002_000),
        ("current-20260724-standard-v6-security-final", 1_784_888_003_000),
        ("replay-20260724-standard-v6-security-final", 1_784_888_004_000),
        ("current-20260724-standard-v7-unsafe", 1_784_888_005_000),
    ):
        unsafe = run_id.endswith("-unsafe")
        _write_json(
            config.research_dir
            / "constraint_experiment_runs"
            / run_id
            / "summary.json",
            {
                "schema_version": "edge-lab-constraint-experiment-summary.v1",
                "run_id": run_id,
                "experiment_id": f"exp-{run_id}",
                "started_at_ms": finished_at_ms - 1_000,
                "finished_at_ms": finished_at_ms,
                "analysis_count": 2,
                "candidate_count": 10,
                "accepted_snapshot_count": 0,
                "failure_counts": {"not_executable": 10},
                "profit_claim": "none_snapshot_diagnostics_only",
                "new_orders_disabled": not unsafe,
                "public_get_only": True,
            },
        )

    result = build_final_bundle(config)

    backtest = json.loads(result.backtest_results.read_text(encoding="utf-8"))
    standard = next(
        row
        for row in backtest["strategies"]
        if row["family"] == "neg_risk_standard"
    )
    assert (
        standard["source_artifact"]
        == "constraint_experiment_runs/current-20260724-standard-v6-security-final/summary.json"
    )
    registry = [
        json.loads(line)
        for line in result.experiment_registry.read_text(encoding="utf-8").splitlines()
    ]
    replay = next(
        row
        for row in registry
        if row["source_artifact"]
        == "constraint_experiment_runs/replay-20260724-standard-v6-security-final/summary.json"
    )
    assert replay["canonical"] is False
    assert replay["superseded"] is True


def test_bound_reward_v2_supersedes_legacy_and_keeps_shadow_out_of_pnl(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _complete_fixture(tmp_path)
    run_dir = config.research_dir / "reward_runs" / "reward-v2"
    experiment = {
        "schema_version": "edge-lab-liquidity-reward-experiment-v2",
        "experiment_id": "reward-v2",
        "started_at": "2026-07-24T05:00:00Z",
        "completed_at": "2026-07-24T05:01:00Z",
        "config": {"max_markets": 1},
        "profit_claim": "none",
        "recognized_trading_pnl": "0",
        "recognized_reward_pnl": "0",
        "recognized_total_pnl": "0",
        "actual_fill_count": 0,
        "explainable_fill_count": 0,
        "classification_counts": {"promising_not_validated": 1},
        "universe": {
            "unique_current_reward_markets": 10,
            "selected_markets": 1,
        },
        "markets": [
            {
                "condition_id": "condition-v2",
                "profit_claim": "none",
                "recognized_trading_pnl": "0",
                "recognized_reward_pnl": "0",
                "recognized_total_pnl": "0",
                "actual_fill_count": 0,
                "explainable_fill_count": 0,
                "classification": "promising_not_validated",
                "theoretical_ledger": {
                    "recognized_pnl": "0",
                    "conditional_pool_allocation_if_snapshot_share_persisted_full_epoch_at_3x": "12.5",
                },
                "shadow_fill_scenario": {
                    "conditional_non_reward_cashflow_before_operation_cost": "999"
                },
                "instantaneous_zero_latency_shadow_one_leg": {
                    "instantaneous_zero_latency_shadow_worst_one_leg_cashflow_before_operation_cost": "-50"
                },
                "conditional_haircuts": [
                    {
                        "reward_multiplier": "0",
                        "conditional_total_cashflow": "777",
                    }
                ],
                "shadow_reward_sensitivity": {
                    "reward_zero_conditional_cashflow_positive": True
                },
            }
        ],
        "safety": {
            "credentials_read": False,
            "orders_submitted": False,
            "transactions_signed": False,
        },
    }
    _write_json(run_dir / "EXPERIMENT.json", experiment)
    _write_json(
        run_dir / "RUN_MANIFEST.json",
        {
            "schema": "edge-lab-reward-run-manifest-v2",
            "experiment_id": "reward-v2",
            "canonical_artifact": "EXPERIMENT.json",
            "config": experiment["config"],
            "files": [],
        },
    )
    from src.edge_lab import reward_archive

    monkeypatch.setattr(
        reward_archive,
        "verify_reward_run_manifest",
        lambda path: {
            "canonical_artifact": "EXPERIMENT.json",
            "canonical_artifact_sha256": hashlib.sha256(
                (run_dir / "EXPERIMENT.json").read_bytes()
            ).hexdigest(),
            "manifest_sha256": hashlib.sha256(
                (run_dir / "RUN_MANIFEST.json").read_bytes()
            ).hexdigest(),
            "network_requests": 0,
            "writes_performed": 0,
        },
    )

    result = build_final_bundle(config)

    backtest = json.loads(result.backtest_results.read_text(encoding="utf-8"))
    reward = next(
        row for row in backtest["strategies"] if row["family"] == "rewards"
    )
    assert reward["experiment_id"] == "reward-v2"
    assert reward["metrics"]["pnl"]["value"] is None
    assert reward["metrics"]["rewards"]["value"] == "0"
    assert reward["diagnostics"]["manifest_binding_verified"] is True
    assert (
        reward["diagnostics"][
            "top_conditional_pool_allocation_if_snapshot_persisted_at_3x"
        ]
        == "12.5"
    )
    assert reward["diagnostics"]["shadow_fill_scenario_count"] == 1
    assert reward["counts"]["authenticated_fill_count"] == 0
    assert len(result.trades.read_text(encoding="utf-8").splitlines()) == 1
    snapshot = json.loads(result.config_snapshot.read_text(encoding="utf-8"))
    assert (
        snapshot["canonical_sources"]["rewards"]["artifact_path"]
        == "reward_runs/reward-v2/EXPERIMENT.json"
    )
    assert (
        snapshot["canonical_sources"]["rewards"]["manifest_path"]
        == "reward_runs/reward-v2/RUN_MANIFEST.json"
    )


def test_unsafe_newer_latency_artifact_cannot_become_canonical(
    tmp_path: Path,
) -> None:
    config = _complete_fixture(tmp_path)
    _write_json(
        config.research_dir / "LATENCY_CAPTURE_EXPERIMENT_v1_unsafe.json",
        {
            "schema_version": "edge-lab-capture-latency-experiment.v1",
            "experiment_id": "latency-unsafe",
            "as_of_received_at": "2026-07-24T06:00:00Z",
            "status": "completed",
            "coverage": {"independent_markets": 999},
            "sample_gate": {"passed": True},
            "conclusion": {
                "classification": "validated_profitable",
                "profitable_strategy_validated": True,
                "trades_or_fills_claimed": 999,
            },
            "safety": {
                "network_requests": False,
                # Integer one must not slip past an identity comparison with
                # the boolean True.
                "orders_submitted": 1,
                "credentials_accessed": False,
                "fills_simulated": True,
                "backtest_performed": True,
            },
        },
    )

    result = build_final_bundle(config)

    backtest = json.loads(result.backtest_results.read_text(encoding="utf-8"))
    latency = next(
        row for row in backtest["strategies"] if row["family"] == "latency"
    )
    assert latency["experiment_id"] == "latency-1"
    assert latency["profitable_strategy_validated"] is False
