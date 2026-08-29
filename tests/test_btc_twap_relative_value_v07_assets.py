from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_btc_twap_relative_value_v07_counterfactual as builder
from scripts.build_btc_twap_relative_value_v07_counterfactual import (
    EXPECTED_PREREGISTRATION_SHA256,
    MANIFEST_SCHEMA_VERSION,
    build_counterfactual_report,
)
from src.edge_lab.btc_twap_relative_value_v07 import (
    MODEL_VERSION,
    SETTLEMENT_MODEL_ID,
    SharedTerminalModelConfig,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
V07_ROOT = (
    PROJECT_ROOT
    / "research"
    / "btc_5m_15m_relative_value_counterfactual_v07_2026-08-15"
)
BUILDER_PATH = (
    PROJECT_ROOT / "scripts" / "build_btc_twap_relative_value_v07_counterfactual.py"
)
FROZEN_RESEARCH_HASHES = {
    (
        "research/btc_5m_15m_relative_value_paper_v05_linux_2026-08-13/"
        "PREREGISTRATION.json"
    ): ("6287445cc16428f7fd2fcef953f4d11809e0b815165dea778f4afe9b8084ac09"),
    (
        "research/btc_5m_15m_relative_value_paper_v05_linux_2026-08-13/"
        "SERVICE_CONFIG.json"
    ): ("0b3474c202963231d0547201789ee8ac46feedab8bc6335a8231b4491c948a79"),
    "research/btc_5m_15m_relative_value_paper_v05_linux_2026-08-13/STRATEGY_SPEC.md": (
        "c96f89c87db0ec4e28b500f56ea32d7da9d9087fbc11ce738a7e737fa2921825"
    ),
    (
        "research/btc_5m_15m_relative_value_paper_v06_linux_2026-08-14/"
        "PREREGISTRATION.json"
    ): ("fdb1969bbba898c61b66665df8cffb3a52a9fde78cc357a39b437719d039aedb"),
    (
        "research/btc_5m_15m_relative_value_paper_v06_linux_2026-08-14/"
        "SERVICE_CONFIG.json"
    ): ("a06cb9361df38621b73e59eae3d26cfcff0dd0d3dd17f20c57ebe618d38eb878"),
    "research/btc_5m_15m_relative_value_paper_v06_linux_2026-08-14/STRATEGY_SPEC.md": (
        "ac39ca354c106e11a0bc7e6fd4bbf2ee954e8044aafdbde9f3f2183c69020d1a"
    ),
}


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_v07_draft_assets_are_present_and_internally_hashed() -> None:
    preregistration_path = V07_ROOT / "PREREGISTRATION.json"
    strategy_path = V07_ROOT / "STRATEGY_SPEC.md"
    schema_path = V07_ROOT / "COUNTERFACTUAL_MANIFEST_SCHEMA.json"
    assert preregistration_path.is_file()
    assert strategy_path.is_file()
    assert schema_path.is_file()

    assert _sha256(preregistration_path) == EXPECTED_PREREGISTRATION_SHA256
    preregistration = _load_json(preregistration_path)
    assert preregistration["schema_version"] == (
        "btc-5m-15m-relative-value-preregistration.v07-draft"
    )
    assert preregistration["deployment_status"] == ("not_deployed_counterfactual_draft")
    assert preregistration["source_baseline"] == (
        "c557160d0c98e195a988f4353bbe19a3b00b3576"
    )
    assert preregistration["drafted_at"] == "2026-08-15T15:37:51Z"
    draft_context = preregistration["draft_context"]
    assert isinstance(draft_context, dict)
    assert draft_context["created_after_known_v06_shadow_results"] is True
    assert draft_context["revision"] == (
        "v7_windows_portability_and_trust_boundary_correction"
    )
    assert draft_context["known_v06_settled_attempts"] == 48
    assert (
        draft_context["provides_ex_ante_qualification_for_known_v06_results"] is False
    )
    scope = preregistration["scope"]
    assert isinstance(scope, dict)
    assert scope["settlement_model_id"] == SETTLEMENT_MODEL_ID
    assert scope["model_version"] == MODEL_VERSION
    assert scope["paper_only"] is True
    assert scope["live_orders_disabled"] is True

    strategy_spec = preregistration["strategy_spec"]
    assert isinstance(strategy_spec, dict)
    assert strategy_spec["path"] == "STRATEGY_SPEC.md"
    assert strategy_spec["sha256"] == _sha256(strategy_path)

    model = preregistration["model"]
    assert isinstance(model, dict)
    assert model == SharedTerminalModelConfig().to_document()
    assert preregistration["model_config_sha256"] == (
        SharedTerminalModelConfig().artifact_hash
    )
    uncertainty = preregistration["model_uncertainty_qualification"]
    assert isinstance(uncertainty, dict)
    assert uncertainty["inverse_sqrt_n_paths_term"] is False
    baseline = preregistration["binding_market_baseline"]
    assert isinstance(baseline, dict)
    assert baseline["target_quantity_shares"] == "5"
    assert baseline["shared_terminal_projection"] is True
    lock_policy = preregistration["prelabel_lock_policy"]
    assert isinstance(lock_policy, dict)
    assert lock_policy["missing_journal_status"] == "counterfactual_insufficient"
    assert lock_policy["test_universe_binds_expiry_and_pair_identities"] is True
    assert lock_policy["public_evaluator_qualification_authority"] is False
    assert lock_policy["builder_authoritative_revalidation_required"] is True
    assert (
        lock_policy["generic_caller_rows_plus_chain_authority_issuer_distributed"]
        is False
    )
    assert lock_policy["trust_model"] == (
        "api_misuse_guard_not_cryptographic_provenance"
    )
    assert lock_policy["adversarial_provenance_claimed"] is False
    assert (
        lock_policy["prediction_lock_and_receipt_strictly_before_common_expiry"] is True
    )
    assert (
        lock_policy["forecast_available_at_ms_equals_prediction_receipt_received_at_ms"]
        is True
    )
    assert lock_policy["counterfactual_computation_availability_delay_ms"] == 5000
    builder_policy = preregistration["builder_verified_evidence_policy"]
    assert isinstance(builder_policy, dict)
    assert builder_policy["public_evaluator_can_grant_qualification"] is False
    assert builder_policy["generated_fixture_builder_authority_allowed"] is False
    assert (
        builder_policy[
            "generic_caller_rows_plus_caller_chain_issuer_helpers_distributed"
        ]
        is False
    )
    assert builder_policy["trust_model"] == (
        "api_misuse_guard_not_cryptographic_provenance"
    )
    assert (
        builder_policy["external_signature_or_third_party_timestamp_present"]
        is False
    )
    assert builder_policy["adversarial_provenance_claimed"] is False
    assert builder_policy["arbitrary_local_code_execution_out_of_scope"] is True
    assert builder_policy["synthetic_gate_math_can_emit_true_edge"] is False
    assert builder_policy["synthetic_gate_math_can_emit_qualified_pnl"] is False
    edge_policy = preregistration["edge_basis_policy"]
    assert isinstance(edge_policy, dict)
    assert edge_policy["structural_requires_strictly_positive_floor"] is True
    assert edge_policy["predictive_qualification_excludes_structural_rows"] is True
    assert edge_policy["structural_true_edge_gate_implemented"] is True
    assert (
        edge_policy["structural_evaluated_before_monte_carlo_and_predictive_readiness"]
        is True
    )
    assert edge_policy["structural_depends_on_shrinkage_or_validation_veto"] is False
    assert edge_policy["structural_scan_requires_monte_carlo"] is False
    assert edge_policy["structural_scan_requires_predictor_history"] is False
    quantity_policy = preregistration["quantity_selection_policy"]
    assert isinstance(quantity_policy, dict)
    assert quantity_policy["quantity_is_optimization_variable"] is True
    assert quantity_policy["risk_budget_role"] == "upper_bound_not_target"
    structural_policy = preregistration["structural_primitive_policy"]
    assert isinstance(structural_policy, dict)
    assert structural_policy["monte_carlo_distribution_required"] is False
    assert structural_policy["predictor_history_required"] is False
    metadata = preregistration["causal_metadata_policy"]
    assert isinstance(metadata, dict)
    assert metadata["capture_config_schema_version"] == (
        builder.CAPTURE_CONFIG_SCHEMA_VERSION
    )
    assert metadata["capture_config_required_true_fields"] == [
        "paper_only",
        "public_only",
        "new_orders_disabled",
    ]
    assert metadata["capture_config_generated_fixture_explicit_bool_required"] is True
    assert metadata["prediction_receipt_strictly_before_common_expiry"] is True
    assert metadata["counterfactual_computation_availability_delay_ms"] == 5000
    assert metadata["counterfactual_timing_is_measured_production_runtime"] is False
    split_policy = preregistration["split_policy"]
    assert isinstance(split_policy, dict)
    assert split_policy["independent_cluster_key"] == (
        "sha256_capture_derived_common_expiry_ms_only"
    )
    assert split_policy["one_pair_per_common_expiry"] == "required"
    concentration = preregistration["true_edge_gates"]
    assert isinstance(concentration, dict)
    assert concentration["binding_concentration_denominator"] == "total_net_pnl"
    assert concentration["independent_predictive_and_structural_gates"] is True
    assert (
        concentration[
            "mixed_predictive_and_structural_rows_cannot_combine_for_sample_gate"
        ]
        is True
    )
    assert (
        concentration["structural_gate_requires_brier_ece_or_model_neighborhood"]
        is False
    )
    assert concentration["builder_authoritative_supported_entry_point"] == (
        "build_counterfactual_report"
    )
    assert (
        concentration[
            "builder_authority_is_api_misuse_guard_not_cryptographic_provenance"
        ]
        is True
    )
    assert concentration["arbitrary_local_code_execution_out_of_scope"] is True
    assert concentration["external_unforgeable_receipt_present"] is False
    assert concentration["adversarial_provenance_claimed"] is False
    user_check = preregistration["non_promotional_user_check"]
    assert (
        user_check[
            "positive_net_pnl_user_check_requires_builder_authoritative_revalidation"
        ]
        is True
    )
    assert user_check["synthetic_or_generated_fixture_can_pass_user_check"] is False
    timing = preregistration["timing_availability_policy"]
    assert timing["verified_receipt_strictly_before_common_expiry"] is True
    assert timing["counterfactual_computation_availability_delay_ms"] == 5000
    assert timing["counterfactual_delay_is_measured_production_runtime"] is False

    strategy_text = strategy_path.read_text(encoding="utf-8")
    assert "contains no `1 / sqrt(n_paths)` term" in strategy_text
    assert "Top-of-book ask normalization is retained only" in strategy_text
    assert "event_cluster_id` is only a human-readable alias" in strategy_text
    assert "v07-common-expiry-sha256:" in strategy_text
    assert "Capture configuration is fail-closed" in strategy_text
    assert "Builder-authoritative supported API and trust boundary" in strategy_text
    assert "API misuse guard" in strategy_text
    assert "not a cryptographic or adversarial provenance system" in strategy_text
    assert "explicit residual threat" in strategy_text
    assert "structural_net_floor_per_pair" in strategy_text
    assert "after the v0.6 development-shadow result of 48" in strategy_text
    assert "forecast_available_at_ms" in strategy_text
    assert "strictly earlier than common expiry" in strategy_text
    assert "5,000ms" in strategy_text
    assert "non-economic mechanism diagnostic" in strategy_text
    assert "structural candidates first" in strategy_text
    assert "Quantity is an optimization variable" in strategy_text
    assert "uses no Monte Carlo scenario" in strategy_text
    assert "independent fail-closed structural true-edge gate" in strategy_text
    assert "counterfactual_insufficient" in strategy_text

    schema = _load_json(schema_path)
    assert schema["$id"] == MANIFEST_SCHEMA_VERSION
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert "cases" in properties
    case_properties = properties["cases"]["items"]["properties"]
    assert "expiry_ms" not in case_properties


def test_frozen_v05_v06_research_assets_retain_exact_hashes() -> None:
    actual = {
        relative_path: _sha256(PROJECT_ROOT / relative_path)
        for relative_path in FROZEN_RESEARCH_HASHES
    }
    assert actual == FROZEN_RESEARCH_HASHES


def test_v07_builder_has_no_network_or_order_submission_imports() -> None:
    tree = ast.parse(BUILDER_PATH.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint(
        {
            "aiohttp",
            "boto3",
            "httpx",
            "py_clob_client",
            "requests",
            "socket",
            "urllib",
            "websockets",
        }
    )
    source = BUILDER_PATH.read_text(encoding="utf-8")
    assert "assert_new_orders_disabled()" in source
    assert 'network_calls_made_by_builder": 0' in source
    for forbidden in (
        "create_order(",
        "post_order(",
        "submit_order(",
        "sign_transaction(",
    ):
        assert forbidden not in source


def test_v07_builder_cli_help_is_available() -> None:
    completed = subprocess.run(
        [sys.executable, str(BUILDER_PATH), "--help"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "--manifest" in completed.stdout
    assert "--output" in completed.stdout


def test_v07_builder_fails_closed_when_raw_capture_is_absent(
    tmp_path: Path,
) -> None:
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "preregistration_path": str(V07_ROOT / "PREREGISTRATION.json"),
        "cases": [
            {
                "event_cluster_id": "missing-capture-cluster",
                "canonical_event_cluster_id": ("v07-expiry-pair-sha256:" + "0" * 64),
                "split": "train",
                "capture_root": "missing-capture",
                "predictor_root": "missing-predictor",
                "capture_config_path": "missing-config.json",
                "history_roots": [],
                "decision_tau_seconds": [120],
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="capture root does not exist"):
        build_counterfactual_report(manifest_path=manifest_path)


def test_v07_builder_rejects_a_tampered_preregistered_gate(tmp_path: Path) -> None:
    strategy_copy = tmp_path / "STRATEGY_SPEC.md"
    strategy_copy.write_bytes((V07_ROOT / "STRATEGY_SPEC.md").read_bytes())
    preregistration = _load_json(V07_ROOT / "PREREGISTRATION.json")
    user_check = preregistration["non_promotional_user_check"]
    assert isinstance(user_check, dict)
    user_check["minimum_distinct_settled_expiry_clusters"] = 99
    preregistration_path = tmp_path / "PREREGISTRATION.json"
    preregistration_path.write_text(
        json.dumps(preregistration, sort_keys=True),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "preregistration_path": str(preregistration_path),
        "cases": [
            {
                "event_cluster_id": "missing-capture-cluster",
                "canonical_event_cluster_id": ("v07-expiry-pair-sha256:" + "0" * 64),
                "split": "train",
                "capture_root": "missing-capture",
                "predictor_root": "missing-predictor",
                "capture_config_path": "missing-config.json",
                "decision_tau_seconds": [120],
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="preregistration hash mismatch",
    ):
        build_counterfactual_report(manifest_path=manifest_path)


def test_boundary_shrinkage_weight_has_only_true_neighbor_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run_setting(**kwargs: object) -> str:
        setting_id = kwargs["setting_id"]
        assert isinstance(setting_id, str)
        captured.append(setting_id)
        return setting_id

    monkeypatch.setattr(builder, "_run_setting", fake_run_setting)
    primary = SimpleNamespace(
        model=SharedTerminalModelConfig(n_paths=100),
        shrinkage=SimpleNamespace(model_weight=Decimal("0")),
    )
    sensitivity = builder._SensitivityGrid(
        volatility_floor_offsets=(Decimal("-0.05"), Decimal("0.05")),
        model_weight_offsets=(Decimal("-0.25"), Decimal("0.25")),
    )

    results = builder._neighbor_settings(
        primary=primary,
        contexts=(),
        strategy=builder.V07StrategyConfig(),
        candidate_weights=(Decimal("0"), Decimal("0.25")),
        sensitivity=sensitivity,
    )

    assert results == tuple(captured)
    assert captured == [
        "volatility_floor_0.20",
        "volatility_floor_0.30",
        "model_weight_0.25",
    ]
