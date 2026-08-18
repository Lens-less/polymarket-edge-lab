"""Causal L2 reconstruction for BTC 5m/15m paper execution.

The recorder stores compact full-book anchors plus target-only price changes.
This module rebuilds only states whose top of book can be reconciled with the
exchange-provided best bid/ask.  An uncertain state is omitted until the next
full anchor, so downstream paper fills fail closed instead of inventing depth.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
    decide_pair_trade,
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


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


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
    return int(parsed.astimezone(UTC).timestamp() * 1_000)


def _source_ms(payload: Mapping[str, Any]) -> int | None:
    raw_value = payload.get("timestamp")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


_DUST_RESIDUAL_MAX_LOSS_USDC = Decimal("0.01")
_SUBSTANTIAL_SECOND_LEG_FILL_RATIO = Decimal("0.999")


def _fill_events(
    execution: PairPaperExecution,
) -> tuple[tuple[int, str, Decimal], ...]:
    events: list[tuple[int, str, Decimal]] = []
    for fill in execution.first_leg.fills:
        events.append((fill.timestamp_ms, "first", fill.quantity))
    for fill in execution.second_leg.fills:
        events.append((fill.timestamp_ms, "second", fill.quantity))
    if execution.unwind_leg is not None:
        for fill in execution.unwind_leg.fills:
            events.append((fill.timestamp_ms, "unwind", fill.quantity))
    return tuple(sorted(events, key=lambda item: (item[0], item[1])))


def _group_fill_events_by_timestamp(
    execution: PairPaperExecution,
) -> tuple[tuple[int, Decimal, Decimal, Decimal], ...]:
    grouped: dict[int, list[Decimal]] = {}
    for timestamp_ms, leg, quantity in _fill_events(execution):
        totals = grouped.setdefault(
            timestamp_ms,
            [Decimal(0), Decimal(0), Decimal(0)],
        )
        if leg == "first":
            totals[0] += quantity
        elif leg == "second":
            totals[1] += quantity
        else:
            totals[2] += quantity
    return tuple(
        (timestamp_ms, totals[0], totals[1], totals[2])
        for timestamp_ms, totals in sorted(grouped.items())
    )


def _terminal_residual_classification(
    *,
    residual_unhedged_max_loss_usdc: Decimal,
    second_leg_fill_ratio: Decimal | None,
) -> str:
    if residual_unhedged_max_loss_usdc <= 0:
        return "none"
    if second_leg_fill_ratio is None:
        return "unknown"
    if (
        residual_unhedged_max_loss_usdc <= _DUST_RESIDUAL_MAX_LOSS_USDC
        and second_leg_fill_ratio >= _SUBSTANTIAL_SECOND_LEG_FILL_RATIO
    ):
        return "dust"
    return "material"


def paper_execution_diagnostics(
    execution: PairPaperExecution,
    *,
    expiry_ms: int,
) -> dict[str, Any]:
    if isinstance(expiry_ms, bool) or not isinstance(expiry_ms, int) or expiry_ms < 0:
        raise ValueError("expiry_ms must be a non-negative integer")
    if not isinstance(execution, PairPaperExecution):
        raise TypeError("execution must be PairPaperExecution")
    first_filled = execution.first_leg.filled_quantity
    second_filled = execution.second_leg.filled_quantity
    unwind_filled = (
        Decimal(0)
        if execution.unwind_leg is None
        else execution.unwind_leg.filled_quantity
    )
    second_leg_fill_ratio = None if first_filled <= 0 else second_filled / first_filled
    initial_second_leg_shortfall_quantity = max(
        Decimal(0), first_filled - second_filled
    )
    residual_unhedged_quantity = execution.unhedged_quantity
    residual_unhedged_max_loss_usdc = residual_unhedged_quantity
    terminal_residual_classification = _terminal_residual_classification(
        residual_unhedged_max_loss_usdc=residual_unhedged_max_loss_usdc,
        second_leg_fill_ratio=second_leg_fill_ratio,
    )
    dust_failed_unhedged = (
        execution.status is PairExecutionStatus.FAILED_UNHEDGED
        and terminal_residual_classification == "dust"
    )
    material_failed_unhedged = (
        execution.status is PairExecutionStatus.FAILED_UNHEDGED
        and terminal_residual_classification == "material"
    )
    material_second_leg_failure = initial_second_leg_shortfall_quantity > 0 and (
        second_leg_fill_ratio is None
        or second_leg_fill_ratio < _SUBSTANTIAL_SECOND_LEG_FILL_RATIO
    )
    first_running = Decimal(0)
    second_running = Decimal(0)
    unwind_running = Decimal(0)
    active_since_ms: int | None = None
    transient_unhedged_duration_ms = 0
    peak_unhedged_quantity = Decimal(0)
    last_timestamp_ms: int | None = None
    event_groups = _group_fill_events_by_timestamp(execution)
    for timestamp_ms, first_delta, second_delta, unwind_delta in event_groups:
        if (
            active_since_ms is not None
            and last_timestamp_ms is not None
            and timestamp_ms > last_timestamp_ms
        ):
            transient_unhedged_duration_ms += timestamp_ms - last_timestamp_ms
        first_running += first_delta
        second_running += second_delta
        unwind_running += unwind_delta
        unhedged_quantity = max(
            Decimal(0),
            first_running - second_running - unwind_running,
        )
        if unhedged_quantity > 0 and active_since_ms is None:
            active_since_ms = timestamp_ms
        if unhedged_quantity <= 0 and active_since_ms is not None:
            active_since_ms = None
        peak_unhedged_quantity = max(peak_unhedged_quantity, unhedged_quantity)
        last_timestamp_ms = timestamp_ms
    if (
        active_since_ms is not None
        and last_timestamp_ms is not None
        and residual_unhedged_quantity > 0
        and expiry_ms > last_timestamp_ms
    ):
        transient_unhedged_duration_ms += expiry_ms - last_timestamp_ms
    economic_attempt = bool(event_groups) or execution.cashflow_after_execution != 0
    return {
        "schema_version": "btc-5m-15m-relative-value-execution-diagnostics.v1",
        "economic_attempt": economic_attempt,
        "second_leg_fill_ratio": _decimal_text(second_leg_fill_ratio),
        "first_leg_filled_quantity": _decimal_text(first_filled),
        "second_leg_filled_quantity": _decimal_text(second_filled),
        "initial_second_leg_shortfall_quantity": _decimal_text(
            initial_second_leg_shortfall_quantity
        ),
        "material_second_leg_failure": material_second_leg_failure,
        "unwind_filled_quantity": _decimal_text(unwind_filled),
        "residual_unhedged_quantity": _decimal_text(residual_unhedged_quantity),
        "residual_unhedged_max_loss_usdc": _decimal_text(
            residual_unhedged_max_loss_usdc
        ),
        "terminal_residual_classification": terminal_residual_classification,
        "dust_failed_unhedged": dust_failed_unhedged,
        "material_failed_unhedged": material_failed_unhedged,
        "transient_unhedged_exposure_peak_quantity": _decimal_text(
            peak_unhedged_quantity
        ),
        "transient_unhedged_exposure_peak_max_loss_usdc": _decimal_text(
            peak_unhedged_quantity
        ),
        "transient_unhedged_exposure_duration_ms": transient_unhedged_duration_ms,
        "residual_unhedged_usdc": _decimal_text(residual_unhedged_max_loss_usdc),
        "transient_naked_exposure_peak_quantity": _decimal_text(peak_unhedged_quantity),
        "transient_naked_exposure_peak_usdc": _decimal_text(peak_unhedged_quantity),
        "transient_naked_exposure_duration_ms": transient_unhedged_duration_ms,
    }


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
    full_depth_available: bool

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
        if not isinstance(self.full_depth_available, bool):
            raise TypeError("full_depth_available must be bool")

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
        if (
            price is None
            or size is None
            or not Decimal(0) <= price <= Decimal(1)
            or size <= 0
        ):
            return None
        result[price] = size
    return result


def _snapshot(
    token: BookReplayToken,
    state: _DepthState,
    *,
    source_at_ms: int,
) -> OrderBookSnapshot | None:
    if not state.bids and not state.asks:
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
    if len(expected_bid) != 1 or len(expected_ask) != 1:
        return False
    actual_bid = max(state.bids) if state.bids else None
    actual_ask = min(state.asks) if state.asks else None
    return actual_bid == next(iter(expected_bid)) and actual_ask == next(
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
    ) -> CausalBookReplay:
        frozen_tokens = dict(tokens)
        if not frozen_tokens or any(
            token_id != token.token_id for token_id, token in frozen_tokens.items()
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
            if event_type not in {
                "book",
                "price_change",
                "clob_snapshot",
                "resnapshot",
            } or not isinstance(payload, Mapping):
                continue
            raw_received_at_ms = _epoch_ms(record.get("received_at"))
            received_at_ms = (
                None
                if raw_received_at_ms is None
                else raw_received_at_ms + receipt_clock_offset_ms
            )
            source_at_ms = _source_ms(payload)
            source_event_id = str(record.get("record_id") or "")
            if received_at_ms is None or not source_event_id:
                continue
            if event_type in {"clob_snapshot", "resnapshot"}:
                responses = payload.get("responses")
                if not isinstance(responses, list):
                    continue
                for index, response in enumerate(responses):
                    if (
                        not isinstance(response, Mapping)
                        or response.get("resource") != "clob_book"
                    ):
                        continue
                    raw_book = response.get("raw_json")
                    if not isinstance(raw_book, Mapping):
                        continue
                    book_source_at_ms = _source_ms(raw_book)
                    if book_source_at_ms is None:
                        continue
                    annotated_book = dict(raw_book)
                    annotated_book["depth_policy"] = "full_depth_http_snapshot"
                    ordered.append(
                        (
                            received_at_ms,
                            book_source_at_ms,
                            f"{source_event_id}:clob_book:{index}",
                            "book",
                            annotated_book,
                        )
                    )
                continue
            if source_at_ms is None:
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
        full_depth_by_token: dict[str, bool] = {}
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
                if bids is None or asks is None or (not bids and not asks):
                    states.pop(token_id, None)
                    full_depth_by_token.pop(token_id, None)
                    invalid.add(token_id)
                    continue
                state = _DepthState(bids=bids, asks=asks)
                snapshot = _snapshot(
                    frozen_tokens[token_id], state, source_at_ms=source_at_ms
                )
                if snapshot is None:
                    states.pop(token_id, None)
                    full_depth_by_token.pop(token_id, None)
                    invalid.add(token_id)
                    continue
                states[token_id] = state
                depth_policy = payload.get("depth_policy")
                full_depth_by_token[token_id] = not (
                    isinstance(depth_policy, str) and depth_policy.startswith("top_5")
                )
                invalid.discard(token_id)
                observations[token_id].append(
                    CausalBookObservation(
                        snapshot=snapshot,
                        received_at_ms=received_at_ms,
                        source_event_id=event_id,
                        update_kind="book",
                        full_depth_available=full_depth_by_token[token_id],
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
                current_state = states.get(token_id)
                if current_state is None or token_id in invalid:
                    continue
                coherent = True
                for change in changes:
                    price = _decimal(change.get("price"))
                    size = _decimal(change.get("size"))
                    side = change.get("side")
                    if (
                        price is None
                        or size is None
                        or not Decimal(0) <= price <= Decimal(1)
                        or size < 0
                        or side not in {"BUY", "SELL"}
                    ):
                        coherent = False
                        break
                    levels = current_state.bids if side == "BUY" else current_state.asks
                    if size == 0:
                        levels.pop(price, None)
                    else:
                        levels[price] = size
                if not coherent or not _top_matches(current_state, changes):
                    invalid.add(token_id)
                    full_depth_by_token.pop(token_id, None)
                    continue
                snapshot = _snapshot(
                    frozen_tokens[token_id], current_state, source_at_ms=source_at_ms
                )
                if snapshot is None:
                    invalid.add(token_id)
                    full_depth_by_token.pop(token_id, None)
                    continue
                observations[token_id].append(
                    CausalBookObservation(
                        snapshot=snapshot,
                        received_at_ms=received_at_ms,
                        source_event_id=event_id,
                        update_kind="price_change",
                        full_depth_available=full_depth_by_token.get(token_id, False),
                    )
                )

        return cls(
            observations_by_token=MappingProxyType(
                {token_id: tuple(values) for token_id, values in observations.items()}
            )
        )

    def signal_books(
        self,
        *,
        token_ids: Sequence[str],
        decision_at_ms: int,
        maximum_age_ms: int,
        require_full_depth: bool = False,
    ) -> Mapping[str, CausalBookObservation]:
        if maximum_age_ms < 0:
            raise ValueError("maximum_age_ms cannot be negative")
        if not isinstance(require_full_depth, bool):
            raise TypeError("require_full_depth must be bool")
        result: dict[str, CausalBookObservation] = {}
        for token_id in token_ids:
            eligible = [
                item
                for item in self.observations_by_token.get(token_id, ())
                if item.source_at_ms <= decision_at_ms
                and item.received_at_ms <= decision_at_ms
                and decision_at_ms - item.source_at_ms <= maximum_age_ms
                and decision_at_ms - item.received_at_ms <= maximum_age_ms
                and (item.full_depth_available or not require_full_depth)
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
            if not_before_ms <= max(item.source_at_ms, item.received_at_ms) <= deadline
        ]
        return eligible[0] if eligible else None


@dataclass(frozen=True)
class PairPaperCycleEvaluation:
    """One raw-model development cycle from decision through settlement."""

    decision: PairDecision
    execution: PairPaperExecution | None
    settlement: PairPaperSettlement | None
    reason_codes: tuple[str, ...]
    expiry_ms: int | None = None

    def to_document(self) -> dict[str, Any]:
        decision = self.decision

        def decimal_text(value: Decimal | None) -> str | None:
            return _decimal_text(value)

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
                "diagnostics": (
                    None
                    if self.expiry_ms is None
                    else paper_execution_diagnostics(
                        self.execution,
                        expiry_ms=self.expiry_ms,
                    )
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


def _contract_for_token(pair: SameExpiryPair, token_id: str) -> TwapMarketContract:
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

    return _evaluate_paper_cycle(
        pair=pair,
        settlement_state=settlement_state,
        distribution=distribution,
        replay=replay,
        health=health,
        config=config,
        market_5_up=market_5_up,
        market_15_up=market_15_up,
        initial_cash=initial_cash,
        max_leg_delay_ms=max_leg_delay_ms,
        decision_builder=decide_pair_trade_shadow,
    )


def evaluate_qualified_paper_cycle(
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
    """Run the calibrated qualified track on causal executable books."""

    return _evaluate_paper_cycle(
        pair=pair,
        settlement_state=settlement_state,
        distribution=distribution,
        replay=replay,
        health=health,
        config=config,
        market_5_up=market_5_up,
        market_15_up=market_15_up,
        initial_cash=initial_cash,
        max_leg_delay_ms=max_leg_delay_ms,
        decision_builder=decide_pair_trade,
    )


def _evaluate_paper_cycle(
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
    decision_builder: Any,
) -> PairPaperCycleEvaluation:
    """Run one decision track on causal executable books."""

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
    decision = decision_builder(
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
            expiry_ms=pair.expires_at_ms,
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
            expiry_ms=pair.expires_at_ms,
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
                expiry_ms=pair.expires_at_ms,
            )
    second_is_timely = (
        max(second.source_at_ms, second.received_at_ms) <= second_deadline_ms
    )
    hedge_result_observable_ms = (
        max(second.source_at_ms, second.received_at_ms)
        if second_is_timely
        else second_deadline_ms
    )
    unwind_not_before = hedge_result_observable_ms + first_contract.taker_delay_ms
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
        expiry_ms=pair.expires_at_ms,
    )
