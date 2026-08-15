"""Invariant tests for the opt-in v0.7 shared-terminal probability model."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal

import pytest

from src.edge_lab.btc_twap_relative_value import (
    OrderBookSnapshot,
    PairAction,
    TimedPrice,
)
from src.edge_lab.btc_twap_relative_value_v07 import (
    ProbabilityPairTrainingRow,
    SharedTerminalModelConfig,
    SharedTerminalTwap60Distribution,
    SharedTerminalTwap60Scenario,
    StrikeOrdering,
    TrainOnlyShrinkageArtifact,
    V07ModelRejection,
    V07StrategyConfig,
    _candidate,
    evaluate_validation_veto,
    executable_binary_ask_probability,
    raw_top_ask_probability,
    simulate_shared_terminal_twap_60_distribution,
)
from src.edge_lab.data_store import canonical_json_bytes
from src.edge_lab.execution import ExecutionFeeSchedule
from tests.test_edge_lab_btc_twap_relative_value import same_expiry_pair

D = Decimal


def _prices(*, decision_at_ms: int, count: int = 300) -> tuple[TimedPrice, ...]:
    start = decision_at_ms - (count - 1) * 1_000
    return tuple(
        TimedPrice(
            timestamp_ms=start + index * 1_000,
            price=D("100") + D(index % 7) / D("1000"),
        )
        for index in range(count)
    )


def _row(
    *,
    cluster: str,
    tau: int,
    model: str,
    market: str,
    actual: bool,
    split: str = "train",
    label_at: int = 10,
) -> ProbabilityPairTrainingRow:
    expiry_ms = 2 + int(hashlib.sha256(cluster.encode()).hexdigest()[:8], 16) % (
        label_at - 2
    )
    return ProbabilityPairTrainingRow(
        event_cluster_id=cluster,
        expiry_ms=expiry_ms,
        decision_tau_seconds=tau,
        decision_at_ms=1,
        label_available_at_ms=label_at,
        model_q_5_up=D(model),
        model_q_15_up=D(model),
        market_q_5_up=D(market),
        market_q_15_up=D(market),
        actual_5_up=actual,
        actual_15_up=actual,
        strike_5=D("100"),
        strike_15=D("100"),
        split=split,
    )


def test_shared_terminal_distribution_enforces_all_strike_orderings() -> None:
    scenarios = tuple(
        SharedTerminalTwap60Scenario(D(value)) for value in ("90", "105", "110", "120")
    )

    below = SharedTerminalTwap60Distribution.from_scenarios(
        scenarios,
        strike_5=D("100"),
        strike_15=D("115"),
    )
    above = SharedTerminalTwap60Distribution.from_scenarios(
        scenarios,
        strike_5=D("115"),
        strike_15=D("100"),
    )
    equal = SharedTerminalTwap60Distribution.from_scenarios(
        scenarios,
        strike_5=D("105"),
        strike_15=D("105"),
    )

    assert below.strike_ordering is StrikeOrdering.FIVE_BELOW_FIFTEEN
    assert below.outcome_counts["5down_15up"] == 0
    assert below.outcome_probabilities["5down_15up"] == 0
    assert below.q_5_up >= below.q_15_up
    assert above.strike_ordering is StrikeOrdering.FIVE_ABOVE_FIFTEEN
    assert above.outcome_counts["5up_15down"] == 0
    assert above.outcome_probabilities["5up_15down"] == 0
    assert above.q_15_up >= above.q_5_up
    assert equal.strike_ordering is StrikeOrdering.EQUAL
    assert equal.outcome_probabilities["5up_15down"] == 0
    assert equal.outcome_probabilities["5down_15up"] == 0
    assert equal.q_5_up == equal.q_15_up


def test_jeffreys_mass_avoids_exact_finite_sample_saturation() -> None:
    distribution = SharedTerminalTwap60Distribution.from_scenarios(
        tuple(SharedTerminalTwap60Scenario(D("101")) for _ in range(100)),
        strike_5=D("100"),
        strike_15=D("100"),
    )

    assert D("0") < distribution.q_5_up < D("1")
    assert D("0") < distribution.q_15_up < D("1")
    assert distribution.impossible_outcomes == (
        "5up_15down",
        "5down_15up",
    )


def test_simulation_is_deterministic_and_flat_tick_history_uses_volatility_floor() -> (
    None
):
    decision_at_ms = 1_000_000
    flat = tuple(
        TimedPrice(
            timestamp_ms=decision_at_ms - (299 - index) * 1_000,
            price=D("100"),
        )
        for index in range(300)
    )
    config = SharedTerminalModelConfig(n_paths=400)

    first = simulate_shared_terminal_twap_60_distribution(
        predictor_prices=flat,
        current_terminal_twap_60=D("100"),
        decision_at_ms=decision_at_ms,
        expiry_ms=decision_at_ms + 60_000,
        strike_5=D("99.98"),
        strike_15=D("100.02"),
        config=config,
        seed_salt="fixture",
    )
    second = simulate_shared_terminal_twap_60_distribution(
        predictor_prices=flat,
        current_terminal_twap_60=D("100"),
        decision_at_ms=decision_at_ms,
        expiry_ms=decision_at_ms + 60_000,
        strike_5=D("99.98"),
        strike_15=D("100.02"),
        config=config,
        seed_salt="fixture",
    )

    assert first.to_document() == second.to_document()
    assert first.annualized_volatility == D("0.25")
    assert first.distribution.outcome_counts["5down_15up"] == 0
    assert dict(first.distribution.uncertainty_scale_path_counts) == {
        "0.75": 80,
        "1": 240,
        "1.5": 80,
    }
    assert set(first.distribution.uncertainty_scale_marginals) == {
        "0.75",
        "1",
        "1.5",
    }
    assert not hasattr(first.distribution.scenarios[0], "twap_30")


def test_sensitivity_settings_use_common_random_numbers() -> None:
    decision_at_ms = 1_000_000
    flat = tuple(
        TimedPrice(
            timestamp_ms=decision_at_ms - (299 - index) * 1_000,
            price=D("100"),
        )
        for index in range(300)
    )
    primary = SharedTerminalModelConfig(n_paths=200)
    lower_floor = replace(primary, annualized_volatility_floor=D("0.20"))
    upper_floor = replace(primary, annualized_volatility_floor=D("0.30"))

    lower = simulate_shared_terminal_twap_60_distribution(
        predictor_prices=flat,
        current_terminal_twap_60=D("100"),
        decision_at_ms=decision_at_ms,
        expiry_ms=decision_at_ms + 60_000,
        strike_5=D("99.99"),
        strike_15=D("100.01"),
        config=lower_floor,
        seed_salt="same-case",
    )
    upper = simulate_shared_terminal_twap_60_distribution(
        predictor_prices=flat,
        current_terminal_twap_60=D("100"),
        decision_at_ms=decision_at_ms,
        expiry_ms=decision_at_ms + 60_000,
        strike_5=D("99.99"),
        strike_15=D("100.01"),
        config=upper_floor,
        seed_salt="same-case",
    )

    assert lower.seed == upper.seed
    assert lower.model_config_hash != upper.model_config_hash


def test_simulation_rejects_predictor_leakage() -> None:
    decision_at_ms = 1_000_000
    prices = (*_prices(decision_at_ms=decision_at_ms), TimedPrice(1_001_000, D("100")))

    with pytest.raises(V07ModelRejection, match="predictor_leakage"):
        simulate_shared_terminal_twap_60_distribution(
            predictor_prices=prices,
            current_terminal_twap_60=D("100"),
            decision_at_ms=decision_at_ms,
            expiry_ms=decision_at_ms + 60_000,
            strike_5=D("100"),
            strike_15=D("101"),
            config=SharedTerminalModelConfig(n_paths=100),
        )


def test_train_only_shrinkage_weights_expiry_clusters_not_tau_rows() -> None:
    rows = tuple(
        _row(
            cluster="many-tau-cluster",
            tau=tau,
            model="1",
            market="0",
            actual=True,
        )
        for tau in range(1, 101)
    ) + (
        _row(
            cluster="single-row-cluster",
            tau=1,
            model="1",
            market="0",
            actual=False,
        ),
    )

    artifact = TrainOnlyShrinkageArtifact.fit(
        rows,
        fit_at_ms=20,
        candidate_model_weights=(D("0"), D("1")),
    )

    # Cluster-equal scoring ties the two candidates; the preregistered
    # conservative tie-break selects the executable-market baseline.
    assert artifact.model_weight == D("0")
    assert artifact.cluster_equal_brier_by_weight["0"] == D("0.5")
    assert artifact.cluster_equal_brier_by_weight["1"] == D("0.5")


def _assert_document_hash(document: dict[str, object]) -> None:
    payload = dict(document)
    recorded = payload.pop("artifact_hash")
    assert recorded == hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def test_calibration_artifacts_are_self_verifying_from_emitted_documents() -> None:
    artifact = TrainOnlyShrinkageArtifact.fit(
        (_row(cluster="train", tau=60, model="0.8", market="0.5", actual=True),),
        fit_at_ms=20,
    )
    diagnostic = artifact.with_diagnostic_model_weight(
        model_weight=D("0.25"),
        setting_id="model_weight_0.25",
    )
    validation = (
        _row(
            cluster="validation",
            tau=60,
            model="0.8",
            market="0.5",
            actual=True,
            split="validation",
            label_at=30,
        ),
    )
    veto = evaluate_validation_veto(
        validation,
        shrinkage=artifact,
        evaluated_at_ms=40,
    )

    _assert_document_hash(artifact.to_document())
    _assert_document_hash(diagnostic.to_document())
    _assert_document_hash(veto.to_document())


def test_validation_veto_scores_coherent_executable_market_baseline() -> None:
    artifact = TrainOnlyShrinkageArtifact.fit(
        (
            _row(
                cluster="train",
                tau=60,
                model="0.9",
                market="0.1",
                actual=True,
            ),
        ),
        fit_at_ms=20,
        candidate_model_weights=(D("0"), D("1")),
    )
    validation = (
        ProbabilityPairTrainingRow(
            event_cluster_id="validation",
            expiry_ms=25,
            decision_tau_seconds=60,
            decision_at_ms=21,
            label_available_at_ms=30,
            model_q_5_up=D("0.9"),
            model_q_15_up=D("0.1"),
            market_q_5_up=D("0.1"),
            market_q_15_up=D("0.9"),
            actual_5_up=True,
            actual_15_up=False,
            strike_5=D("100"),
            strike_15=D("110"),
            split="validation",
        ),
    )

    veto = evaluate_validation_veto(
        validation,
        shrinkage=artifact,
        evaluated_at_ms=40,
    )

    assert artifact.model_weight == D("1")
    assert veto.market_brier_5m == D("0.25")
    assert veto.market_brier_15m == D("0.25")
    assert veto.passed


def test_shrinkage_and_validation_reject_current_event_labels() -> None:
    training = (_row(cluster="train", tau=60, model="0.8", market="0.5", actual=True),)
    with pytest.raises(ValueError, match="predate fit_at_ms"):
        TrainOnlyShrinkageArtifact.fit(training, fit_at_ms=10)

    artifact = TrainOnlyShrinkageArtifact.fit(training, fit_at_ms=20)
    validation = (
        _row(
            cluster="validation",
            tau=60,
            model="0.8",
            market="0.5",
            actual=True,
            split="validation",
            label_at=30,
        ),
    )
    with pytest.raises(ValueError, match="predate veto evaluation"):
        evaluate_validation_veto(validation, shrinkage=artifact, evaluated_at_ms=30)


def test_training_row_rejects_structurally_impossible_probability_or_label() -> None:
    with pytest.raises(ValueError, match="model probabilities violate strike ordering"):
        ProbabilityPairTrainingRow(
            event_cluster_id="cluster",
            expiry_ms=5,
            decision_tau_seconds=60,
            decision_at_ms=1,
            label_available_at_ms=10,
            model_q_5_up=D("0.2"),
            model_q_15_up=D("0.8"),
            market_q_5_up=D("0.5"),
            market_q_15_up=D("0.5"),
            actual_5_up=True,
            actual_15_up=False,
            strike_5=D("100"),
            strike_15=D("110"),
        )

    with pytest.raises(ValueError, match="actual outcomes violate strike ordering"):
        ProbabilityPairTrainingRow(
            event_cluster_id="cluster",
            expiry_ms=5,
            decision_tau_seconds=60,
            decision_at_ms=1,
            label_available_at_ms=10,
            model_q_5_up=D("0.8"),
            model_q_15_up=D("0.2"),
            market_q_5_up=D("0.5"),
            market_q_15_up=D("0.5"),
            actual_5_up=False,
            actual_15_up=True,
            strike_5=D("100"),
            strike_15=D("110"),
        )


def test_diagnostic_weight_override_retains_train_artifact_provenance() -> None:
    artifact = TrainOnlyShrinkageArtifact.fit(
        (_row(cluster="train", tau=60, model="0.8", market="0.5", actual=True),),
        fit_at_ms=20,
    )

    diagnostic = artifact.with_diagnostic_model_weight(
        model_weight=D("0.25"),
        setting_id="model_weight_0.25",
    )

    assert diagnostic.model_weight == D("0.25")
    assert diagnostic.train_selected_model_weight == artifact.model_weight
    assert diagnostic.selection_overridden_for_preregistered_sensitivity
    assert diagnostic.diagnostic_setting_id == "model_weight_0.25"
    assert diagnostic.base_train_artifact_hash == artifact.artifact_hash
    assert diagnostic.training_rows_sha256 == artifact.training_rows_sha256
    assert diagnostic.artifact_hash != artifact.artifact_hash


def _depth_book(
    token_id: str,
    *,
    bids: tuple[tuple[str, str], ...],
    asks: tuple[tuple[str, str], ...],
) -> OrderBookSnapshot:
    return OrderBookSnapshot.from_tuples(
        token_id,
        bids=tuple((D(price), D(size)) for price, size in bids),
        asks=tuple((D(price), D(size)) for price, size in asks),
        timestamp_ms=1,
        tick_size=D("0.01"),
        minimum_order_size=D("1"),
    )


def test_executable_market_probability_walks_depth_and_includes_fees() -> None:
    fee_schedule = ExecutionFeeSchedule(
        rate=D("0.07"),
        exponent=D("1"),
        taker_only=True,
    )
    books = {
        "up": _depth_book(
            "up",
            bids=(("0.19", "10"),),
            asks=(("0.20", "1"), ("0.80", "4")),
        ),
        "down": _depth_book(
            "down",
            bids=(("0.39", "10"),),
            asks=(("0.40", "5"),),
        ),
    }

    raw = raw_top_ask_probability(
        books,
        up_token_id="up",
        down_token_id="down",
    )
    executable = executable_binary_ask_probability(
        books,
        up_token_id="up",
        down_token_id="down",
        target_quantity=D("5"),
        fee_schedule=fee_schedule,
    )

    up_cost = (
        D("0.20")
        + D("0.80") * D("4")
        + fee_schedule.fee(D("1"), D("0.20"), maker=False)
        + fee_schedule.fee(D("4"), D("0.80"), maker=False)
    ) / D("5")
    down_cost = (
        D("0.40") * D("5") + fee_schedule.fee(D("5"), D("0.40"), maker=False)
    ) / D("5")
    expected = up_cost / (up_cost + down_cost)

    assert raw == D("0.20") / D("0.60")
    assert executable == expected
    assert executable > raw
    assert (
        executable_binary_ask_probability(
            books,
            up_token_id="up",
            down_token_id="down",
            target_quantity=D("6"),
            fee_schedule=fee_schedule,
        )
        is None
    )


def test_pure_market_weight_cannot_beat_coherent_market_baseline() -> None:
    row = ProbabilityPairTrainingRow(
        event_cluster_id="validation",
        expiry_ms=5,
        decision_tau_seconds=60,
        decision_at_ms=1,
        label_available_at_ms=10,
        model_q_5_up=D("0.9"),
        model_q_15_up=D("0.1"),
        market_q_5_up=D("0.1"),
        market_q_15_up=D("0.9"),
        actual_5_up=True,
        actual_15_up=False,
        strike_5=D("100"),
        strike_15=D("101"),
        split="validation",
    )
    train_row = replace(
        row,
        event_cluster_id="train",
        expiry_ms=1,
        split="train",
        decision_at_ms=0,
        label_available_at_ms=1,
    )
    shrinkage = TrainOnlyShrinkageArtifact.fit(
        (train_row,),
        fit_at_ms=2,
        candidate_model_weights=(D("0"),),
    )

    veto = evaluate_validation_veto(
        (row,),
        shrinkage=shrinkage,
        evaluated_at_ms=20,
    )

    assert shrinkage.model_weight == D("0")
    assert veto.blended_brier_5m == veto.market_brier_5m == D("0.25")
    assert veto.blended_brier_15m == veto.market_brier_15m == D("0.25")
    assert not veto.passed
    assert "validation_5m_brier_does_not_beat_coherent_market" in veto.reason_codes
    assert "validation_15m_brier_does_not_beat_coherent_market" in veto.reason_codes


@pytest.mark.parametrize("scenario_count", (2_000, 8_000, 20_000))
def test_qualification_penalty_has_no_inverse_sqrt_path_count_term(
    scenario_count: int,
) -> None:
    pair = same_expiry_pair()
    scenarios = tuple(
        SharedTerminalTwap60Scenario(D("100.5")) for _ in range(scenario_count)
    )
    distribution = SharedTerminalTwap60Distribution.from_scenarios(
        scenarios,
        strike_5=D("100"),
        strike_15=D("101"),
    )
    books = {
        pair.market_5.up_token_id: _depth_book(
            pair.market_5.up_token_id,
            bids=(("0.39", "100"),),
            asks=(("0.40", "100"),),
        ),
        pair.market_15.down_token_id: _depth_book(
            pair.market_15.down_token_id,
            bids=(("0.39", "100"),),
            asks=(("0.40", "100"),),
        ),
    }
    config = V07StrategyConfig(
        pair_risk_usdc=D("25"),
        minimum_net_expected_pnl_per_pair=D("0.65"),
        uncertainty_multiplier=D("1.25"),
        market_baseline_quantity=D("5"),
    )

    candidate = _candidate(
        action=PairAction.LONG_5_UP_LONG_15_DOWN,
        first_token_id=pair.market_5.up_token_id,
        second_token_id=pair.market_15.down_token_id,
        first_contract=pair.market_5,
        second_contract=pair.market_15,
        books=books,
        distribution=distribution,
        q_5_up=D("0.5"),
        q_15_up=D("0"),
        uncertainty_probability_pairs=(
            (D("0.6"), D("0")),
            (D("0.5"), D("0")),
            (D("0.4"), D("0")),
        ),
        config=config,
    )

    assert candidate is not None
    assert candidate.model_uncertainty_downside_per_pair == D("0.1")
    assert candidate.adjusted_pnl_per_pair == (candidate.net_pnl_per_pair - D("0.125"))
    assert candidate.adjusted_pnl_per_pair <= config.minimum_net_expected_pnl_per_pair
