"""Generated-fixture replay invariants for the non-promotional v0.7 track."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from src.edge_lab.btc_twap_relative_value import (
    PairAction,
    PairSettlementState,
    SameExpiryPair,
)
from src.edge_lab.btc_twap_relative_value_v07 import (
    ProbabilityPairTrainingRow,
    SharedTerminalDataHealth,
    SharedTerminalSimulationResult,
    SharedTerminalTwap60Distribution,
    SharedTerminalTwap60Scenario,
    TrainOnlyShrinkageArtifact,
    V07EdgeBasis,
    V07ForecastAvailabilityBasis,
    V07StrategyConfig,
    evaluate_validation_veto,
)
from src.edge_lab.btc_twap_relative_value_v07_replay import (
    evaluate_shared_terminal_paper_cycle,
)
from src.edge_lab.settlement_regime import V06_SETTLEMENT_REGIME_ID
from tests.test_edge_lab_btc_twap_relative_value import (
    _replay_observation,
    _ReplayStub,
    same_expiry_pair,
)

D = Decimal


def _v07_pair() -> SameExpiryPair:
    legacy = same_expiry_pair()
    source = "https://data.chain.link/streams/btc-usd-twap-60s-streams"
    market_5 = replace(
        legacy.market_5,
        twap_window_seconds=60,
        source_topic="crypto_prices_twap_sixty",
        resolution_source=source,
        settlement_regime=V06_SETTLEMENT_REGIME_ID,
        rule_hash="a" * 64,
    )
    market_15 = replace(
        legacy.market_15,
        twap_window_seconds=60,
        source_topic="crypto_prices_twap_sixty",
        resolution_source=source,
        settlement_regime=V06_SETTLEMENT_REGIME_ID,
        rule_hash="b" * 64,
    )
    return SameExpiryPair.from_contracts(market_5, market_15)


def _probability_row(
    *,
    cluster: str,
    split: str,
    label_at_ms: int,
) -> ProbabilityPairTrainingRow:
    return ProbabilityPairTrainingRow(
        event_cluster_id=cluster,
        expiry_ms=label_at_ms - 1,
        decision_tau_seconds=60,
        decision_at_ms=1,
        label_available_at_ms=label_at_ms,
        model_q_5_up=D("0.9"),
        model_q_15_up=D("0.1"),
        market_q_5_up=D("0.5"),
        market_q_15_up=D("0.5"),
        actual_5_up=True,
        actual_15_up=False,
        strike_5=D("100"),
        strike_15=D("101"),
        split=split,
    )


def _artifacts(decision_at_ms: int):
    shrinkage = TrainOnlyShrinkageArtifact.fit(
        (_probability_row(cluster="train", split="train", label_at_ms=10),),
        fit_at_ms=decision_at_ms - 20_000,
        candidate_model_weights=(D("1"),),
    )
    veto = evaluate_validation_veto(
        (
            _probability_row(
                cluster="validation",
                split="validation",
                label_at_ms=20,
            ),
        ),
        shrinkage=shrinkage,
        evaluated_at_ms=decision_at_ms - 10_000,
    )
    assert veto.passed
    return shrinkage, veto


def _simulation() -> SharedTerminalSimulationResult:
    distribution = SharedTerminalTwap60Distribution.from_scenarios(
        tuple(SharedTerminalTwap60Scenario(D("100.5")) for _ in range(200)),
        strike_5=D("100"),
        strike_15=D("101"),
    )
    return SharedTerminalSimulationResult(
        distribution=distribution,
        annualized_volatility=D("0.25"),
        volatility_estimates={"rms_15s": D("0"), "rms_60s": D("0")},
        current_local_twap_60=D("100.5"),
        chainlink_minus_local_basis=D("0"),
        model_config_hash="c" * 64,
        seed=712,
    )


def _settlement_state(pair: SameExpiryPair) -> PairSettlementState:
    return PairSettlementState(
        market_5_rule_hash=pair.market_5.rule_hash,
        market_15_rule_hash=pair.market_15.rule_hash,
        market_5_open_timestamp_ms=pair.market_5.opens_at_ms,
        market_15_open_timestamp_ms=pair.market_15.opens_at_ms,
        strike_5=D("100"),
        strike_15=D("101"),
        opening_5_source_event_id="open-5",
        opening_15_source_event_id="open-15",
    )


def _replay(
    pair: SameExpiryPair,
    decision_at_ms: int,
    *,
    forecast_available_at_ms: int,
    include_second: bool,
    include_unwind: bool = True,
    second_at_delta_ms: int = 500,
    include_pre_receipt_first: bool = False,
    include_post_receipt_first: bool = True,
):
    tokens = (
        pair.market_5.up_token_id,
        pair.market_5.down_token_id,
        pair.market_15.up_token_id,
        pair.market_15.down_token_id,
    )
    signal = {
        token: _replay_observation(
            token,
            bid="0.39",
            ask="0.40",
            timestamp_ms=decision_at_ms - 100,
            received_at_ms=decision_at_ms - 100,
            source_event_id=f"signal-{token}",
        )
        for token in tokens
    }
    first_at_ms = forecast_available_at_ms + 250
    first_surfaces = (
        (
            _replay_observation(
                pair.market_5.up_token_id,
                bid="0.39",
                ask="0.40",
                timestamp_ms=decision_at_ms + 250,
                received_at_ms=decision_at_ms + 250,
                source_event_id="pre-receipt-5-up",
            ),
        )
        if include_pre_receipt_first
        else ()
    )
    if include_post_receipt_first:
        first_surfaces += (
            _replay_observation(
                pair.market_5.up_token_id,
                bid="0.39",
                ask="0.40",
                timestamp_ms=first_at_ms,
                received_at_ms=first_at_ms,
                source_event_id="first-5-up",
            ),
        )
    if include_unwind:
        first_surfaces += (
            _replay_observation(
                pair.market_5.up_token_id,
                bid="0.39",
                ask="0.40",
                timestamp_ms=first_at_ms + 1_000,
                received_at_ms=first_at_ms + 1_000,
                source_event_id="unwind-5-up",
            ),
        )
    second_at_ms = first_at_ms + second_at_delta_ms - 250
    executable = {
        pair.market_5.up_token_id: first_surfaces,
        pair.market_15.down_token_id: (
            (
                _replay_observation(
                    pair.market_15.down_token_id,
                    bid="0.39",
                    ask="0.40",
                    timestamp_ms=second_at_ms,
                    received_at_ms=second_at_ms,
                    source_event_id="second-15-down",
                ),
            )
            if include_second
            else ()
        ),
    }
    return _ReplayStub(signal_books=signal, executable_books=executable)


def _health(
    decision_at_ms: int,
    *,
    forecast_available_at_ms: int | None = None,
    availability_basis: V07ForecastAvailabilityBasis = (
        V07ForecastAvailabilityBasis.PREREGISTERED_COUNTERFACTUAL_DELAY
    ),
) -> SharedTerminalDataHealth:
    shrinkage, veto = _artifacts(decision_at_ms)
    available_at_ms = (
        decision_at_ms + 5_000
        if forecast_available_at_ms is None
        else forecast_available_at_ms
    )
    return SharedTerminalDataHealth(
        decision_at_ms=decision_at_ms,
        forecast_available_at_ms=available_at_ms,
        forecast_availability_basis=availability_basis,
        terminal_twap_60_observed_at_ms=decision_at_ms - 100,
        terminal_twap_60_received_at_ms=decision_at_ms - 100,
        absolute_clock_drift_ms=20,
        shrinkage=shrinkage,
        validation_veto=veto,
    )


def test_v07_replay_is_deterministic_and_keeps_legacy_qualification_false() -> None:
    pair = _v07_pair()
    decision_at_ms = pair.expires_at_ms - 60_000
    forecast_available_at_ms = decision_at_ms + 5_000
    kwargs = {
        "event_cluster_id": "fixture-cluster",
        "event_cluster_alias": "fixture-cluster",
        "decision_tau_seconds": 60,
        "pair": pair,
        "settlement_state": _settlement_state(pair),
        "simulation": _simulation(),
        "replay": _replay(
            pair,
            decision_at_ms,
            forecast_available_at_ms=forecast_available_at_ms,
            include_second=True,
        ),
        "health": _health(
            decision_at_ms,
            forecast_available_at_ms=forecast_available_at_ms,
        ),
        "config": V07StrategyConfig(uncertainty_multiplier=D("0")),
        "market_5_up": True,
        "market_15_up": False,
        "locked_oos": True,
    }

    first = evaluate_shared_terminal_paper_cycle(**kwargs)
    second = evaluate_shared_terminal_paper_cycle(**kwargs)

    assert first.to_document() == second.to_document()
    assert first.decision.action is PairAction.LONG_5_UP_LONG_15_DOWN
    assert first.decision.edge_basis is V07EdgeBasis.STRUCTURAL
    assert first.decision.structural_quantity_executable
    assert first.decision.structural_worst_case_payoff_per_pair == D("1")
    assert first.decision.structural_net_floor_per_pair is not None
    assert first.decision.structural_net_floor_per_pair > D("0")
    decision_document = first.to_document()["decision"]
    assert decision_document["edge_basis"] == "structural"
    assert decision_document["structural_quantity_executable"] is True
    assert decision_document["structural_net_floor_per_pair"] == str(
        first.decision.structural_net_floor_per_pair
    )
    edge_classification = first.to_document()["edge_classification"]
    assert edge_classification["edge_basis"] == "structural"
    assert edge_classification["structural_quantity_executable"] is True
    assert first.economic_attempt
    assert first.settlement is not None
    assert first.settlement.explainable
    assert not first.settlement.qualified_sample
    assert first.to_document()["qualified_sample_for_v05_v06"] is False
    assert first.to_document()["orders_submitted"] == 0


def test_structural_replay_does_not_require_monte_carlo_simulation() -> None:
    pair = _v07_pair()
    decision_at_ms = pair.expires_at_ms - 60_000
    forecast_available_at_ms = decision_at_ms + 5_000

    evaluation = evaluate_shared_terminal_paper_cycle(
        event_cluster_id="structural-without-mc",
        event_cluster_alias="structural-without-mc",
        decision_tau_seconds=60,
        pair=pair,
        settlement_state=_settlement_state(pair),
        simulation=None,
        replay=_replay(
            pair,
            decision_at_ms,
            forecast_available_at_ms=forecast_available_at_ms,
            include_second=True,
        ),
        health=_health(
            decision_at_ms,
            forecast_available_at_ms=forecast_available_at_ms,
        ),
        config=V07StrategyConfig(uncertainty_multiplier=D("0")),
        market_5_up=True,
        market_15_up=False,
        locked_oos=True,
    )

    assert evaluation.decision.action is PairAction.LONG_5_UP_LONG_15_DOWN
    assert evaluation.decision.edge_basis is V07EdgeBasis.STRUCTURAL
    assert evaluation.simulation is None
    assert evaluation.market_q_5_up is None
    assert evaluation.market_q_15_up is None
    assert evaluation.to_document()["settlement_model"] is None
    assert evaluation.to_document()["settlement_model_diagnostic_status"] == (
        "not_applicable_structural_primitive"
    )
    assert evaluation.execution is not None
    assert evaluation.settlement is not None


def test_v07_replay_fails_closed_without_post_decision_second_leg_surface() -> None:
    pair = _v07_pair()
    decision_at_ms = pair.expires_at_ms - 60_000
    forecast_available_at_ms = decision_at_ms + 5_000

    evaluation = evaluate_shared_terminal_paper_cycle(
        event_cluster_id="fixture-cluster",
        event_cluster_alias="fixture-cluster",
        decision_tau_seconds=60,
        pair=pair,
        settlement_state=_settlement_state(pair),
        simulation=_simulation(),
        replay=_replay(
            pair,
            decision_at_ms,
            forecast_available_at_ms=forecast_available_at_ms,
            include_second=False,
        ),
        health=_health(
            decision_at_ms,
            forecast_available_at_ms=forecast_available_at_ms,
        ),
        config=V07StrategyConfig(uncertainty_multiplier=D("0")),
        market_5_up=True,
        market_15_up=False,
        locked_oos=True,
    )

    assert evaluation.decision.action is PairAction.LONG_5_UP_LONG_15_DOWN
    assert evaluation.execution is None
    assert evaluation.cycle.reason_codes == ("second_leg_timeout_surface_missing",)
    assert not evaluation.economic_attempt


def test_v07_replay_fails_closed_when_a_required_unwind_surface_is_absent() -> None:
    pair = _v07_pair()
    decision_at_ms = pair.expires_at_ms - 60_000
    forecast_available_at_ms = decision_at_ms + 5_000

    evaluation = evaluate_shared_terminal_paper_cycle(
        event_cluster_id="fixture-cluster",
        event_cluster_alias="fixture-cluster",
        decision_tau_seconds=60,
        pair=pair,
        settlement_state=_settlement_state(pair),
        simulation=_simulation(),
        replay=_replay(
            pair,
            decision_at_ms,
            forecast_available_at_ms=forecast_available_at_ms,
            include_second=True,
            include_unwind=False,
            second_at_delta_ms=1_001,
        ),
        health=_health(
            decision_at_ms,
            forecast_available_at_ms=forecast_available_at_ms,
        ),
        config=V07StrategyConfig(uncertainty_multiplier=D("0")),
        market_5_up=True,
        market_15_up=False,
        locked_oos=True,
    )

    assert evaluation.decision.action is PairAction.LONG_5_UP_LONG_15_DOWN
    assert evaluation.execution is None
    assert evaluation.cycle.reason_codes == ("unwind_execution_book_missing",)
    assert not evaluation.economic_attempt


def test_verified_receipt_delays_execution_until_receipt_plus_taker_delay() -> None:
    pair = _v07_pair()
    decision_at_ms = pair.expires_at_ms - 60_000
    receipt_at_ms = decision_at_ms + 1_000

    evaluation = evaluate_shared_terminal_paper_cycle(
        event_cluster_id="receipt-timed",
        event_cluster_alias="receipt-timed",
        decision_tau_seconds=60,
        pair=pair,
        settlement_state=_settlement_state(pair),
        simulation=_simulation(),
        replay=_replay(
            pair,
            decision_at_ms,
            forecast_available_at_ms=receipt_at_ms,
            include_second=True,
            include_pre_receipt_first=True,
        ),
        health=_health(
            decision_at_ms,
            forecast_available_at_ms=receipt_at_ms,
            availability_basis=(
                V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
            ),
        ),
        config=V07StrategyConfig(uncertainty_multiplier=D("0")),
        market_5_up=True,
        market_15_up=False,
        locked_oos=True,
    )

    assert evaluation.first_execution_not_before_ms == receipt_at_ms + 250
    assert evaluation.first_execution_observed_at_ms == receipt_at_ms + 250
    assert evaluation.execution is not None
    assert evaluation.execution.first_leg.source_event_id == "first-5-up"
    assert evaluation.effective_signal_to_execution_latency_ms == 1_250
    assert (
        evaluation.to_document()["timing"]["effective_signal_to_execution_latency_ms"]
        == 1_250
    )


def test_only_pre_receipt_execution_surface_fails_closed() -> None:
    pair = _v07_pair()
    decision_at_ms = pair.expires_at_ms - 60_000
    receipt_at_ms = decision_at_ms + 1_000

    evaluation = evaluate_shared_terminal_paper_cycle(
        event_cluster_id="pre-receipt-only",
        event_cluster_alias="pre-receipt-only",
        decision_tau_seconds=60,
        pair=pair,
        settlement_state=_settlement_state(pair),
        simulation=_simulation(),
        replay=_replay(
            pair,
            decision_at_ms,
            forecast_available_at_ms=receipt_at_ms,
            include_second=False,
            include_unwind=False,
            include_pre_receipt_first=True,
            include_post_receipt_first=False,
        ),
        health=_health(
            decision_at_ms,
            forecast_available_at_ms=receipt_at_ms,
            availability_basis=(
                V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
            ),
        ),
        config=V07StrategyConfig(uncertainty_multiplier=D("0")),
        market_5_up=True,
        market_15_up=False,
        locked_oos=True,
    )

    assert evaluation.execution is None
    assert evaluation.cycle.reason_codes == ("first_leg_execution_book_missing",)
    assert evaluation.first_execution_not_before_ms == receipt_at_ms + 250
    assert not evaluation.economic_attempt
