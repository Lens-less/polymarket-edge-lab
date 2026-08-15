from __future__ import annotations

import hashlib

import pytest

from src.edge_lab.btc_twap_relative_value_v07 import (
    CANONICAL_EVENT_CLUSTER_PREFIX,
    V07EdgeBasis,
    V07ForecastAvailabilityBasis,
    V07QuantitySelectionBasis,
)
from src.edge_lab.btc_twap_relative_value_v07_evaluation import (
    LockedOOSEconomicAttempt,
    LockedOOSForecastRow,
    ParameterNeighborhoodEvidence,
    PreLabelLockProvenance,
    V07EvidenceStatus,
    _issue_verified_prelabel_lock_provenance,
    evaluate_gate_mechanism_diagnostic,
    evaluate_locked_oos_evidence,
)

D = __import__("decimal").Decimal
_EXPIRY_BY_LABEL: dict[str, int] = {}


def _cluster(label: str) -> str:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return f"{CANONICAL_EVENT_CLUSTER_PREFIX}{digest}"


def _expiry(label: str) -> int:
    return _EXPIRY_BY_LABEL.setdefault(label, 20_000 + len(_EXPIRY_BY_LABEL) * 100)


def _counterfactual() -> PreLabelLockProvenance:
    return PreLabelLockProvenance.counterfactual("test_counterfactual")


def _verified_lock(
    *,
    decision_at_ms: int,
    expiry_ms: int,
    suffix: str,
    received_at_ms: int | None = None,
) -> PreLabelLockProvenance:
    receipt_at_ms = decision_at_ms + 1 if received_at_ms is None else received_at_ms
    return _issue_verified_prelabel_lock_provenance(
        test_universe_sha256=hashlib.sha256(b"universe").hexdigest(),
        test_universe_locked_at_ms=100,
        test_universe_received_at_ms=200,
        test_universe_receipt_id="universe-receipt",
        prediction_locked_at_ms=decision_at_ms,
        prediction_received_at_ms=receipt_at_ms,
        prediction_receipt_id=f"prediction-{suffix}",
        forecast_payload_sha256=hashlib.sha256(
            f"forecast-{suffix}".encode()
        ).hexdigest(),
        decision_payload_sha256=hashlib.sha256(
            f"decision-{suffix}".encode()
        ).hexdigest(),
    )


def _forecast(
    cluster_label: str,
    *,
    tau: int = 60,
    expiry_ms: int | None = None,
    lock: PreLabelLockProvenance | None = None,
    forecast_q_5_up: str = "0.99",
    forecast_q_15_up: str = "0.01",
    market_q_5_up: str = "0.5",
    market_q_15_up: str = "0.5",
    edge_basis: V07EdgeBasis = V07EdgeBasis.PREDICTIVE,
) -> LockedOOSForecastRow:
    decision_at_ms = 1_000 + tau
    actual_expiry_ms = _expiry(cluster_label) if expiry_ms is None else expiry_ms
    actual_lock = lock or _counterfactual()
    if actual_lock.verified:
        assert actual_lock.prediction_received_at_ms is not None
        available_at_ms = actual_lock.prediction_received_at_ms
        basis = V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
    else:
        available_at_ms = decision_at_ms + 5_000
        basis = V07ForecastAvailabilityBasis.PREREGISTERED_COUNTERFACTUAL_DELAY
    return LockedOOSForecastRow(
        event_cluster_id=_cluster(cluster_label),
        event_cluster_alias=cluster_label,
        expiry_ms=actual_expiry_ms,
        decision_tau_seconds=tau,
        decision_at_ms=decision_at_ms,
        forecast_available_at_ms=available_at_ms,
        forecast_availability_basis=basis,
        label_available_at_ms=100_000,
        forecast_q_5_up=(
            None if edge_basis is V07EdgeBasis.STRUCTURAL else D(forecast_q_5_up)
        ),
        forecast_q_15_up=(
            None if edge_basis is V07EdgeBasis.STRUCTURAL else D(forecast_q_15_up)
        ),
        market_q_5_up=(
            None if edge_basis is V07EdgeBasis.STRUCTURAL else D(market_q_5_up)
        ),
        market_q_15_up=(
            None if edge_basis is V07EdgeBasis.STRUCTURAL else D(market_q_15_up)
        ),
        raw_top_ask_q_5_up=(
            None if edge_basis is V07EdgeBasis.STRUCTURAL else D("0.5")
        ),
        raw_top_ask_q_15_up=(
            None if edge_basis is V07EdgeBasis.STRUCTURAL else D("0.5")
        ),
        actual_5_up=True,
        actual_15_up=False,
        strike_5=D("100"),
        strike_15=D("110"),
        lock_provenance=actual_lock,
        edge_basis=edge_basis,
        edge_evaluation_quantity=D("5"),
        structural_worst_case_payoff_per_pair=(
            D("1") if edge_basis is V07EdgeBasis.STRUCTURAL else D("0")
        ),
        structural_net_floor_per_pair=(
            D("0.1") if edge_basis is V07EdgeBasis.STRUCTURAL else D("-0.1")
        ),
        structural_quantity_executable=True,
        quantity_selection_basis=(
            V07QuantitySelectionBasis.STRUCTURAL_MAX_GUARANTEED_TOTAL_PNL
            if edge_basis is V07EdgeBasis.STRUCTURAL
            else V07QuantitySelectionBasis.PREDICTIVE_MAX_UNCERTAINTY_ADJUSTED_TOTAL_PNL
        ),
        quantity_candidate_breakpoint_count=2,
        selected_guaranteed_total_pnl=(
            D("0.5") if edge_basis is V07EdgeBasis.STRUCTURAL else D("-0.5")
        ),
        selected_uncertainty_adjusted_total_pnl=(
            None if edge_basis is V07EdgeBasis.STRUCTURAL else D("0.5")
        ),
        probability_diagnostics_applicable=(edge_basis is V07EdgeBasis.PREDICTIVE),
    )


def _attempt(
    cluster_label: str,
    net: str,
    *,
    tau: int = 60,
    expiry_ms: int | None = None,
    lock: PreLabelLockProvenance | None = None,
    causal_no_fill: bool = False,
    edge_basis: V07EdgeBasis = V07EdgeBasis.PREDICTIVE,
) -> LockedOOSEconomicAttempt:
    decision_at_ms = 1_000 + tau
    actual_expiry_ms = _expiry(cluster_label) if expiry_ms is None else expiry_ms
    actual_lock = lock or _counterfactual()
    if actual_lock.verified:
        assert actual_lock.prediction_received_at_ms is not None
        available_at_ms = actual_lock.prediction_received_at_ms
        basis = V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
    else:
        available_at_ms = decision_at_ms + 5_000
        basis = V07ForecastAvailabilityBasis.PREREGISTERED_COUNTERFACTUAL_DELAY
    first_not_before_ms = available_at_ms + 250
    first_observed_at_ms = first_not_before_ms
    return LockedOOSEconomicAttempt(
        attempt_id=f"attempt-{cluster_label}-{tau}",
        event_cluster_id=_cluster(cluster_label),
        event_cluster_alias=cluster_label,
        expiry_ms=actual_expiry_ms,
        decision_tau_seconds=tau,
        decision_at_ms=decision_at_ms,
        forecast_available_at_ms=available_at_ms,
        forecast_availability_basis=basis,
        first_execution_not_before_ms=first_not_before_ms,
        first_execution_observed_at_ms=first_observed_at_ms,
        effective_signal_to_execution_latency_ms=(
            first_observed_at_ms - decision_at_ms
        ),
        net_pnl=D(net),
        explainable=not causal_no_fill,
        complete_cost_evidence=True,
        complete_execution_evidence=True,
        complete_settlement_evidence=True,
        immutable_public_capture_evidence=True,
        signal_strength=(None if edge_basis is V07EdgeBasis.STRUCTURAL else D("0.98")),
        execution_status="no_fill" if causal_no_fill else "both_filled",
        economic_attempt=not causal_no_fill,
        causal_no_fill=causal_no_fill,
        reconciliation_status=(
            "causal_no_fill_zero_pnl" if causal_no_fill else "settled_execution"
        ),
        lock_provenance=actual_lock,
        edge_basis=edge_basis,
        edge_evaluation_quantity=D("5"),
        structural_worst_case_payoff_per_pair=(
            D("1") if edge_basis is V07EdgeBasis.STRUCTURAL else D("0")
        ),
        structural_net_floor_per_pair=(
            D("0.1") if edge_basis is V07EdgeBasis.STRUCTURAL else D("-0.1")
        ),
        structural_quantity_executable=True,
        quantity_selection_basis=(
            V07QuantitySelectionBasis.STRUCTURAL_MAX_GUARANTEED_TOTAL_PNL
            if edge_basis is V07EdgeBasis.STRUCTURAL
            else V07QuantitySelectionBasis.PREDICTIVE_MAX_UNCERTAINTY_ADJUSTED_TOTAL_PNL
        ),
        quantity_candidate_breakpoint_count=2,
        selected_guaranteed_total_pnl=(
            D("0.5") if edge_basis is V07EdgeBasis.STRUCTURAL else D("-0.5")
        ),
        selected_uncertainty_adjusted_total_pnl=(
            None if edge_basis is V07EdgeBasis.STRUCTURAL else D("0.5")
        ),
        probability_diagnostics_applicable=(edge_basis is V07EdgeBasis.PREDICTIVE),
    )


def _stable_neighborhood() -> ParameterNeighborhoodEvidence:
    required = ("base", "paths_low", "paths_high", "weight_low", "weight_high")
    return ParameterNeighborhoodEvidence(
        required_setting_ids=required,
        net_pnl_by_setting={setting: D("1") for setting in required},
    )


def test_verified_lock_provenance_cannot_be_publicly_constructed() -> None:
    with pytest.raises(TypeError, match="cannot be caller-constructed"):
        PreLabelLockProvenance(  # type: ignore[call-arg]
            status="verified_pre_label"
        )


def test_locked_oos_evaluation_marks_small_counterfactual_insufficient() -> None:
    forecasts = tuple(_forecast(f"c{i}") for i in range(10))
    attempts = tuple(_attempt(f"c{i}", "0.1") for i in range(10))
    result = evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=None,
    )
    assert result.status is V07EvidenceStatus.COUNTERFACTUAL_INSUFFICIENT
    assert result.qualified_net_pnl is None
    assert result.auditable_prelabel_lock_evidence is False
    assert "fewer_than_100_distinct_settled_expiry_clusters" in result.reason_codes


def test_public_evaluator_rejects_100_synthetic_verified_receipts() -> None:
    forecasts = []
    attempts = []
    for index in range(100):
        label = f"c{index}"
        decision_at_ms = 1_060
        lock = _verified_lock(
            decision_at_ms=decision_at_ms,
            expiry_ms=_expiry(label),
            suffix=label,
        )
        forecasts.append(_forecast(label, lock=lock))
        attempts.append(_attempt(label, "0.1", lock=lock))
    result = evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=_stable_neighborhood(),
    )
    assert result.status is V07EvidenceStatus.COUNTERFACTUAL_INSUFFICIENT
    assert result.diagnostic_positive_sample_pnl is True
    assert result.positive_net_pnl_user_check_passed is False
    assert result.true_edge_gate_satisfied is False
    assert result.auditable_prelabel_lock_evidence is False
    assert result.builder_verified_evidence_chain is False
    assert result.qualified_net_pnl is None
    assert result.largest_positive_cluster_to_total_net_pnl == D("0.01")
    assert "builder_verified_evidence_chain_missing" in result.reason_codes


def test_synthetic_gate_math_uses_non_economic_diagnostic_result() -> None:
    forecasts = []
    attempts = []
    for index in range(100):
        label = f"mechanism-{index}"
        lock = _verified_lock(
            decision_at_ms=1_060,
            expiry_ms=_expiry(label),
            suffix=label,
        )
        forecasts.append(_forecast(label, lock=lock))
        attempts.append(_attempt(label, "0.1", lock=lock))

    diagnostic = evaluate_gate_mechanism_diagnostic(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=_stable_neighborhood(),
    )

    assert diagnostic.mathematical_predictive_gate_conditions_satisfied is True
    assert diagnostic.economic_evidence_status == "non_economic_mechanism_diagnostic"
    assert diagnostic.true_edge_gate_satisfied is False
    assert diagnostic.qualified_net_pnl is None


def test_synthetic_structural_rows_never_become_economic_qualification() -> None:
    forecasts = []
    attempts = []
    for index in range(100):
        label = f"structural-{index}"
        lock = _verified_lock(
            decision_at_ms=1_060,
            expiry_ms=_expiry(label),
            suffix=label,
        )
        forecasts.append(
            _forecast(label, lock=lock, edge_basis=V07EdgeBasis.STRUCTURAL)
        )
        attempts.append(
            _attempt(
                label,
                "0.1",
                lock=lock,
                edge_basis=V07EdgeBasis.STRUCTURAL,
            )
        )

    result = evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=_stable_neighborhood(),
    )

    assert result.status is V07EvidenceStatus.COUNTERFACTUAL_INSUFFICIENT
    assert result.predictive_economic_attempt_count == 0
    assert result.predictive_net_pnl == D("0")
    assert result.predictive_qualified_net_pnl is None
    assert result.structural_economic_attempt_count == 100
    assert result.structural_net_pnl == D("10")
    assert result.structural_true_edge_gate_satisfied is False
    assert result.structural_qualified_net_pnl is None
    assert result.true_edge_gate_satisfied is False
    assert result.qualified_net_pnl is None
    document = result.to_document()
    assert document["predictive_true_edge"] is False
    assert document["structural_true_edge"] is False
    assert document["predictive_qualified_net_pnl"] is None
    assert document["structural_qualified_net_pnl"] is None


def test_structural_gate_math_is_non_economic_diagnostic_only() -> None:
    forecasts = []
    attempts = []
    for index in range(100):
        label = f"structural-mechanism-{index}"
        lock = _verified_lock(
            decision_at_ms=1_060,
            expiry_ms=_expiry(label),
            suffix=label,
        )
        forecasts.append(
            _forecast(label, lock=lock, edge_basis=V07EdgeBasis.STRUCTURAL)
        )
        attempts.append(
            _attempt(
                label,
                "0.1",
                lock=lock,
                edge_basis=V07EdgeBasis.STRUCTURAL,
            )
        )

    diagnostic = evaluate_gate_mechanism_diagnostic(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=None,
    )

    assert diagnostic.structural_sample_gate_passed is True
    assert diagnostic.structural_diagnostic_positive_sample_pnl is True
    assert diagnostic.structural_bootstrap_gate_passed is True
    assert diagnostic.structural_concentration_gate_passed is True
    assert diagnostic.structural_positive_floor_gate_passed is True
    assert diagnostic.mathematical_structural_gate_conditions_satisfied is True
    assert diagnostic.economic_evidence_status == "non_economic_mechanism_diagnostic"
    assert diagnostic.true_edge_gate_satisfied is False
    assert diagnostic.qualified_net_pnl is None


def test_mixed_fifty_predictive_and_fifty_structural_do_not_form_one_gate() -> None:
    forecasts = []
    attempts = []
    for index in range(100):
        label = f"mixed-{index}"
        edge_basis = V07EdgeBasis.PREDICTIVE if index < 50 else V07EdgeBasis.STRUCTURAL
        forecasts.append(_forecast(label, edge_basis=edge_basis))
        attempts.append(_attempt(label, "0.1", edge_basis=edge_basis))

    result = evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=_stable_neighborhood(),
    )

    assert result.settled_expiry_cluster_count == 100
    assert result.predictive_settled_expiry_cluster_count == 50
    assert result.structural_settled_expiry_cluster_count == 50
    assert result.predictive_true_edge_gate_satisfied is False
    assert result.structural_true_edge_gate_satisfied is False
    assert result.true_edge_gate_satisfied is False
    assert result.predictive_qualified_net_pnl is None
    assert result.structural_qualified_net_pnl is None
    assert result.qualified_net_pnl is None


def test_self_reported_complete_and_immutable_flags_cannot_qualify() -> None:
    forecasts = tuple(_forecast(f"flags-{index}") for index in range(100))
    attempts = tuple(_attempt(f"flags-{index}", "0.1") for index in range(100))
    result = evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=_stable_neighborhood(),
    )
    assert all(row.immutable_public_capture_evidence for row in attempts)
    assert all(row.complete_cost_evidence for row in attempts)
    assert result.complete_cost_and_execution_evidence is False
    assert result.true_edge_gate_satisfied is False
    assert result.qualified_net_pnl is None


def test_concentration_uses_largest_positive_cluster_over_total_net_pnl() -> None:
    nets = ["30", *("1" for _ in range(99)), "-29"]
    forecasts = []
    attempts = []
    for index, net in enumerate(nets):
        label = f"cluster-{index}"
        lock = _verified_lock(
            decision_at_ms=1_060,
            expiry_ms=_expiry(label),
            suffix=label,
        )
        forecasts.append(_forecast(label, lock=lock))
        attempts.append(_attempt(label, net, lock=lock))
    result = evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=_stable_neighborhood(),
    )
    assert result.net_pnl == D("100")
    assert result.largest_positive_cluster_to_total_net_pnl == D("0.3")
    assert result.largest_positive_cluster_to_total_positive_cluster_pnl == D("30") / D(
        "129"
    )
    assert result.maximum_absolute_cluster_contribution_share == D("30") / D("158")
    assert result.true_edge_gate_satisfied is False
    assert (
        "largest_positive_expiry_cluster_exceeds_20_percent_of_net_pnl"
        in result.reason_codes
    )


def test_tau_rows_do_not_inflate_expiry_cluster_count() -> None:
    forecasts = []
    attempts = []
    for cluster_index in range(10):
        for tau in range(10):
            label = f"cluster-{cluster_index}"
            actual_tau = 60 + tau
            forecasts.append(_forecast(label, tau=actual_tau))
            attempts.append(_attempt(label, "0.1", tau=actual_tau))
    result = evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=_stable_neighborhood(),
    )
    assert result.economic_attempt_count == 100
    assert result.settled_expiry_cluster_count == 10
    assert result.positive_net_pnl_user_check_passed is False


def test_one_common_expiry_cannot_be_renamed_into_100_pair_clusters() -> None:
    common_expiry_ms = 9_000
    forecasts = tuple(
        _forecast(
            f"renamed-pair-{index}",
            tau=60 + index,
            expiry_ms=common_expiry_ms,
        )
        for index in range(100)
    )
    attempts = tuple(
        _attempt(
            f"renamed-pair-{index}",
            "0.1",
            tau=60 + index,
            expiry_ms=common_expiry_ms,
        )
        for index in range(100)
    )

    with pytest.raises(
        ValueError,
        match="one common expiry maps to multiple market pairs",
    ):
        evaluate_locked_oos_evidence(
            forecasts=forecasts,
            economic_attempts=attempts,
            parameter_neighborhood=_stable_neighborhood(),
        )


def test_structurally_impossible_actual_outcome_is_rejected() -> None:
    with pytest.raises(ValueError, match="actual outcomes violate strike ordering"):
        LockedOOSForecastRow(
            event_cluster_id=_cluster("bad"),
            event_cluster_alias="bad",
            expiry_ms=1_500,
            decision_tau_seconds=60,
            decision_at_ms=1_000,
            forecast_available_at_ms=1_100,
            forecast_availability_basis=(
                V07ForecastAvailabilityBasis.PREREGISTERED_COUNTERFACTUAL_DELAY
            ),
            label_available_at_ms=2_000,
            forecast_q_5_up=D("0.5"),
            forecast_q_15_up=D("0.4"),
            market_q_5_up=D("0.5"),
            market_q_15_up=D("0.4"),
            actual_5_up=False,
            actual_15_up=True,
            strike_5=D("100"),
            strike_15=D("110"),
            lock_provenance=_counterfactual(),
        )


def test_post_expiry_prelabel_prediction_receipt_fails_closed() -> None:
    provenance = _issue_verified_prelabel_lock_provenance(
        test_universe_sha256="0" * 64,
        test_universe_locked_at_ms=100,
        test_universe_received_at_ms=200,
        test_universe_receipt_id="universe",
        prediction_locked_at_ms=1_000,
        prediction_received_at_ms=9_000,
        prediction_receipt_id="prediction",
        forecast_payload_sha256="1" * 64,
        decision_payload_sha256="2" * 64,
    )
    with pytest.raises(ValueError, match="common expiry"):
        LockedOOSForecastRow(
            event_cluster_id=_cluster("late"),
            event_cluster_alias="late",
            expiry_ms=2_000,
            decision_tau_seconds=60,
            decision_at_ms=1_000,
            forecast_available_at_ms=9_000,
            forecast_availability_basis=(
                V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
            ),
            label_available_at_ms=10_000,
            forecast_q_5_up=D("1"),
            forecast_q_15_up=D("0"),
            market_q_5_up=D("0.5"),
            market_q_15_up=D("0.5"),
            actual_5_up=True,
            actual_15_up=False,
            strike_5=D("100"),
            strike_15=D("110"),
            lock_provenance=provenance,
        )


def test_preexpiry_receipt_binds_forecast_availability() -> None:
    label = "preexpiry"
    expiry_ms = _expiry(label)
    provenance = _verified_lock(
        decision_at_ms=1_060,
        expiry_ms=expiry_ms,
        received_at_ms=1_500,
        suffix=label,
    )
    row = _forecast(label, expiry_ms=expiry_ms, lock=provenance)
    assert row.forecast_available_at_ms == 1_500
    assert row.forecast_availability_basis is (
        V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
    )


def test_weight_zero_forecast_ties_coherent_market_and_fails_improvement() -> None:
    forecast = _forecast(
        "pure-market",
        forecast_q_5_up="0.5",
        forecast_q_15_up="0.5",
        market_q_5_up="0.1",
        market_q_15_up="0.9",
    )
    result = evaluate_locked_oos_evidence(
        forecasts=(forecast,),
        economic_attempts=(),
        parameter_neighborhood=None,
    )
    metrics = result.forecast_metrics
    assert metrics["5m"]["model_brier"] == D("0.25")
    assert metrics["5m"]["coherent_executable_market_brier"] == D("0.25")
    assert metrics["15m"]["model_brier"] == D("0.25")
    assert metrics["15m"]["coherent_executable_market_brier"] == D("0.25")
    assert "5m_brier_does_not_beat_coherent_executable_market" in result.reason_codes
    assert "15m_brier_does_not_beat_coherent_executable_market" in result.reason_codes


def test_causal_no_fill_is_retained_in_reconciliation_ledger() -> None:
    forecasts = (_forecast("filled"), _forecast("no-fill"))
    attempts = (
        _attempt("filled", "0.5"),
        _attempt("no-fill", "0", causal_no_fill=True),
    )
    result = evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=None,
    )
    assert result.reconciled_actionable_decision_count == 2
    assert result.causal_no_fill_count == 1
    assert result.economic_attempt_count == 1
    assert result.net_pnl == D("0.5")


def test_parameter_neighborhood_is_diagnostic_only_and_requires_all_cells() -> None:
    evidence = ParameterNeighborhoodEvidence(
        required_setting_ids=("a", "b"),
        net_pnl_by_setting={"a": D("1"), "b": None},
    )
    assert evidence.complete is False
    assert evidence.stable_positive is False
    document = evidence.to_document()
    assert document["diagnostic_only"] is True
    assert document["action_changing"] is False
