from __future__ import annotations

import hashlib

import pytest

from src.edge_lab.btc_twap_relative_value_v07 import (
    CANONICAL_EVENT_CLUSTER_PREFIX,
)
from src.edge_lab.btc_twap_relative_value_v07_evaluation import (
    LockedOOSEconomicAttempt,
    LockedOOSForecastRow,
    ParameterNeighborhoodEvidence,
    PreLabelLockProvenance,
    PreLabelLockStatus,
    V07EvidenceStatus,
    evaluate_locked_oos_evidence,
)

D = __import__("decimal").Decimal
_EXPIRY_BY_LABEL: dict[str, int] = {}


def _cluster(label: str) -> str:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return f"{CANONICAL_EVENT_CLUSTER_PREFIX}{digest}"


def _expiry(label: str) -> int:
    return _EXPIRY_BY_LABEL.setdefault(label, 2_000 + len(_EXPIRY_BY_LABEL) * 10)


def _counterfactual() -> PreLabelLockProvenance:
    return PreLabelLockProvenance.counterfactual("test_counterfactual")


def _verified_lock(
    *,
    decision_at_ms: int,
    label_available_at_ms: int,
    suffix: str,
) -> PreLabelLockProvenance:
    if decision_at_ms >= label_available_at_ms:
        raise ValueError("test lock timestamps are invalid")
    return PreLabelLockProvenance(
        status=PreLabelLockStatus.VERIFIED,
        test_universe_sha256=hashlib.sha256(b"universe").hexdigest(),
        test_universe_locked_at_ms=100,
        test_universe_received_at_ms=200,
        test_universe_receipt_id="universe-receipt",
        prediction_locked_at_ms=decision_at_ms,
        prediction_received_at_ms=decision_at_ms + 1,
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
) -> LockedOOSForecastRow:
    decision_at_ms = 1_000 + tau
    label_available_at_ms = 10_000
    return LockedOOSForecastRow(
        event_cluster_id=_cluster(cluster_label),
        event_cluster_alias=cluster_label,
        expiry_ms=_expiry(cluster_label) if expiry_ms is None else expiry_ms,
        decision_tau_seconds=tau,
        decision_at_ms=decision_at_ms,
        label_available_at_ms=label_available_at_ms,
        forecast_q_5_up=D(forecast_q_5_up),
        forecast_q_15_up=D(forecast_q_15_up),
        market_q_5_up=D(market_q_5_up),
        market_q_15_up=D(market_q_15_up),
        actual_5_up=True,
        actual_15_up=False,
        strike_5=D("100"),
        strike_15=D("110"),
        lock_provenance=lock or _counterfactual(),
    )


def _attempt(
    cluster_label: str,
    net: str,
    *,
    tau: int = 60,
    expiry_ms: int | None = None,
    lock: PreLabelLockProvenance | None = None,
    causal_no_fill: bool = False,
) -> LockedOOSEconomicAttempt:
    return LockedOOSEconomicAttempt(
        attempt_id=f"attempt-{cluster_label}-{tau}",
        event_cluster_id=_cluster(cluster_label),
        event_cluster_alias=cluster_label,
        expiry_ms=_expiry(cluster_label) if expiry_ms is None else expiry_ms,
        decision_tau_seconds=tau,
        net_pnl=D(net),
        explainable=not causal_no_fill,
        complete_cost_evidence=True,
        complete_execution_evidence=True,
        complete_settlement_evidence=True,
        immutable_public_capture_evidence=True,
        signal_strength=D("0.98"),
        execution_status="no_fill" if causal_no_fill else "both_filled",
        economic_attempt=not causal_no_fill,
        causal_no_fill=causal_no_fill,
        reconciliation_status=(
            "causal_no_fill_zero_pnl" if causal_no_fill else "settled_execution"
        ),
        lock_provenance=lock or _counterfactual(),
    )


def _stable_neighborhood() -> ParameterNeighborhoodEvidence:
    required = ("base", "paths_low", "paths_high", "weight_low", "weight_high")
    return ParameterNeighborhoodEvidence(
        required_setting_ids=required,
        net_pnl_by_setting={setting: D("1") for setting in required},
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


def test_locked_oos_evaluation_can_satisfy_all_gates_with_verified_locks() -> None:
    forecasts = []
    attempts = []
    for index in range(100):
        label = f"c{index}"
        decision_at_ms = 1_060
        lock = _verified_lock(
            decision_at_ms=decision_at_ms,
            label_available_at_ms=10_000,
            suffix=label,
        )
        forecasts.append(_forecast(label, lock=lock))
        attempts.append(_attempt(label, "0.1", lock=lock))
    result = evaluate_locked_oos_evidence(
        forecasts=forecasts,
        economic_attempts=attempts,
        parameter_neighborhood=_stable_neighborhood(),
    )
    assert result.status is V07EvidenceStatus.TRUE_EDGE_GATE_SATISFIED
    assert result.positive_net_pnl_user_check_passed is True
    assert result.true_edge_gate_satisfied is True
    assert result.auditable_prelabel_lock_evidence is True
    assert result.qualified_net_pnl == result.net_pnl
    assert result.largest_positive_cluster_to_total_net_pnl == D("0.01")


def test_concentration_uses_largest_positive_cluster_over_total_net_pnl() -> None:
    nets = ["30", *("1" for _ in range(99)), "-29"]
    forecasts = []
    attempts = []
    for index, net in enumerate(nets):
        label = f"cluster-{index}"
        lock = _verified_lock(
            decision_at_ms=1_060,
            label_available_at_ms=10_000,
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


def test_post_label_prediction_lock_fails_closed() -> None:
    provenance = PreLabelLockProvenance(
        status=PreLabelLockStatus.VERIFIED,
        test_universe_sha256="0" * 64,
        test_universe_locked_at_ms=100,
        test_universe_received_at_ms=200,
        test_universe_receipt_id="universe",
        prediction_locked_at_ms=2_000,
        prediction_received_at_ms=2_001,
        prediction_receipt_id="prediction",
        forecast_payload_sha256="1" * 64,
        decision_payload_sha256="2" * 64,
    )
    with pytest.raises(ValueError, match="prediction lock must equal decision_at_ms"):
        LockedOOSForecastRow(
            event_cluster_id=_cluster("late"),
            event_cluster_alias="late",
            expiry_ms=1_200,
            decision_tau_seconds=60,
            decision_at_ms=1_000,
            label_available_at_ms=1_500,
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
