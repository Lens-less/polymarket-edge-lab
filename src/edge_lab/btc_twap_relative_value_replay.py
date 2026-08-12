"""Causal L2 reconstruction for BTC 5m/15m paper execution.

The recorder stores compact full-book anchors plus target-only price changes.
This module rebuilds only states whose top of book can be reconciled with the
exchange-provided best bid/ask.  An uncertain state is omitted until the next
full anchor, so downstream paper fills fail closed instead of inventing depth.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from .btc_twap_relative_value import (
    DataHealth,
    JointDistribution,
    OrderBookSnapshot,
    PairAction,
    PairDecision,
    PairExecutionStatus,
    PairPaperExecution,
    PairPaperSettlement,
    PairSettlementState,
    SameExpiryPair,
    StrategyConfig,
    TwapMarketContract,
    decide_pair_trade_shadow,
    execute_pair_paper,
    settle_pair_paper_execution,
)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _epoch_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(
            f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000)


def _source_ms(payload: Mapping[str, Any]) -> int | None:
    try:
        value = int(payload.get("timestamp"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


@dataclass(frozen=True)
class BookReplayToken:
    """Frozen execution metadata needed to construct one token book."""

    token_id: str
    tick_size: Decimal
    minimum_order_size: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.token_id, str) or not self.token_id:
            raise ValueError("token_id must be non-empty")
        tick = _decimal(self.tick_size)
        minimum = _decimal(self.minimum_order_size)
        if tick is None or tick <= 0:
            raise ValueError("tick_size must be positive")
        if minimum is None or minimum <= 0:
            raise ValueError("minimum_order_size must be positive")
        object.__setattr__(self, "tick_size", tick)
        object.__setattr__(self, "minimum_order_size", minimum)


@dataclass(frozen=True)
class CausalBookObservation:
    """One fully reconstructed book with source and local receipt clocks."""

    snapshot: OrderBookSnapshot
    received_at_ms: int
    source_event_id: str
    update_kind: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, OrderBookSnapshot):
            raise TypeError("snapshot must be an OrderBookSnapshot")
        if (
            isinstance(self.received_at_ms, bool)
            or not isinstance(self.received_at_ms, int)
            or self.received_at_ms < 0
        ):
            raise ValueError("received_at_ms must be non-negative")
        if not isinstance(self.source_event_id, str) or not self.source_event_id:
            raise ValueError("source_event_id must be non-empty")
        if self.update_kind not in {"book", "price_change"}:
            raise ValueError("update_kind must be book or price_change")

    @property
    def source_at_ms(self) -> int:
        return self.snapshot.timestamp_ms


@dataclass
class _DepthState:
    bids: dict[Decimal, Decimal]
    asks: dict[Decimal, Decimal]


def _levels(value: Any) -> dict[Decimal, Decimal] | None:
    if not isinstance(value, list):
        return None
    result: dict[Decimal, Decimal] = {}
    for item in value:
        if not isinstance(item, Mapping):
            return None
        price = _decimal(item.get("price"))
        size = _decimal(item.get("size"))
        if price is None or size is None or not Decimal("0") <= price <= Decimal(
            "1"
        ) or size <= 0:
            return None
        result[price] = size
    return result


def _snapshot(
    token: BookReplayToken,
    state: _DepthState,
    *,
    source_at_ms: int,
) -> OrderBookSnapshot | None:
    if not state.bids or not state.asks:
        return None
    return OrderBookSnapshot.from_tuples(
        token.token_id,
        bids=tuple(state.bids.items()),
        asks=tuple(state.asks.items()),
        timestamp_ms=source_at_ms,
        tick_size=token.tick_size,
        minimum_order_size=token.minimum_order_size,
    )


def _top_matches(
    state: _DepthState,
    changes: Sequence[Mapping[str, Any]],
) -> bool:
    expected_bid = {_decimal(item.get("best_bid")) for item in changes}
    expected_ask = {_decimal(item.get("best_ask")) for item in changes}
    if (
        None in expected_bid
        or None in expected_ask
        or len(expected_bid) != 1
        or len(expected_ask) != 1
        or not state.bids
        or not state.asks
    ):
        return False
    return max(state.bids) == next(iter(expected_bid)) and min(state.asks) == next(
        iter(expected_ask)
    )


@dataclass(frozen=True)
class CausalBookReplay:
    """Immutable reconstructed event stream indexed by token."""

    observations_by_token: Mapping[str, tuple[CausalBookObservation, ...]]

    @classmethod
    def from_records(
        cls,
        records: Iterable[Mapping[str, Any]],
        *,
        tokens: Mapping[str, BookReplayToken],
        receipt_clock_offset_ms: int = 0,
    ) -> "CausalBookReplay":
        frozen_tokens = dict(tokens)
        if not frozen_tokens or any(
            token_id != token.token_id
            for token_id, token in frozen_tokens.items()
        ):
            raise ValueError("tokens must map each token_id to matching metadata")
        if isinstance(receipt_clock_offset_ms, bool) or not isinstance(
            receipt_clock_offset_ms, int
        ):
            raise TypeError("receipt_clock_offset_ms must be an integer")
        ordered: list[tuple[int, int, str, str, Mapping[str, Any]]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            envelope = record.get("payload")
            if not isinstance(envelope, Mapping):
                continue
            event_type = envelope.get("event_type")
            payload = envelope.get("payload")
            if event_type not in {"book", "price_change"} or not isinstance(
                payload, Mapping
            ):
                continue
            raw_received_at_ms = _epoch_ms(record.get("received_at"))
            received_at_ms = (
                None
                if raw_received_at_ms is None
                else raw_received_at_ms + receipt_clock_offset_ms
            )
            source_at_ms = _source_ms(payload)
            source_event_id = str(record.get("record_id") or "")
            if (
                received_at_ms is None
                or source_at_ms is None
                or not source_event_id
            ):
                continue
            ordered.append(
                (
                    received_at_ms,
                    source_at_ms,
                    source_event_id,
                    str(event_type),
                    payload,
                )
            )
        ordered.sort(key=lambda item: item[:3])

        states: dict[str, _DepthState] = {}
        invalid: set[str] = set()
        observations: dict[str, list[CausalBookObservation]] = {
            token_id: [] for token_id in frozen_tokens
        }
        for received_at_ms, source_at_ms, event_id, event_type, payload in ordered:
            if event_type == "book":
                token_id = str(payload.get("asset_id") or "")
                if token_id not in frozen_tokens:
                    continue
                bids = _levels(payload.get("bids"))
                asks = _levels(payload.get("asks"))
                if bids is None or asks is None or not bids or not asks:
                    states.pop(token_id, None)
                    invalid.add(token_id)
                    continue
                state = _DepthState(bids=bids, asks=asks)
                snapshot = _snapshot(
                    frozen_tokens[token_id], state, source_at_ms=source_at_ms
                )
                if snapshot is None:
                    states.pop(token_id, None)
                    invalid.add(token_id)
                    continue
                states[token_id] = state
                invalid.discard(token_id)
                observations[token_id].append(
                    CausalBookObservation(
                        snapshot=snapshot,
                        received_at_ms=received_at_ms,
                        source_event_id=event_id,
                        update_kind="book",
                    )
                )
                continue

            raw_changes = payload.get("price_changes")
            if not isinstance(raw_changes, list):
                continue
            by_token: dict[str, list[Mapping[str, Any]]] = {}
            for item in raw_changes:
                if not isinstance(item, Mapping):
                    continue
                token_id = str(item.get("asset_id") or "")
                if token_id in frozen_tokens:
                    by_token.setdefault(token_id, []).append(item)
            for token_id, changes in by_token.items():
                state = states.get(token_id)
                if state is None or token_id in invalid:
                    continue
                coherent = True
                for change in changes:
                    price = _decimal(change.get("price"))
                    size = _decimal(change.get("size"))
                    side = change.get("side")
                    if (
                        price is None
                        or size is None
                        or not Decimal("0") <= price <= Decimal("1")
                        or size < 0
                        or side not in {"BUY", "SELL"}
                    ):
                        coherent = False
                        break
                    levels = state.bids if side == "BUY" else state.asks
                    if size == 0:
                        levels.pop(price, None)
                    else:
                        levels[price] = size
                if not coherent or not _top_matches(state, changes):
                    invalid.add(token_id)
                    continue
                snapshot = _snapshot(
                    frozen_tokens[token_id], state, source_at_ms=source_at_ms
                )
                if snapshot is None:
                    invalid.add(token_id)
                    continue
                observations[token_id].append(
                    CausalBookObservation(
                        snapshot=snapshot,
                        received_at_ms=received_at_ms,
                        source_event_id=event_id,
                        update_kind="price_change",
                    )
                )

        return cls(
            observations_by_token=MappingProxyType(
                {
                    token_id: tuple(values)
                    for token_id, values in observations.items()
                }
            )
        )

    def signal_books(
        self,
        *,
        token_ids: Sequence[str],
        decision_at_ms: int,
        maximum_age_ms: int,
    ) -> Mapping[str, CausalBookObservation]:
        if maximum_age_ms < 0:
            raise ValueError("maximum_age_ms cannot be negative")
        result: dict[str, CausalBookObservation] = {}
        for token_id in token_ids:
            eligible = [
                item
                for item in self.observations_by_token.get(token_id, ())
                if item.source_at_ms <= decision_at_ms
                and item.received_at_ms <= decision_at_ms
                and decision_at_ms - item.source_at_ms <= maximum_age_ms
                and decision_at_ms - item.received_at_ms <= maximum_age_ms
            ]
            if eligible:
                result[token_id] = eligible[-1]
        return MappingProxyType(result)

    def first_executable_book(
        self,
        *,
        token_id: str,
        not_before_ms: int,
        maximum_wait_ms: int,
    ) -> CausalBookObservation | None:
        if maximum_wait_ms < 0:
            raise ValueError("maximum_wait_ms cannot be negative")
        deadline = not_before_ms + maximum_wait_ms
        eligible = [
            item
            for item in self.observations_by_token.get(token_id, ())
            if not_before_ms
            <= max(item.source_at_ms, item.received_at_ms)
            <= deadline
        ]
        return eligible[0] if eligible else None


@dataclass(frozen=True)
class PairPaperCycleEvaluation:
    """One raw-model development cycle from decision through settlement."""

    decision: PairDecision
    execution: PairPaperExecution | None
    settlement: PairPaperSettlement | None
    reason_codes: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        decision = self.decision

        def decimal_text(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        def leg_document(result: Any) -> dict[str, Any]:
            return {
                "status": result.status.value,
                "requested_quantity": str(result.requested_quantity),
                "filled_quantity": str(result.filled_quantity),
                "notional": str(result.notional),
                "fee": str(result.fee),
                "average_price": decimal_text(result.average_price),
                "source_event_id": result.source_event_id,
                "decision_id": result.decision_id,
                "reason": result.reason,
                "fills": [
                    {
                        "token_id": fill.token_id,
                        "side": fill.side.value,
                        "quantity": str(fill.quantity),
                        "price": str(fill.price),
                        "notional": str(fill.notional),
                        "fee": str(fill.fee),
                        "liquidity_role": fill.liquidity_role.value,
                        "source_event_id": fill.source_event_id,
                        "decision_id": fill.decision_id,
                        "timestamp_ms": fill.timestamp_ms,
                    }
                    for fill in result.fills
                ],
            }

        execution_document = None
        if self.execution is not None:
            execution_document = {
                "status": self.execution.status.value,
                "matched_quantity": str(self.execution.matched_quantity),
                "unhedged_quantity": str(self.execution.unhedged_quantity),
                "fees_paid": str(self.execution.fees_paid),
                "legging_cost": str(self.execution.legging_cost),
                "cashflow_after_execution": str(
                    self.execution.cashflow_after_execution
                ),
                "first_leg": leg_document(self.execution.first_leg),
                "second_leg": leg_document(self.execution.second_leg),
                "unwind_leg": (
                    None
                    if self.execution.unwind_leg is None
                    else leg_document(self.execution.unwind_leg)
                ),
            }
        settlement_document = None
        if self.settlement is not None:
            settlement_document = {
                "market_5_up": self.settlement.market_5_up,
                "market_15_up": self.settlement.market_15_up,
                "first_token_wins": self.settlement.first_token_wins,
                "second_token_wins": self.settlement.second_token_wins,
                "payout": str(self.settlement.payout),
                "net_pnl": str(self.settlement.net_pnl),
                "explainable": self.settlement.explainable,
                "qualified_sample": self.settlement.qualified_sample,
            }
        return {
            "schema_version": "btc-5m-15m-relative-value-paper-cycle.v1",
            "track": decision.qualification,
            "decision": {
                "action": decision.action.value,
                "decision_at_ms": decision.decision_at_ms,
                "quantity": str(decision.quantity),
                "first_token_id": decision.first_token_id,
                "second_token_id": decision.second_token_id,
                "execution_notional_per_pair": decimal_text(
                    decision.execution_notional_per_pair
                ),
                "fee_per_pair": decimal_text(decision.fee_per_pair),
                "execution_cost_per_pair": decimal_text(
                    decision.execution_cost_per_pair
                ),
                "expected_gross_pnl_per_pair": decimal_text(
                    decision.expected_gross_pnl_per_pair
                ),
                "expected_net_pnl_per_pair": decimal_text(
                    decision.expected_net_pnl_per_pair
                ),
                "uncertainty_adjusted_pnl_per_pair": decimal_text(
                    decision.uncertainty_adjusted_pnl_per_pair
                ),
                "cvar_per_pair": decimal_text(decision.cvar_per_pair),
                "loss_probability": decimal_text(decision.loss_probability),
                "q_5_raw": str(decision.q_5_raw),
                "q_15_raw": str(decision.q_15_raw),
                "q_5_calibrated": decimal_text(decision.q_5_calibrated),
                "q_15_calibrated": decimal_text(decision.q_15_calibrated),
                "reason_codes": list(decision.reason_codes),
                "qualification": decision.qualification,
            },
            "execution": execution_document,
            "settlement": settlement_document,
            "reason_codes": list(self.reason_codes),
            "paper_only": True,
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
        }


def _contract_for_token(
    pair: SameExpiryPair, token_id: str
) -> TwapMarketContract:
    for contract in (pair.market_5, pair.market_15):
        if token_id in {contract.up_token_id, contract.down_token_id}:
            return contract
    raise ValueError(f"token is not in same-expiry pair: {token_id}")


def _observable_snapshot(
    observation: CausalBookObservation,
) -> OrderBookSnapshot:
    """Timestamp a captured book when it first became causally observable."""

    snapshot = observation.snapshot
    return OrderBookSnapshot.from_tuples(
        snapshot.token_id,
        bids=tuple((level.price, level.size) for level in snapshot.bids),
        asks=tuple((level.price, level.size) for level in snapshot.asks),
        timestamp_ms=max(observation.source_at_ms, observation.received_at_ms),
        tick_size=snapshot.tick_size,
        minimum_order_size=snapshot.minimum_order_size,
    )


def evaluate_shadow_paper_cycle(
    *,
    pair: SameExpiryPair,
    settlement_state: PairSettlementState,
    distribution: JointDistribution,
    replay: CausalBookReplay,
    health: DataHealth,
    config: StrategyConfig,
    market_5_up: bool | None,
    market_15_up: bool | None,
    initial_cash: Decimal,
    max_leg_delay_ms: int,
) -> PairPaperCycleEvaluation:
    """Run the uncalibrated development track on causal executable books."""

    required_tokens = (
        pair.market_5.up_token_id,
        pair.market_5.down_token_id,
        pair.market_15.up_token_id,
        pair.market_15.down_token_id,
    )
    signal = replay.signal_books(
        token_ids=required_tokens,
        decision_at_ms=health.decision_at_ms,
        maximum_age_ms=config.maximum_book_staleness_ms,
    )
    decision = decide_pair_trade_shadow(
        pair=pair,
        settlement_state=settlement_state,
        distribution=distribution,
        books={token_id: item.snapshot for token_id, item in signal.items()},
        health=health,
        config=config,
    )
    if decision.action is PairAction.NO_TRADE:
        return PairPaperCycleEvaluation(
            decision=decision,
            execution=None,
            settlement=None,
            reason_codes=(),
        )
    assert decision.first_token_id is not None
    assert decision.second_token_id is not None
    first_contract = _contract_for_token(pair, decision.first_token_id)
    second_contract = _contract_for_token(pair, decision.second_token_id)
    first = replay.first_executable_book(
        token_id=decision.first_token_id,
        not_before_ms=health.decision_at_ms + first_contract.taker_delay_ms,
        maximum_wait_ms=config.maximum_book_staleness_ms,
    )
    if first is None:
        return PairPaperCycleEvaluation(
            decision=decision,
            execution=None,
            settlement=None,
            reason_codes=("first_leg_execution_book_missing",),
        )
    first_observable_ms = max(first.source_at_ms, first.received_at_ms)
    second_deadline_ms = first_observable_ms + max_leg_delay_ms
    second_not_before = first_observable_ms + second_contract.taker_delay_ms
    second = (
        None
        if second_not_before > second_deadline_ms
        else replay.first_executable_book(
            token_id=decision.second_token_id,
            not_before_ms=second_not_before,
            maximum_wait_ms=second_deadline_ms - second_not_before,
        )
    )
    if second is None:
        # The first leg has already filled in this replay.  Once the frozen
        # leg timer expires, that economic exposure cannot be erased merely
        # because no timely second-leg book arrived.  Use the first strictly
        # post-timeout second-token observation only as an auditable failure
        # marker; execute_pair_paper will kill the late leg and charge the
        # subsequent first-leg unwind from the timeout onward.
        second = replay.first_executable_book(
            token_id=decision.second_token_id,
            not_before_ms=second_deadline_ms + 1,
            maximum_wait_ms=config.maximum_book_staleness_ms,
        )
        if second is None:
            return PairPaperCycleEvaluation(
                decision=decision,
                execution=None,
                settlement=None,
                reason_codes=("second_leg_timeout_surface_missing",),
            )
    second_is_timely = max(
        second.source_at_ms, second.received_at_ms
    ) <= second_deadline_ms
    hedge_result_observable_ms = (
        max(second.source_at_ms, second.received_at_ms)
        if second_is_timely
        else second_deadline_ms
    )
    unwind_not_before = (
        hedge_result_observable_ms + first_contract.taker_delay_ms
    )
    unwind = replay.first_executable_book(
        token_id=decision.first_token_id,
        not_before_ms=unwind_not_before,
        maximum_wait_ms=config.maximum_book_staleness_ms,
    )
    execution = execute_pair_paper(
        decision=decision,
        pair=pair,
        first_book=_observable_snapshot(first),
        second_book=_observable_snapshot(second),
        unwind_book=_observable_snapshot(unwind or first),
        first_source_event_id=first.source_event_id,
        second_source_event_id=second.source_event_id,
        unwind_source_event_id=(unwind or first).source_event_id,
        initial_cash=initial_cash,
        max_leg_delay_ms=max_leg_delay_ms,
        max_book_age_ms=config.maximum_book_staleness_ms,
    )
    settlement = None
    if market_5_up is not None and market_15_up is not None:
        settlement = settle_pair_paper_execution(
            decision=decision,
            pair=pair,
            execution=execution,
            market_5_up=market_5_up,
            market_15_up=market_15_up,
        )
    reasons: tuple[str, ...] = ()
    if execution.status is PairExecutionStatus.NO_FILL:
        reasons = ("first_leg_unfilled",)
    elif execution.status is PairExecutionStatus.FAILED_UNHEDGED:
        reasons = ("failed_unhedged",)
    elif settlement is None:
        reasons = ("settlement_missing",)
    return PairPaperCycleEvaluation(
        decision=decision,
        execution=execution,
        settlement=settlement,
        reason_codes=reasons,
    )
