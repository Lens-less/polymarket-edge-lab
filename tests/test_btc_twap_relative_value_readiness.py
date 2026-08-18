from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from src.edge_lab.btc_twap_pair_pricing import PairPricingPolicy
from src.edge_lab.btc_twap_relative_value import (
    OrderBookSnapshot,
    PairAction,
    SameExpiryPair,
)
from src.edge_lab.btc_twap_relative_value_readiness import (
    CohortCoverageInput,
    ExecutionProbePrerequisites,
    PerfectInformationAttempt,
    StrategyLiveInputs,
    evaluate_execution_probe_readiness,
    evaluate_perfect_information_upper_bound,
    evaluate_strategy_live_readiness_inputs,
    validate_cohort_data_coverage,
    validate_structural_floor,
)
from src.edge_lab.execution import ExecutionFeeSchedule
from tests.test_btc_twap_relative_value_v07_replay import (
    _settlement_state,
    _v07_pair,
)

D = Decimal


def _book(token_id: str, *, ask: str, bid: str | None = None) -> OrderBookSnapshot:
    best_bid = D(ask) - D("0.01") if bid is None else D(bid)
    return OrderBookSnapshot.from_tuples(
        token_id,
        bids=((best_bid, D("10")),),
        asks=((D(ask), D("10")),),
        timestamp_ms=1_000,
        tick_size=D("0.01"),
        minimum_order_size=D("1"),
    )


def _books() -> dict[str, OrderBookSnapshot]:
    pair = _v07_pair()
    return {
        pair.market_5.up_token_id: _book(pair.market_5.up_token_id, ask="0.40"),
        pair.market_5.down_token_id: _book(pair.market_5.down_token_id, ask="0.20"),
        pair.market_15.up_token_id: _book(pair.market_15.up_token_id, ask="0.30"),
        pair.market_15.down_token_id: _book(pair.market_15.down_token_id, ask="0.25"),
    }


def test_structural_floor_uses_strike_order_to_select_the_only_valid_pair() -> None:
    pair = _v07_pair()
    books = _books()

    below = validate_structural_floor(
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=books,
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("5")),
    )
    above = validate_structural_floor(
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("101"),
            strike_15=D("100"),
        ),
        books=books,
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("5")),
    )

    assert below.selected_action is PairAction.LONG_5_UP_LONG_15_DOWN
    assert below.selected_first_token_id == pair.market_5.up_token_id
    assert below.selected_second_token_id == pair.market_15.down_token_id
    assert above.selected_action is PairAction.LONG_15_UP_LONG_5_DOWN
    assert above.selected_first_token_id == pair.market_15.up_token_id
    assert above.selected_second_token_id == pair.market_5.down_token_id


def test_structural_floor_equal_strikes_compares_both_routes_and_keeps_probe_buffer() -> (
    None
):
    pair = _v07_pair()
    books = _books()

    verdict = validate_structural_floor(
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("100"),
        ),
        books=books,
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("5")),
    )

    assert {level.action for level in verdict.depth_ladder} == {
        PairAction.LONG_15_UP_LONG_5_DOWN,
        PairAction.LONG_5_UP_LONG_15_DOWN,
    }
    assert verdict.selected_action is PairAction.LONG_15_UP_LONG_5_DOWN
    assert verdict.deterministic_floor_exists is True
    assert verdict.probe_buffer_ok is True
    assert verdict.split_probability_live_fallback_allowed is False


def test_structural_floor_distinguishes_c_equal_1_from_point_99_probe_buffer() -> None:
    original = _v07_pair()
    fee_exempt = ExecutionFeeSchedule.fee_exempt(
        reason="boundary-test",
        source_ref="unit-fixture",
    )
    pair = SameExpiryPair.from_contracts(
        replace(
            original.market_5,
            fee_schedule=fee_exempt,
            rule_hash="1" * 64,
        ),
        replace(
            original.market_15,
            fee_schedule=fee_exempt,
            rule_hash="2" * 64,
        ),
    )
    state = replace(
        _settlement_state(original),
        market_5_rule_hash=pair.market_5.rule_hash,
        market_15_rule_hash=pair.market_15.rule_hash,
        strike_5=D("100"),
        strike_15=D("101"),
    )

    at_one = validate_structural_floor(
        pair=pair,
        settlement_state=state,
        books={
            pair.market_5.up_token_id: _book(pair.market_5.up_token_id, ask="0.50"),
            pair.market_15.down_token_id: _book(
                pair.market_15.down_token_id, ask="0.50"
            ),
        },
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("10")),
    )
    at_point_99 = validate_structural_floor(
        pair=pair,
        settlement_state=state,
        books={
            pair.market_5.up_token_id: _book(pair.market_5.up_token_id, ask="0.49"),
            pair.market_15.down_token_id: _book(
                pair.market_15.down_token_id, ask="0.50"
            ),
        },
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("10")),
    )

    assert at_one.selected_level is not None
    assert at_one.selected_level.all_in_cost_per_pair == D("1")
    assert at_one.deterministic_floor_exists is True
    assert at_one.positive_edge_after_cost is False
    assert at_one.probe_buffer_ok is False
    assert at_point_99.selected_level is not None
    assert at_point_99.selected_level.all_in_cost_per_pair == D("0.99")
    assert at_point_99.positive_edge_after_cost is True
    assert at_point_99.probe_buffer_ok is True


def test_perfect_information_upper_bound_is_gate_zero_and_stops_non_positive_route() -> (
    None
):
    pair = _v07_pair()
    books = _books()
    positive = PerfectInformationAttempt(
        attempt_id="positive",
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=books,
        actual_5_up=True,
        actual_15_up=False,
    )
    negative_books = {
        pair.market_5.up_token_id: _book(pair.market_5.up_token_id, ask="0.80"),
        pair.market_5.down_token_id: _book(pair.market_5.down_token_id, ask="0.80"),
        pair.market_15.up_token_id: _book(pair.market_15.up_token_id, ask="0.80"),
        pair.market_15.down_token_id: _book(pair.market_15.down_token_id, ask="0.80"),
    }
    negative = PerfectInformationAttempt(
        attempt_id="negative",
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=negative_books,
        actual_5_up=True,
        actual_15_up=True,
    )

    positive_report = evaluate_perfect_information_upper_bound((positive,))
    negative_report = evaluate_perfect_information_upper_bound((negative,))

    assert positive_report.gate_0_passed is True
    assert positive_report.stop_recommended is False
    assert positive_report.attempts[0].best_action is PairAction.LONG_5_UP_LONG_15_DOWN
    assert negative_report.gate_0_passed is False
    assert negative_report.stop_recommended is True
    assert set(positive_report.attempts[0].per_action_best_total_pnl) == {
        PairAction.LONG_15_UP_LONG_5_DOWN.value,
        PairAction.LONG_5_UP_LONG_15_DOWN.value,
    }
    assert all(
        value >= D("0")
        for value in negative_report.attempts[0].per_action_best_total_pnl.values()
    )


def test_gate_zero_enumerates_non_floor_direction_as_a_true_hindsight_bound() -> None:
    pair = _v07_pair()
    books = {
        pair.market_5.up_token_id: _book(pair.market_5.up_token_id, ask="0.80"),
        pair.market_5.down_token_id: _book(pair.market_5.down_token_id, ask="0.10"),
        pair.market_15.up_token_id: _book(pair.market_15.up_token_id, ask="0.10"),
        pair.market_15.down_token_id: _book(pair.market_15.down_token_id, ask="0.80"),
    }
    attempt = PerfectInformationAttempt(
        attempt_id="hindsight-direction",
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=books,
        actual_5_up=False,
        actual_15_up=False,
    )

    report = evaluate_perfect_information_upper_bound((attempt,))

    assert report.attempts[0].best_action is PairAction.LONG_15_UP_LONG_5_DOWN
    assert report.attempts[0].best_total_pnl > D("0")


def test_coverage_validator_requires_continuous_official_rtds_and_complete_inputs() -> (
    None
):
    complete = validate_cohort_data_coverage(
        CohortCoverageInput(
            cohort_id="cohort-ok",
            market_15_open_ms=0,
            common_expiry_ms=3_000,
            rtds_observed_at_ms=(0, 1_000, 2_000, 3_000),
            rtds_received_at_ms=(10, 1_010, 2_010, 3_010),
            market_5_l2_complete=True,
            market_15_l2_complete=True,
            market_5_trades_complete=True,
            market_15_trades_complete=True,
            fee_complete=True,
            rule_complete=True,
            source_timestamps_complete=True,
            receipt_timestamps_complete=True,
            disconnect_count=0,
            error_count=0,
            clock_sync_valid=True,
        )
    )
    incomplete = validate_cohort_data_coverage(
        CohortCoverageInput(
            cohort_id="cohort-gap",
            market_15_open_ms=0,
            common_expiry_ms=3_000,
            rtds_observed_at_ms=(1_000, 3_500),
            rtds_received_at_ms=(10, 20),
            market_5_l2_complete=False,
            market_15_l2_complete=True,
            market_5_trades_complete=True,
            market_15_trades_complete=False,
            fee_complete=False,
            rule_complete=True,
            source_timestamps_complete=False,
            receipt_timestamps_complete=True,
            disconnect_count=1,
            error_count=1,
            clock_sync_valid=False,
        )
    )

    assert complete.complete is True
    assert incomplete.complete is False
    assert "official_rtds_starts_after_15m_open" in incomplete.reason_codes
    assert "official_rtds_gap_detected" in incomplete.reason_codes
    assert "market_5_l2_incomplete" in incomplete.reason_codes
    assert "fee_metadata_incomplete" in incomplete.reason_codes
    assert "official_rtds_disconnect_observed" in incomplete.reason_codes
    assert "official_rtds_error_observed" in incomplete.reason_codes
    assert "clock_sync_invalid" in incomplete.reason_codes


def test_probe_and_strategy_live_readiness_are_separated_and_fail_closed() -> None:
    pair = _v07_pair()
    books = _books()
    floor = validate_structural_floor(
        pair=pair,
        settlement_state=replace(
            _settlement_state(pair),
            strike_5=D("100"),
            strike_15=D("101"),
        ),
        books=books,
        pricing_policy=PairPricingPolicy(pair_risk_usdc=D("5")),
    )
    coverage = tuple(
        validate_cohort_data_coverage(
            CohortCoverageInput(
                cohort_id=f"cohort-{index}",
                market_15_open_ms=0,
                common_expiry_ms=3_000,
                rtds_observed_at_ms=(0, 1_000, 2_000, 3_000),
                rtds_received_at_ms=(10, 1_010, 2_010, 3_010),
                market_5_l2_complete=True,
                market_15_l2_complete=True,
                market_5_trades_complete=True,
                market_15_trades_complete=True,
                fee_complete=True,
                rule_complete=True,
                source_timestamps_complete=True,
                receipt_timestamps_complete=True,
            )
        )
        for index in range(4)
    )
    probe = evaluate_execution_probe_readiness(
        shadow_net_pnl=D("1"),
        clean_common_terminal_cohort_count=4,
        coverage_results=coverage,
        structural_floor=floor,
        prerequisites=ExecutionProbePrerequisites(
            service_continuously_healthy=True,
            authenticated_read_verified=True,
            fill_stream_verified=True,
            failure_drills_complete=True,
            immutable_probe_preregistration_present=True,
            full_hedge_depth_verified=True,
            gate_0_passed=True,
        ),
    )
    insufficient_unique_probe = evaluate_execution_probe_readiness(
        shadow_net_pnl=D("1"),
        clean_common_terminal_cohort_count=4,
        coverage_results=(coverage[0], coverage[0], coverage[0], coverage[0]),
        structural_floor=floor,
        prerequisites=ExecutionProbePrerequisites(
            service_continuously_healthy=True,
            authenticated_read_verified=True,
            fill_stream_verified=True,
            failure_drills_complete=True,
            immutable_probe_preregistration_present=True,
            full_hedge_depth_verified=True,
            gate_0_passed=True,
        ),
    )

    common_live_inputs = StrategyLiveInputs(
        builder_verified_evidence_chain=True,
        auditable_prelabel_lock_evidence=True,
        clean_prelabeled_common_terminal_cohort_count=100,
        structural_settled_expiry_cluster_count=100,
        structural_explainable_economic_attempt_count=100,
        structural_bootstrap_cluster_mean_lower_95=D("0.01"),
        structural_true_edge_gate_satisfied=True,
        structural_qualified_net_pnl=D("10"),
        structural_gate_0_passed=True,
        structural_max_single_expiry_pnl_concentration=D("0.10"),
        complete_real_execution_evidence=True,
        all_locked_cohorts_in_pnl_distribution=True,
        service_continuously_healthy=True,
    )
    live = evaluate_strategy_live_readiness_inputs(common_live_inputs)
    predictive_only = evaluate_strategy_live_readiness_inputs(
        replace(
            common_live_inputs,
            structural_true_edge_gate_satisfied=False,
        )
    )

    assert probe.eligible is True
    assert insufficient_unique_probe.eligible is False
    assert "fewer_than_4_unique_complete_common_terminal_cohorts" in (
        insufficient_unique_probe.reason_codes
    )
    assert live.eligible is True
    assert predictive_only.eligible is False
    assert "structural_true_edge_gate_not_satisfied" in predictive_only.reason_codes


def test_gate_zero_is_mandatory_for_probe_and_live() -> None:
    live = evaluate_strategy_live_readiness_inputs(
        StrategyLiveInputs(
            builder_verified_evidence_chain=True,
            auditable_prelabel_lock_evidence=True,
            clean_prelabeled_common_terminal_cohort_count=100,
            structural_settled_expiry_cluster_count=100,
            structural_explainable_economic_attempt_count=100,
            structural_bootstrap_cluster_mean_lower_95=D("0.01"),
            structural_true_edge_gate_satisfied=True,
            structural_qualified_net_pnl=D("10"),
            structural_gate_0_passed=False,
            structural_max_single_expiry_pnl_concentration=D("0.10"),
            complete_real_execution_evidence=True,
            all_locked_cohorts_in_pnl_distribution=True,
            service_continuously_healthy=True,
        )
    )

    assert live.eligible is False
    assert "structural_gate_0_not_passed" in live.reason_codes
