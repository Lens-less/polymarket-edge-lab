"""Causal, execution-aware tests for the RTDS--CLOB latency lab."""

from decimal import Decimal

from src.edge_lab.latency import (
    ClockEstimate,
    ClockProbe,
    ClobQuoteEvent,
    LatencyStrategyConfig,
    RuleWindow,
    SourcePriceEvent,
    Trade,
    align_asof,
    chronological_rule_split,
    estimate_clock,
    evaluate_rule_signal,
    lead_lag_study,
    run_latency_backtest,
)


D = Decimal


def source(
    trace_ref: str,
    *,
    price: str,
    server_ms: int,
    received_ms: int,
    carried: bool = False,
    stale: bool = False,
    capture_seq: int = 20,
) -> SourcePriceEvent:
    return SourcePriceEvent(
        trace_ref=trace_ref,
        symbol="BTCUSDT",
        source="binance",
        price=D(price),
        server_event_time_ms=server_ms,
        received_at_ms=received_ms,
        is_carried_forward=carried,
        is_stale=stale,
        capture_seq=capture_seq,
    )


def quote(
    trace_ref: str,
    *,
    bid: str,
    ask: str,
    server_ms: int,
    received_ms: int,
    outcome: str = "YES",
    bid_size: str = "100",
    ask_size: str = "100",
    capture_seq: int = 10,
) -> ClobQuoteEvent:
    return ClobQuoteEvent(
        trace_ref=trace_ref,
        market_id="btc-window",
        token_id=f"btc-{outcome.lower()}",
        outcome=outcome,
        best_bid=D(bid),
        best_ask=D(ask),
        best_bid_size=D(bid_size),
        best_ask_size=D(ask_size),
        server_event_time_ms=server_ms,
        received_at_ms=received_ms,
        capture_seq=capture_seq,
    )


def test_clock_offset_is_estimated_without_future_data_leakage() -> None:
    probes = [
        ClockProbe("probe-1", 900, 1_050, 1_000),
        ClockProbe("probe-2", 1_900, 2_050, 2_000),
        ClockProbe("probe-slow", 2_800, 3_050, 3_000),
    ]
    full_clock = estimate_clock(probes, as_of_ms=3_000)
    assert full_clock.offset_ms == D("100")
    assert full_clock.rtt_ms == D("100")
    assert full_clock.sample_count == 3

    clock = estimate_clock(probes, as_of_ms=1_500)
    assert clock.sample_count == 1
    assert clock.observed_at_ms == 1_000

    future_source = source(
        "source-not-received",
        price="101",
        server_ms=1_600,
        received_ms=1_700,
    )
    visible_quote = quote(
        "quote-visible",
        bid="0.49",
        ask="0.51",
        server_ms=1_550,
        received_ms=1_480,
    )
    result = align_asof(
        decision_at_ms=1_500,
        source_events=[future_source],
        quote_events=[visible_quote],
        source_clock=clock,
        clob_clock=clock,
        max_source_age_ms=500,
        max_quote_age_ms=500,
    )

    assert not result.accepted
    assert result.source is None
    assert result.quote == visible_quote
    assert "no_source_received_before_decision" in result.reject_reasons


def test_asof_join_reports_carry_forward_stale_and_age_failures() -> None:
    flagged_source = source(
        "source-carried",
        price="100",
        server_ms=1_000,
        received_ms=1_010,
        carried=True,
        stale=True,
    )
    flagged_quote = ClobQuoteEvent(
        trace_ref="quote-stale",
        market_id="btc-window",
        token_id="btc-yes",
        outcome="YES",
        best_bid=D("0.48"),
        best_ask=D("0.52"),
        server_event_time_ms=1_100,
        received_at_ms=1_110,
        is_stale=True,
    )
    result = align_asof(
        decision_at_ms=2_000,
        source_events=[flagged_source],
        quote_events=[flagged_quote],
        max_source_age_ms=500,
        max_quote_age_ms=500,
    )

    assert not result.accepted
    assert result.source_age_ms == D("1000")
    assert result.quote_age_ms == D("900")
    assert set(result.reject_reasons) == {
        "source_too_old",
        "source_carried_forward",
        "source_explicitly_stale",
        "quote_too_old",
        "quote_explicitly_stale",
    }


def test_fixed_lag_study_detects_source_lead_and_excludes_flagged_ticks() -> None:
    sources = [
        source("source-0", price="100", server_ms=1_000, received_ms=1_000),
        source("source-up", price="101", server_ms=2_000, received_ms=2_000),
        source("source-down", price="100", server_ms=3_000, received_ms=3_000),
        source(
            "source-carried",
            price="102",
            server_ms=4_000,
            received_ms=4_000,
            carried=True,
        ),
    ]
    quotes = [
        quote("quote-0", bid="0.49", ask="0.51", server_ms=1_000, received_ms=1_000),
        quote("quote-flat", bid="0.49", ask="0.51", server_ms=2_000, received_ms=2_000),
        quote("quote-up", bid="0.54", ask="0.56", server_ms=2_499, received_ms=2_499),
        quote("quote-before-down", bid="0.54", ask="0.56", server_ms=3_000, received_ms=3_000),
        quote("quote-down", bid="0.44", ask="0.46", server_ms=3_499, received_ms=3_499),
    ]

    study = lead_lag_study(
        source_events=sources,
        quote_events=quotes,
        lag_grid_ms=(0, 500),
        symbol="BTCUSDT",
        source_name="binance",
        market_id="btc-window",
        token_id="btc-yes",
        outcome="YES",
        max_source_age_ms=250,
        max_quote_age_ms=750,
    )

    assert [score.lag_ms for score in study.scores] == [0, 500]
    assert study.scores[0].correlation is None
    assert study.scores[1].samples == 2
    assert study.scores[1].correlation == D("1")
    assert study.scores[1].direction_hit_rate == D("1")
    assert study.scores[1].source_trace_refs == ("source-up", "source-down")
    assert study.rejection_counts["source_carried_forward"] == 1


def test_fixed_lag_source_order_uses_capture_seq_inside_same_millisecond() -> None:
    sources = [
        source(
            "z-first-by-capture",
            price="100",
            server_ms=1_000,
            received_ms=1_000,
            capture_seq=1,
        ),
        source(
            "a-second-by-capture",
            price="110",
            server_ms=1_000,
            received_ms=1_000,
            capture_seq=2,
        ),
        source(
            "m-third-by-capture",
            price="120",
            server_ms=2_000,
            received_ms=2_000,
            capture_seq=3,
        ),
    ]
    quotes = [
        quote(
            "quote-before-first-ms",
            bid="0.49",
            ask="0.51",
            server_ms=999,
            received_ms=999,
            capture_seq=0,
        ),
        quote(
            "quote-before-second-ms",
            bid="0.54",
            ask="0.56",
            server_ms=1_999,
            received_ms=1_999,
            capture_seq=2,
        ),
    ]

    study = lead_lag_study(
        source_events=sources,
        quote_events=quotes,
        lag_grid_ms=(0,),
        symbol="BTCUSDT",
        source_name="binance",
        market_id="btc-window",
        token_id="btc-yes",
        outcome="YES",
        max_source_age_ms=0,
        max_quote_age_ms=2_000,
    )

    assert study.scores[0].samples == 2
    assert study.scores[0].source_trace_refs == (
        "a-second-by-capture",
        "m-third-by-capture",
    )


def test_rule_windows_split_chronologically_without_market_leakage() -> None:
    windows = [
        RuleWindow(
            trace_ref=f"rule-{index}",
            market_id=f"market-{index}",
            source_symbol="BTCUSDT",
            opens_at_ms=index * 1_000,
            closes_at_ms=(index + 1) * 1_000,
            entry_deadline_ms=(index + 1) * 1_000 - 100,
            price_to_beat=D("100"),
            observed_at_ms=max(0, index * 1_000 - 100),
            effective_at_ms=index * 1_000,
            resolved_outcome="YES",
            resolution_observed_at_ms=(index + 1) * 1_000 + 100,
        )
        for index in reversed(range(10))
    ]
    split = chronological_rule_split(
        windows,
        train_fraction=D("0.6"),
        validation_fraction=D("0.2"),
    )

    assert [rule.market_id for rule in split.train] == [
        f"market-{index}" for index in range(6)
    ]
    assert [rule.market_id for rule in split.validation] == [
        "market-6",
        "market-7",
    ]
    assert [rule.market_id for rule in split.test] == ["market-8", "market-9"]
    assert split.as_experiment_splits() == {
        "train": tuple(f"market-{index}" for index in range(6)),
        "validation": ("market-6", "market-7"),
        "test": ("market-8", "market-9"),
    }


def test_price_to_beat_signal_is_traceable_and_deadline_gated() -> None:
    rule = RuleWindow(
        trace_ref="rule-btc-1",
        market_id="btc-window",
        source_symbol="BTCUSDT",
        opens_at_ms=1_000,
        closes_at_ms=3_000,
        entry_deadline_ms=2_500,
        price_to_beat=D("100"),
        observed_at_ms=900,
        effective_at_ms=1_000,
        resolved_outcome="YES",
        resolution_observed_at_ms=3_100,
    )
    event = source(
        "source-signal",
        price="101",
        server_ms=1_900,
        received_ms=2_000,
    )
    accepted = evaluate_rule_signal(
        rule=rule,
        source_event=event,
        decision_at_ms=2_000,
        threshold=D("0.5"),
        max_source_age_ms=1_000,
    )
    assert accepted.accepted
    assert accepted.outcome == "YES"
    assert accepted.trace_refs == ("rule-btc-1", "source-signal")

    late = evaluate_rule_signal(
        rule=rule,
        source_event=event,
        decision_at_ms=2_600,
        threshold=D("0.5"),
        max_source_age_ms=1_000,
    )
    assert not late.accepted
    assert late.outcome == "YES"
    assert late.reject_reasons == ("past_entry_deadline",)


def test_millisecond_ideal_edge_disappears_after_executable_friction() -> None:
    rule = RuleWindow(
        trace_ref="rule-latency",
        market_id="btc-window",
        source_symbol="BTCUSDT",
        opens_at_ms=1_000,
        closes_at_ms=3_000,
        entry_deadline_ms=2_500,
        price_to_beat=D("100"),
        observed_at_ms=900,
        effective_at_ms=1_000,
        resolved_outcome="YES",
        resolution_observed_at_ms=3_100,
    )
    signal = source(
        "source-leading-tick",
        price="101",
        server_ms=2_000,
        received_ms=2_000,
    )
    quotes = [
        quote(
            "quote-before-reprice",
            bid="0.96",
            ask="0.970",
            server_ms=2_000,
            received_ms=2_000,
        ),
        quote(
            "quote-after-reprice",
            bid="0.998",
            ask="0.999",
            server_ms=2_049,
            received_ms=2_049,
        ),
    ]
    result = run_latency_backtest(
        source_events=[signal],
        quote_events=quotes,
        trades=[],
        rules=[rule],
        config=LatencyStrategyConfig(
            action="taker",
            quantity=D("1"),
            signal_threshold=D("0.5"),
            base_placement_latency_ms=100,
            slippage_per_share=D("0.002"),
            max_source_age_ms=500,
            max_quote_age_ms=500,
            fee_rate=D("0.05"),
            fee_exponent=D("1"),
            fee_source_ref="fixture://btc-window/fd",
            market_id="btc-window",
            tick_size=D("0.001"),
            min_order_size=D("1"),
        ),
    )

    ideal = result.scenario("ideal")
    neutral = result.scenario("neutral")
    assert ideal.fill_count == 1
    assert ideal.net_profit == D("0.02854")
    assert ideal.executions[0].quote_trace_ref == "quote-before-reprice"
    assert neutral.fill_count == 1
    assert neutral.net_profit == D("-0.00105")
    assert neutral.fees == D("0.00005")
    assert neutral.slippage_cost == D("0.002")
    assert neutral.executions[0].quote_trace_ref == "quote-after-reprice"
    assert result.classification == "rejected"
    assert "ideal_only_latency_edge" in result.reject_reasons
    assert set(neutral.executions[0].trace_refs) == {
        "rule-latency",
        "source-leading-tick",
        "quote-after-reprice",
    }
    interval = result.bootstrap(scenario="neutral", n_resamples=25, seed=17)
    assert interval.n_events == 1
    assert interval.estimate == D("-0.00105")
    assert interval.lower == interval.upper == D("-0.00105")


def test_selective_maker_quote_requires_post_placement_trade_and_queue() -> None:
    rule = RuleWindow(
        trace_ref="rule-maker",
        market_id="btc-window",
        source_symbol="BTCUSDT",
        opens_at_ms=1_000,
        closes_at_ms=3_000,
        entry_deadline_ms=2_500,
        price_to_beat=D("100"),
        observed_at_ms=900,
        effective_at_ms=1_000,
        resolved_outcome="YES",
        resolution_observed_at_ms=3_100,
    )
    signal = source(
        "source-maker",
        price="101",
        server_ms=2_000,
        received_ms=2_000,
    )
    book = quote(
        "quote-maker",
        bid="0.60",
        ask="0.62",
        bid_size="1",
        server_ms=2_000,
        received_ms=2_000,
    )
    trades = [
        Trade(
            trace_ref="trade-occurred-before-neutral-placement",
            market_id="btc-window",
            token_id="btc-yes",
            outcome="YES",
            aggressor_side="SELL",
            price=D("0.60"),
            size=D("10"),
            server_event_time_ms=2_050,
            received_at_ms=2_150,
        ),
        Trade(
            trace_ref="trade-confirmed-after-placement",
            market_id="btc-window",
            token_id="btc-yes",
            outcome="YES",
            aggressor_side="SELL",
            price=D("0.60"),
            size=D("3"),
            server_event_time_ms=2_250,
            received_at_ms=2_250,
        ),
    ]
    result = run_latency_backtest(
        source_events=[signal],
        quote_events=[book],
        trades=trades,
        rules=[rule],
        clob_clock=ClockEstimate(
            offset_ms=D("0"),
            rtt_ms=D("0"),
            uncertainty_ms=D("0"),
            sample_count=1,
            trace_refs=("fixture-clock",),
            observed_at_ms=1_900,
            valid_for_ms=2_000,
        ),
        config=LatencyStrategyConfig(
            action="maker",
            quantity=D("2"),
            signal_threshold=D("0.5"),
            base_placement_latency_ms=100,
            slippage_per_share=D("0"),
            max_source_age_ms=500,
            max_quote_age_ms=500,
            fee_rate=D("0.05"),
            fee_exponent=D("1"),
            fee_source_ref="fixture://btc-window/fd",
            market_id="btc-window",
            tick_size=D("0.001"),
            min_order_size=D("1"),
        ),
    )

    neutral = result.scenario("neutral")
    execution = neutral.executions[0]
    assert execution.status == "filled"
    assert execution.fill_quantity == D("2")
    assert execution.fee == D("0")
    assert execution.net_profit == D("0.80")
    assert execution.trade_trace_refs == ("trade-confirmed-after-placement",)
    assert "trade-occurred-before-neutral-placement" not in execution.trace_refs
    pessimistic_execution = result.scenario("pessimistic").executions[0]
    assert pessimistic_execution.status == "partial"
    assert pessimistic_execution.fill_quantity == D("1")
    assert result.classification == "insufficient_data"
    assert "independent_settled_events<100" in result.reject_reasons

    unclocked = run_latency_backtest(
        source_events=[signal],
        quote_events=[book],
        trades=trades,
        rules=[rule],
        config=LatencyStrategyConfig(
            action="maker",
            quantity=D("2"),
            signal_threshold=D("0.5"),
            base_placement_latency_ms=100,
            slippage_per_share=D("0"),
            max_source_age_ms=500,
            max_quote_age_ms=500,
            fee_rate=D("0.05"),
            fee_exponent=D("1"),
            fee_source_ref="fixture://btc-window/fd",
            market_id="btc-window",
            tick_size=D("0.001"),
            min_order_size=D("1"),
        ),
    )
    assert unclocked.scenario("neutral").fill_count == 0
    assert unclocked.scenario("neutral").executions[0].reject_reasons == (
        "maker_trade_clock_unverified",
    )


def test_taker_signal_refuses_to_invent_top_of_book_capacity() -> None:
    rule = RuleWindow(
        trace_ref="rule-thin",
        market_id="btc-window",
        source_symbol="BTCUSDT",
        opens_at_ms=1_000,
        closes_at_ms=3_000,
        entry_deadline_ms=2_500,
        price_to_beat=D("100"),
        observed_at_ms=900,
        effective_at_ms=1_000,
        resolved_outcome="YES",
        resolution_observed_at_ms=3_100,
    )
    result = run_latency_backtest(
        source_events=[
            source(
                "source-thin",
                price="101",
                server_ms=2_000,
                received_ms=2_000,
            )
        ],
        quote_events=[
            quote(
                "quote-thin",
                bid="0.50",
                ask="0.51",
                ask_size="0.5",
                server_ms=2_000,
                received_ms=2_000,
            )
        ],
        trades=[],
        rules=[rule],
        config=LatencyStrategyConfig(
            action="taker",
            quantity=D("1"),
            signal_threshold=D("0.5"),
            base_placement_latency_ms=10,
            slippage_per_share=D("0.001"),
            max_source_age_ms=500,
            max_quote_age_ms=500,
            fee_rate=D("0.05"),
            fee_exponent=D("1"),
            fee_source_ref="fixture://btc-window/fd",
            market_id="btc-window",
            tick_size=D("0.001"),
            min_order_size=D("1"),
        ),
    )

    ideal = result.scenario("ideal")
    assert ideal.fill_count == 0
    assert ideal.executions[0].reject_reasons == (
        "insufficient_visible_ask_depth",
    )
