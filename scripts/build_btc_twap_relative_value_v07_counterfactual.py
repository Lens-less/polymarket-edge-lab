#!/usr/bin/env python3
"""Build a deterministic v0.7 counterfactual report from immutable captures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_btc_twap_relative_value_pilot_report import (  # noqa: E402
    _assert_clean_integrity,
    _combined_series,
    _exact_observation,
    _iso_to_epoch_ms,
    _json_string_list,
    _latest_before,
    _opening_event_id,
    _pair_from_capture_targets,
    _receipt_clock_offset_ms,
    _records,
    _resample_one_second,
    _resolution_event_validation,
    _series,
)
from src.edge_lab.btc_twap_relative_value import (  # noqa: E402
    PairAction,
    PairExecutionStatus,
    PairSettlementState,
    SameExpiryPair,
    TimedPrice,
)
from src.edge_lab.btc_twap_relative_value_replay import (  # noqa: E402
    BookReplayToken,
    CausalBookReplay,
    paper_execution_diagnostics,
)
from src.edge_lab.btc_twap_relative_value_v07 import (  # noqa: E402
    MODEL_VERSION,
    SETTLEMENT_MODEL_ID,
    ProbabilityPairTrainingRow,
    SharedTerminalDataHealth,
    SharedTerminalModelConfig,
    SharedTerminalSimulationResult,
    TrainOnlyShrinkageArtifact,
    V07EdgeBasis,
    V07ForecastAvailabilityBasis,
    V07ModelRejection,
    V07StrategyConfig,
    ValidationVetoEvidence,
    canonical_event_cluster_id,
    canonical_expiry_cluster_id,
    evaluate_validation_veto,
    executable_binary_ask_probability,
    raw_top_ask_probability,
    simulate_shared_terminal_twap_60_distribution,
)
from src.edge_lab.btc_twap_relative_value_v07_evaluation import (  # noqa: E402
    MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS,
    MINIMUM_SETTLED_CLUSTERS,
    LockedOOSEconomicAttempt,
    LockedOOSForecastRow,
    ParameterNeighborhoodEvidence,
    PreLabelLockProvenance,
    PreLabelLockStatus,
    V07EvidenceStatus,
    V07LockedOOSEvaluation,
    evaluate_gate_mechanism_diagnostic,
    evaluate_locked_oos_evidence,
)
from src.edge_lab.btc_twap_relative_value_v07_replay import (  # noqa: E402
    V07PaperCycleEvaluation,
    evaluate_shared_terminal_paper_cycle,
)
from src.edge_lab.compatibility import (  # noqa: E402
    LiveExecutionBlocked,
    assert_new_orders_disabled,
)
from src.edge_lab.data_store import canonical_json_bytes  # noqa: E402
from src.edge_lab.network_safety import high_confidence_secret_findings  # noqa: E402
from src.edge_lab.settlement_regime import (  # noqa: E402
    V06_SETTLEMENT_REGIME_ID,
    canonicalize_resolution_source,
)

MANIFEST_SCHEMA_VERSION = "btc-5m-15m-v07-counterfactual-manifest.v2"
REPORT_SCHEMA_VERSION = "btc-5m-15m-v07-counterfactual-report.v8"
LOCK_JOURNAL_SCHEMA_VERSION = "btc-5m-15m-v07-lock-journal.v4"
LOCK_JOURNAL_SOURCE = "v07_locked_oos_journal"
CAPTURE_CONFIG_SCHEMA_VERSION = "btc-5m-15m-relative-value-capture.v1"
SOURCE_BASELINE = "c557160d0c98e195a988f4353bbe19a3b00b3576"
EXPECTED_PREREGISTRATION_SHA256 = (
    "de79f3e4d43b513a7c71e3196877ee110f04f7d31cec4b3cd060f6184f541bfe"
)
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "preregistration_path",
        "prelabel_lock_journal_root",
        "cases",
    }
)
_REQUIRED_MANIFEST_KEYS = frozenset({"schema_version", "preregistration_path", "cases"})
_CASE_KEYS = frozenset(
    {
        "event_cluster_id",
        "canonical_event_cluster_id",
        "split",
        "capture_root",
        "predictor_root",
        "capture_config_path",
        "history_roots",
        "decision_tau_seconds",
    }
)
_REQUIRED_CASE_KEYS = frozenset(
    {
        "event_cluster_id",
        "canonical_event_cluster_id",
        "split",
        "capture_root",
        "predictor_root",
        "capture_config_path",
        "decision_tau_seconds",
    }
)
_FORBIDDEN_PROXY_KEYS = frozenset(
    {
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "proxies",
        "proxy",
        "proxy_url",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_identity(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise ValueError(f"capture root does not exist: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"immutable capture tree contains symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(
                f"immutable capture tree contains non-regular file: {path}"
            )
        files.append(path)
    payload = [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in files
    ]
    return {
        "schema_version": "immutable-capture-tree.v1",
        "file_count": len(payload),
        "tree_sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
    }


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json_bytes(document) + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(nested) for key, nested in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return tuple(_deep_freeze(nested) for nested in value)
    return value


def _json_document(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_document(nested) for key, nested in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_json_document(nested) for nested in value]
    return value


def _assert_exact_keys(
    document: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    label: str,
) -> None:
    keys = set(document)
    extra = sorted(keys - allowed)
    missing = sorted(required - keys)
    if extra:
        raise ValueError(f"{label} contains unsupported keys: {', '.join(extra)}")
    if missing:
        raise ValueError(f"{label} is missing required keys: {', '.join(missing)}")


def _forbidden_proxy_paths(value: Any, *, path: str = "$") -> tuple[str, ...]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            nested_path = f"{path}.{key}"
            if normalized in _FORBIDDEN_PROXY_KEYS and nested not in (
                None,
                "",
                [],
                {},
            ):
                findings.append(nested_path)
            findings.extend(_forbidden_proxy_paths(nested, path=nested_path))
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for index, nested in enumerate(value):
            findings.extend(_forbidden_proxy_paths(nested, path=f"{path}[{index}]"))
    return tuple(findings)


def _confirm_public_capture_config(document: Mapping[str, Any]) -> None:
    findings = high_confidence_secret_findings(document)
    if findings:
        paths = ", ".join(sorted({item["path"] for item in findings}))
        raise ValueError(
            f"capture config contains forbidden credential material at {paths}"
        )
    proxy_paths = _forbidden_proxy_paths(document)
    if proxy_paths:
        raise ValueError(
            "capture config contains forbidden proxy configuration at "
            + ", ".join(sorted(set(proxy_paths)))
        )
    schema_version = document.get("schema_version")
    if type(schema_version) is not str or schema_version != (
        CAPTURE_CONFIG_SCHEMA_VERSION
    ):
        raise ValueError(
            f"capture config schema_version must equal {CAPTURE_CONFIG_SCHEMA_VERSION}"
        )
    for key in ("paper_only", "public_only", "new_orders_disabled"):
        if key not in document or document[key] is not True:
            raise ValueError(f"capture config must explicitly set {key}=true")
    if "generated_fixture" not in document:
        raise ValueError("capture config must explicitly declare generated_fixture")
    if type(document["generated_fixture"]) is not bool:
        raise TypeError("capture config generated_fixture must be bool")


def _path(base: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty path")
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else base / candidate).resolve()


def _int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{label} must be exact decimal")
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return value


def _confirm_paper_only_boundary(document: Mapping[str, Any]) -> None:
    findings = high_confidence_secret_findings(document)
    if findings:
        paths = ", ".join(sorted({item["path"] for item in findings}))
        raise ValueError(f"manifest contains forbidden credential material at {paths}")
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("live-order safety guard did not fail closed")


def _model_config(document: Mapping[str, Any]) -> SharedTerminalModelConfig:
    return SharedTerminalModelConfig(
        minimum_history_seconds=_int(
            document["minimum_history_seconds"],
            label="model.minimum_history_seconds",
        ),
        n_paths=_int(document["n_paths"], label="model.n_paths"),
        seed=_int(document["seed"], label="model.seed"),
        annualized_volatility_floor=_decimal(
            document["annualized_volatility_floor"],
            label="model.annualized_volatility_floor",
        ),
        return_intervals_seconds=tuple(
            _int(value, label="model.return_intervals_seconds[]")
            for value in document["return_intervals_seconds"]
        ),
        student_t_df=_int(document["student_t_df"], label="model.student_t_df"),
        uncertainty_scales=tuple(
            _decimal(value, label="model.uncertainty_scales[]")
            for value in document["uncertainty_scales"]
        ),
        uncertainty_weights=tuple(
            _decimal(value, label="model.uncertainty_weights[]")
            for value in document["uncertainty_weights"]
        ),
        jeffreys_alpha=_decimal(
            document["jeffreys_alpha"],
            label="model.jeffreys_alpha",
        ),
        maximum_predictor_staleness_ms=_int(
            document["maximum_predictor_staleness_ms"],
            label="model.maximum_predictor_staleness_ms",
        ),
    )


def _strategy_config(document: Mapping[str, Any]) -> V07StrategyConfig:
    return V07StrategyConfig(
        tau_min_seconds=_int(document["tau_min_seconds"], label="tau_min_seconds"),
        tau_max_seconds=_int(document["tau_max_seconds"], label="tau_max_seconds"),
        maximum_spread_each_leg=_decimal(
            document["maximum_spread_each_leg"],
            label="maximum_spread_each_leg",
        ),
        maximum_chainlink_staleness_ms=_int(
            document["maximum_chainlink_staleness_ms"],
            label="maximum_chainlink_staleness_ms",
        ),
        maximum_book_staleness_ms=_int(
            document["maximum_book_staleness_ms"],
            label="maximum_book_staleness_ms",
        ),
        maximum_clock_drift_ms=_int(
            document["maximum_clock_drift_ms"],
            label="maximum_clock_drift_ms",
        ),
        minimum_market_price=_decimal(
            document["minimum_market_price"],
            label="minimum_market_price",
        ),
        maximum_market_price=_decimal(
            document["maximum_market_price"],
            label="maximum_market_price",
        ),
        pair_risk_usdc=_decimal(
            document["pair_risk_usdc"],
            label="pair_risk_usdc",
        ),
        market_baseline_quantity=_decimal(
            document["market_baseline_quantity"],
            label="market_baseline_quantity",
        ),
        minimum_net_expected_pnl_per_pair=_decimal(
            document["minimum_net_expected_pnl_per_pair"],
            label="minimum_net_expected_pnl_per_pair",
        ),
        uncertainty_multiplier=_decimal(
            document["uncertainty_multiplier"],
            label="uncertainty_multiplier",
        ),
        cvar_tail_probability=_decimal(
            document["cvar_tail_probability"],
            label="cvar_tail_probability",
        ),
        max_leg_delay_ms=_int(
            document["max_leg_delay_ms"],
            label="max_leg_delay_ms",
        ),
        counterfactual_computation_availability_delay_ms=_int(
            document["counterfactual_computation_availability_delay_ms"],
            label="counterfactual_computation_availability_delay_ms",
        ),
        initial_cash=_decimal(document["initial_cash"], label="initial_cash"),
        settlement_regime=str(document["settlement_regime"]),
    )


@dataclass(frozen=True)
class _SensitivityGrid:
    volatility_floor_offsets: tuple[Decimal, ...]
    model_weight_offsets: tuple[Decimal, ...]


def _verify_preregistration(
    preregistration: Mapping[str, Any],
    *,
    preregistration_path: Path,
) -> tuple[
    SharedTerminalModelConfig,
    V07StrategyConfig,
    tuple[Decimal, ...],
    _SensitivityGrid,
]:
    if _sha256(preregistration_path) != EXPECTED_PREREGISTRATION_SHA256:
        raise ValueError("v0.7 preregistration hash mismatch")
    if preregistration.get("schema_version") != (
        "btc-5m-15m-relative-value-preregistration.v07-draft"
    ):
        raise ValueError("unsupported v0.7 preregistration schema")
    if preregistration.get("deployment_status") != (
        "not_deployed_counterfactual_draft"
    ):
        raise ValueError("v0.7 preregistration must remain an undeployed draft")
    if preregistration.get("source_baseline") != SOURCE_BASELINE:
        raise ValueError("v0.7 preregistration source baseline mismatch")
    if preregistration.get("drafted_at") != "2026-08-15T15:37:51Z":
        raise ValueError("v0.7 preregistration draft timestamp mismatch")
    draft_context = _mapping(
        preregistration.get("draft_context"),
        label="preregistration.draft_context",
    )
    if dict(draft_context) != {
        "revision": "v7_windows_portability_and_trust_boundary_correction",
        "created_after_known_v06_shadow_results": True,
        "known_v06_settled_attempts": 48,
        "known_v06_independent_expiry_clusters": 24,
        "known_v06_net_pnl_usdc": "87.523635",
        "known_v06_results_role": ("known_before_v7_draft_not_locked_oos_input"),
        "provides_ex_ante_qualification_for_known_v06_results": False,
    }:
        raise ValueError("v0.7 preregistration draft context mismatch")
    scope = _mapping(preregistration.get("scope"), label="preregistration.scope")
    if (
        scope.get("paper_only") is not True
        or scope.get("live_orders_disabled") is not True
        or scope.get("credentials_allowed") is not False
        or scope.get("authenticated_endpoints_allowed") is not False
        or scope.get("proxy_urls_allowed") is not False
        or scope.get("settlement_regime") != V06_SETTLEMENT_REGIME_ID
        or scope.get("settlement_model_id") != SETTLEMENT_MODEL_ID
        or scope.get("model_version") != MODEL_VERSION
    ):
        raise ValueError("v0.7 preregistration safety/model scope is invalid")
    strategy_spec = _mapping(
        preregistration.get("strategy_spec"),
        label="preregistration.strategy_spec",
    )
    spec_path = _path(
        preregistration_path.parent,
        strategy_spec.get("path"),
        label="strategy_spec.path",
    )
    if _sha256(spec_path) != strategy_spec.get("sha256"):
        raise ValueError("v0.7 strategy specification hash mismatch")
    model = _model_config(
        _mapping(preregistration.get("model"), label="preregistration.model")
    )
    if model.artifact_hash != preregistration.get("model_config_sha256"):
        raise ValueError("v0.7 model configuration hash mismatch")
    strategy = _strategy_config(
        _mapping(
            preregistration.get("frozen_strategy"),
            label="preregistration.frozen_strategy",
        )
    )
    shrinkage = _mapping(
        preregistration.get("train_only_shrinkage"),
        label="preregistration.train_only_shrinkage",
    )
    weights = tuple(
        _decimal(value, label="candidate_model_weights[]")
        for value in shrinkage.get("candidate_model_weights", ())
    )
    if not weights or len(set(weights)) != len(weights):
        raise ValueError("preregistration has no unique shrinkage candidate grid")
    if (
        shrinkage.get("selection_metric") != ("cluster_equal_mean_of_two_horizon_brier")
        or shrinkage.get("tie_break") != "lowest_model_weight"
    ):
        raise ValueError("preregistration shrinkage selection policy mismatch")

    split_policy = _mapping(
        preregistration.get("split_policy"),
        label="preregistration.split_policy",
    )
    expected_split_policy = {
        "method": "chronological_common_expiry_disjoint",
        "shuffle": False,
        "parameters_selected_on": "train_only",
        "validation_role": "veto_only_no_retraining",
        "test_is_locked": True,
        "label_must_predate_fit_or_veto": True,
        "label_available_at_definition": (
            "max_expiry_closing_receipt_and_both_official_resolution_receipts"
        ),
        "independent_cluster_key": ("sha256_capture_derived_common_expiry_ms_only"),
        "pair_integrity_key": (
            "sha256_common_expiry_5m_market_condition_15m_market_condition"
        ),
        "manifest_expiry_ms_role": "forbidden_not_accepted",
        "manifest_event_cluster_id_role": "diagnostic_alias_only_not_counted",
        "one_pair_per_common_expiry": "required",
        "duplicate_common_expiry_across_pair_alias_case_or_split": "reject",
        "prelabel_test_universe_lock_required_for_true_edge": True,
    }
    if dict(split_policy) != expected_split_policy:
        raise ValueError("preregistration split policy mismatch")

    metadata_policy = _mapping(
        preregistration.get("causal_metadata_policy"),
        label="preregistration.causal_metadata_policy",
    )
    if dict(metadata_policy) != {
        "rules_and_fee_snapshots_received_by": (
            "earliest_decision_at_ms_per_expiry_cluster"
        ),
        "capture_config_path": "capture_root/capture-config.json",
        "capture_config_sha256_recorded": True,
        "capture_config_schema_version": CAPTURE_CONFIG_SCHEMA_VERSION,
        "capture_config_required_true_fields": [
            "paper_only",
            "public_only",
            "new_orders_disabled",
        ],
        "capture_config_generated_fixture_explicit_bool_required": True,
        "capture_started_no_later_than_earliest_decision": True,
        "reconstruct_and_verify_rule_hash": True,
        "official_resolution_receipt_not_before_expiry": True,
        "both_official_resolutions_required": True,
        "exact_taker_delay_ms": 250,
        "missing_required_execution_surface": "abort_case",
        "pair_integrity_identity": ("common_expiry_plus_both_market_and_condition_ids"),
        "independent_cluster_identity": "capture_derived_common_expiry_ms_only",
        "one_pair_per_common_expiry": True,
        (
            "prelabel_lock_journal_optional_for_counterfactual_"
            "but_required_for_true_edge"
        ): True,
        "actionable_decision_reconciliation": "one_row_or_invalid_window",
        "prediction_receipt_strictly_before_common_expiry": True,
        "forecast_available_at_ms_verified_source": (
            "immutable_prediction_receipt_received_at_ms"
        ),
        "backdated_prediction_locked_at_cannot_substitute_receipt_clock": True,
        "first_execution_eligibility": (
            "forecast_available_at_ms_plus_captured_taker_delay_ms"
        ),
        "counterfactual_computation_availability_delay_ms": 5000,
        "counterfactual_timing_is_measured_production_runtime": False,
        "effective_signal_to_execution_latency_reported": True,
    }:
        raise ValueError("preregistration causal metadata policy mismatch")

    sensitivity = _mapping(
        preregistration.get("sensitivity_grid"),
        label="preregistration.sensitivity_grid",
    )
    expected_sensitivity = {
        "diagnostic_only": True,
        "action_changing": False,
        "volatility_floor_offsets": ["-0.05", "+0.05"],
        "model_weight_offsets": ["-0.25", "+0.25"],
        "common_random_numbers_across_model_configs": True,
        "boundary_policy": "omit_out_of_range_neighbors_no_duplicate_primary",
    }
    if dict(sensitivity) != expected_sensitivity:
        raise ValueError("preregistration sensitivity grid mismatch")
    volatility_offsets = tuple(
        _decimal(value, label="volatility_floor_offsets[]")
        for value in sensitivity["volatility_floor_offsets"]
    )
    model_weight_offsets = tuple(
        _decimal(value, label="model_weight_offsets[]")
        for value in sensitivity["model_weight_offsets"]
    )

    user_check = _mapping(
        preregistration.get("non_promotional_user_check"),
        label="preregistration.non_promotional_user_check",
    )
    if dict(user_check) != {
        "minimum_distinct_settled_expiry_clusters": MINIMUM_SETTLED_CLUSTERS,
        "settled_expiry_cluster_definition": (
            "distinct_capture_derived_common_expiries_with_at_least_one_"
            "explainable_economic_attempt"
        ),
        "minimum_explainable_locked_oos_economic_attempts": (
            MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS
        ),
        "net_pnl_must_be_positive": True,
        "diagnostic_positive_sample_pnl_field": ("diagnostic_positive_sample_pnl"),
        "positive_net_pnl_user_check_requires_builder_authoritative_revalidation": (
            True
        ),
        "synthetic_or_generated_fixture_can_pass_user_check": False,
    }:
        raise ValueError("preregistration non-promotional sample gate mismatch")

    true_edge = _mapping(
        preregistration.get("true_edge_gates"),
        label="preregistration.true_edge_gates",
    )
    if hashlib.sha256(canonical_json_bytes(dict(true_edge))).hexdigest() != (
        "7e126171f61729522ec380ae067a279a330eb3a961a2059400b2bffda43079fb"
    ):
        raise ValueError("preregistration true-edge gate mismatch")

    model_uncertainty = _mapping(
        preregistration.get("model_uncertainty_qualification"),
        label="preregistration.model_uncertainty_qualification",
    )
    if dict(model_uncertainty) != {
        "method": "fixed_volatility_scale_conditional_ev_downside",
        "conditional_scales": ["0.75", "1", "1.5"],
        "downside": "max_0_mixture_net_ev_minus_minimum_fixed_scale_net_ev",
        "adjusted_edge": ("mixture_net_ev_minus_uncertainty_multiplier_times_downside"),
        "monte_carlo_standard_error_penalty": False,
        "inverse_sqrt_n_paths_term": False,
    }:
        raise ValueError("preregistration model-uncertainty policy mismatch")

    market_baseline = _mapping(
        preregistration.get("binding_market_baseline"),
        label="preregistration.binding_market_baseline",
    )
    if dict(market_baseline) != {
        "name": "coherent_depth_fee_executable_market",
        "target_quantity_shares": "5",
        "depth_method": "walk_decision_time_asks_to_frozen_quantity",
        "fee_method": "captured_dynamic_taker_fee_per_fill_level",
        "normalization": "all_in_up_cost_over_all_in_up_plus_down_cost",
        "shared_terminal_projection": True,
        "top_of_book_ask_probability_role": "diagnostic_only",
        "strict_brier_improvement_required": True,
    }:
        raise ValueError("preregistration binding-market policy mismatch")

    builder_evidence_policy = _mapping(
        preregistration.get("builder_verified_evidence_policy"),
        label="preregistration.builder_verified_evidence_policy",
    )
    if hashlib.sha256(
        canonical_json_bytes(dict(builder_evidence_policy))
    ).hexdigest() != (
        "100c82349f212aa1f7b273fd171ae2d4c5f080c0e6bc7de6b3cc4f1a5c061612"
    ):
        raise ValueError("preregistration builder-evidence policy mismatch")

    edge_basis_policy = _mapping(
        preregistration.get("edge_basis_policy"),
        label="preregistration.edge_basis_policy",
    )
    if hashlib.sha256(canonical_json_bytes(dict(edge_basis_policy))).hexdigest() != (
        "5313caab621031e5958c4827b267d7241021bc4e1c8e223ae07fb88541d52230"
    ):
        raise ValueError("preregistration edge-basis policy mismatch")

    quantity_policy = _mapping(
        preregistration.get("quantity_selection_policy"),
        label="preregistration.quantity_selection_policy",
    )
    if dict(quantity_policy) != {
        "schema_version": "btc-5m-15m-v07-quantity-selection-policy.v1",
        "quantity_is_optimization_variable": True,
        "candidate_breakpoints": [
            "common_minimum_order_size",
            ("each_jointly_executable_cumulative_ask_depth_break_on_either_leg"),
            ("exact_six_decimal_risk_budget_boundary_with_actual_fee_rounding"),
        ],
        "equal_quantity_required_on_both_legs": True,
        "full_depth_walk_required": True,
        "fee_method": (
            "captured_dynamic_taker_fee_per_fill_level_with_actual_rounding"
        ),
        "risk_budget_role": "upper_bound_not_target",
        "structural_eligibility": (
            "strictly_positive_fee_inclusive_floor_and_full_two_leg_execution"
        ),
        "structural_objective": (
            "maximize_quantity_times_structural_net_floor_per_pair"
        ),
        "predictive_eligibility": (
            "per_pair_minimum_edge_and_positive_uncertainty_adjusted_total_pnl"
        ),
        "predictive_objective": "maximize_uncertainty_adjusted_total_pnl",
        "stable_tie_break": [
            "higher_total_objective",
            "higher_per_pair_qualification_edge",
            "lower_quantity",
            "lexicographic_action_id",
        ],
        "audit_fields": [
            "quantity_selection_basis",
            "quantity_candidate_breakpoint_count",
            "selected_guaranteed_total_pnl",
            "selected_uncertainty_adjusted_total_pnl",
        ],
    }:
        raise ValueError("preregistration quantity-selection policy mismatch")

    structural_policy = _mapping(
        preregistration.get("structural_primitive_policy"),
        label="preregistration.structural_primitive_policy",
    )
    if dict(structural_policy) != {
        "schema_version": "btc-5m-15m-v07-structural-primitive-policy.v1",
        "inputs": [
            "verified_shared_terminal_settlement_rule",
            "captured_strike_5",
            "captured_strike_15",
            "decision_time_books",
            "captured_dynamic_fees",
            "common_safety_and_timing_state",
        ],
        "feasible_outcomes_source": (
            "strike_ordering_under_one_shared_terminal_twap_60"
        ),
        "monte_carlo_distribution_required": False,
        "predictor_history_required": False,
        "volatility_shrinkage_brier_or_validation_veto_required": False,
        "builder_scan_order": (
            "common_safety_then_structural_then_predictive_simulation"
        ),
        "positive_structural_candidate_survives_predictive_model_rejection": True,
        "nonstructural_without_predictive_distribution": "no_trade_fail_closed",
        "structural_probability_and_brier_fields": ("explicit_non_applicable_null"),
    }:
        raise ValueError("preregistration structural-primitive policy mismatch")

    lock_policy = _mapping(
        preregistration.get("prelabel_lock_policy"),
        label="preregistration.prelabel_lock_policy",
    )
    if dict(lock_policy) != {
        "journal_source": "v07_prelabel_lock",
        "journal_schema_version": LOCK_JOURNAL_SCHEMA_VERSION,
        "test_universe_receipt_before_earliest_test_decision": True,
        "test_universe_binds_expiry_and_pair_identities": True,
        "prediction_locked_at_equals_decision_at": True,
        "canonical_forecast_and_decision_hashes_required": True,
        "public_evaluator_qualification_authority": False,
        "builder_authoritative_revalidation_required": True,
        "row_set_and_parameter_neighborhood_hash_binding": True,
        "generic_caller_rows_plus_chain_authority_issuer_distributed": False,
        "generated_fixture_builder_authority_allowed": False,
        "trust_model": "api_misuse_guard_not_cryptographic_provenance",
        "adversarial_provenance_claimed": False,
        "arbitrary_local_code_execution_out_of_scope": True,
        "missing_journal_status": "counterfactual_insufficient",
        "qualified_net_pnl_without_verified_lock": None,
        "prediction_lock_and_receipt_strictly_before_common_expiry": True,
        "forecast_available_at_ms_equals_prediction_receipt_received_at_ms": True,
        "backdated_lock_cannot_substitute_receipt_time": True,
        "first_execution_not_before_forecast_available_plus_taker_delay": True,
        "counterfactual_computation_availability_delay_ms": 5000,
        "counterfactual_timing_is_production_runtime_validation": False,
    }:
        raise ValueError("preregistration pre-label lock policy mismatch")

    timing_policy = _mapping(
        preregistration.get("timing_availability_policy"),
        label="preregistration.timing_availability_policy",
    )
    if dict(timing_policy) != {
        "schema_version": "btc-5m-15m-v07-timing-availability-policy.v1",
        "verified_forecast_available_source": (
            "immutable_prediction_receipt_received_at_ms"
        ),
        "verified_receipt_strictly_before_common_expiry": True,
        "first_execution_eligibility": (
            "forecast_available_at_ms_plus_captured_taker_delay_ms"
        ),
        "counterfactual_computation_availability_delay_ms": 5000,
        "counterfactual_delay_is_measured_production_runtime": False,
        "counterfactual_timing_role": "paper_replay_timing_diagnostic_only",
        "effective_signal_to_execution_latency_serialized": True,
    }:
        raise ValueError("preregistration timing-availability policy mismatch")

    reconciliation_policy = _mapping(
        preregistration.get("action_reconciliation_policy"),
        label="preregistration.action_reconciliation_policy",
    )
    if dict(reconciliation_policy) != {
        "every_locked_actionable_decision_reconciled": True,
        "allowed_rows": [
            "settled_fill_partial_unwind_or_failed_unhedged",
            "causal_no_fill_zero_pnl",
            "invalid_window_on_missing_required_surface",
        ],
        "causal_no_fill_counts_as_economic_attempt": False,
        "no_trade_forecast_retained": True,
    }:
        raise ValueError("preregistration action reconciliation policy mismatch")

    return (
        model,
        strategy,
        tuple(sorted(weights)),
        _SensitivityGrid(
            volatility_floor_offsets=volatility_offsets,
            model_weight_offsets=model_weight_offsets,
        ),
    )


def _validate_clock(
    capture_config: Mapping[str, Any],
    *,
    capture_root: Path,
    decision_at_ms: int,
    strategy: V07StrategyConfig,
) -> tuple[int, dict[str, Any]]:
    clock = _mapping(capture_config.get("clock_sync"), label="capture clock_sync")
    if clock.get("schema_version") != "btc-twap-clock-sync.v1":
        raise ValueError("capture clock-sync schema is missing or unsupported")
    if clock.get("system_clock_mutated") is not False:
        raise ValueError("capture clock evidence permits system-clock mutation")
    offset = _int(
        clock.get("causal_receipt_offset_ms"),
        label="clock_sync.causal_receipt_offset_ms",
    )
    uncertainty = _int(
        clock.get("uncertainty_ms"),
        label="clock_sync.uncertainty_ms",
    )
    measured_at_raw = _int(
        clock.get("measured_at_raw_ms"),
        label="clock_sync.measured_at_raw_ms",
    )
    if uncertainty < 0 or uncertainty > strategy.maximum_clock_drift_ms:
        raise ValueError("capture clock uncertainty exceeds the v0.7 maximum")
    measured_at_corrected = measured_at_raw + offset
    measurement_age_ms = decision_at_ms - measured_at_corrected
    if not 0 <= measurement_age_ms <= 1_200_000:
        raise ValueError("capture clock measurement is future or older than 20 minutes")
    if _receipt_clock_offset_ms(capture_root) != offset:
        raise ValueError("capture-root receipt offset differs from capture config")
    return offset, {
        "causal_receipt_offset_ms": offset,
        "uncertainty_ms": uncertainty,
        "measurement_age_ms": measurement_age_ms,
        "system_clock_mutated": False,
    }


@dataclass(frozen=True)
class _RuleSnapshotEvidence:
    raw_rule: Mapping[str, Any]
    received_at_ms: int
    source_event_id: str


def _causal_rule_snapshots(
    *,
    capture_root: Path,
    market_ids: set[str],
    available_by_ms: int,
) -> Mapping[str, _RuleSnapshotEvidence]:
    receipt_offset_ms = _receipt_clock_offset_ms(capture_root)
    latest: dict[str, _RuleSnapshotEvidence] = {}
    for record in _records(capture_root, "rules_http"):
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            continue
        received_at_ms = _iso_to_epoch_ms(
            record.get("received_at"),
            receipt_clock_offset_ms=receipt_offset_ms,
        )
        if received_at_ms > available_by_ms:
            continue
        envelope = record.get("payload")
        snapshot = envelope.get("payload") if isinstance(envelope, Mapping) else None
        responses = snapshot.get("responses") if isinstance(snapshot, Mapping) else None
        if not isinstance(responses, list):
            continue
        for response in responses:
            raw = response.get("raw_json") if isinstance(response, Mapping) else None
            market_id = str(raw.get("id")) if isinstance(raw, Mapping) else ""
            if market_id not in market_ids or not isinstance(raw, Mapping):
                continue
            candidate = _RuleSnapshotEvidence(
                raw_rule=MappingProxyType(dict(raw)),
                received_at_ms=received_at_ms,
                source_event_id=record_id,
            )
            previous = latest.get(market_id)
            if previous is None or candidate.received_at_ms > previous.received_at_ms:
                latest[market_id] = candidate
                continue
            if candidate.received_at_ms == previous.received_at_ms:
                previous_hash = hashlib.sha256(
                    canonical_json_bytes(previous.raw_rule)
                ).hexdigest()
                candidate_hash = hashlib.sha256(
                    canonical_json_bytes(candidate.raw_rule)
                ).hexdigest()
                if candidate_hash != previous_hash:
                    raise ValueError(
                        "conflicting causal public rule snapshots at one receipt time "
                        f"for {market_id}"
                    )
    return MappingProxyType(latest)


def _target_rule_identity(
    target: Mapping[str, Any],
    *,
    description_sha256: str,
) -> dict[str, Any]:
    fee = _mapping(target.get("fee_schedule"), label="target fee_schedule")
    return {
        "schema_version": "btc-twap-market-contract.v1",
        "horizon": str(target["horizon"]),
        "slug": str(target["slug"]),
        "market_id": str(target["market_id"]),
        "condition_id": str(target["condition_id"]).lower(),
        "token_ids": [str(target["up_token_id"]), str(target["down_token_id"])],
        "opens_at_ms": _int(target["opens_at_ms"], label="target opens_at_ms"),
        "closes_at_ms": _int(target["closes_at_ms"], label="target closes_at_ms"),
        "twap_window_seconds": _int(
            target["twap_window_seconds"],
            label="target twap_window_seconds",
        ),
        "source_topic": str(target["source_topic"]),
        "resolution_source": str(target["resolution_source"]),
        "settlement_regime": str(target["settlement_regime"]),
        "description_sha256": description_sha256,
        "tick_size": str(_decimal(target["tick_size"], label="target tick_size")),
        "minimum_order_size": str(
            _decimal(
                target["minimum_order_size"],
                label="target minimum_order_size",
            )
        ),
        "fee_rate": str(_decimal(fee.get("rate"), label="target fee rate")),
        "fee_exponent": str(_decimal(fee.get("exponent"), label="target fee exponent")),
        "fee_taker_only": fee.get("taker_only"),
        "taker_delay_ms": _int(
            target["taker_delay_ms"],
            label="target taker_delay_ms",
        ),
    }


def _validate_rules_and_fees(
    *,
    capture_root: Path,
    targets: Sequence[Mapping[str, Any]],
    available_by_ms: int,
) -> dict[str, Any]:
    market_ids = {str(target["market_id"]) for target in targets}
    latest = _causal_rule_snapshots(
        capture_root=capture_root,
        market_ids=market_ids,
        available_by_ms=available_by_ms,
    )
    result: dict[str, Any] = {}
    for target in targets:
        market_id = str(target["market_id"])
        evidence = latest.get(market_id)
        if evidence is None:
            raise ValueError(
                "causal captured public rule metadata missing by earliest decision "
                f"for {market_id}"
            )
        rule = evidence.raw_rule
        description = rule.get("description")
        if not isinstance(description, str):
            raise ValueError(f"captured rule text missing for {market_id}")
        description_hash = hashlib.sha256(description.encode("utf-8")).hexdigest()
        if description_hash != target.get("rules_text_sha256"):
            raise ValueError(f"captured rule text hash mismatch for {market_id}")
        if (
            rule.get("slug") != target.get("slug")
            or str(rule.get("conditionId", "")).lower()
            != str(target.get("condition_id", "")).lower()
            or _json_string_list(rule.get("outcomes")) != ("Up", "Down")
            or _json_string_list(rule.get("clobTokenIds"))
            != (target.get("up_token_id"), target.get("down_token_id"))
        ):
            raise ValueError(f"captured rule identity mismatch for {market_id}")
        if canonicalize_resolution_source(str(rule.get("resolutionSource"))) != (
            canonicalize_resolution_source(str(target.get("resolution_source")))
        ):
            raise ValueError(f"captured resolution source mismatch for {market_id}")
        frozen_fee = _mapping(
            target.get("fee_schedule"),
            label=f"target {market_id} fee_schedule",
        )
        current_fee = _mapping(
            rule.get("feeSchedule"),
            label=f"captured rule {market_id} feeSchedule",
        )
        if (
            _decimal(current_fee.get("rate"), label="fee rate")
            != _decimal(frozen_fee.get("rate"), label="frozen fee rate")
            or _decimal(current_fee.get("exponent"), label="fee exponent")
            != _decimal(frozen_fee.get("exponent"), label="frozen fee exponent")
            or current_fee.get("takerOnly") is not frozen_fee.get("taker_only")
        ):
            raise ValueError(f"captured fee schedule mismatch for {market_id}")
        rule_identity = _target_rule_identity(
            target,
            description_sha256=description_hash,
        )
        reconstructed_rule_hash = hashlib.sha256(
            canonical_json_bytes(rule_identity)
        ).hexdigest()
        if reconstructed_rule_hash != target.get("rule_hash"):
            raise ValueError(f"reconstructed frozen rule hash mismatch for {market_id}")
        result[market_id] = {
            "rule_hash": reconstructed_rule_hash,
            "rules_text_sha256": description_hash,
            "resolution_source": target.get("resolution_source"),
            "fee_schedule": dict(frozen_fee),
            "source_event_id": evidence.source_event_id,
            "received_at_ms": evidence.received_at_ms,
            "available_by_earliest_decision": (
                evidence.received_at_ms <= available_by_ms
            ),
        }
    return result


def _validated_resolution_evidence(
    *,
    capture_root: Path,
    targets: Sequence[Mapping[str, Any]],
    expected_outcomes: Mapping[str, str],
    expiry_ms: int,
) -> dict[str, Any]:
    target_by_market = {str(target["market_id"]): target for target in targets}
    receipt_offset_ms = _receipt_clock_offset_ms(capture_root)
    candidates: dict[str, list[dict[str, Any]]] = {
        market_id: [] for market_id in target_by_market
    }
    for record in _records(capture_root, "clob_market_ws"):
        envelope = record.get("payload")
        if not isinstance(envelope, Mapping) or envelope.get("event_type") != (
            "market_resolved"
        ):
            continue
        payload = envelope.get("payload")
        market_id = str(payload.get("id")) if isinstance(payload, Mapping) else ""
        if market_id not in target_by_market or not isinstance(payload, Mapping):
            continue
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(f"resolution source event ID missing for {market_id}")
        received_at_ms = _iso_to_epoch_ms(
            record.get("received_at"),
            receipt_clock_offset_ms=receipt_offset_ms,
        )
        event_at_raw = record.get("event_at")
        event_at_ms = None if event_at_raw is None else _iso_to_epoch_ms(event_at_raw)
        event = {
            "event_at": event_at_raw,
            "event_at_ms": event_at_ms,
            "received_at_ms": received_at_ms,
            "source_event_id": record_id,
            "condition_id": payload.get("market"),
            "winning_asset_id": payload.get("winning_asset_id"),
            "winning_outcome": payload.get("winning_outcome"),
        }
        valid, reason = _resolution_event_validation(
            event,
            target_by_market[market_id],
        )
        if not valid:
            raise ValueError(
                f"conflicting official resolution event for {market_id}: {reason}"
            )
        if received_at_ms < expiry_ms:
            raise ValueError(
                f"official resolution receipt predates expiry for {market_id}"
            )
        if event_at_ms is not None and event_at_ms < expiry_ms:
            raise ValueError(
                f"official resolution event time predates expiry for {market_id}"
            )
        if event["winning_outcome"] != expected_outcomes[market_id]:
            raise ValueError(
                "official resolution conflicts with shared terminal boundary "
                f"for {market_id}"
            )
        candidates[market_id].append(event)

    result: dict[str, Any] = {}
    for market_id, events in candidates.items():
        if not events:
            raise ValueError(f"official resolution evidence missing for {market_id}")
        semantics = {
            (
                str(event["condition_id"]).lower(),
                str(event["winning_asset_id"]),
                str(event["winning_outcome"]),
            )
            for event in events
        }
        if len(semantics) != 1:
            raise ValueError(f"official resolution evidence conflicts for {market_id}")
        selected = min(
            events,
            key=lambda event: (
                int(event["received_at_ms"]),
                str(event["source_event_id"]),
            ),
        )
        result[market_id] = {
            "source_event_id": selected["source_event_id"],
            "received_at_ms": selected["received_at_ms"],
            "event_at_ms": selected["event_at_ms"],
            "condition_id": selected["condition_id"],
            "winning_asset_id": selected["winning_asset_id"],
            "winning_outcome": selected["winning_outcome"],
            "duplicate_consistent_event_count": len(events),
        }
    return result


def _validate_pair_targets(
    targets: Sequence[Mapping[str, Any]],
) -> tuple[SameExpiryPair, dict[str, Mapping[str, Any]]]:
    if len(targets) != 2:
        raise ValueError(
            "capture config must contain exactly one 5m and one 15m target"
        )
    by_horizon = {str(target.get("horizon")): target for target in targets}
    if set(by_horizon) != {"5m", "15m"}:
        raise ValueError("capture targets must be exactly 5m and 15m")
    for horizon, target in by_horizon.items():
        if (
            target.get("twap_window_seconds") != 60
            or target.get("source_topic") != "crypto_prices_twap_sixty"
            or target.get("settlement_regime") != V06_SETTLEMENT_REGIME_ID
            or target.get("taker_delay_ms") != 250
        ):
            raise ValueError(
                f"{horizon} target is not frozen to shared 60s/250ms semantics"
            )
    pair = _pair_from_capture_targets(by_horizon)
    if pair is None:
        raise ValueError("capture targets cannot form a strict same-expiry pair")
    return pair, by_horizon


@dataclass(frozen=True)
class _RawDecisionContext:
    event_cluster_id: str
    event_cluster_alias: str
    manifest_case_index: int
    split: str
    decision_tau_seconds: int
    decision_at_ms: int
    expiry_ms: int
    label_available_at_ms: int
    pair: SameExpiryPair
    settlement_state: PairSettlementState
    predictor_prices: tuple[TimedPrice, ...]
    current_terminal_twap_60: Decimal
    terminal_state_observed_at_ms: int
    terminal_state_received_at_ms: int
    actual_5_up: bool
    actual_15_up: bool
    replay: CausalBookReplay
    market_q_5_up: Decimal | None
    market_q_15_up: Decimal | None
    raw_top_ask_q_5_up: Decimal | None
    raw_top_ask_q_15_up: Decimal | None
    clock_uncertainty_ms: int
    capture_identity: Mapping[str, Any]
    rules_and_fees: Mapping[str, Any]
    opening_source_event_ids: Mapping[str, str]
    closing_source_event_id: str
    resolution_source_event_ids: Mapping[str, str]
    resolution_received_at_ms: Mapping[str, int]
    immutable_public_capture_evidence: bool
    capture_config_evidence: Mapping[str, Any]

    @property
    def identity(self) -> tuple[str, int]:
        return self.expiry_cluster_id, self.decision_tau_seconds

    @property
    def canonical_pair_id(self) -> str:
        return self.event_cluster_id

    @property
    def expiry_cluster_id(self) -> str:
        return canonical_expiry_cluster_id(self.expiry_ms)


def _load_case_contexts(
    case: Mapping[str, Any],
    *,
    manifest_case_index: int,
    manifest_dir: Path,
    model: SharedTerminalModelConfig,
    strategy: V07StrategyConfig,
) -> tuple[_RawDecisionContext, ...]:
    _assert_exact_keys(
        case,
        allowed=_CASE_KEYS,
        required=_REQUIRED_CASE_KEYS,
        label="counterfactual case",
    )
    cluster_alias = case.get("event_cluster_id")
    split = case.get("split")
    if not isinstance(cluster_alias, str) or not cluster_alias:
        raise ValueError("case event_cluster_id must be non-empty")
    if split not in {"train", "validation", "test"}:
        raise ValueError(f"case {cluster_alias} has invalid split")
    capture_root = _path(
        manifest_dir,
        case.get("capture_root"),
        label=f"case {cluster_alias} capture_root",
    )
    predictor_root = _path(
        manifest_dir,
        case.get("predictor_root"),
        label=f"case {cluster_alias} predictor_root",
    )
    capture_config_path = _path(
        manifest_dir,
        case.get("capture_config_path"),
        label=f"case {cluster_alias} capture_config_path",
    )
    history_root_values = case.get("history_roots", ())
    if not isinstance(history_root_values, Sequence) or isinstance(
        history_root_values,
        (str, bytes, bytearray),
    ):
        raise TypeError(f"case {cluster_alias} history_roots must be an array")
    history_roots = tuple(
        _path(
            manifest_dir,
            value,
            label=f"case {cluster_alias} history_roots[]",
        )
        for value in history_root_values
    )
    root_roles = (
        ("capture_root", capture_root),
        ("predictor_root", predictor_root),
        *((f"history_root_{index}", root) for index, root in enumerate(history_roots)),
    )
    integrity = {
        role: {
            "integrity": _assert_clean_integrity(root),
            "immutable_tree": _tree_identity(root),
        }
        for role, root in root_roles
    }
    expected_capture_config_path = (capture_root / "capture-config.json").resolve()
    if capture_config_path != expected_capture_config_path:
        raise ValueError(
            f"case {cluster_alias} capture config must be "
            "capture-root/capture-config.json"
        )
    capture_config = _load_json(capture_config_path, label="capture config")
    _confirm_public_capture_config(capture_config)
    integrity["capture_config"] = {
        "path": "capture-config.json",
        "size": capture_config_path.stat().st_size,
        "sha256": _sha256(capture_config_path),
    }
    targets_raw = capture_config.get("targets")
    if not isinstance(targets_raw, list) or any(
        not isinstance(target, Mapping) for target in targets_raw
    ):
        raise ValueError(f"case {cluster_alias} capture targets are malformed")
    targets = tuple(targets_raw)
    pair, _ = _validate_pair_targets(targets)
    cluster_id = canonical_event_cluster_id(pair)
    declared_cluster_id = case.get("canonical_event_cluster_id")
    if declared_cluster_id != cluster_id:
        raise ValueError(
            f"case {cluster_alias} canonical_event_cluster_id does not match "
            "the captured expiry/condition/market pair"
        )
    expiry_ms = pair.expires_at_ms
    tau_values = case.get("decision_tau_seconds", ())
    if not isinstance(tau_values, Sequence) or isinstance(
        tau_values,
        (str, bytes, bytearray),
    ):
        raise TypeError(f"case {cluster_alias} decision_tau_seconds must be an array")
    taus = tuple(
        _int(value, label=f"case {cluster_alias} decision_tau_seconds[]")
        for value in tau_values
    )
    if not taus or len(set(taus)) != len(taus):
        raise ValueError(f"case {cluster_alias} tau list must be unique and non-empty")
    if any(
        not strategy.tau_min_seconds <= tau <= strategy.tau_max_seconds for tau in taus
    ):
        raise ValueError(f"case {cluster_alias} tau is outside v0.7 bounds")
    earliest_decision_at_ms = expiry_ms - max(taus) * 1_000
    capture_started_at_ms = _int(
        capture_config.get("capture_started_at_ms"),
        label=f"case {cluster_alias} capture_started_at_ms",
    )
    if capture_started_at_ms < 0:
        raise ValueError(f"case {cluster_alias} capture start must be non-negative")
    if capture_started_at_ms > earliest_decision_at_ms:
        raise ValueError(f"case {cluster_alias} capture config postdates a decision")
    rules_and_fees = _validate_rules_and_fees(
        capture_root=capture_root,
        targets=targets,
        available_by_ms=earliest_decision_at_ms,
    )

    twap_roots = (*history_roots, capture_root)
    shared_twap = _combined_series(
        twap_roots,
        topic="crypto_prices_twap_sixty",
        symbol="btc/usd",
    )
    predictor = _series(predictor_root, topic="crypto_prices", symbol="btcusdt")
    opening_5 = _exact_observation(shared_twap, pair.market_5.opens_at_ms)
    opening_15 = _exact_observation(shared_twap, pair.market_15.opens_at_ms)
    closing = _exact_observation(shared_twap, expiry_ms)
    if opening_5 is None or opening_15 is None or closing is None:
        raise ValueError(
            f"case {cluster_alias} exact shared 60s boundaries are missing"
        )
    opening_5_event = _opening_event_id(
        twap_roots,
        topic="crypto_prices_twap_sixty",
        timestamp_ms=pair.market_5.opens_at_ms,
        expected_value=opening_5[2],
    )
    opening_15_event = _opening_event_id(
        twap_roots,
        topic="crypto_prices_twap_sixty",
        timestamp_ms=pair.market_15.opens_at_ms,
        expected_value=opening_15[2],
    )
    closing_event = _opening_event_id(
        twap_roots,
        topic="crypto_prices_twap_sixty",
        timestamp_ms=expiry_ms,
        expected_value=closing[2],
    )
    if not opening_5_event or not opening_15_event or not closing_event:
        raise ValueError(f"case {cluster_alias} boundary source event IDs are missing")

    actual5 = closing[2] >= opening_5[2]
    actual15 = closing[2] >= opening_15[2]
    expected_outcomes = {
        pair.market_5.market_id: "Up" if actual5 else "Down",
        pair.market_15.market_id: "Up" if actual15 else "Down",
    }
    resolution_evidence = _validated_resolution_evidence(
        capture_root=capture_root,
        targets=targets,
        expected_outcomes=expected_outcomes,
        expiry_ms=expiry_ms,
    )
    resolution_source_event_ids = {
        horizon: str(resolution_evidence[contract.market_id]["source_event_id"])
        for horizon, contract in (
            ("5m", pair.market_5),
            ("15m", pair.market_15),
        )
    }
    resolution_received_at_ms = {
        horizon: int(resolution_evidence[contract.market_id]["received_at_ms"])
        for horizon, contract in (
            ("5m", pair.market_5),
            ("15m", pair.market_15),
        )
    }
    label_available_at_ms = max(
        expiry_ms,
        closing[1],
        *resolution_received_at_ms.values(),
    )

    receipt_offset = _receipt_clock_offset_ms(capture_root)
    token_contracts = {
        contract.up_token_id: contract for contract in (pair.market_5, pair.market_15)
    } | {
        contract.down_token_id: contract for contract in (pair.market_5, pair.market_15)
    }
    replay = CausalBookReplay.from_records(
        _records(capture_root, "clob_market_ws"),
        receipt_clock_offset_ms=receipt_offset,
        tokens={
            token_id: BookReplayToken(
                token_id=token_id,
                tick_size=contract.tick_size,
                minimum_order_size=contract.minimum_order_size,
            )
            for token_id, contract in token_contracts.items()
        },
    )

    contexts: list[_RawDecisionContext] = []
    for tau in sorted(taus, reverse=True):
        decision_at_ms = expiry_ms - tau * 1_000
        offset, clock = _validate_clock(
            capture_config,
            capture_root=capture_root,
            decision_at_ms=decision_at_ms,
            strategy=strategy,
        )
        if offset != receipt_offset:
            raise RuntimeError("validated clock offset changed during case loading")
        strike5 = _exact_observation(
            shared_twap,
            pair.market_5.opens_at_ms,
            available_by_ms=decision_at_ms,
        )
        strike15 = _exact_observation(
            shared_twap,
            pair.market_15.opens_at_ms,
            available_by_ms=decision_at_ms,
        )
        state = _latest_before(shared_twap, decision_at_ms)
        if strike5 is None or strike15 is None or state is None:
            raise ValueError(
                f"case {cluster_alias} has missing causal strike/state at tau {tau}"
            )
        if (
            decision_at_ms - state[0] > strategy.maximum_chainlink_staleness_ms
            or decision_at_ms - state[1] > strategy.maximum_chainlink_staleness_ms
        ):
            raise ValueError(
                f"case {cluster_alias} terminal state is stale at tau {tau}"
            )
        predictor_prices = _resample_one_second(
            predictor,
            decision_at_ms=decision_at_ms,
            seconds=model.minimum_history_seconds,
        )
        if split != "test" and len(predictor_prices) != model.minimum_history_seconds:
            raise ValueError(
                f"case {cluster_alias} lacks causal predictor history at tau {tau}"
            )
        required_tokens = (
            pair.market_5.up_token_id,
            pair.market_5.down_token_id,
            pair.market_15.up_token_id,
            pair.market_15.down_token_id,
        )
        signal = replay.signal_books(
            token_ids=required_tokens,
            decision_at_ms=decision_at_ms,
            maximum_age_ms=strategy.maximum_book_staleness_ms,
        )
        if set(signal) != set(required_tokens):
            raise ValueError(
                f"case {cluster_alias} has incomplete causal signal books at tau {tau}"
            )
        signal_books = {key: value.snapshot for key, value in signal.items()}
        market5 = executable_binary_ask_probability(
            signal_books,
            up_token_id=pair.market_5.up_token_id,
            down_token_id=pair.market_5.down_token_id,
            target_quantity=strategy.market_baseline_quantity,
            fee_schedule=pair.market_5.fee_schedule,
        )
        market15 = executable_binary_ask_probability(
            signal_books,
            up_token_id=pair.market_15.up_token_id,
            down_token_id=pair.market_15.down_token_id,
            target_quantity=strategy.market_baseline_quantity,
            fee_schedule=pair.market_15.fee_schedule,
        )
        raw_market5 = raw_top_ask_probability(
            signal_books,
            up_token_id=pair.market_5.up_token_id,
            down_token_id=pair.market_5.down_token_id,
        )
        raw_market15 = raw_top_ask_probability(
            signal_books,
            up_token_id=pair.market_15.up_token_id,
            down_token_id=pair.market_15.down_token_id,
        )
        if split != "test" and (
            market5 is None
            or market15 is None
            or raw_market5 is None
            or raw_market15 is None
        ):
            raise ValueError(
                f"case {cluster_alias} executable market baseline is missing "
                f"at tau {tau}"
            )
        contexts.append(
            _RawDecisionContext(
                event_cluster_id=cluster_id,
                event_cluster_alias=cluster_alias,
                manifest_case_index=manifest_case_index,
                split=split,
                decision_tau_seconds=tau,
                decision_at_ms=decision_at_ms,
                expiry_ms=expiry_ms,
                label_available_at_ms=label_available_at_ms,
                pair=pair,
                settlement_state=PairSettlementState(
                    market_5_rule_hash=pair.market_5.rule_hash,
                    market_15_rule_hash=pair.market_15.rule_hash,
                    market_5_open_timestamp_ms=pair.market_5.opens_at_ms,
                    market_15_open_timestamp_ms=pair.market_15.opens_at_ms,
                    strike_5=strike5[2],
                    strike_15=strike15[2],
                    opening_5_source_event_id=opening_5_event,
                    opening_15_source_event_id=opening_15_event,
                ),
                predictor_prices=tuple(predictor_prices),
                current_terminal_twap_60=state[2],
                terminal_state_observed_at_ms=state[0],
                terminal_state_received_at_ms=state[1],
                actual_5_up=actual5,
                actual_15_up=actual15,
                replay=replay,
                market_q_5_up=market5,
                market_q_15_up=market15,
                raw_top_ask_q_5_up=raw_market5,
                raw_top_ask_q_15_up=raw_market15,
                clock_uncertainty_ms=clock["uncertainty_ms"],
                capture_identity=_deep_freeze(integrity),
                rules_and_fees=_deep_freeze(rules_and_fees),
                opening_source_event_ids=MappingProxyType(
                    {"5m": opening_5_event, "15m": opening_15_event}
                ),
                closing_source_event_id=closing_event,
                resolution_source_event_ids=MappingProxyType(
                    resolution_source_event_ids
                ),
                resolution_received_at_ms=MappingProxyType(resolution_received_at_ms),
                immutable_public_capture_evidence=(
                    capture_config["generated_fixture"] is False
                ),
                capture_config_evidence=MappingProxyType(
                    {
                        "schema_version": capture_config["schema_version"],
                        "paper_only": capture_config["paper_only"],
                        "public_only": capture_config["public_only"],
                        "new_orders_disabled": capture_config["new_orders_disabled"],
                        "generated_fixture": capture_config["generated_fixture"],
                        "capture_config_sha256": integrity["capture_config"]["sha256"],
                    }
                ),
            )
        )
    return tuple(contexts)


def _assert_split_policy(contexts: Sequence[_RawDecisionContext]) -> None:
    split_by_expiry: dict[str, str] = {}
    case_by_expiry: dict[str, int] = {}
    alias_by_expiry: dict[str, str] = {}
    expiry_by_alias: dict[str, str] = {}
    pair_by_expiry: dict[str, str] = {}
    expiry_by_pair: dict[str, str] = {}
    expiry_ms_by_cluster: dict[str, int] = {}
    for context in contexts:
        expiry_cluster_id = context.expiry_cluster_id
        previous_expiry_ms = expiry_ms_by_cluster.setdefault(
            expiry_cluster_id,
            context.expiry_ms,
        )
        if previous_expiry_ms != context.expiry_ms:
            raise ValueError("canonical common-expiry key collision")
        previous_pair = pair_by_expiry.setdefault(
            expiry_cluster_id,
            context.canonical_pair_id,
        )
        if previous_pair != context.canonical_pair_id:
            raise ValueError("one common expiry maps to multiple 5m/15m pairs")
        previous_expiry = expiry_by_pair.setdefault(
            context.canonical_pair_id,
            expiry_cluster_id,
        )
        if previous_expiry != expiry_cluster_id:
            raise ValueError("one canonical pair maps to multiple common expiries")
        previous_split = split_by_expiry.setdefault(expiry_cluster_id, context.split)
        if previous_split != context.split:
            raise ValueError("one common expiry appears in more than one split")
        previous_case = case_by_expiry.setdefault(
            expiry_cluster_id,
            context.manifest_case_index,
        )
        if previous_case != context.manifest_case_index:
            raise ValueError("one common expiry appears in multiple manifest cases")
        previous_alias = alias_by_expiry.setdefault(
            expiry_cluster_id,
            context.event_cluster_alias,
        )
        if previous_alias != context.event_cluster_alias:
            raise ValueError("one common expiry has multiple manifest aliases")
        previous_alias_expiry = expiry_by_alias.setdefault(
            context.event_cluster_alias,
            expiry_cluster_id,
        )
        if previous_alias_expiry != expiry_cluster_id:
            raise ValueError("one manifest alias maps to multiple common expiries")
    for split in ("train", "validation", "test"):
        if split not in set(split_by_expiry.values()):
            raise ValueError(f"counterfactual manifest has no {split} common expiries")
    identities = tuple(context.identity for context in contexts)
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate common-expiry/tau context")
    train = tuple(item for item in contexts if item.split == "train")
    validation = tuple(item for item in contexts if item.split == "validation")
    test = tuple(item for item in contexts if item.split == "test")
    if max(item.label_available_at_ms for item in train) >= min(
        item.decision_at_ms for item in validation
    ):
        raise ValueError("training labels overlap the validation decision period")
    if max(item.label_available_at_ms for item in validation) >= min(
        item.decision_at_ms for item in test
    ):
        raise ValueError("validation labels overlap the locked test decision period")


@dataclass(frozen=True)
class _UniverseLockReceipt:
    test_universe_sha256: str
    locked_at_ms: int
    received_at_ms: int
    record_id: str


@dataclass(frozen=True)
class _PredictionDecisionLockReceipt:
    test_universe_sha256: str
    canonical_pair_id: str
    expiry_ms: int
    expiry_cluster_id: str
    decision_tau_seconds: int
    decision_at_ms: int
    prediction_locked_at_ms: int
    forecast_payload_sha256: str
    decision_payload_sha256: str
    received_at_ms: int
    record_id: str


@dataclass(frozen=True)
class _PreLabelLockIndex:
    journal_root: Path
    journal_identity: Mapping[str, Any]
    universe: _UniverseLockReceipt
    prediction_by_identity: Mapping[tuple[str, int], _PredictionDecisionLockReceipt]


def _sha256_text(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    return value


def _test_universe_document(
    contexts: Sequence[_RawDecisionContext],
    *,
    preregistration_sha256: str,
) -> dict[str, Any]:
    test_rows = [
        {
            "canonical_pair_id": context.canonical_pair_id,
            "expiry_ms": context.expiry_ms,
            "expiry_cluster_id": context.expiry_cluster_id,
            "decision_tau_seconds": context.decision_tau_seconds,
            "decision_at_ms": context.decision_at_ms,
        }
        for context in sorted(
            (item for item in contexts if item.split == "test"),
            key=lambda item: item.identity,
        )
    ]
    return {
        "schema_version": "btc-5m-15m-v07-test-universe-lock.v1",
        "preregistration_sha256": preregistration_sha256,
        "canonical_test_rows": test_rows,
    }


def _record_received_at_ms(record: Mapping[str, Any]) -> int:
    received_at = record.get("received_at")
    if not isinstance(received_at, str):
        raise ValueError("lock-journal record has no received_at timestamp")
    return _iso_to_epoch_ms(received_at)


def _load_prelabel_lock_index(
    journal_root_value: Any,
    *,
    manifest_dir: Path,
    contexts: Sequence[_RawDecisionContext],
    preregistration_sha256: str,
) -> _PreLabelLockIndex | None:
    if journal_root_value is None:
        return None
    journal_root = _path(
        manifest_dir,
        journal_root_value,
        label="prelabel_lock_journal_root",
    )
    journal_identity = {
        "integrity": _assert_clean_integrity(journal_root),
        "immutable_tree": _tree_identity(journal_root),
    }
    universe_document = _test_universe_document(
        contexts,
        preregistration_sha256=preregistration_sha256,
    )
    expected_universe_hash = hashlib.sha256(
        canonical_json_bytes(universe_document)
    ).hexdigest()
    earliest_test_decision = min(
        context.decision_at_ms for context in contexts if context.split == "test"
    )
    universe_receipts: list[_UniverseLockReceipt] = []
    prediction_receipts: dict[tuple[str, int], _PredictionDecisionLockReceipt] = {}
    for record in _records(journal_root, LOCK_JOURNAL_SOURCE):
        payload = _mapping(record.get("payload"), label="lock-journal payload")
        if payload.get("schema_version") != LOCK_JOURNAL_SCHEMA_VERSION:
            raise ValueError("unsupported pre-label lock-journal schema")
        kind = payload.get("kind")
        received_at_ms = _record_received_at_ms(record)
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("lock-journal record ID is missing")
        if kind == "test_universe_lock":
            _assert_exact_keys(
                payload,
                allowed=frozenset(
                    {
                        "schema_version",
                        "kind",
                        "test_universe_sha256",
                        "locked_at_ms",
                    }
                ),
                required=frozenset(
                    {
                        "schema_version",
                        "kind",
                        "test_universe_sha256",
                        "locked_at_ms",
                    }
                ),
                label="test-universe lock receipt",
            )
            universe_hash = _sha256_text(
                payload.get("test_universe_sha256"),
                label="test_universe_sha256",
            )
            locked_at_ms = _int(
                payload.get("locked_at_ms"),
                label="test-universe locked_at_ms",
            )
            if universe_hash != expected_universe_hash:
                raise ValueError(
                    "test-universe lock hash does not match manifest cases"
                )
            if locked_at_ms > received_at_ms:
                raise ValueError("test-universe lock timestamp follows its receipt")
            if received_at_ms >= earliest_test_decision:
                raise ValueError(
                    "test universe was selected retrospectively after the test began"
                )
            universe_receipts.append(
                _UniverseLockReceipt(
                    test_universe_sha256=universe_hash,
                    locked_at_ms=locked_at_ms,
                    received_at_ms=received_at_ms,
                    record_id=record_id,
                )
            )
            continue
        if kind != "forecast_decision_lock":
            raise ValueError("unknown pre-label lock-journal record kind")
        _assert_exact_keys(
            payload,
            allowed=frozenset(
                {
                    "schema_version",
                    "kind",
                    "test_universe_sha256",
                    "canonical_pair_id",
                    "expiry_ms",
                    "expiry_cluster_id",
                    "decision_tau_seconds",
                    "decision_at_ms",
                    "prediction_locked_at_ms",
                    "forecast_payload_sha256",
                    "decision_payload_sha256",
                }
            ),
            required=frozenset(
                {
                    "schema_version",
                    "kind",
                    "test_universe_sha256",
                    "canonical_pair_id",
                    "expiry_ms",
                    "expiry_cluster_id",
                    "decision_tau_seconds",
                    "decision_at_ms",
                    "prediction_locked_at_ms",
                    "forecast_payload_sha256",
                    "decision_payload_sha256",
                }
            ),
            label="forecast/decision lock receipt",
        )
        pair_id = payload.get("canonical_pair_id")
        if not isinstance(pair_id, str) or not pair_id:
            raise ValueError("forecast lock canonical pair ID is missing")
        expiry_ms = _int(payload.get("expiry_ms"), label="forecast-lock expiry_ms")
        expiry_cluster_id = payload.get("expiry_cluster_id")
        if expiry_cluster_id != canonical_expiry_cluster_id(expiry_ms):
            raise ValueError("forecast lock common-expiry key does not match expiry_ms")
        tau = _int(
            payload.get("decision_tau_seconds"),
            label="forecast-lock decision_tau_seconds",
        )
        identity = expiry_cluster_id, tau
        if identity in prediction_receipts:
            raise ValueError("duplicate forecast/decision lock receipt")
        prediction_receipts[identity] = _PredictionDecisionLockReceipt(
            test_universe_sha256=_sha256_text(
                payload.get("test_universe_sha256"),
                label="test_universe_sha256",
            ),
            canonical_pair_id=pair_id,
            expiry_ms=expiry_ms,
            expiry_cluster_id=expiry_cluster_id,
            decision_tau_seconds=tau,
            decision_at_ms=_int(
                payload.get("decision_at_ms"),
                label="forecast-lock decision_at_ms",
            ),
            prediction_locked_at_ms=_int(
                payload.get("prediction_locked_at_ms"),
                label="prediction_locked_at_ms",
            ),
            forecast_payload_sha256=_sha256_text(
                payload.get("forecast_payload_sha256"),
                label="forecast_payload_sha256",
            ),
            decision_payload_sha256=_sha256_text(
                payload.get("decision_payload_sha256"),
                label="decision_payload_sha256",
            ),
            received_at_ms=received_at_ms,
            record_id=record_id,
        )
    if len(universe_receipts) != 1:
        raise ValueError("lock journal must contain exactly one test-universe receipt")
    universe = universe_receipts[0]
    expected_identities = {
        context.identity for context in contexts if context.split == "test"
    }
    if set(prediction_receipts) != expected_identities:
        raise ValueError(
            "lock journal must contain one forecast/decision receipt per test row"
        )
    contexts_by_identity = {
        context.identity: context for context in contexts if context.split == "test"
    }
    for identity, receipt in prediction_receipts.items():
        context = contexts_by_identity[identity]
        if receipt.test_universe_sha256 != universe.test_universe_sha256:
            raise ValueError("forecast lock references a different test universe")
        if receipt.expiry_ms != context.expiry_ms:
            raise ValueError("forecast lock expiry does not match captured expiry")
        if receipt.expiry_cluster_id != context.expiry_cluster_id:
            raise ValueError("forecast lock common-expiry key does not match context")
        if receipt.canonical_pair_id != context.canonical_pair_id:
            raise ValueError("forecast lock pair identity does not match context")
        if receipt.decision_at_ms != context.decision_at_ms:
            raise ValueError("forecast lock decision timestamp does not match context")
        if receipt.prediction_locked_at_ms != context.decision_at_ms:
            raise ValueError("prediction lock must equal the decision timestamp")
        if receipt.received_at_ms >= context.expiry_ms:
            raise ValueError(
                "forecast/decision lock receipt is not strictly pre-expiry"
            )
    return _PreLabelLockIndex(
        journal_root=journal_root,
        journal_identity=MappingProxyType(journal_identity),
        universe=universe,
        prediction_by_identity=MappingProxyType(prediction_receipts),
    )


def _forecast_availability(
    *,
    setting_id: str,
    context: _RawDecisionContext,
    strategy: V07StrategyConfig,
    lock_index: _PreLabelLockIndex | None,
) -> tuple[int, V07ForecastAvailabilityBasis]:
    if setting_id == "primary" and lock_index is not None:
        receipt = lock_index.prediction_by_identity[context.identity]
        return (
            receipt.received_at_ms,
            V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT,
        )
    return (
        context.decision_at_ms
        + strategy.counterfactual_computation_availability_delay_ms,
        V07ForecastAvailabilityBasis.PREREGISTERED_COUNTERFACTUAL_DELAY,
    )


def _forecast_lock_document(
    *,
    setting_id: str,
    context: _RawDecisionContext,
    cycle: V07PaperCycleEvaluation,
    forecast_q_5_up: Decimal | None,
    forecast_q_15_up: Decimal | None,
    forecast_available_at_ms: int,
    forecast_availability_basis: V07ForecastAvailabilityBasis,
) -> dict[str, Any]:
    decision = cycle.decision

    def decimal_text(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    predictive = decision.edge_basis is V07EdgeBasis.PREDICTIVE
    simulation = cycle.simulation
    shrinkage = cycle.shrinkage
    veto = cycle.validation_veto
    return {
        "schema_version": "btc-5m-15m-v07-forecast-lock-payload.v6",
        "setting_id": setting_id,
        "canonical_pair_id": context.canonical_pair_id,
        "expiry_cluster_id": context.expiry_cluster_id,
        "decision_tau_seconds": context.decision_tau_seconds,
        "decision_at_ms": context.decision_at_ms,
        "forecast_available_at_ms": forecast_available_at_ms,
        "forecast_availability_basis": forecast_availability_basis.value,
        "expiry_ms": context.expiry_ms,
        "strike_5": str(context.settlement_state.strike_5),
        "strike_15": str(context.settlement_state.strike_15),
        "edge_basis": decision.edge_basis.value,
        "probability_diagnostics_applicable": (
            decision.probability_diagnostics_applicable
        ),
        "forecast_q_5_up": decimal_text(forecast_q_5_up if predictive else None),
        "forecast_q_15_up": decimal_text(forecast_q_15_up if predictive else None),
        "executable_market_q_5_up": decimal_text(
            cycle.market_q_5_up if predictive else None
        ),
        "executable_market_q_15_up": decimal_text(
            cycle.market_q_15_up if predictive else None
        ),
        "model_config_sha256": (
            None if simulation is None else simulation.model_config_hash
        ),
        "shrinkage_artifact_hash": (
            None if shrinkage is None else shrinkage.artifact_hash
        ),
        "validation_veto_artifact_hash": (None if veto is None else veto.artifact_hash),
        "structural_primitive": (
            {
                "settlement_model_id": SETTLEMENT_MODEL_ID,
                "strike_5": str(context.settlement_state.strike_5),
                "strike_15": str(context.settlement_state.strike_15),
                "monte_carlo_required": False,
            }
            if decision.edge_basis is V07EdgeBasis.STRUCTURAL
            else None
        ),
    }


def _decision_lock_document(
    *,
    setting_id: str,
    context: _RawDecisionContext,
    cycle: V07PaperCycleEvaluation,
) -> dict[str, Any]:
    decision = cycle.decision

    def decimal_text(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    return {
        "schema_version": "btc-5m-15m-v07-decision-lock-payload.v6",
        "setting_id": setting_id,
        "canonical_pair_id": context.canonical_pair_id,
        "expiry_ms": context.expiry_ms,
        "expiry_cluster_id": context.expiry_cluster_id,
        "decision_tau_seconds": context.decision_tau_seconds,
        "decision_at_ms": decision.decision_at_ms,
        "forecast_available_at_ms": decision.forecast_available_at_ms,
        "forecast_availability_basis": decision.forecast_availability_basis.value,
        "action": decision.action.value,
        "quantity": str(decision.quantity),
        "first_token_id": decision.first_token_id,
        "second_token_id": decision.second_token_id,
        "execution_notional_per_pair": decimal_text(
            decision.execution_notional_per_pair
        ),
        "fee_per_pair": decimal_text(decision.fee_per_pair),
        "execution_cost_per_pair": decimal_text(decision.execution_cost_per_pair),
        "expected_gross_pnl_per_pair": decimal_text(
            decision.expected_gross_pnl_per_pair
        ),
        "expected_net_pnl_per_pair": decimal_text(decision.expected_net_pnl_per_pair),
        "uncertainty_adjusted_pnl_per_pair": decimal_text(
            decision.uncertainty_adjusted_pnl_per_pair
        ),
        "cvar_per_pair": decimal_text(decision.cvar_per_pair),
        "loss_probability": decimal_text(decision.loss_probability),
        "q_5_raw": decimal_text(decision.q_5_raw),
        "q_15_raw": decimal_text(decision.q_15_raw),
        "q_5_calibrated": decimal_text(decision.q_5_calibrated),
        "q_15_calibrated": decimal_text(decision.q_15_calibrated),
        "edge_basis": decision.edge_basis.value,
        "edge_evaluation_quantity": decimal_text(decision.edge_evaluation_quantity),
        "structural_worst_case_payoff_per_pair": decimal_text(
            decision.structural_worst_case_payoff_per_pair
        ),
        "structural_net_floor_per_pair": decimal_text(
            decision.structural_net_floor_per_pair
        ),
        "structural_quantity_executable": (decision.structural_quantity_executable),
        "quantity_selection_basis": decision.quantity_selection_basis.value,
        "quantity_candidate_breakpoint_count": (
            decision.quantity_candidate_breakpoint_count
        ),
        "selected_guaranteed_total_pnl": decimal_text(
            decision.selected_guaranteed_total_pnl
        ),
        "selected_uncertainty_adjusted_total_pnl": decimal_text(
            decision.selected_uncertainty_adjusted_total_pnl
        ),
        "probability_diagnostics_applicable": (
            decision.probability_diagnostics_applicable
        ),
        "qualification_edge_per_pair": decimal_text(
            decision.qualification_edge_per_pair
        ),
        "reason_codes": list(decision.reason_codes),
        "qualification": decision.qualification,
    }


def _prelabel_lock_provenance(
    *,
    setting_id: str,
    context: _RawDecisionContext,
    lock_index: _PreLabelLockIndex | None,
    forecast_document: Mapping[str, Any],
    decision_document: Mapping[str, Any],
) -> PreLabelLockProvenance:
    if setting_id != "primary":
        return PreLabelLockProvenance.counterfactual(
            "diagnostic_sensitivity_setting_has_no_preregistered_action_lock"
        )
    if lock_index is None:
        return PreLabelLockProvenance.counterfactual(
            "counterfactual_replay_has_no_prelabel_manifest_prediction_decision_receipt"
        )
    receipt = lock_index.prediction_by_identity[context.identity]
    forecast_hash = hashlib.sha256(canonical_json_bytes(forecast_document)).hexdigest()
    decision_hash = hashlib.sha256(canonical_json_bytes(decision_document)).hexdigest()
    if forecast_hash != receipt.forecast_payload_sha256:
        raise ValueError("forecast payload does not match its pre-label lock receipt")
    if decision_hash != receipt.decision_payload_sha256:
        raise ValueError("decision payload does not match its pre-label lock receipt")
    universe = lock_index.universe
    return PreLabelLockProvenance._create(
        status=PreLabelLockStatus.VERIFIED,
        test_universe_sha256=universe.test_universe_sha256,
        test_universe_locked_at_ms=universe.locked_at_ms,
        test_universe_received_at_ms=universe.received_at_ms,
        test_universe_receipt_id=universe.record_id,
        prediction_locked_at_ms=receipt.prediction_locked_at_ms,
        prediction_received_at_ms=receipt.received_at_ms,
        prediction_receipt_id=receipt.record_id,
        forecast_payload_sha256=forecast_hash,
        decision_payload_sha256=decision_hash,
    )


def _simulate_contexts(
    contexts: Sequence[_RawDecisionContext],
    *,
    model: SharedTerminalModelConfig,
) -> dict[tuple[str, int], SharedTerminalSimulationResult]:
    return {
        context.identity: simulate_shared_terminal_twap_60_distribution(
            predictor_prices=context.predictor_prices,
            current_terminal_twap_60=context.current_terminal_twap_60,
            decision_at_ms=context.decision_at_ms,
            expiry_ms=context.expiry_ms,
            strike_5=context.settlement_state.strike_5,
            strike_15=context.settlement_state.strike_15,
            config=model,
            seed_salt=(f"{context.event_cluster_id}:{context.decision_tau_seconds}"),
        )
        for context in contexts
    }


def _probability_row(
    context: _RawDecisionContext,
    simulation: SharedTerminalSimulationResult,
) -> ProbabilityPairTrainingRow:
    if context.market_q_5_up is None or context.market_q_15_up is None:
        raise ValueError("predictive training row lacks executable market baseline")
    return ProbabilityPairTrainingRow(
        event_cluster_id=context.event_cluster_id,
        expiry_ms=context.expiry_ms,
        decision_tau_seconds=context.decision_tau_seconds,
        decision_at_ms=context.decision_at_ms,
        label_available_at_ms=context.label_available_at_ms,
        model_q_5_up=simulation.distribution.q_5_up,
        model_q_15_up=simulation.distribution.q_15_up,
        market_q_5_up=context.market_q_5_up,
        market_q_15_up=context.market_q_15_up,
        actual_5_up=context.actual_5_up,
        actual_15_up=context.actual_15_up,
        strike_5=context.settlement_state.strike_5,
        strike_15=context.settlement_state.strike_15,
        split=context.split,
        raw_top_ask_q_5_up=context.raw_top_ask_q_5_up,
        raw_top_ask_q_15_up=context.raw_top_ask_q_15_up,
    )


@dataclass(frozen=True)
class _SettingResult:
    setting_id: str
    model: SharedTerminalModelConfig
    shrinkage: TrainOnlyShrinkageArtifact | None
    validation_veto: ValidationVetoEvidence | None
    simulations: Mapping[tuple[str, int], SharedTerminalSimulationResult]
    cycles: tuple[V07PaperCycleEvaluation, ...]
    forecasts: tuple[LockedOOSForecastRow, ...]
    attempts: tuple[LockedOOSEconomicAttempt, ...]

    @property
    def net_pnl(self) -> Decimal:
        return sum((row.net_pnl for row in self.attempts), Decimal(0))

    @property
    def predictive_net_pnl(self) -> Decimal:
        return sum(
            (
                row.net_pnl
                for row in self.attempts
                if row.edge_basis is V07EdgeBasis.PREDICTIVE
            ),
            Decimal(0),
        )

    @property
    def structural_net_pnl(self) -> Decimal:
        return sum(
            (
                row.net_pnl
                for row in self.attempts
                if row.edge_basis is V07EdgeBasis.STRUCTURAL
            ),
            Decimal(0),
        )


def _run_setting(
    *,
    setting_id: str,
    contexts: Sequence[_RawDecisionContext],
    model: SharedTerminalModelConfig,
    strategy: V07StrategyConfig,
    candidate_weights: tuple[Decimal, ...],
    forced_model_weight: Decimal | None = None,
    lock_index: _PreLabelLockIndex | None = None,
) -> _SettingResult:
    test_contexts = tuple(context for context in contexts if context.split == "test")
    initial_cycles: dict[tuple[str, int], V07PaperCycleEvaluation] = {}
    structural_identities: set[tuple[str, int]] = set()

    # Structural scanning is deliberately first and model-free.  It consumes only
    # the verified settlement rule/strikes, causal decision books, captured fees,
    # and the already established forecast-availability clock.
    for context in test_contexts:
        forecast_available_at_ms, forecast_availability_basis = _forecast_availability(
            setting_id=setting_id,
            context=context,
            strategy=strategy,
            lock_index=lock_index,
        )
        structural_health = SharedTerminalDataHealth(
            decision_at_ms=context.decision_at_ms,
            forecast_available_at_ms=forecast_available_at_ms,
            forecast_availability_basis=forecast_availability_basis,
            terminal_twap_60_observed_at_ms=context.terminal_state_observed_at_ms,
            terminal_twap_60_received_at_ms=context.terminal_state_received_at_ms,
            absolute_clock_drift_ms=context.clock_uncertainty_ms,
            shrinkage=None,
            validation_veto=None,
        )
        structural_cycle = evaluate_shared_terminal_paper_cycle(
            event_cluster_id=context.event_cluster_id,
            event_cluster_alias=context.event_cluster_alias,
            decision_tau_seconds=context.decision_tau_seconds,
            pair=context.pair,
            settlement_state=context.settlement_state,
            simulation=None,
            replay=context.replay,
            health=structural_health,
            config=strategy,
            market_5_up=context.actual_5_up,
            market_15_up=context.actual_15_up,
            locked_oos=True,
        )
        initial_cycles[context.identity] = structural_cycle
        if (
            structural_cycle.decision.action is not PairAction.NO_TRADE
            and structural_cycle.decision.edge_basis is V07EdgeBasis.STRUCTURAL
        ):
            structural_identities.add(context.identity)

    predictive_test_contexts = tuple(
        context
        for context in test_contexts
        if context.identity not in structural_identities
        and "predictive_distribution_unavailable_after_structural_scan"
        in initial_cycles[context.identity].decision.reason_codes
    )
    simulations: dict[tuple[str, int], SharedTerminalSimulationResult] = {}
    predictive_rejections: dict[tuple[str, int], str] = {}
    shrinkage: TrainOnlyShrinkageArtifact | None = None
    veto: ValidationVetoEvidence | None = None
    if predictive_test_contexts:
        simulation_contexts = tuple(
            context for context in contexts if context.split in {"train", "validation"}
        )
        try:
            predictive_training_simulations = _simulate_contexts(
                simulation_contexts,
                model=model,
            )
        except V07ModelRejection as exc:
            rejection = (
                f"predictive_training_model_rejected_after_structural_scan:{exc.code}"
            )
            predictive_rejections.update(
                {context.identity: rejection for context in predictive_test_contexts}
            )
        else:
            simulations.update(predictive_training_simulations)
            train_rows = tuple(
                _probability_row(context, simulations[context.identity])
                for context in contexts
                if context.split == "train"
            )
            validation_rows = tuple(
                _probability_row(context, simulations[context.identity])
                for context in contexts
                if context.split == "validation"
            )
            fit_at_ms = min(
                context.decision_at_ms
                for context in contexts
                if context.split == "validation"
            )
            shrinkage = TrainOnlyShrinkageArtifact.fit(
                train_rows,
                fit_at_ms=fit_at_ms,
                candidate_model_weights=candidate_weights,
            )
            if forced_model_weight is not None:
                shrinkage = shrinkage.with_diagnostic_model_weight(
                    model_weight=forced_model_weight,
                    setting_id=setting_id,
                )
            veto_at_ms = min(context.decision_at_ms for context in test_contexts)
            veto = evaluate_validation_veto(
                validation_rows,
                shrinkage=shrinkage,
                evaluated_at_ms=veto_at_ms,
            )
            for context in predictive_test_contexts:
                if (
                    len(context.predictor_prices) != model.minimum_history_seconds
                    or context.market_q_5_up is None
                    or context.market_q_15_up is None
                    or context.raw_top_ask_q_5_up is None
                    or context.raw_top_ask_q_15_up is None
                ):
                    predictive_rejections[context.identity] = (
                        "predictive_inputs_unavailable_after_structural_scan"
                    )
                    continue
                try:
                    simulations[context.identity] = (
                        simulate_shared_terminal_twap_60_distribution(
                            predictor_prices=context.predictor_prices,
                            current_terminal_twap_60=(context.current_terminal_twap_60),
                            decision_at_ms=context.decision_at_ms,
                            expiry_ms=context.expiry_ms,
                            strike_5=context.settlement_state.strike_5,
                            strike_15=context.settlement_state.strike_15,
                            config=model,
                            seed_salt=(
                                f"{context.event_cluster_id}:"
                                f"{context.decision_tau_seconds}"
                            ),
                        )
                    )
                except V07ModelRejection as exc:
                    predictive_rejections[context.identity] = (
                        f"predictive_model_rejected_after_structural_scan:{exc.code}"
                    )

    cycles: list[V07PaperCycleEvaluation] = []
    forecasts: list[LockedOOSForecastRow] = []
    attempts: list[LockedOOSEconomicAttempt] = []
    for context in test_contexts:
        forecast_available_at_ms, forecast_availability_basis = _forecast_availability(
            setting_id=setting_id,
            context=context,
            strategy=strategy,
            lock_index=lock_index,
        )
        if context.identity in structural_identities:
            simulation = None
            q5 = None
            q15 = None
            cycle = initial_cycles[context.identity]
        elif context.identity not in simulations:
            simulation = None
            q5 = None
            q15 = None
            cycle = initial_cycles[context.identity]
            rejection = predictive_rejections.get(context.identity)
            if rejection is not None:
                decision = replace(
                    cycle.decision,
                    reason_codes=(*cycle.decision.reason_codes, rejection),
                )
                cycle = replace(cycle, cycle=replace(cycle.cycle, decision=decision))
        else:
            if shrinkage is None or veto is None:
                raise ValueError(
                    "predictive context lacks train-only shrinkage/validation veto"
                )
            simulation = simulations[context.identity]
            assert context.market_q_5_up is not None
            assert context.market_q_15_up is not None
            q5, q15, _ = shrinkage.transform(
                simulation.distribution,
                market_q_5_up=context.market_q_5_up,
                market_q_15_up=context.market_q_15_up,
            )
            health = SharedTerminalDataHealth(
                decision_at_ms=context.decision_at_ms,
                forecast_available_at_ms=forecast_available_at_ms,
                forecast_availability_basis=forecast_availability_basis,
                terminal_twap_60_observed_at_ms=(context.terminal_state_observed_at_ms),
                terminal_twap_60_received_at_ms=(context.terminal_state_received_at_ms),
                absolute_clock_drift_ms=context.clock_uncertainty_ms,
                shrinkage=shrinkage,
                validation_veto=veto,
            )
            cycle = evaluate_shared_terminal_paper_cycle(
                event_cluster_id=context.event_cluster_id,
                event_cluster_alias=context.event_cluster_alias,
                decision_tau_seconds=context.decision_tau_seconds,
                pair=context.pair,
                settlement_state=context.settlement_state,
                simulation=simulation,
                replay=context.replay,
                health=health,
                config=strategy,
                market_5_up=context.actual_5_up,
                market_15_up=context.actual_15_up,
                locked_oos=True,
            )
        cycles.append(cycle)
        missing_surface_reasons = {
            "first_leg_execution_book_missing",
            "second_leg_timeout_surface_missing",
            "unwind_execution_book_missing",
        }
        if missing_surface_reasons.intersection(cycle.cycle.reason_codes):
            reasons = ", ".join(cycle.cycle.reason_codes)
            raise ValueError(
                "required causal execution book surface is absent for "
                f"{context.event_cluster_id} tau={context.decision_tau_seconds}: "
                f"{reasons}"
            )
        forecast_document = _forecast_lock_document(
            setting_id=setting_id,
            context=context,
            cycle=cycle,
            forecast_q_5_up=q5,
            forecast_q_15_up=q15,
            forecast_available_at_ms=forecast_available_at_ms,
            forecast_availability_basis=forecast_availability_basis,
        )
        decision_document = _decision_lock_document(
            setting_id=setting_id,
            context=context,
            cycle=cycle,
        )
        lock_provenance = _prelabel_lock_provenance(
            setting_id=setting_id,
            context=context,
            lock_index=lock_index,
            forecast_document=forecast_document,
            decision_document=decision_document,
        )
        decision = cycle.decision
        forecasts.append(
            LockedOOSForecastRow(
                event_cluster_id=context.event_cluster_id,
                event_cluster_alias=context.event_cluster_alias,
                expiry_ms=context.expiry_ms,
                decision_tau_seconds=context.decision_tau_seconds,
                decision_at_ms=context.decision_at_ms,
                forecast_available_at_ms=forecast_available_at_ms,
                forecast_availability_basis=forecast_availability_basis,
                label_available_at_ms=context.label_available_at_ms,
                forecast_q_5_up=q5,
                forecast_q_15_up=q15,
                market_q_5_up=cycle.market_q_5_up,
                market_q_15_up=cycle.market_q_15_up,
                raw_top_ask_q_5_up=cycle.raw_top_ask_q_5_up,
                raw_top_ask_q_15_up=cycle.raw_top_ask_q_15_up,
                actual_5_up=context.actual_5_up,
                actual_15_up=context.actual_15_up,
                strike_5=context.settlement_state.strike_5,
                strike_15=context.settlement_state.strike_15,
                lock_provenance=lock_provenance,
                edge_basis=decision.edge_basis,
                edge_evaluation_quantity=decision.edge_evaluation_quantity,
                structural_worst_case_payoff_per_pair=(
                    decision.structural_worst_case_payoff_per_pair
                ),
                structural_net_floor_per_pair=(decision.structural_net_floor_per_pair),
                structural_quantity_executable=(
                    decision.structural_quantity_executable
                ),
                quantity_selection_basis=decision.quantity_selection_basis,
                quantity_candidate_breakpoint_count=(
                    decision.quantity_candidate_breakpoint_count
                ),
                selected_guaranteed_total_pnl=(decision.selected_guaranteed_total_pnl),
                selected_uncertainty_adjusted_total_pnl=(
                    decision.selected_uncertainty_adjusted_total_pnl
                ),
                probability_diagnostics_applicable=(
                    decision.probability_diagnostics_applicable
                ),
            )
        )
        if decision.action is PairAction.NO_TRADE:
            continue
        if cycle.settlement is None or cycle.execution is None:
            raise ValueError(
                "locked actionable decision is not fully reconciled for "
                f"{context.event_cluster_id} tau={context.decision_tau_seconds}"
            )
        diagnostics = paper_execution_diagnostics(
            cycle.execution,
            expiry_ms=context.expiry_ms,
        )
        economic_attempt = diagnostics.get("economic_attempt") is True
        causal_no_fill = (
            cycle.execution.status is PairExecutionStatus.NO_FILL
            and not economic_attempt
        )
        if not economic_attempt and not causal_no_fill:
            raise ValueError(
                "actionable decision has neither an economic attempt nor a "
                "causally demonstrated no-fill"
            )
        attempts.append(
            LockedOOSEconomicAttempt(
                attempt_id=hashlib.sha256(
                    canonical_json_bytes(
                        {
                            "setting_id": setting_id,
                            "canonical_pair_id": context.canonical_pair_id,
                            "expiry_ms": context.expiry_ms,
                            "expiry_cluster_id": context.expiry_cluster_id,
                            "decision_tau_seconds": context.decision_tau_seconds,
                        }
                    )
                ).hexdigest(),
                event_cluster_id=context.event_cluster_id,
                event_cluster_alias=context.event_cluster_alias,
                expiry_ms=context.expiry_ms,
                decision_tau_seconds=context.decision_tau_seconds,
                decision_at_ms=context.decision_at_ms,
                forecast_available_at_ms=cycle.forecast_available_at_ms,
                forecast_availability_basis=cycle.forecast_availability_basis,
                first_execution_not_before_ms=(
                    cycle.first_execution_not_before_ms
                    if cycle.first_execution_not_before_ms is not None
                    else cycle.forecast_available_at_ms
                ),
                first_execution_observed_at_ms=(cycle.first_execution_observed_at_ms),
                effective_signal_to_execution_latency_ms=(
                    cycle.effective_signal_to_execution_latency_ms
                    if cycle.effective_signal_to_execution_latency_ms is not None
                    else cycle.forecast_available_at_ms - context.decision_at_ms
                ),
                net_pnl=cycle.settlement.net_pnl,
                explainable=(
                    cycle.settlement.explainable if economic_attempt else False
                ),
                complete_cost_evidence=(
                    decision.execution_notional_per_pair is not None
                    and decision.fee_per_pair is not None
                    and decision.execution_cost_per_pair is not None
                    and len(context.rules_and_fees) == 2
                    and all(
                        isinstance(item.get("source_event_id"), str)
                        and bool(item["source_event_id"])
                        and item.get("available_by_earliest_decision") is True
                        for item in context.rules_and_fees.values()
                    )
                ),
                complete_execution_evidence=(
                    (economic_attempt or causal_no_fill)
                    and len(cycle.signal_source_event_ids) == 4
                    and all(cycle.signal_source_event_ids)
                    and bool(cycle.execution.first_leg.source_event_id)
                    and bool(cycle.execution.second_leg.source_event_id)
                    and (
                        cycle.execution.unwind_leg is None
                        or bool(cycle.execution.unwind_leg.source_event_id)
                    )
                ),
                complete_settlement_evidence=(
                    len(context.opening_source_event_ids) == 2
                    and all(context.opening_source_event_ids.values())
                    and bool(context.closing_source_event_id)
                    and len(context.resolution_source_event_ids) == 2
                    and all(context.resolution_source_event_ids.values())
                    and len(context.resolution_received_at_ms) == 2
                    and min(context.resolution_received_at_ms.values())
                    >= context.expiry_ms
                    and context.label_available_at_ms
                    >= max(context.resolution_received_at_ms.values())
                ),
                immutable_public_capture_evidence=(
                    context.immutable_public_capture_evidence
                ),
                signal_strength=(
                    None
                    if decision.edge_basis is V07EdgeBasis.STRUCTURAL
                    else abs(q5 - q15)
                ),
                execution_status=cycle.execution.status.value,
                economic_attempt=economic_attempt,
                causal_no_fill=causal_no_fill,
                reconciliation_status=(
                    "causal_no_fill_zero_pnl" if causal_no_fill else "settled_execution"
                ),
                decision_action=decision.action.value,
                lock_provenance=lock_provenance,
                edge_basis=decision.edge_basis,
                edge_evaluation_quantity=decision.edge_evaluation_quantity,
                structural_worst_case_payoff_per_pair=(
                    decision.structural_worst_case_payoff_per_pair
                ),
                structural_net_floor_per_pair=(decision.structural_net_floor_per_pair),
                structural_quantity_executable=(
                    decision.structural_quantity_executable
                ),
                quantity_selection_basis=decision.quantity_selection_basis,
                quantity_candidate_breakpoint_count=(
                    decision.quantity_candidate_breakpoint_count
                ),
                selected_guaranteed_total_pnl=(decision.selected_guaranteed_total_pnl),
                selected_uncertainty_adjusted_total_pnl=(
                    decision.selected_uncertainty_adjusted_total_pnl
                ),
                probability_diagnostics_applicable=(
                    decision.probability_diagnostics_applicable
                ),
            )
        )
    return _SettingResult(
        setting_id=setting_id,
        model=model,
        shrinkage=shrinkage,
        validation_veto=veto,
        simulations=MappingProxyType(simulations),
        cycles=tuple(cycles),
        forecasts=tuple(forecasts),
        attempts=tuple(attempts),
    )


def _neighbor_settings(
    *,
    primary: _SettingResult,
    contexts: Sequence[_RawDecisionContext],
    strategy: V07StrategyConfig,
    candidate_weights: tuple[Decimal, ...],
    sensitivity: _SensitivityGrid,
) -> tuple[_SettingResult, ...]:
    if primary.shrinkage is None:
        return ()
    definitions: list[tuple[str, SharedTerminalModelConfig, Decimal | None]] = []
    floor = primary.model.annualized_volatility_floor
    for offset in sensitivity.volatility_floor_offsets:
        neighboring_floor = floor + offset
        if neighboring_floor <= Decimal(0) or neighboring_floor == floor:
            continue
        definitions.append(
            (
                f"volatility_floor_{neighboring_floor}",
                replace(
                    primary.model,
                    annualized_volatility_floor=neighboring_floor,
                ),
                None,
            )
        )
    selected = primary.shrinkage.model_weight
    for offset in sensitivity.model_weight_offsets:
        neighboring_weight = selected + offset
        if not Decimal(0) <= neighboring_weight <= Decimal(1):
            continue
        if neighboring_weight == selected:
            continue
        definitions.append(
            (
                f"model_weight_{neighboring_weight}",
                primary.model,
                neighboring_weight,
            )
        )
    setting_ids = tuple(item[0] for item in definitions)
    if not setting_ids or len(set(setting_ids)) != len(setting_ids):
        raise ValueError("preregistered sensitivity grid has no unique neighbors")
    return tuple(
        _run_setting(
            setting_id=setting_id,
            contexts=contexts,
            model=model,
            strategy=strategy,
            candidate_weights=candidate_weights,
            forced_model_weight=forced_weight,
        )
        for setting_id, model, forced_weight in definitions
    )


def _setting_document(
    setting: _SettingResult, *, include_cycles: bool
) -> dict[str, Any]:
    latencies = tuple(
        latency
        for cycle in setting.cycles
        if (latency := cycle.effective_signal_to_execution_latency_ms) is not None
    )
    basis_counts = {
        basis.value: sum(
            cycle.forecast_availability_basis is basis for cycle in setting.cycles
        )
        for basis in V07ForecastAvailabilityBasis
    }
    document: dict[str, Any] = {
        "setting_id": setting.setting_id,
        "model_config": setting.model.to_document(),
        "model_config_sha256": setting.model.artifact_hash,
        "shrinkage": (
            None if setting.shrinkage is None else setting.shrinkage.to_document()
        ),
        "validation_veto": (
            None
            if setting.validation_veto is None
            else setting.validation_veto.to_document()
        ),
        "predictive_model_required": setting.shrinkage is not None,
        "forecast_count": len(setting.forecasts),
        "action_reconciliation_count": len(setting.attempts),
        "economic_attempt_count": sum(
            attempt.economic_attempt for attempt in setting.attempts
        ),
        "causal_no_fill_count": sum(
            attempt.causal_no_fill for attempt in setting.attempts
        ),
        "net_pnl": str(setting.net_pnl),
        "timing": {
            "forecast_availability_basis_counts": basis_counts,
            "effective_signal_to_execution_latency_ms": {
                "values": list(latencies),
                "minimum": None if not latencies else min(latencies),
                "maximum": None if not latencies else max(latencies),
            },
            "verified_receipt_paths_execute_after_receipt_plus_taker_delay": True,
            "counterfactual_computation_delay_is_preregistered": True,
            "counterfactual_delay_is_production_runtime_validation": False,
            "paper_replay_only": True,
        },
        "edge_basis_breakdown": {
            "predictive": {
                "forecast_count": sum(
                    row.edge_basis is V07EdgeBasis.PREDICTIVE
                    for row in setting.forecasts
                ),
                "action_reconciliation_count": sum(
                    row.edge_basis is V07EdgeBasis.PREDICTIVE
                    for row in setting.attempts
                ),
                "net_pnl": str(setting.predictive_net_pnl),
            },
            "structural": {
                "forecast_count": sum(
                    row.edge_basis is V07EdgeBasis.STRUCTURAL
                    for row in setting.forecasts
                ),
                "action_reconciliation_count": sum(
                    row.edge_basis is V07EdgeBasis.STRUCTURAL
                    for row in setting.attempts
                ),
                "net_pnl": str(setting.structural_net_pnl),
                "separate_true_edge_gate_implemented": True,
                "requires_realized_execution_and_settlement_pnl": True,
                "brier_ece_and_model_neighborhood_not_applicable": True,
            },
        },
        "quantity_selection": {
            "basis_by_forecast": [
                row.quantity_selection_basis.value for row in setting.forecasts
            ],
            "candidate_breakpoint_count_by_forecast": [
                row.quantity_candidate_breakpoint_count for row in setting.forecasts
            ],
            "selected_guaranteed_total_pnl_by_forecast": [
                None
                if row.selected_guaranteed_total_pnl is None
                else str(row.selected_guaranteed_total_pnl)
                for row in setting.forecasts
            ],
            "selected_uncertainty_adjusted_total_pnl_by_forecast": [
                None
                if row.selected_uncertainty_adjusted_total_pnl is None
                else str(row.selected_uncertainty_adjusted_total_pnl)
                for row in setting.forecasts
            ],
            "paper_only": True,
        },
        "forecasts": [row.to_document() for row in setting.forecasts],
        "action_reconciliations": [row.to_document() for row in setting.attempts],
    }
    if include_cycles:
        document["cycles"] = [cycle.to_document() for cycle in setting.cycles]
    return document


def _strict_context_capture_evidence(context: _RawDecisionContext) -> bool:
    evidence = dict(context.capture_config_evidence)
    expected_keys = {
        "schema_version",
        "paper_only",
        "public_only",
        "new_orders_disabled",
        "generated_fixture",
        "capture_config_sha256",
    }
    if set(evidence) != expected_keys:
        return False
    if evidence["schema_version"] != CAPTURE_CONFIG_SCHEMA_VERSION:
        return False
    if any(
        evidence[name] is not True
        for name in ("paper_only", "public_only", "new_orders_disabled")
    ):
        return False
    if type(evidence["generated_fixture"]) is not bool:
        return False
    if evidence["generated_fixture"] is not False:
        return False
    config_hash = evidence["capture_config_sha256"]
    if (
        not isinstance(config_hash, str)
        or len(config_hash) != 64
        or any(character not in "0123456789abcdef" for character in config_hash)
    ):
        return False
    capture_config_identity = context.capture_identity.get("capture_config")
    if not isinstance(capture_config_identity, Mapping):
        return False
    return (
        capture_config_identity.get("sha256") == config_hash
        and context.immutable_public_capture_evidence
    )


def _evaluate_builder_authoritative_run(
    *,
    contexts: Sequence[_RawDecisionContext],
    primary: _SettingResult,
    lock_index: _PreLabelLockIndex | None,
    preregistration_sha256: str,
    parameter_neighborhood: ParameterNeighborhoodEvidence | None,
) -> tuple[V07LockedOOSEvaluation, Mapping[str, Any] | None]:
    """Evaluate one builder run after revalidating its loaded evidence spine.

    The supported authority boundary is the high-level builder CLI, which reads
    capture roots and the pre-expiry journal before this function is reached.
    This is an API-misuse guard, not cryptographic protection from arbitrary
    local Python execution or source modification.
    """

    evaluation = evaluate_locked_oos_evidence(
        forecasts=primary.forecasts,
        economic_attempts=primary.attempts,
        parameter_neighborhood=parameter_neighborhood,
    )
    if lock_index is None:
        return evaluation, None
    test_contexts = tuple(context for context in contexts if context.split == "test")
    if not test_contexts:
        return evaluation, None
    generated_fixture_count = sum(
        context.capture_config_evidence.get("generated_fixture") is True
        for context in test_contexts
    )
    if generated_fixture_count:
        return evaluation, None
    strict_contexts = tuple(
        context
        for context in test_contexts
        if _strict_context_capture_evidence(context)
    )
    if len(strict_contexts) != len(test_contexts):
        return evaluation, None
    if any(not row.lock_provenance.verified for row in primary.forecasts):
        return evaluation, None
    if any(not row.lock_provenance.verified for row in primary.attempts):
        return evaluation, None
    if any(
        row.forecast_availability_basis
        is not V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
        or row.forecast_available_at_ms >= row.expiry_ms
        or row.lock_provenance.prediction_received_at_ms
        != row.forecast_available_at_ms
        for row in primary.forecasts
    ):
        return evaluation, None
    if any(
        row.forecast_availability_basis
        is not V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
        or row.forecast_available_at_ms >= row.expiry_ms
        or row.first_execution_not_before_ms != row.forecast_available_at_ms + 250
        for row in primary.attempts
    ):
        return evaluation, None

    contexts_by_pair_tau = {
        (context.canonical_pair_id, context.decision_tau_seconds): context
        for context in test_contexts
    }
    if len(contexts_by_pair_tau) != len(test_contexts):
        raise ValueError("test contexts have duplicate pair/tau identities")
    if len(primary.cycles) != len(test_contexts):
        raise ValueError("not every test context produced a replay cycle")
    attempt_identities = {row.identity for row in primary.attempts}
    actionable_count = 0
    for cycle in primary.cycles:
        context = contexts_by_pair_tau.get(
            (cycle.event_cluster_id, cycle.decision_tau_seconds)
        )
        if context is None:
            raise ValueError("replay cycle has no matching verified test context")
        if cycle.decision.action is PairAction.NO_TRADE:
            if context.identity in attempt_identities:
                raise ValueError("no-trade cycle unexpectedly has reconciliation row")
            continue
        actionable_count += 1
        if context.identity not in attempt_identities:
            raise ValueError("actionable cycle is missing reconciliation row")
    if actionable_count != len(primary.attempts):
        raise ValueError("reconciliation ledger does not match actionable cycles")

    universe_hashes = {
        row.lock_provenance.test_universe_sha256 for row in primary.forecasts
    }
    universe_hashes.update(
        row.lock_provenance.test_universe_sha256 for row in primary.attempts
    )
    if universe_hashes != {lock_index.universe.test_universe_sha256}:
        raise ValueError("builder rows do not bind to the verified test universe")

    def hash_documents(documents: Sequence[Mapping[str, Any]]) -> str:
        return hashlib.sha256(canonical_json_bytes(list(documents))).hexdigest()

    forecast_documents = sorted(
        (row.to_document() for row in primary.forecasts),
        key=lambda item: (
            item["expiry_ms"],
            item["decision_tau_seconds"],
            item["canonical_pair_id"],
        ),
    )
    reconciliation_documents = sorted(
        (row.to_document() for row in primary.attempts),
        key=lambda item: item["attempt_id"],
    )
    neighborhood_document: Mapping[str, Any] = (
        {"parameter_neighborhood": None}
        if parameter_neighborhood is None
        else parameter_neighborhood.to_document()
    )
    context_documents = [
        {
            "canonical_pair_id": context.canonical_pair_id,
            "expiry_ms": context.expiry_ms,
            "expiry_cluster_id": context.expiry_cluster_id,
            "decision_tau_seconds": context.decision_tau_seconds,
            "capture_config_evidence": _json_document(
                context.capture_config_evidence
            ),
            "capture_identity": _json_document(context.capture_identity),
        }
        for context in sorted(test_contexts, key=lambda item: item.identity)
    ]
    cycle_reconciliation_document = {
        "cycles": [
            cycle.to_document()
            for cycle in sorted(
                primary.cycles,
                key=lambda item: (
                    item.event_cluster_id,
                    item.decision_tau_seconds,
                ),
            )
        ],
        "reconciliations": reconciliation_documents,
    }
    verification_chain = {
        "schema_version": "btc-5m-15m-v07-builder-verification-chain.v5",
        "builder_id": "scripts.build_btc_twap_relative_value_v07_counterfactual",
        "supported_entry_point": "build_counterfactual_report",
        "source_baseline": SOURCE_BASELINE,
        "preregistration_sha256": preregistration_sha256,
        "capture_config_schema_version": CAPTURE_CONFIG_SCHEMA_VERSION,
        "strict_capture_config_count": len(strict_contexts),
        "generated_fixture_count": generated_fixture_count,
        "immutable_capture_count": sum(
            context.immutable_public_capture_evidence for context in test_contexts
        ),
        "test_context_count": len(test_contexts),
        "forecast_count": len(primary.forecasts),
        "reconciliation_count": len(primary.attempts),
        "predictive_forecast_count": sum(
            row.edge_basis is V07EdgeBasis.PREDICTIVE for row in primary.forecasts
        ),
        "structural_forecast_count": sum(
            row.edge_basis is V07EdgeBasis.STRUCTURAL for row in primary.forecasts
        ),
        "non_edge_forecast_count": sum(
            row.edge_basis is V07EdgeBasis.NONE for row in primary.forecasts
        ),
        "predictive_reconciliation_count": sum(
            row.edge_basis is V07EdgeBasis.PREDICTIVE for row in primary.attempts
        ),
        "structural_reconciliation_count": sum(
            row.edge_basis is V07EdgeBasis.STRUCTURAL for row in primary.attempts
        ),
        "structural_executable_positive_floor_reconciliation_count": sum(
            row.edge_basis is V07EdgeBasis.STRUCTURAL
            and row.structural_quantity_executable
            and row.structural_net_floor_per_pair is not None
            and row.structural_net_floor_per_pair > 0
            and row.selected_guaranteed_total_pnl is not None
            and row.selected_guaranteed_total_pnl > 0
            for row in primary.attempts
        ),
        "preexpiry_prediction_receipt_count": len(primary.forecasts),
        "receipt_timed_replay_count": len(primary.attempts),
        "counterfactual_timing_count": 0,
        "test_universe_sha256": lock_index.universe.test_universe_sha256,
        "prelabel_lock_journal_identity_sha256": hashlib.sha256(
            canonical_json_bytes(_json_document(lock_index.journal_identity))
        ).hexdigest(),
        "capture_config_and_tree_verification_sha256": hashlib.sha256(
            canonical_json_bytes(context_documents)
        ).hexdigest(),
        "cycle_reconciliation_sha256": hashlib.sha256(
            canonical_json_bytes(cycle_reconciliation_document)
        ).hexdigest(),
        "forecast_rows_sha256": hash_documents(forecast_documents),
        "reconciliation_rows_sha256": hash_documents(reconciliation_documents),
        "parameter_neighborhood_sha256": hashlib.sha256(
            canonical_json_bytes(neighborhood_document)
        ).hexdigest(),
    }
    verification_sha256 = hashlib.sha256(
        canonical_json_bytes(verification_chain)
    ).hexdigest()

    predictive_rows = tuple(
        row
        for row in primary.attempts
        if row.edge_basis is V07EdgeBasis.PREDICTIVE
    )
    structural_rows = tuple(
        row
        for row in primary.attempts
        if row.edge_basis is V07EdgeBasis.STRUCTURAL
    )

    def complete_track(rows: Sequence[LockedOOSEconomicAttempt]) -> bool:
        return bool(rows) and all(
            row.complete_cost_evidence
            and row.complete_execution_evidence
            and row.complete_settlement_evidence
            and row.immutable_public_capture_evidence
            and (row.explainable if row.economic_attempt else row.causal_no_fill)
            for row in rows
        )

    predictive_complete = complete_track(predictive_rows)
    structural_complete = complete_track(structural_rows) and all(
        row.structural_quantity_executable
        and row.structural_net_floor_per_pair is not None
        and row.structural_net_floor_per_pair > 0
        and row.selected_guaranteed_total_pnl is not None
        and row.selected_guaranteed_total_pnl > 0
        and not row.probability_diagnostics_applicable
        for row in structural_rows
    )
    complete_evidence = complete_track(primary.attempts)
    diagnostic = evaluate_gate_mechanism_diagnostic(
        forecasts=primary.forecasts,
        economic_attempts=primary.attempts,
        parameter_neighborhood=parameter_neighborhood,
    )
    predictive_true = (
        diagnostic.mathematical_predictive_gate_conditions_satisfied
        and predictive_complete
    )
    structural_true = (
        diagnostic.mathematical_structural_gate_conditions_satisfied
        and structural_complete
    )
    true_edge = predictive_true or structural_true
    predictive_qualified = (
        evaluation.predictive_net_pnl if predictive_true else None
    )
    structural_qualified = (
        evaluation.structural_net_pnl if structural_true else None
    )
    qualified_values = tuple(
        value
        for value in (predictive_qualified, structural_qualified)
        if value is not None
    )
    qualified_net_pnl = (
        sum(qualified_values, Decimal("0")) if qualified_values else None
    )
    if true_edge:
        status = V07EvidenceStatus.TRUE_EDGE_GATE_SATISFIED
    elif not (
        diagnostic.sample_gate_passed
        or diagnostic.structural_sample_gate_passed
    ):
        status = V07EvidenceStatus.INSUFFICIENT_DATA
    elif not (
        evaluation.predictive_diagnostic_positive_sample_pnl
        or evaluation.structural_diagnostic_positive_sample_pnl
    ):
        status = V07EvidenceStatus.NOT_PROFITABLE
    else:
        status = V07EvidenceStatus.POSITIVE_BUT_NOT_TRUE_EDGE

    removed_reasons = {
        "positive_sample_pnl_is_diagnostic_without_builder_verification",
        "builder_verified_evidence_chain_missing",
        "auditable_prelabel_manifest_prediction_decision_lock_missing",
    }
    if predictive_complete:
        removed_reasons.add("cost_execution_or_settlement_evidence_incomplete")
    if structural_complete:
        removed_reasons.add(
            "structural_cost_execution_or_settlement_evidence_incomplete"
        )
    reason_codes = tuple(
        reason for reason in evaluation.reason_codes if reason not in removed_reasons
    )
    evaluation = replace(
        evaluation,
        status=status,
        qualified_net_pnl=qualified_net_pnl,
        predictive_qualified_net_pnl=predictive_qualified,
        predictive_true_edge_gate_satisfied=predictive_true,
        structural_qualified_net_pnl=structural_qualified,
        structural_true_edge_gate_satisfied=structural_true,
        positive_net_pnl_user_check_passed=true_edge,
        true_edge_gate_satisfied=true_edge,
        auditable_prelabel_lock_evidence=True,
        builder_verified_evidence_chain=True,
        builder_verification_sha256=verification_sha256,
        complete_cost_and_execution_evidence=complete_evidence,
        predictive_complete_cost_and_execution_evidence=predictive_complete,
        structural_complete_cost_and_execution_evidence=structural_complete,
        reason_codes=reason_codes,
    )
    authority_document = MappingProxyType(
        {
            "schema_version": (
                "btc-5m-15m-v07-builder-authoritative-evidence.v5"
            ),
            "supported_entry_point": "build_counterfactual_report",
            "verification_chain_sha256": verification_sha256,
            "verification_chain": verification_chain,
            "trust_model": "api_misuse_guard_not_cryptographic_provenance",
            "external_signature_or_third_party_timestamp_present": False,
            "adversarial_provenance_claimed": False,
            "arbitrary_local_code_execution_out_of_scope": True,
            "residual_threat": (
                "an actor with arbitrary local Python execution or source-modification "
                "authority can invoke or alter private internals and synthesize an "
                "authority-shaped result"
            ),
        }
    )
    return evaluation, authority_document


def build_counterfactual_report(
    *,
    manifest_path: Path,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = _load_json(manifest_path, label="counterfactual manifest")
    _assert_exact_keys(
        manifest,
        allowed=_MANIFEST_KEYS,
        required=_REQUIRED_MANIFEST_KEYS,
        label="counterfactual manifest",
    )
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported counterfactual manifest schema")
    _confirm_paper_only_boundary(manifest)
    manifest_dir = manifest_path.parent
    preregistration_path = _path(
        manifest_dir,
        manifest.get("preregistration_path"),
        label="preregistration_path",
    )
    preregistration = _load_json(
        preregistration_path,
        label="v0.7 preregistration",
    )
    model, strategy, candidate_weights, sensitivity = _verify_preregistration(
        preregistration,
        preregistration_path=preregistration_path,
    )
    cases = manifest.get("cases")
    if (
        not isinstance(cases, list)
        or not cases
        or any(not isinstance(case, Mapping) for case in cases)
    ):
        raise ValueError("counterfactual manifest cases must be non-empty objects")
    contexts = tuple(
        context
        for case_index, case in enumerate(cases)
        for context in _load_case_contexts(
            case,
            manifest_case_index=case_index,
            manifest_dir=manifest_dir,
            model=model,
            strategy=strategy,
        )
    )
    _assert_split_policy(contexts)
    lock_index = _load_prelabel_lock_index(
        manifest.get("prelabel_lock_journal_root"),
        manifest_dir=manifest_dir,
        contexts=contexts,
        preregistration_sha256=_sha256(preregistration_path),
    )
    primary = _run_setting(
        setting_id="primary",
        contexts=contexts,
        model=model,
        strategy=strategy,
        candidate_weights=candidate_weights,
        lock_index=lock_index,
    )
    neighbors = _neighbor_settings(
        primary=primary,
        contexts=contexts,
        strategy=strategy,
        candidate_weights=candidate_weights,
        sensitivity=sensitivity,
    )
    neighborhood = (
        None
        if not neighbors
        else ParameterNeighborhoodEvidence(
            net_pnl_by_setting={
                setting.setting_id: setting.predictive_net_pnl for setting in neighbors
            },
            required_setting_ids=tuple(setting.setting_id for setting in neighbors),
        )
    )
    preregistration_sha256 = _sha256(preregistration_path)
    evaluation, builder_authority = _evaluate_builder_authoritative_run(
        contexts=contexts,
        primary=primary,
        lock_index=lock_index,
        preregistration_sha256=preregistration_sha256,
        parameter_neighborhood=neighborhood,
    )
    context_documents = [
        {
            "canonical_pair_id": context.canonical_pair_id,
            "expiry_cluster_id": context.expiry_cluster_id,
            "event_cluster_alias": context.event_cluster_alias,
            "manifest_case_index": context.manifest_case_index,
            "split": context.split,
            "decision_tau_seconds": context.decision_tau_seconds,
            "decision_at_ms": context.decision_at_ms,
            "expiry_ms": context.expiry_ms,
            "label_available_at_ms": context.label_available_at_ms,
            "strike_5": str(context.settlement_state.strike_5),
            "strike_15": str(context.settlement_state.strike_15),
            "actual_5_up": context.actual_5_up,
            "actual_15_up": context.actual_15_up,
            "market_q_5_up": (
                None if context.market_q_5_up is None else str(context.market_q_5_up)
            ),
            "market_q_15_up": (
                None if context.market_q_15_up is None else str(context.market_q_15_up)
            ),
            "raw_top_ask_q_5_up_diagnostic": (
                None
                if context.raw_top_ask_q_5_up is None
                else str(context.raw_top_ask_q_5_up)
            ),
            "raw_top_ask_q_15_up_diagnostic": (
                None
                if context.raw_top_ask_q_15_up is None
                else str(context.raw_top_ask_q_15_up)
            ),
            "opening_source_event_ids": dict(context.opening_source_event_ids),
            "closing_source_event_id": context.closing_source_event_id,
            "resolution_source_event_ids": dict(context.resolution_source_event_ids),
            "resolution_received_at_ms": dict(context.resolution_received_at_ms),
            "immutable_public_capture_evidence": (
                context.immutable_public_capture_evidence
            ),
            "capture_config_evidence": _json_document(context.capture_config_evidence),
            "clock_uncertainty_ms": context.clock_uncertainty_ms,
            "capture_identity": _json_document(context.capture_identity),
            "rules_and_fees": _json_document(context.rules_and_fees),
            "simulation": (
                None
                if primary.simulations.get(context.identity) is None
                else primary.simulations[context.identity].to_document()
            ),
            "simulation_status": (
                "not_required_or_unavailable_after_structural_scan"
                if primary.simulations.get(context.identity) is None
                else "available_predictive_diagnostic"
            ),
            "predictive_simulation_required_for_structural_decision": False,
        }
        for context in sorted(
            contexts,
            key=lambda item: (
                item.split,
                item.expiry_ms,
                item.canonical_pair_id,
                item.decision_tau_seconds,
            ),
        )
    ]
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "source_baseline": SOURCE_BASELINE,
        "manifest": {
            "path": manifest_path.name,
            "sha256": _sha256(manifest_path),
        },
        "preregistration": {
            "path": preregistration_path.name,
            "sha256": _sha256(preregistration_path),
            "drafted_at": preregistration["drafted_at"],
            "draft_context": _json_document(preregistration["draft_context"]),
            "draft_only": True,
            "deployment_claimed": False,
            "postdates_known_48_attempt_v06_shadow_result": True,
            "provides_ex_ante_qualification_for_known_v06_results": False,
        },
        "settlement_model_id": SETTLEMENT_MODEL_ID,
        "model_version": MODEL_VERSION,
        "model_config": model.to_document(),
        "model_config_sha256": model.artifact_hash,
        "split_policy": {
            "shuffle": False,
            "parameters_selected_on": "train_only",
            "validation_role": "veto_only_no_retraining",
            "test_is_locked": True,
            "common_expiry_is_split_and_statistical_unit": True,
            "independent_cluster_key": "sha256(capture_derived_common_expiry_ms)",
            "pair_integrity_key": (
                "sha256(common_expiry_ms,5m_market_and_condition_identity,"
                "15m_market_and_condition_identity)"
            ),
            "one_pair_per_common_expiry": True,
            "manifest_expiry_field_accepted": False,
            "manifest_event_cluster_id_is_alias_only": True,
        },
        "qualification_authority": {
            "public_evaluator_can_grant_qualification": False,
            "supported_authority_entry_point": "build_counterfactual_report",
            "builder_authoritative_revalidation_required": True,
            "generic_caller_rows_plus_chain_issuer_distributed": False,
            "caller_rows_hashes_timestamps_and_evidence_flags_are_authority": False,
            "generated_fixture_builder_authority_allowed": False,
            "trust_model": "api_misuse_guard_not_cryptographic_provenance",
            "external_signature_or_third_party_timestamp_present": False,
            "adversarial_provenance_claimed": False,
            "arbitrary_local_code_execution_out_of_scope": True,
            "residual_threat": (
                "arbitrary local Python execution or source modification can "
                "invoke or alter private internals and synthesize an "
                "authority-shaped result"
            ),
        },
        "timing_policy": {
            "forecast_available_at_ms_verified_source": (
                "immutable_prediction_receipt_received_at_ms"
            ),
            "verified_receipt_strictly_before_common_expiry": True,
            "first_execution_eligibility": (
                "forecast_available_at_ms_plus_captured_taker_delay_ms"
            ),
            "counterfactual_computation_availability_delay_ms": (
                strategy.counterfactual_computation_availability_delay_ms
            ),
            "counterfactual_delay_is_measured_production_runtime": False,
            "counterfactual_timing_role": "paper_replay_timing_diagnostic_only",
            "effective_signal_to_execution_latency_serialized": True,
        },
        "edge_basis_policy": {
            "common_safety_checks_precede_all_candidates": True,
            "structural_evaluated_before_predictive_readiness": True,
            "structural_depends_on_predictive_veto": False,
            "structural_floor_uses_feasible_shared_terminal_states": True,
            "full_decision_quantity_depth_and_dynamic_fees_required": True,
            "structural_requires_two_executable_legs_and_positive_floor": True,
            "predictive_and_structural_evidence_separate": True,
            "predictive_qualification_excludes_structural_rows": True,
            "quantity_is_optimized_not_budget_filled": True,
            "structural_quantity_objective": (
                "maximum_guaranteed_total_pnl_over_joint_depth_breakpoints"
            ),
            "predictive_quantity_objective": (
                "maximum_uncertainty_adjusted_total_pnl_over_joint_depth_breakpoints"
            ),
            "stable_quantity_tie_break": (
                "higher_per_pair_edge_then_lower_quantity_then_action_id"
            ),
            "structural_scan_requires_monte_carlo": False,
            "structural_true_edge_gate_implemented": True,
            "structural_gate_requires_real_execution_and_realized_pnl": True,
            "structural_gate_requires_brier_ece_or_model_neighborhood": False,
        },
        "prelabel_lock": {
            "journal_supplied": lock_index is not None,
            "counterfactual_when_absent": True,
            "journal_root": (
                None if lock_index is None else lock_index.journal_root.name
            ),
            "journal_identity": (
                None
                if lock_index is None
                else _json_document(lock_index.journal_identity)
            ),
            "test_universe_sha256": (
                None if lock_index is None else lock_index.universe.test_universe_sha256
            ),
            "builder_verified_evidence": (
                None
                if builder_authority is None
                else _json_document(builder_authority)
            ),
            "caller_supplied_evidence_flags_are_not_authority": True,
            "trust_model": "api_misuse_guard_not_cryptographic_provenance",
            "arbitrary_local_code_execution_out_of_scope": True,
            "adversarial_provenance_claimed": False,
        },
        "primary": _setting_document(primary, include_cycles=True),
        "sensitivity": {
            "diagnostic_only": True,
            "action_changing": False,
            "common_random_numbers_across_model_configs": True,
            "volatility_floor_offsets": [
                str(value) for value in sensitivity.volatility_floor_offsets
            ],
            "model_weight_offsets": [
                str(value) for value in sensitivity.model_weight_offsets
            ],
            "settings": [
                _setting_document(setting, include_cycles=True) for setting in neighbors
            ],
        },
        "parameter_neighborhood": (
            None if neighborhood is None else neighborhood.to_document()
        ),
        "evaluation": evaluation.to_document(),
        "contexts": context_documents,
        "safety": {
            "paper_only": True,
            "live_orders_disabled_guard_confirmed": True,
            "orders_submitted": 0,
            "credentials_used": 0,
            "authenticated_endpoints_used": 0,
            "proxy_urls_used": 0,
            "network_calls_made_by_builder": 0,
        },
        "limitations": [
            "Generated fixtures prove invariants only and never satisfy "
            "economic gates.",
            "The public evaluator cannot grant qualification from caller-created "
            "rows, receipt-looking strings, or self-reported evidence booleans.",
            "Structural-floor and predictive PnL are reported and gated separately; "
            "the structural gate still requires 100 real common-expiry attempts, "
            "positive realized PnL, bootstrap, concentration, and complete evidence.",
            "This v6 draft was created after the known 48-attempt v0.6 shadow "
            "result and cannot provide ex-ante qualification for those outcomes.",
            "Counterfactual computation availability uses a preregistered 5000ms "
            "paper timing assumption, not measured production runtime validation.",
            "A positive shadow or counterfactual sample is not production validation.",
            "Qualified net PnL remains null until every cluster-level gate passes.",
        ],
    }
    report["report_sha256"] = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = build_counterfactual_report(manifest_path=args.manifest)
    _atomic_write_json(args.output.expanduser().resolve(), report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
