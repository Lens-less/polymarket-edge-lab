"""Public-contract tests for the BTC 5m/15m TWAP relative-value lab."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.edge_lab.btc_twap_relative_value import (
    CalibrationPoint,
    DataHealth,
    IsotonicProbabilityCalibrator,
    JointDistribution,
    OrderBookSnapshot,
    PairAction,
    PairDecision,
    PairExecutionStatus,
    PairPaperSettlement,
    PairSettlementState,
    RelativeValueRejection,
    SameExpiryPair,
    SettlementScenario,
    StrategyConfig,
    TimedPrice,
    TwapMarketContract,
    ValidationEvidence,
    ValidationStatus,
    decide_pair_trade,
    decide_pair_trade_shadow,
    evaluate_validation,
    execute_pair_paper,
    settle_pair_paper_execution,
    simulate_ewma_joint_distribution,
)

D = Decimal


def public_market_fixture(
    *,
    horizon: str,
    opens_at: int,
    market_id: str,
    condition_id: str,
    up_token: str,
    down_token: str,
) -> tuple[dict[str, object], dict[str, object]]:
    window = {"5m": 30, "15m": 60}[horizon]
    slug = f"btc-updown-{horizon}-{opens_at}"
    source = (
        "https://data.chain.link/streams/"
        f"btc-usd-twap-{window}s-streams"
    )
    gamma = {
        "id": market_id,
        "conditionId": condition_id,
        "slug": slug,
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": f'["{up_token}", "{down_token}"]',
        "endDate": "2026-08-12T10:15:00Z",
        "resolutionSource": source,
        "description": (
            "This market resolves Up when the closing TWAP is greater than or "
            "equal to the opening TWAP, using the BTC/USD TWAP data stream "
            f"available at {source}."
        ),
    }
    clob = {
        "c": condition_id,
        "t": [
            {"t": up_token, "o": "Up"},
            {"t": down_token, "o": "Down"},
        ],
        "mos": 5,
        "mts": 0.01,
        "ao": True,
        "itode": True,
        "fd": {"r": 0.07, "e": 1, "to": True},
    }
    return gamma, clob


def same_expiry_pair() -> SameExpiryPair:
    gamma15, clob15 = public_market_fixture(
        horizon="15m",
        opens_at=1_786_528_800,
        market_id="3510551",
        condition_id="0x" + "15" * 32,
        up_token="15-up",
        down_token="15-down",
    )
    gamma5, clob5 = public_market_fixture(
        horizon="5m",
        opens_at=1_786_529_400,
        market_id="3511046",
        condition_id="0x" + "05" * 32,
        up_token="5-up",
        down_token="5-down",
    )
    return SameExpiryPair.from_contracts(
        TwapMarketContract.from_public_metadata(gamma5, clob5),
        TwapMarketContract.from_public_metadata(gamma15, clob15),
    )


def past_only_calibrator(
    decision_at_ms: int,
    horizon: str,
) -> IsotonicProbabilityCalibrator:
    return IsotonicProbabilityCalibrator.fit(
        (
            CalibrationPoint(
                event_id="past-down",
                prediction=D("0.25"),
                outcome=False,
                split="train",
                label_available_at_ms=decision_at_ms - 20_000,
            ),
            CalibrationPoint(
                event_id="past-up",
                prediction=D("0.75"),
                outcome=True,
                split="train",
                label_available_at_ms=decision_at_ms - 10_000,
            ),
        ),
        fit_at_ms=decision_at_ms - 5_000,
        horizon=horizon,
    )


def settlement_state(
    pair: SameExpiryPair,
    *,
    strike_5: Decimal = D("100"),
    strike_15: Decimal = D("200"),
) -> PairSettlementState:
    return PairSettlementState(
        market_5_rule_hash=pair.market_5.rule_hash,
        market_15_rule_hash=pair.market_15.rule_hash,
        market_5_open_timestamp_ms=pair.market_5.opens_at_ms,
        market_15_open_timestamp_ms=pair.market_15.opens_at_ms,
        strike_5=strike_5,
        strike_15=strike_15,
        opening_5_source_event_id="twap-30-opening",
        opening_15_source_event_id="twap-60-opening",
    )
def test_current_public_contracts_form_one_strict_same_expiry_pair() -> None:
    gamma15, clob15 = public_market_fixture(
        horizon="15m",
        opens_at=1_786_528_800,
        market_id="3510551",
        condition_id="0x" + "15" * 32,
        up_token="15-up",
        down_token="15-down",
    )
    gamma5, clob5 = public_market_fixture(
        horizon="5m",
        opens_at=1_786_529_400,
        market_id="3511046",
        condition_id="0x" + "05" * 32,
        up_token="5-up",
        down_token="5-down",
    )

    pair = SameExpiryPair.from_contracts(
        TwapMarketContract.from_public_metadata(gamma5, clob5),
        TwapMarketContract.from_public_metadata(gamma15, clob15),
    )

    assert pair.market_5.twap_window_seconds == 30
    assert pair.market_15.twap_window_seconds == 60
    assert pair.expires_at_ms == 1_786_529_700_000
    assert pair.market_5.fee_schedule.rate == D("0.07")
    assert pair.market_15.taker_delay_ms == 250
    assert pair.market_5.up_token_id == "5-up"
    assert pair.market_15.down_token_id == "15-down"


def test_rule_hash_changes_when_current_market_rules_text_changes() -> None:
    gamma, clob = public_market_fixture(
        horizon="5m",
        opens_at=1_786_529_400,
        market_id="3511046",
        condition_id="0x" + "05" * 32,
        up_token="5-up",
        down_token="5-down",
    )
    original = TwapMarketContract.from_public_metadata(gamma, clob)
    gamma["description"] = f'{gamma["description"]} Current wording revision.'
    revised = TwapMarketContract.from_public_metadata(gamma, clob)

    assert revised.rule_hash != original.rule_hash


def test_one_joint_scenario_set_prices_both_markets_and_pair_payoffs() -> None:
    scenarios = (
        SettlementScenario(twap_30=D("101"), twap_60=D("201")),
        SettlementScenario(twap_30=D("101"), twap_60=D("199")),
        SettlementScenario(twap_30=D("99"), twap_60=D("201")),
        SettlementScenario(twap_30=D("99"), twap_60=D("199")),
    )

    distribution = JointDistribution.from_scenarios(
        scenarios,
        strike_5=D("100"),
        strike_15=D("200"),
    )

    assert distribution.q_5_up == D("0.5")
    assert distribution.q_15_up == D("0.5")
    assert distribution.outcome_counts == {
        "5up_15up": 1,
        "5up_15down": 1,
        "5down_15up": 1,
        "5down_15down": 1,
    }
    assert distribution.payoffs("long_15_up_long_5_down") == (
        D("1"),
        D("0"),
        D("2"),
        D("1"),
    )
    assert distribution.payoffs("long_5_up_long_15_down") == (
        D("1"),
        D("2"),
        D("0"),
        D("1"),
    )


def test_calibrated_marginals_form_one_coherent_joint_distribution() -> None:
    distribution = JointDistribution.from_scenarios(
        (
            SettlementScenario(twap_30=D("101"), twap_60=D("201")),
            SettlementScenario(twap_30=D("101"), twap_60=D("199")),
            SettlementScenario(twap_30=D("99"), twap_60=D("199")),
            SettlementScenario(twap_30=D("99"), twap_60=D("199")),
        ),
        strike_5=D("100"),
        strike_15=D("200"),
    )

    probabilities = distribution.calibrated_outcome_probabilities(
        q_5_up=D("0.7"),
        q_15_up=D("0.4"),
    )

    assert sum(probabilities.values(), D("0")) == D("1")
    assert probabilities["5up_15up"] + probabilities["5up_15down"] == D(
        "0.7"
    )
    assert probabilities["5up_15up"] + probabilities["5down_15up"] == D(
        "0.4"
    )
    assert all(value >= D("0") for value in probabilities.values())


def test_decision_uses_executable_depth_and_fees_for_the_relative_value_leg() -> None:
    pair = same_expiry_pair()
    favorable = SettlementScenario(twap_30=D("99"), twap_60=D("201"))
    distribution = JointDistribution.from_scenarios(
        (favorable,) * 200,
        strike_5=D("100"),
        strike_15=D("200"),
    )
    decision_at = pair.expires_at_ms - 120_000

    def book(token: str, bid: str, ask: str) -> OrderBookSnapshot:
        return OrderBookSnapshot.from_tuples(
            token,
            bids=((D(bid), D("100")),),
            asks=((D(ask), D("100")),),
            timestamp_ms=decision_at - 50,
            tick_size=D("0.01"),
            minimum_order_size=D("5"),
        )

    decision = decide_pair_trade(
        pair=pair,
        settlement_state=settlement_state(pair),
        distribution=distribution,
        books={
            "15-up": book("15-up", "0.59", "0.60"),
            "15-down": book("15-down", "0.39", "0.40"),
            "5-up": book("5-up", "0.49", "0.50"),
            "5-down": book("5-down", "0.49", "0.50"),
        },
        health=DataHealth(
            decision_at_ms=decision_at,
            twap_30_observed_at_ms=decision_at - 100,
            twap_60_observed_at_ms=decision_at - 100,
            twap_30_received_at_ms=decision_at - 100,
            twap_60_received_at_ms=decision_at - 100,
            absolute_clock_drift_ms=20,
            calibration_5=past_only_calibrator(decision_at, "5m"),
            calibration_15=past_only_calibrator(decision_at, "15m"),
        ),
        config=StrategyConfig(),
    )

    assert decision.action is PairAction.LONG_15_UP_LONG_5_DOWN
    assert decision.quantity >= D("5")
    assert decision.execution_notional_per_pair == D("1.10")
    assert decision.fee_per_pair > D("0")
    assert decision.execution_cost_per_pair > D("1.10")
    assert decision.expected_net_pnl_per_pair > D("0.8")
    assert decision.q_5_calibrated == D("0")
    assert decision.q_15_calibrated == D("1")
    assert decision.reason_codes == ()


def test_decision_fails_closed_without_past_only_probability_calibration() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    favorable = SettlementScenario(twap_30=D("99"), twap_60=D("201"))
    distribution = JointDistribution.from_scenarios(
        (favorable,) * 20,
        strike_5=D("100"),
        strike_15=D("200"),
    )
    books = {
        token_id: OrderBookSnapshot.from_tuples(
            token_id,
            bids=((D("0.49"), D("100")),),
            asks=((D("0.50"), D("100")),),
            timestamp_ms=decision_at - 50,
            tick_size=D("0.01"),
            minimum_order_size=D("5"),
        )
        for token_id in ("15-up", "15-down", "5-up", "5-down")
    }

    decision = decide_pair_trade(
        pair=pair,
        settlement_state=settlement_state(pair),
        distribution=distribution,
        books=books,
        health=DataHealth(
            decision_at_ms=decision_at,
            twap_30_observed_at_ms=decision_at - 100,
            twap_60_observed_at_ms=decision_at - 100,
            twap_30_received_at_ms=decision_at - 100,
            twap_60_received_at_ms=decision_at - 100,
            absolute_clock_drift_ms=20,
            calibration_5=None,
            calibration_15=None,
        ),
        config=StrategyConfig(),
    )

    assert decision.action is PairAction.NO_TRADE
    assert decision.reason_codes == ("past_only_calibration_missing",)


def test_uncalibrated_shadow_can_trade_without_becoming_qualified() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    favorable = SettlementScenario(twap_30=D("99"), twap_60=D("201"))
    distribution = JointDistribution.from_scenarios(
        (favorable,) * 200,
        strike_5=D("100"),
        strike_15=D("200"),
    )
    books = {
        token_id: OrderBookSnapshot.from_tuples(
            token_id,
            bids=((D("0.49"), D("100")),),
            asks=((D("0.50"), D("100")),),
            timestamp_ms=decision_at - 50,
            tick_size=D("0.01"),
            minimum_order_size=D("5"),
        )
        for token_id in ("15-up", "15-down", "5-up", "5-down")
    }
    health = DataHealth(
        decision_at_ms=decision_at,
        twap_30_observed_at_ms=decision_at - 2_000,
        twap_60_observed_at_ms=decision_at - 2_000,
        twap_30_received_at_ms=decision_at - 100,
        twap_60_received_at_ms=decision_at - 100,
        absolute_clock_drift_ms=20,
        calibration_5=None,
        calibration_15=None,
    )

    strict = decide_pair_trade(
        pair=pair,
        settlement_state=settlement_state(pair),
        distribution=distribution,
        books=books,
        health=health,
        config=StrategyConfig(maximum_chainlink_staleness_ms=5_000),
    )
    shadow = decide_pair_trade_shadow(
        pair=pair,
        settlement_state=settlement_state(pair),
        distribution=distribution,
        books=books,
        health=health,
        config=StrategyConfig(maximum_chainlink_staleness_ms=5_000),
    )

    assert strict.action is PairAction.NO_TRADE
    assert shadow.action is PairAction.LONG_15_UP_LONG_5_DOWN
    assert shadow.qualification == "development_shadow"
    assert shadow.q_5_calibrated == shadow.q_5_raw == D("0")
    assert shadow.q_15_calibrated == shadow.q_15_raw == D("1")
    assert shadow.reason_codes == ("uncalibrated_shadow_only",)


def test_shadow_execution_settles_to_explainable_after_fee_net_pnl() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    decision = PairDecision(
        action=PairAction.LONG_15_UP_LONG_5_DOWN,
        decision_at_ms=decision_at,
        quantity=D("5"),
        first_token_id="15-up",
        second_token_id="5-down",
        execution_notional_per_pair=D("1.00"),
        fee_per_pair=D("0.03"),
        execution_cost_per_pair=D("1.03"),
        expected_gross_pnl_per_pair=D("0.20"),
        expected_net_pnl_per_pair=D("0.17"),
        uncertainty_adjusted_pnl_per_pair=D("0.15"),
        cvar_per_pair=D("-1.03"),
        loss_probability=D("0.1"),
        q_5_raw=D("0.2"),
        q_15_raw=D("0.8"),
        q_5_calibrated=D("0.2"),
        q_15_calibrated=D("0.8"),
        reason_codes=("uncalibrated_shadow_only",),
        qualification="development_shadow",
    )

    def book(token_id: str, at_ms: int) -> OrderBookSnapshot:
        return OrderBookSnapshot.from_tuples(
            token_id,
            bids=((D("0.49"), D("20")),),
            asks=((D("0.50"), D("20")),),
            timestamp_ms=at_ms,
            tick_size=D("0.01"),
            minimum_order_size=D("5"),
        )

    execution = execute_pair_paper(
        decision=decision,
        pair=pair,
        first_book=book("15-up", decision_at + 250),
        second_book=book("5-down", decision_at + 500),
        unwind_book=book("15-up", decision_at + 500),
        first_source_event_id="first",
        second_source_event_id="second",
        unwind_source_event_id="unwind",
        initial_cash=D("100"),
        max_leg_delay_ms=750,
        max_book_age_ms=750,
    )
    settlement = settle_pair_paper_execution(
        decision=decision,
        pair=pair,
        execution=execution,
        market_5_up=False,
        market_15_up=True,
    )

    assert isinstance(settlement, PairPaperSettlement)
    assert execution.status is PairExecutionStatus.COMPLETE
    assert settlement.explainable is True
    assert settlement.qualified_sample is False
    assert settlement.payout == D("10")
    assert settlement.net_pnl == execution.cashflow_after_execution + D("10")
    assert settlement.net_pnl > D("4")


def test_executable_depth_sizing_never_exceeds_frozen_pair_risk() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    favorable = SettlementScenario(twap_30=D("99"), twap_60=D("201"))
    distribution = JointDistribution.from_scenarios(
        (favorable,) * 200,
        strike_5=D("100"),
        strike_15=D("200"),
    )

    def book(
        token: str,
        bid: str,
        asks: tuple[tuple[Decimal, Decimal], ...],
    ) -> OrderBookSnapshot:
        return OrderBookSnapshot.from_tuples(
            token,
            bids=((D(bid), D("100")),),
            asks=asks,
            timestamp_ms=decision_at - 50,
            tick_size=D("0.01"),
            minimum_order_size=D("5"),
        )

    decision = decide_pair_trade(
        pair=pair,
        settlement_state=settlement_state(pair),
        distribution=distribution,
        books={
            "15-up": book(
                "15-up",
                "0.59",
                ((D("0.60"), D("5")), (D("0.90"), D("100"))),
            ),
            "15-down": book("15-down", "0.39", ((D("0.40"), D("100")),)),
            "5-up": book("5-up", "0.49", ((D("0.50"), D("100")),)),
            "5-down": book("5-down", "0.49", ((D("0.50"), D("100")),)),
        },
        health=DataHealth(
            decision_at_ms=decision_at,
            twap_30_observed_at_ms=decision_at - 100,
            twap_60_observed_at_ms=decision_at - 100,
            twap_30_received_at_ms=decision_at - 100,
            twap_60_received_at_ms=decision_at - 100,
            absolute_clock_drift_ms=20,
            calibration_5=past_only_calibrator(decision_at, "5m"),
            calibration_15=past_only_calibrator(decision_at, "15m"),
        ),
        config=StrategyConfig(),
    )

    assert decision.action is PairAction.LONG_15_UP_LONG_5_DOWN
    assert decision.execution_cost_per_pair is not None
    assert decision.quantity * decision.execution_cost_per_pair <= D("25")


def test_ewma_bootstrap_is_seeded_and_rejects_future_predictor_ticks() -> None:
    decision_at = 1_786_529_520_000
    prices = tuple(
        TimedPrice(
            timestamp_ms=decision_at - (120 - index) * 1_000,
            price=D("65000") + D(index % 7) - D("3"),
        )
        for index in range(120)
    )
    kwargs = {
        "predictor_prices": prices,
        "current_twap_30": D("65001"),
        "current_twap_60": D("65000"),
        "decision_at_ms": decision_at,
        "expiry_ms": decision_at + 60_000,
        "strike_5": D("65002"),
        "strike_15": D("64999"),
        "n_paths": 128,
        "seed": 712,
    }

    first = simulate_ewma_joint_distribution(**kwargs)
    second = simulate_ewma_joint_distribution(**kwargs)

    assert first.scenarios == second.scenarios
    assert first.outcome_counts == second.outcome_counts
    assert sum(first.outcome_counts.values()) == 128
    assert D("0") <= first.q_5_up <= D("1")
    assert D("0") <= first.q_15_up <= D("1")

    with pytest.raises(RelativeValueRejection, match="predictor_leakage"):
        simulate_ewma_joint_distribution(
            **{
                **kwargs,
                "predictor_prices": (
                    *prices,
                    TimedPrice(decision_at + 1, D("65010")),
                ),
            }
        )

    irregular = tuple(
        TimedPrice(
            timestamp_ms=item.timestamp_ms + (1_000 if index >= 50 else 0),
            price=item.price,
        )
        for index, item in enumerate(prices)
    )
    with pytest.raises(RelativeValueRejection, match="predictor_sampling_invalid"):
        simulate_ewma_joint_distribution(
            **{**kwargs, "predictor_prices": irregular}
        )


def test_pair_execution_rejects_action_token_mismatch() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    invalid = PairDecision(
        action=PairAction.LONG_15_UP_LONG_5_DOWN,
        decision_at_ms=decision_at,
        quantity=D("5"),
        first_token_id="5-up",
        second_token_id="15-down",
        execution_notional_per_pair=D("0.9"),
        fee_per_pair=D("0.03"),
        execution_cost_per_pair=D("0.93"),
        expected_gross_pnl_per_pair=D("0.1"),
        expected_net_pnl_per_pair=D("0.07"),
        uncertainty_adjusted_pnl_per_pair=D("0.05"),
        cvar_per_pair=D("-0.93"),
        loss_probability=D("0.1"),
        q_5_raw=D("0.6"),
        q_15_raw=D("0.4"),
        q_5_calibrated=D("0.6"),
        q_15_calibrated=D("0.4"),
        reason_codes=(),
    )

    book = OrderBookSnapshot.from_tuples(
        "5-up",
        bids=((D("0.4"), D("10")),),
        asks=((D("0.5"), D("10")),),
        timestamp_ms=decision_at + 250,
        tick_size=D("0.01"),
        minimum_order_size=D("5"),
    )
    other = OrderBookSnapshot.from_tuples(
        "15-down",
        bids=((D("0.4"), D("10")),),
        asks=((D("0.5"), D("10")),),
        timestamp_ms=decision_at + 500,
        tick_size=D("0.01"),
        minimum_order_size=D("5"),
    )

    with pytest.raises(RelativeValueRejection, match="execution_action_mismatch"):
        execute_pair_paper(
            decision=invalid,
            pair=pair,
            first_book=book,
            second_book=other,
            unwind_book=book,
            first_source_event_id="first",
            second_source_event_id="second",
            unwind_source_event_id="unwind",
            initial_cash=D("100"),
            max_leg_delay_ms=750,
            max_book_age_ms=750,
        )


def test_pair_execution_distinguishes_no_fill_from_complete_pair() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    decision = PairDecision(
        action=PairAction.LONG_15_UP_LONG_5_DOWN,
        decision_at_ms=decision_at,
        quantity=D("5"),
        first_token_id="15-up",
        second_token_id="5-down",
        execution_notional_per_pair=D("1.1"),
        fee_per_pair=D("0.03"),
        execution_cost_per_pair=D("1.13"),
        expected_gross_pnl_per_pair=D("0.1"),
        expected_net_pnl_per_pair=D("0.07"),
        uncertainty_adjusted_pnl_per_pair=D("0.05"),
        cvar_per_pair=D("-1.13"),
        loss_probability=D("0.1"),
        q_5_raw=D("0.4"),
        q_15_raw=D("0.6"),
        q_5_calibrated=D("0.4"),
        q_15_calibrated=D("0.6"),
        reason_codes=(),
    )

    def book(
        token: str,
        *,
        asks: tuple[tuple[Decimal, Decimal], ...],
    ) -> OrderBookSnapshot:
        return OrderBookSnapshot.from_tuples(
            token,
            bids=((D("0.45"), D("10")),),
            asks=asks,
            timestamp_ms=decision_at + 250,
            tick_size=D("0.01"),
            minimum_order_size=D("5"),
        )

    result = execute_pair_paper(
        decision=decision,
        pair=pair,
        first_book=book("15-up", asks=()),
        second_book=book("5-down", asks=((D("0.55"), D("10")),)),
        unwind_book=book("15-up", asks=()),
        first_source_event_id="first",
        second_source_event_id="second",
        unwind_source_event_id="unwind",
        initial_cash=D("100"),
        max_leg_delay_ms=750,
        max_book_age_ms=750,
    )

    assert result.status is PairExecutionStatus.NO_FILL
    assert result.matched_quantity == D("0")
    assert result.unhedged_quantity == D("0")
    assert result.second_leg.reason == "first_leg_unfilled"


def test_pair_execution_cannot_unwind_before_leg_timeout_is_observable() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    decision = PairDecision(
        action=PairAction.LONG_15_UP_LONG_5_DOWN,
        decision_at_ms=decision_at,
        quantity=D("6"),
        first_token_id="15-up",
        second_token_id="5-down",
        execution_notional_per_pair=D("1.1"),
        fee_per_pair=D("0.03"),
        execution_cost_per_pair=D("1.13"),
        expected_gross_pnl_per_pair=D("0.1"),
        expected_net_pnl_per_pair=D("0.07"),
        uncertainty_adjusted_pnl_per_pair=D("0.05"),
        cvar_per_pair=D("-1.13"),
        loss_probability=D("0.1"),
        q_5_raw=D("0.4"),
        q_15_raw=D("0.6"),
        q_5_calibrated=D("0.4"),
        q_15_calibrated=D("0.6"),
        reason_codes=(),
    )

    def snapshot(
        token: str,
        at_ms: int,
        *,
        bid_size: str = "10",
        ask_size: str = "10",
    ) -> OrderBookSnapshot:
        return OrderBookSnapshot.from_tuples(
            token,
            bids=((D("0.45"), D(bid_size)),),
            asks=((D("0.55"), D(ask_size)),),
            timestamp_ms=at_ms,
            tick_size=D("0.01"),
            minimum_order_size=D("5"),
        )

    result = execute_pair_paper(
        decision=decision,
        pair=pair,
        first_book=snapshot("15-up", decision_at + 250),
        second_book=snapshot("5-down", decision_at + 1_100),
        unwind_book=snapshot("15-up", decision_at + 700),
        first_source_event_id="first",
        second_source_event_id="late-second",
        unwind_source_event_id="too-early-unwind",
        initial_cash=D("100"),
        max_leg_delay_ms=750,
        max_book_age_ms=750,
    )

    assert result.status is PairExecutionStatus.FAILED_UNHEDGED
    assert result.unwind_leg is not None
    assert result.unwind_leg.reason == "unwind_book_outside_frozen_window"
    assert result.unhedged_quantity == D("6")


def test_isotonic_calibration_is_monotone_and_train_only() -> None:
    points = tuple(
        CalibrationPoint(
            event_id=f"train-{index}",
            prediction=prediction,
            outcome=outcome,
            split="train",
            label_available_at_ms=100 + index,
        )
        for index, (prediction, outcome) in enumerate(
            (
                (D("0.2"), False),
                (D("0.4"), True),
                (D("0.6"), False),
                (D("0.8"), True),
            )
        )
    )

    calibrator = IsotonicProbabilityCalibrator.fit(
        points,
        fit_at_ms=1_000,
        horizon="5m",
    )

    assert tuple(
        calibrator.transform(value)
        for value in (D("0.2"), D("0.4"), D("0.6"), D("0.8"))
    ) == (D("0"), D("0.5"), D("0.5"), D("1"))
    assert calibrator.training_event_ids == (
        "train-0",
        "train-1",
        "train-2",
        "train-3",
    )
    assert len(calibrator.artifact_hash) == 64

    invalid = (
        *points,
        CalibrationPoint(
            event_id="test-leak",
            prediction=D("0.5"),
            outcome=True,
            split="test",
            label_available_at_ms=999,
        ),
    )
    with pytest.raises(RelativeValueRejection, match="calibration_split_invalid"):
        IsotonicProbabilityCalibrator.fit(
            invalid,
            fit_at_ms=1_000,
            horizon="5m",
        )


def test_pair_execution_unwinds_first_leg_excess_after_partial_second_leg() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    decision = PairDecision(
        action=PairAction.LONG_15_UP_LONG_5_DOWN,
        decision_at_ms=decision_at,
        quantity=D("12"),
        first_token_id="15-up",
        second_token_id="5-down",
        execution_notional_per_pair=D("1.10"),
        fee_per_pair=D("0.03"),
        execution_cost_per_pair=D("1.13"),
        expected_gross_pnl_per_pair=D("0.10"),
        expected_net_pnl_per_pair=D("0.07"),
        uncertainty_adjusted_pnl_per_pair=D("0.05"),
        cvar_per_pair=D("-1.13"),
        loss_probability=D("0.1"),
        q_5_raw=D("0.4"),
        q_15_raw=D("0.6"),
        q_5_calibrated=D("0.4"),
        q_15_calibrated=D("0.6"),
        reason_codes=(),
    )

    def book(
        token_id: str,
        *,
        bids: tuple[tuple[Decimal, Decimal], ...],
        asks: tuple[tuple[Decimal, Decimal], ...],
        at_ms: int,
    ) -> OrderBookSnapshot:
        return OrderBookSnapshot.from_tuples(
            token_id,
            bids=bids,
            asks=asks,
            timestamp_ms=at_ms,
            tick_size=D("0.01"),
            minimum_order_size=D("5"),
        )

    result = execute_pair_paper(
        decision=decision,
        pair=pair,
        first_book=book(
            "15-up",
            bids=((D("0.58"), D("100")),),
            asks=((D("0.60"), D("12")),),
            at_ms=decision_at + 250,
        ),
        second_book=book(
            "5-down",
            bids=((D("0.48"), D("100")),),
            asks=((D("0.50"), D("6")),),
            at_ms=decision_at + 600,
        ),
        unwind_book=book(
            "15-up",
            bids=((D("0.55"), D("12")),),
            asks=((D("0.61"), D("100")),),
            at_ms=decision_at + 850,
        ),
        first_source_event_id="book-first",
        second_source_event_id="book-second",
        unwind_source_event_id="book-unwind",
        initial_cash=D("10000"),
        max_leg_delay_ms=750,
        max_book_age_ms=750,
    )

    assert result.status is PairExecutionStatus.UNWOUND_TO_MATCHED
    assert result.first_leg.filled_quantity == D("12")
    assert result.second_leg.filled_quantity == D("6")
    assert result.unwind_leg is not None
    assert result.unwind_leg.filled_quantity == D("6")
    assert result.matched_quantity == D("6")
    assert result.unhedged_quantity == D("0")
    assert result.legging_cost > D("0")
    assert result.fees_paid > D("0")


def test_fully_unwound_first_leg_loss_remains_explainable_pnl() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    decision = PairDecision(
        action=PairAction.LONG_15_UP_LONG_5_DOWN,
        decision_at_ms=decision_at,
        quantity=D("6"),
        first_token_id="15-up",
        second_token_id="5-down",
        execution_notional_per_pair=D("1.10"),
        fee_per_pair=D("0.03"),
        execution_cost_per_pair=D("1.13"),
        expected_gross_pnl_per_pair=D("0.10"),
        expected_net_pnl_per_pair=D("0.07"),
        uncertainty_adjusted_pnl_per_pair=D("0.05"),
        cvar_per_pair=D("-1.13"),
        loss_probability=D("0.1"),
        q_5_raw=D("0.4"),
        q_15_raw=D("0.6"),
        q_5_calibrated=D("0.4"),
        q_15_calibrated=D("0.6"),
        reason_codes=("uncalibrated_shadow_only",),
        qualification="development_shadow",
    )

    def book(
        token_id: str,
        at_ms: int,
        *,
        bids: tuple[tuple[Decimal, Decimal], ...],
        asks: tuple[tuple[Decimal, Decimal], ...],
    ) -> OrderBookSnapshot:
        return OrderBookSnapshot.from_tuples(
            token_id,
            bids=bids,
            asks=asks,
            timestamp_ms=at_ms,
            tick_size=D("0.01"),
            minimum_order_size=D("5"),
        )

    execution = execute_pair_paper(
        decision=decision,
        pair=pair,
        first_book=book(
            "15-up",
            decision_at + 250,
            bids=((D("0.58"), D("20")),),
            asks=((D("0.60"), D("20")),),
        ),
        second_book=book(
            "5-down",
            decision_at + 500,
            bids=((D("0.48"), D("20")),),
            asks=(),
        ),
        unwind_book=book(
            "15-up",
            decision_at + 750,
            bids=((D("0.55"), D("20")),),
            asks=((D("0.61"), D("20")),),
        ),
        first_source_event_id="first",
        second_source_event_id="empty-second",
        unwind_source_event_id="unwind",
        initial_cash=D("100"),
        max_leg_delay_ms=750,
        max_book_age_ms=750,
    )
    settlement = settle_pair_paper_execution(
        decision=decision,
        pair=pair,
        execution=execution,
        market_5_up=False,
        market_15_up=True,
    )

    assert execution.status is PairExecutionStatus.UNWOUND_TO_MATCHED
    assert execution.matched_quantity == D("0")
    assert execution.unhedged_quantity == D("0")
    assert settlement.explainable is True
    assert settlement.payout == D("0")
    assert settlement.net_pnl == execution.cashflow_after_execution
    assert settlement.net_pnl < D("0")


def test_validation_keeps_missing_economic_evidence_null_instead_of_zero() -> None:
    report = evaluate_validation(
        ValidationEvidence(
            resolved_current_regime_markets=1,
            expected_current_regime_markets=1,
            markets_with_complete_capture=1,
            unknown_resolution_mapping_count=0,
            explainable_simulated_trades=0,
            explainable_fills=0,
            explainable_net_pnls=(),
            chronological_oos_complete=False,
            complete_taker_cost_model=True,
            delay_depth_and_legging_replay_complete=False,
            bootstrap_net_pnl_lower_95=None,
            oos_brier_5=None,
            oos_brier_15=None,
            market_brier_5=None,
            market_brier_15=None,
            oos_expected_calibration_error_5=None,
            oos_expected_calibration_error_15=None,
            maximum_single_event_pnl_share=None,
            direction_exposure_below_single_leg=None,
            signal_strength_net_ev_monotonic=None,
        )
    )

    assert report.status is ValidationStatus.INSUFFICIENT_DATA
    assert report.qualified_net_pnl is None
    assert report.observed_explainable_net_pnl is None
    assert "resolved_markets_below_2000" in report.reason_codes
    assert "simulated_trades_below_500" in report.reason_codes
