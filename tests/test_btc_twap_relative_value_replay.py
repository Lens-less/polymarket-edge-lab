"""Public replay seams for executable BTC relative-value paper trades."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from src.edge_lab.btc_twap_relative_value import (
    DataHealth,
    JointDistribution,
    PairAction,
    PairExecutionStatus,
    PairPaperExecution,
    SettlementScenario,
    StrategyConfig,
)
from src.edge_lab.btc_twap_relative_value_replay import (
    BookReplayToken,
    CausalBookReplay,
    evaluate_shadow_paper_cycle,
    paper_execution_diagnostics,
)
from src.edge_lab.execution import (
    ExecutionResult,
    ExecutionStatus,
    Fill,
    LiquidityRole,
    OrderSide,
)
from tests.test_edge_lab_btc_twap_relative_value import (
    same_expiry_pair,
    settlement_state,
)

D = Decimal


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(
        timestamp_ms / 1_000,
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _fill(
    *,
    token_id: str,
    side: OrderSide,
    quantity: str,
    price: str,
    timestamp_ms: int,
) -> Fill:
    return Fill(
        token_id=token_id,
        side=side,
        quantity=D(quantity),
        price=D(price),
        fee=D("0"),
        liquidity_role=LiquidityRole.TAKER,
        source_event_id=f"event-{token_id}-{timestamp_ms}",
        decision_id="decision-1",
        timestamp_ms=timestamp_ms,
    )


def _result(
    *,
    status: ExecutionStatus,
    requested_quantity: str,
    fills: tuple[Fill, ...],
    reason: str | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        status=status,
        requested_quantity=D(requested_quantity),
        fills=fills,
        source_event_id="source-1",
        decision_id="decision-1",
        reason=reason,
    )


def _execution(
    *,
    status: PairExecutionStatus,
    first_leg: ExecutionResult,
    second_leg: ExecutionResult,
    unwind_leg: ExecutionResult | None,
    matched_quantity: str,
    unhedged_quantity: str,
) -> PairPaperExecution:
    return PairPaperExecution(
        status=status,
        first_leg=first_leg,
        second_leg=second_leg,
        unwind_leg=unwind_leg,
        matched_quantity=D(matched_quantity),
        unhedged_quantity=D(unhedged_quantity),
        fees_paid=D("0"),
        legging_cost=D("0"),
        cashflow_after_execution=D("0"),
    )


def _record(
    *,
    event_type: str,
    received_at: str,
    payload: dict[str, object],
    record_id: str,
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "received_at": received_at,
        "payload": {
            "event_type": event_type,
            "payload": payload,
        },
    }


def test_replay_applies_price_changes_to_causal_signal_and_delayed_books() -> None:
    tokens = {
        token_id: BookReplayToken(
            token_id=token_id,
            tick_size=D("0.01"),
            minimum_order_size=D("5"),
        )
        for token_id in ("15-up", "15-down", "5-up", "5-down")
    }
    records: list[dict[str, object]] = []
    for index, token_id in enumerate(tokens):
        records.append(
            _record(
                event_type="book",
                received_at=f"1970-01-01T00:01:39.{600 + index:03d}Z",
                record_id=f"anchor-{token_id}",
                payload={
                    "asset_id": token_id,
                    "timestamp": "99500",
                    "bids": [{"price": "0.49", "size": "20"}],
                    "asks": [{"price": "0.50", "size": "20"}],
                },
            )
        )
    records.append(
        _record(
            event_type="price_change",
            received_at="1970-01-01T00:01:40.300Z",
            record_id="delta-all-tokens",
            payload={
                "timestamp": "100250",
                "price_changes": [
                    {
                        "asset_id": token_id,
                        "side": "SELL",
                        "price": "0.50",
                        "size": "0",
                        "best_bid": "0.49",
                        "best_ask": "0.51",
                    }
                    for token_id in tokens
                ]
                + [
                    {
                        "asset_id": token_id,
                        "side": "SELL",
                        "price": "0.51",
                        "size": "30",
                        "best_bid": "0.49",
                        "best_ask": "0.51",
                    }
                    for token_id in tokens
                ],
            },
        )
    )

    replay = CausalBookReplay.from_records(records, tokens=tokens)
    signal = replay.signal_books(
        token_ids=tuple(tokens),
        decision_at_ms=100_000,
        maximum_age_ms=750,
    )
    delayed = {
        token_id: replay.first_executable_book(
            token_id=token_id,
            not_before_ms=100_250,
            maximum_wait_ms=750,
        )
        for token_id in tokens
    }

    assert set(signal) == set(tokens)
    assert all(item.snapshot.best_ask == D("0.50") for item in signal.values())
    assert all(item is not None for item in delayed.values())
    assert all(
        item.snapshot.best_ask == D("0.51")
        and item.snapshot.asks[0].size == D("30")
        and item.received_at_ms == 100_300
        for item in delayed.values()
        if item is not None
    )


def test_replay_uses_causal_http_full_book_anchor_without_top_five_truncation() -> None:
    token = BookReplayToken(
        token_id="token",
        tick_size=D("0.01"),
        minimum_order_size=D("1"),
    )
    asks = [
        {"price": str(D("0.50") + D(index) / D("100")), "size": "10"}
        for index in range(7)
    ]
    replay = CausalBookReplay.from_records(
        (
            _record(
                event_type="clob_snapshot",
                received_at="1970-01-01T00:01:39.950Z",
                record_id="http-full-book",
                payload={
                    "snapshot_kind": "clob",
                    "responses": [
                        {
                            "resource": "clob_book",
                            "request_key": "token",
                            "raw_json": {
                                "asset_id": "token",
                                "timestamp": "99900",
                                "bids": [{"price": "0.49", "size": "10"}],
                                "asks": asks,
                            },
                        }
                    ],
                },
            ),
        ),
        tokens={"token": token},
    )

    signal = replay.signal_books(
        token_ids=("token",),
        decision_at_ms=100_000,
        maximum_age_ms=750,
        require_full_depth=True,
    )

    assert len(signal["token"].snapshot.asks) == 7
    assert signal["token"].full_depth_available is True
    assert signal["token"].source_event_id == "http-full-book:clob_book:0"


def test_replay_does_not_label_a_legacy_top_five_anchor_as_full_depth() -> None:
    token = BookReplayToken(
        token_id="token",
        tick_size=D("0.01"),
        minimum_order_size=D("1"),
    )
    replay = CausalBookReplay.from_records(
        (
            _record(
                event_type="book",
                received_at="1970-01-01T00:01:39.950Z",
                record_id="legacy-top-five",
                payload={
                    "asset_id": "token",
                    "timestamp": "99900",
                    "bids": [{"price": "0.49", "size": "10"}],
                    "asks": [{"price": "0.50", "size": "10"}],
                    "depth_policy": "top_5_each_side",
                },
            ),
        ),
        tokens={"token": token},
    )

    assert "token" in replay.signal_books(
        token_ids=("token",),
        decision_at_ms=100_000,
        maximum_age_ms=750,
    )
    assert "token" not in replay.signal_books(
        token_ids=("token",),
        decision_at_ms=100_000,
        maximum_age_ms=750,
        require_full_depth=True,
    )


def test_replay_fails_closed_when_delta_top_of_book_cannot_be_reconciled() -> None:
    token = BookReplayToken(
        token_id="token",
        tick_size=D("0.01"),
        minimum_order_size=D("5"),
    )
    replay = CausalBookReplay.from_records(
        (
            _record(
                event_type="book",
                received_at="1970-01-01T00:01:39.600Z",
                record_id="anchor",
                payload={
                    "asset_id": "token",
                    "timestamp": "99500",
                    "bids": [{"price": "0.49", "size": "20"}],
                    "asks": [{"price": "0.50", "size": "20"}],
                },
            ),
            _record(
                event_type="price_change",
                received_at="1970-01-01T00:01:40.300Z",
                record_id="inconsistent-delta",
                payload={
                    "timestamp": "100250",
                    "price_changes": [
                        {
                            "asset_id": "token",
                            "side": "SELL",
                            "price": "0.60",
                            "size": "10",
                            "best_bid": "0.49",
                            "best_ask": "0.51",
                        }
                    ],
                },
            ),
        ),
        tokens={"token": token},
    )

    assert (
        replay.first_executable_book(
            token_id="token",
            not_before_ms=100_250,
            maximum_wait_ms=750,
        )
        is None
    )


def test_replay_applies_frozen_clock_offset_before_causal_selection() -> None:
    token = BookReplayToken(
        token_id="token",
        tick_size=D("0.01"),
        minimum_order_size=D("5"),
    )
    replay = CausalBookReplay.from_records(
        (
            _record(
                event_type="book",
                received_at="1970-01-01T00:01:39.900Z",
                record_id="raw-local-before-decision",
                payload={
                    "asset_id": "token",
                    "timestamp": "99900",
                    "bids": [{"price": "0.49", "size": "20"}],
                    "asks": [{"price": "0.50", "size": "20"}],
                },
            ),
        ),
        tokens={"token": token},
        receipt_clock_offset_ms=200,
    )

    assert not replay.signal_books(
        token_ids=("token",),
        decision_at_ms=100_000,
        maximum_age_ms=750,
    )
    assert replay.observations_by_token["token"][0].received_at_ms == 100_100


def test_shadow_cycle_runs_signal_to_fill_to_settlement_ledger() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    token_contracts = {
        contract.up_token_id: contract
        for contract in (pair.market_5, pair.market_15)
    } | {
        contract.down_token_id: contract
        for contract in (pair.market_5, pair.market_15)
    }
    tokens = {
        token_id: BookReplayToken(
            token_id=token_id,
            tick_size=contract.tick_size,
            minimum_order_size=contract.minimum_order_size,
        )
        for token_id, contract in token_contracts.items()
    }
    records: list[dict[str, object]] = []
    for index, token_id in enumerate(tokens):
        records.append(
            _record(
                event_type="book",
                received_at=_iso(decision_at - 20 + index),
                record_id=f"signal-{token_id}",
                payload={
                    "asset_id": token_id,
                    "timestamp": str(decision_at - 50),
                    "bids": [{"price": "0.49", "size": "50"}],
                    "asks": [{"price": "0.50", "size": "50"}],
                },
            )
        )
    execution_times = {
        "15-up": (decision_at + 250, decision_at + 260),
        # The second taker order is sized only after the first fill is
        # observable, so its own 250ms market delay starts then.
        "5-down": (decision_at + 500, decision_at + 510),
        "15-down": (decision_at + 320, decision_at + 330),
        "5-up": (decision_at + 340, decision_at + 350),
    }
    records.append(
        _record(
            event_type="book",
            received_at=_iso(decision_at + 310),
            record_id="second-before-sequential-delay",
            payload={
                "asset_id": "5-down",
                "timestamp": str(decision_at + 300),
                "bids": [{"price": "0.49", "size": "50"}],
                "asks": [{"price": "0.49", "size": "50"}],
            },
        )
    )
    for token_id, (source_at, received_at) in execution_times.items():
        records.append(
            _record(
                event_type="book",
                received_at=_iso(received_at),
                record_id=f"execution-{token_id}",
                payload={
                    "asset_id": token_id,
                    "timestamp": str(source_at),
                    "bids": [{"price": "0.49", "size": "50"}],
                    "asks": [{"price": "0.50", "size": "50"}],
                },
            )
        )
    records.append(
        _record(
            event_type="book",
            received_at=_iso(decision_at + 370),
            record_id="unwind-15-up",
            payload={
                "asset_id": "15-up",
                "timestamp": str(decision_at + 360),
                "bids": [{"price": "0.49", "size": "50"}],
                "asks": [{"price": "0.50", "size": "50"}],
            },
        )
    )
    replay = CausalBookReplay.from_records(records, tokens=tokens)
    favorable = SettlementScenario(twap_30=D("99"), twap_60=D("201"))
    distribution = JointDistribution.from_scenarios(
        (favorable,) * 200,
        strike_5=D("100"),
        strike_15=D("200"),
    )

    cycle = evaluate_shadow_paper_cycle(
        pair=pair,
        settlement_state=settlement_state(pair),
        distribution=distribution,
        replay=replay,
        health=DataHealth(
            decision_at_ms=decision_at,
            twap_30_observed_at_ms=decision_at - 2_000,
            twap_60_observed_at_ms=decision_at - 2_000,
            twap_30_received_at_ms=decision_at - 100,
            twap_60_received_at_ms=decision_at - 100,
            absolute_clock_drift_ms=20,
            calibration_5=None,
            calibration_15=None,
        ),
        config=StrategyConfig(maximum_chainlink_staleness_ms=5_000),
        market_5_up=False,
        market_15_up=True,
        initial_cash=D("10000"),
        max_leg_delay_ms=750,
    )

    assert cycle.decision.action is PairAction.LONG_15_UP_LONG_5_DOWN
    assert cycle.execution is not None
    assert cycle.execution.status is PairExecutionStatus.COMPLETE
    assert cycle.execution.first_leg.fills[0].timestamp_ms == decision_at + 260
    assert cycle.execution.second_leg.fills[0].timestamp_ms == decision_at + 510
    assert cycle.settlement is not None
    assert cycle.settlement.explainable is True
    assert cycle.settlement.net_pnl > D("0")
    assert cycle.reason_codes == ()
    document = cycle.to_document()
    assert document["track"] == "development_shadow"
    assert document["decision"]["action"] == "long_15_up_long_5_down"
    assert document["execution"]["status"] == "complete"
    assert len(document["execution"]["first_leg"]["fills"]) == 1
    assert len(document["execution"]["second_leg"]["fills"]) == 1
    assert D(document["settlement"]["net_pnl"]) == cycle.settlement.net_pnl
    assert document["settlement"]["qualified_sample"] is False
    assert document["execution"]["diagnostics"] == {
        "schema_version": "btc-5m-15m-relative-value-execution-diagnostics.v1",
        "economic_attempt": True,
        "second_leg_fill_ratio": "1",
        "first_leg_filled_quantity": _text(cycle.execution.first_leg.filled_quantity),
        "second_leg_filled_quantity": _text(cycle.execution.second_leg.filled_quantity),
        "initial_second_leg_shortfall_quantity": "0",
        "material_second_leg_failure": False,
        "unwind_filled_quantity": "0",
        "residual_unhedged_quantity": _text(cycle.execution.unhedged_quantity),
        "residual_unhedged_max_loss_usdc": _text(cycle.execution.unhedged_quantity),
        "terminal_residual_classification": "none",
        "residual_unhedged_usdc": _text(cycle.execution.unhedged_quantity),
        "dust_failed_unhedged": False,
        "material_failed_unhedged": False,
        "transient_unhedged_exposure_peak_quantity": _text(
            cycle.execution.first_leg.filled_quantity
        ),
        "transient_unhedged_exposure_peak_max_loss_usdc": _text(
            cycle.execution.first_leg.filled_quantity
        ),
        "transient_unhedged_exposure_duration_ms": 250,
        "transient_naked_exposure_peak_quantity": _text(
            cycle.execution.first_leg.filled_quantity
        ),
        "transient_naked_exposure_peak_usdc": _text(
            cycle.execution.first_leg.filled_quantity
        ),
        "transient_naked_exposure_duration_ms": 250,
    }
    assert document["orders_submitted"] == 0
    assert document["authenticated_endpoints_used"] == 0


def test_shadow_cycle_records_first_leg_loss_when_second_leg_times_out() -> None:
    pair = same_expiry_pair()
    decision_at = pair.expires_at_ms - 120_000
    token_contracts = {
        contract.up_token_id: contract
        for contract in (pair.market_5, pair.market_15)
    } | {
        contract.down_token_id: contract
        for contract in (pair.market_5, pair.market_15)
    }
    tokens = {
        token_id: BookReplayToken(
            token_id=token_id,
            tick_size=contract.tick_size,
            minimum_order_size=contract.minimum_order_size,
        )
        for token_id, contract in token_contracts.items()
    }
    records: list[dict[str, object]] = []
    for token_id in tokens:
        records.append(
            _record(
                event_type="book",
                received_at=_iso(decision_at - 20),
                record_id=f"signal-{token_id}",
                payload={
                    "asset_id": token_id,
                    "timestamp": str(decision_at - 50),
                    "bids": [{"price": "0.49", "size": "50"}],
                    "asks": [{"price": "0.50", "size": "50"}],
                },
            )
        )
    records.extend(
        (
            _record(
                event_type="book",
                received_at=_iso(decision_at + 250),
                record_id="first-15-up",
                payload={
                    "asset_id": "15-up",
                    "timestamp": str(decision_at + 250),
                    "bids": [{"price": "0.49", "size": "50"}],
                    "asks": [{"price": "0.50", "size": "50"}],
                },
            ),
            _record(
                event_type="book",
                received_at=_iso(decision_at + 1_020),
                record_id="late-second-5-down",
                payload={
                    "asset_id": "5-down",
                    "timestamp": str(decision_at + 1_020),
                    "bids": [{"price": "0.48", "size": "50"}],
                    "asks": [{"price": "0.51", "size": "50"}],
                },
            ),
            _record(
                event_type="book",
                received_at=_iso(decision_at + 1_260),
                record_id="unwind-15-up",
                payload={
                    "asset_id": "15-up",
                    "timestamp": str(decision_at + 1_250),
                    "bids": [{"price": "0.45", "size": "50"}],
                    "asks": [{"price": "0.51", "size": "50"}],
                },
            ),
        )
    )
    replay = CausalBookReplay.from_records(records, tokens=tokens)
    favorable = SettlementScenario(twap_30=D("99"), twap_60=D("201"))
    distribution = JointDistribution.from_scenarios(
        (favorable,) * 200,
        strike_5=D("100"),
        strike_15=D("200"),
    )

    cycle = evaluate_shadow_paper_cycle(
        pair=pair,
        settlement_state=settlement_state(pair),
        distribution=distribution,
        replay=replay,
        health=DataHealth(
            decision_at_ms=decision_at,
            twap_30_observed_at_ms=decision_at - 2_000,
            twap_60_observed_at_ms=decision_at - 2_000,
            twap_30_received_at_ms=decision_at - 100,
            twap_60_received_at_ms=decision_at - 100,
            absolute_clock_drift_ms=20,
            calibration_5=None,
            calibration_15=None,
        ),
        config=StrategyConfig(maximum_chainlink_staleness_ms=5_000),
        market_5_up=False,
        market_15_up=True,
        initial_cash=D("10000"),
        max_leg_delay_ms=750,
    )

    assert cycle.execution is not None
    assert cycle.execution.status is PairExecutionStatus.UNWOUND_TO_MATCHED
    assert cycle.execution.first_leg.filled_quantity > D("0")
    assert cycle.execution.second_leg.filled_quantity == D("0")
    assert cycle.execution.unwind_leg is not None
    assert cycle.execution.unwind_leg.filled_quantity > D("0")
    assert cycle.settlement is not None
    assert cycle.settlement.explainable is True
    assert cycle.settlement.net_pnl < D("0")
    assert cycle.to_document()["execution"]["diagnostics"] == {
        "schema_version": "btc-5m-15m-relative-value-execution-diagnostics.v1",
        "economic_attempt": True,
        "second_leg_fill_ratio": "0",
        "first_leg_filled_quantity": _text(cycle.execution.first_leg.filled_quantity),
        "second_leg_filled_quantity": "0",
        "initial_second_leg_shortfall_quantity": _text(
            cycle.execution.first_leg.filled_quantity
        ),
        "material_second_leg_failure": True,
        "unwind_filled_quantity": _text(cycle.execution.unwind_leg.filled_quantity),
        "residual_unhedged_quantity": _text(cycle.execution.unhedged_quantity),
        "residual_unhedged_max_loss_usdc": _text(cycle.execution.unhedged_quantity),
        "terminal_residual_classification": "none",
        "residual_unhedged_usdc": _text(cycle.execution.unhedged_quantity),
        "dust_failed_unhedged": False,
        "material_failed_unhedged": False,
        "transient_unhedged_exposure_peak_quantity": _text(
            cycle.execution.first_leg.filled_quantity
        ),
        "transient_unhedged_exposure_peak_max_loss_usdc": _text(
            cycle.execution.first_leg.filled_quantity
        ),
        "transient_unhedged_exposure_duration_ms": 1010,
        "transient_naked_exposure_peak_quantity": _text(
            cycle.execution.first_leg.filled_quantity
        ),
        "transient_naked_exposure_peak_usdc": _text(
            cycle.execution.first_leg.filled_quantity
        ),
        "transient_naked_exposure_duration_ms": 1010,
    }


def test_paper_execution_diagnostics_marks_failed_unhedged_dust_only_when_second_leg_substantially_filled() -> None:
    quantity = D("10")
    execution = _execution(
        status=PairExecutionStatus.FAILED_UNHEDGED,
        first_leg=_result(
            status=ExecutionStatus.FILLED,
            requested_quantity="10",
            fills=(
                _fill(
                    token_id="first",
                    side=OrderSide.BUY,
                    quantity="10",
                    price="0.5",
                    timestamp_ms=1_000,
                ),
            ),
        ),
        second_leg=_result(
            status=ExecutionStatus.PARTIAL,
            requested_quantity="10",
            fills=(
                _fill(
                    token_id="second",
                    side=OrderSide.BUY,
                    quantity="9.995",
                    price="0.5",
                    timestamp_ms=1_250,
                ),
            ),
        ),
        unwind_leg=None,
        matched_quantity="9.995",
        unhedged_quantity="0.005",
    )

    diagnostics = paper_execution_diagnostics(execution, expiry_ms=2_000)

    assert diagnostics["second_leg_fill_ratio"] == "0.9995"
    assert diagnostics["initial_second_leg_shortfall_quantity"] == "0.005"
    assert diagnostics["material_second_leg_failure"] is False
    assert diagnostics["terminal_residual_classification"] == "dust"
    assert diagnostics["dust_failed_unhedged"] is True
    assert diagnostics["material_failed_unhedged"] is False
    assert diagnostics["transient_unhedged_exposure_peak_quantity"] == _text(quantity)
    assert diagnostics["transient_unhedged_exposure_duration_ms"] == 1000


def test_paper_execution_diagnostics_marks_zero_second_leg_unwind_as_material_second_leg_failure_not_terminal_residual() -> None:
    execution = _execution(
        status=PairExecutionStatus.UNWOUND_TO_MATCHED,
        first_leg=_result(
            status=ExecutionStatus.FILLED,
            requested_quantity="12",
            fills=(
                _fill(
                    token_id="first",
                    side=OrderSide.BUY,
                    quantity="12",
                    price="0.5",
                    timestamp_ms=1_000,
                ),
            ),
        ),
        second_leg=_result(
            status=ExecutionStatus.KILLED,
            requested_quantity="12",
            fills=(),
            reason="maximum_leg_delay_exceeded",
        ),
        unwind_leg=_result(
            status=ExecutionStatus.FILLED,
            requested_quantity="12",
            fills=(
                _fill(
                    token_id="first",
                    side=OrderSide.SELL,
                    quantity="12",
                    price="0.45",
                    timestamp_ms=2_010,
                ),
            ),
        ),
        matched_quantity="0",
        unhedged_quantity="0",
    )

    diagnostics = paper_execution_diagnostics(execution, expiry_ms=5_000)

    assert diagnostics["second_leg_fill_ratio"] == "0"
    assert diagnostics["initial_second_leg_shortfall_quantity"] == "12"
    assert diagnostics["material_second_leg_failure"] is True
    assert diagnostics["terminal_residual_classification"] == "none"
    assert diagnostics["dust_failed_unhedged"] is False
    assert diagnostics["material_failed_unhedged"] is False
    assert diagnostics["transient_unhedged_exposure_duration_ms"] == 1010


def test_paper_execution_diagnostics_marks_material_terminal_residual_and_extends_exposure_to_expiry() -> None:
    execution = _execution(
        status=PairExecutionStatus.FAILED_UNHEDGED,
        first_leg=_result(
            status=ExecutionStatus.FILLED,
            requested_quantity="10",
            fills=(
                _fill(
                    token_id="first",
                    side=OrderSide.BUY,
                    quantity="10",
                    price="0.5",
                    timestamp_ms=1_000,
                ),
            ),
        ),
        second_leg=_result(
            status=ExecutionStatus.PARTIAL,
            requested_quantity="10",
            fills=(
                _fill(
                    token_id="second",
                    side=OrderSide.BUY,
                    quantity="7",
                    price="0.5",
                    timestamp_ms=1_250,
                ),
            ),
        ),
        unwind_leg=_result(
            status=ExecutionStatus.PARTIAL,
            requested_quantity="3",
            fills=(
                _fill(
                    token_id="first",
                    side=OrderSide.SELL,
                    quantity="1",
                    price="0.45",
                    timestamp_ms=1_500,
                ),
            ),
        ),
        matched_quantity="7",
        unhedged_quantity="2",
    )

    diagnostics = paper_execution_diagnostics(execution, expiry_ms=4_000)

    assert diagnostics["second_leg_fill_ratio"] == "0.7"
    assert diagnostics["initial_second_leg_shortfall_quantity"] == "3"
    assert diagnostics["material_second_leg_failure"] is True
    assert diagnostics["terminal_residual_classification"] == "material"
    assert diagnostics["dust_failed_unhedged"] is False
    assert diagnostics["material_failed_unhedged"] is True
    assert diagnostics["residual_unhedged_max_loss_usdc"] == "2"
    assert diagnostics["transient_unhedged_exposure_peak_quantity"] == "10"
    assert diagnostics["transient_unhedged_exposure_duration_ms"] == 3000
