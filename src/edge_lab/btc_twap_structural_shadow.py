"""Paper-only double-maker replay for shared-terminal structural pairs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any

from .btc_twap_relative_value import TwapMarketContract
from .execution import (
    BookEvent,
    DepthBook,
    ExecutionStatus,
    Ledger,
    MakerOrderRequest,
    MakerReplay,
    OrderSide,
    QueueScenario,
    TimeInForce,
    TraceRef,
    TradeEvent,
    execute_taker,
)

ZERO = Decimal(0)
ONE = Decimal(1)
MAXIMUM_SINGLE_EXPIRY_CONCENTRATION = Decimal("0.20")
STRUCTURAL_DIRECTIONS = frozenset(
    {"five_above_fifteen", "five_below_fifteen", "equal_strikes"}
)


def _decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name} must be an exact decimal")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


class SingleLegDisposition(str, Enum):
    """Pre-registered response when only one double-maker leg fills."""

    PASSIVE_WAIT = "passive_wait"
    TAKER_HEDGE = "taker_hedge"
    FAK_UNWIND = "fak_unwind"


@dataclass(frozen=True)
class StructuralMakerLeg:
    contract: TwapMarketContract
    token_id: str
    price: Decimal
    quantity: Decimal
    visible_queue_ahead: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.contract, TwapMarketContract):
            raise TypeError("contract must be a TwapMarketContract")
        if self.token_id not in {
            self.contract.up_token_id,
            self.contract.down_token_id,
        }:
            raise ValueError("maker token does not belong to its contract")
        price = _decimal(self.price, name="maker price")
        quantity = _decimal(self.quantity, name="maker quantity")
        queue = _decimal(self.visible_queue_ahead, name="visible_queue_ahead")
        if not ZERO < price < ONE:
            raise ValueError("maker price must be strictly between zero and one")
        if price % self.contract.tick_size != ZERO:
            raise ValueError("maker price must align to the captured tick")
        if quantity < self.contract.minimum_order_size:
            raise ValueError("maker quantity is below the captured minimum")
        if queue < ZERO:
            raise ValueError("visible_queue_ahead cannot be negative")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "visible_queue_ahead", queue)


@dataclass(frozen=True)
class StructuralDoubleMakerPlan:
    attempt_id: str
    expiry_ms: int
    direction: str
    first: StructuralMakerLeg
    second: StructuralMakerLeg
    submitted_at_ms: int
    book_timestamp_ms: int
    terminal_payouts: Mapping[str, Decimal]
    initial_cash: Decimal
    max_book_age_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ValueError("attempt_id must be non-empty")
        if self.direction not in STRUCTURAL_DIRECTIONS:
            raise ValueError("unsupported structural direction")
        if self.first.token_id == self.second.token_id:
            raise ValueError("double-maker legs must use distinct tokens")
        if self.first.contract.market_id == self.second.contract.market_id:
            raise ValueError("double-maker legs must use distinct markets")
        if self.first.contract.closes_at_ms != self.second.contract.closes_at_ms:
            raise ValueError("double-maker legs must share one terminal timestamp")
        if self.expiry_ms != self.first.contract.closes_at_ms:
            raise ValueError("expiry_ms must match both contracts")
        if self.first.quantity != self.second.quantity:
            raise ValueError("double-maker legs must quote one common quantity")
        leg_pair = frozenset(
            (
                (
                    self.first.contract.horizon,
                    "up"
                    if self.first.token_id == self.first.contract.up_token_id
                    else "down",
                ),
                (
                    self.second.contract.horizon,
                    "up"
                    if self.second.token_id == self.second.contract.up_token_id
                    else "down",
                ),
            )
        )
        allowed_pairs = {
            "five_above_fifteen": {
                frozenset({("15m", "up"), ("5m", "down")})
            },
            "five_below_fifteen": {
                frozenset({("5m", "up"), ("15m", "down")})
            },
            "equal_strikes": {
                frozenset({("15m", "up"), ("5m", "down")}),
                frozenset({("5m", "up"), ("15m", "down")}),
            },
        }
        if leg_pair not in allowed_pairs[self.direction]:
            raise ValueError("structural direction conflicts with selected legs")
        for name in ("expiry_ms", "submitted_at_ms", "book_timestamp_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not self.book_timestamp_ms <= self.submitted_at_ms < self.expiry_ms:
            raise ValueError("book/submit/expiry timestamps are inconsistent")
        if (
            isinstance(self.max_book_age_ms, bool)
            or not isinstance(self.max_book_age_ms, int)
            or self.max_book_age_ms < 0
        ):
            raise ValueError("max_book_age_ms must be non-negative")
        initial_cash = _decimal(self.initial_cash, name="initial_cash")
        if initial_cash <= ZERO:
            raise ValueError("initial_cash must be positive")
        object.__setattr__(self, "initial_cash", initial_cash)

        payouts = {
            str(token_id): _decimal(value, name=f"payout[{token_id}]")
            for token_id, value in self.terminal_payouts.items()
        }
        expected_tokens = {
            self.first.contract.up_token_id,
            self.first.contract.down_token_id,
            self.second.contract.up_token_id,
            self.second.contract.down_token_id,
        }
        if set(payouts) != expected_tokens:
            raise ValueError("terminal payouts must cover both binary markets")
        for contract in (self.first.contract, self.second.contract):
            if (
                payouts[contract.up_token_id] not in {ZERO, ONE}
                or payouts[contract.down_token_id] not in {ZERO, ONE}
                or payouts[contract.up_token_id]
                + payouts[contract.down_token_id]
                != ONE
            ):
                raise ValueError("each binary market must have one terminal winner")
        if payouts[self.first.token_id] + payouts[self.second.token_id] < ONE:
            raise ValueError("selected legs do not form a structural floor pair")
        object.__setattr__(self, "terminal_payouts", MappingProxyType(payouts))


@dataclass(frozen=True)
class StructuralShadowResult:
    attempt_id: str
    expiry_ms: int
    direction: str
    net_pnl: Decimal
    scenario: QueueScenario = QueueScenario.NEUTRAL
    disposition: SingleLegDisposition = SingleLegDisposition.PASSIVE_WAIT
    first_maker_quantity: Decimal = ZERO
    second_maker_quantity: Decimal = ZERO
    paired_quantity: Decimal = ZERO
    unpaired_quantity: Decimal = ZERO
    taker_hedge_quantity: Decimal = ZERO
    fak_unwind_quantity: Decimal = ZERO
    fees_paid: Decimal = ZERO
    terminal_equity: Decimal = ZERO
    action_status: str = "not_required"
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ValueError("attempt_id must be non-empty")
        if (
            isinstance(self.expiry_ms, bool)
            or not isinstance(self.expiry_ms, int)
            or self.expiry_ms < 0
        ):
            raise ValueError("expiry_ms must be a non-negative integer")
        if self.direction not in STRUCTURAL_DIRECTIONS:
            raise ValueError("unsupported structural direction")
        object.__setattr__(
            self,
            "scenario",
            self.scenario
            if isinstance(self.scenario, QueueScenario)
            else QueueScenario(self.scenario),
        )
        object.__setattr__(
            self,
            "disposition",
            self.disposition
            if isinstance(self.disposition, SingleLegDisposition)
            else SingleLegDisposition(self.disposition),
        )
        object.__setattr__(self, "net_pnl", _decimal(self.net_pnl, name="net_pnl"))
        for name in (
            "first_maker_quantity",
            "second_maker_quantity",
            "paired_quantity",
            "unpaired_quantity",
            "taker_hedge_quantity",
            "fak_unwind_quantity",
            "fees_paid",
            "terminal_equity",
        ):
            value = _decimal(getattr(self, name), name=name)
            if value < ZERO:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)
        if not isinstance(self.action_status, str) or not self.action_status:
            raise ValueError("action_status must be a non-empty string")
        if any(not isinstance(reason, str) or not reason for reason in self.reason_codes):
            raise ValueError("reason_codes must contain non-empty strings")

    @classmethod
    def for_robustness(
        cls,
        *,
        attempt_id: str,
        expiry_ms: int,
        direction: str,
        net_pnl: Decimal,
    ) -> "StructuralShadowResult":
        return cls(
            attempt_id=attempt_id,
            expiry_ms=expiry_ms,
            direction=direction,
            net_pnl=_decimal(net_pnl, name="net_pnl"),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "expiry_ms": self.expiry_ms,
            "direction": self.direction,
            "scenario": self.scenario.value,
            "disposition": self.disposition.value,
            "first_maker_quantity": str(self.first_maker_quantity),
            "second_maker_quantity": str(self.second_maker_quantity),
            "paired_quantity": str(self.paired_quantity),
            "unpaired_quantity": str(self.unpaired_quantity),
            "taker_hedge_quantity": str(self.taker_hedge_quantity),
            "fak_unwind_quantity": str(self.fak_unwind_quantity),
            "fees_paid": str(self.fees_paid),
            "terminal_equity": str(self.terminal_equity),
            "net_pnl": str(self.net_pnl),
            "action_status": self.action_status,
            "reason_codes": list(self.reason_codes),
        }


def _register_contract(ledger: Ledger, contract: TwapMarketContract) -> None:
    ledger.register_binary_market(
        contract.market_id,
        yes_token_id=contract.up_token_id,
        no_token_id=contract.down_token_id,
    )


def _maker_request(
    plan: StructuralDoubleMakerPlan,
    leg: StructuralMakerLeg,
    *,
    suffix: str,
) -> MakerOrderRequest:
    return MakerOrderRequest(
        order_id=f"{plan.attempt_id}:{suffix}",
        market_id=leg.contract.market_id,
        token_id=leg.token_id,
        side=OrderSide.BUY,
        price=leg.price,
        quantity=leg.quantity,
        tick_size=leg.contract.tick_size,
        fee_schedule=leg.contract.fee_schedule,
        capacity=leg.quantity,
        max_book_age_ms=plan.max_book_age_ms,
    )


def _copy_depth_book(book: DepthBook) -> DepthBook:
    """Keep each scenario replay independent of shared captured evidence."""

    return DepthBook.from_tuples(
        book.token_id,
        bids=((level.price, level.size) for level in book.bids),
        asks=((level.price, level.size) for level in book.asks),
        tick_size=book.tick_size,
        min_order_size=book.min_order_size,
        timestamp_ms=book.timestamp_ms,
    )


def replay_structural_double_maker(
    plan: StructuralDoubleMakerPlan,
    *,
    public_events: Sequence[BookEvent | TradeEvent],
    scenario: QueueScenario = QueueScenario.NEUTRAL,
    disposition: SingleLegDisposition = SingleLegDisposition.PASSIVE_WAIT,
    hedge_books: Mapping[str, DepthBook] | None = None,
    unwind_books: Mapping[str, DepthBook] | None = None,
) -> StructuralShadowResult:
    """Replay both resting buys and account one pre-registered imbalance action."""

    if not isinstance(plan, StructuralDoubleMakerPlan):
        raise TypeError("plan must be a StructuralDoubleMakerPlan")
    active_scenario = (
        scenario if isinstance(scenario, QueueScenario) else QueueScenario(scenario)
    )
    active_disposition = (
        disposition
        if isinstance(disposition, SingleLegDisposition)
        else SingleLegDisposition(disposition)
    )
    ledger = Ledger(plan.initial_cash)
    _register_contract(ledger, plan.first.contract)
    _register_contract(ledger, plan.second.contract)
    replay = MakerReplay(ledger, scenario=active_scenario)
    first_order = replay.submit(
        _maker_request(plan, plan.first, suffix="first"),
        visible_queue_ahead=plan.first.visible_queue_ahead,
        now_ms=plan.submitted_at_ms,
        book_timestamp_ms=plan.book_timestamp_ms,
        trace=TraceRef(
            source_event_id=f"{plan.attempt_id}:submit:first",
            decision_id=plan.attempt_id,
            timestamp_ms=plan.submitted_at_ms,
        ),
    )
    second_order = replay.submit(
        _maker_request(plan, plan.second, suffix="second"),
        visible_queue_ahead=plan.second.visible_queue_ahead,
        now_ms=plan.submitted_at_ms,
        book_timestamp_ms=plan.book_timestamp_ms,
        trace=TraceRef(
            source_event_id=f"{plan.attempt_id}:submit:second",
            decision_id=plan.attempt_id,
            timestamp_ms=plan.submitted_at_ms,
        ),
    )

    taker_hedge_quantity = ZERO
    fak_unwind_quantity = ZERO
    action_status = "not_required"
    reason_codes: list[str] = []
    imbalance_action_attempted = False
    ordered_events = sorted(
        enumerate(public_events),
        key=lambda item: (item[1].timestamp_ms, item[0]),
    )
    last_timestamp_ms = plan.submitted_at_ms
    for _index, event in ordered_events:
        if event.timestamp_ms > plan.expiry_ms:
            raise ValueError("public event occurs after common expiry")
        last_timestamp_ms = max(last_timestamp_ms, event.timestamp_ms)
        if isinstance(event, BookEvent):
            replay.on_book(event)
            continue
        fills = replay.on_trade(event)
        if (
            not fills
            or active_disposition is SingleLegDisposition.PASSIVE_WAIT
            or imbalance_action_attempted
        ):
            continue
        first_position = ledger.position(plan.first.token_id)
        second_position = ledger.position(plan.second.token_id)
        if first_position == second_position:
            continue
        imbalance_action_attempted = True
        for order in (first_order, second_order):
            replay.request_cancel(
                order.order_id,
                now_ms=event.timestamp_ms,
                trace=TraceRef(
                    source_event_id=(
                        f"{plan.attempt_id}:cancel:{order.order_id}"
                    ),
                    decision_id=plan.attempt_id,
                    timestamp_ms=event.timestamp_ms,
                ),
            )
        if first_position > second_position:
            excess_leg = plan.first
            missing_leg = plan.second
            imbalance = first_position - second_position
        else:
            excess_leg = plan.second
            missing_leg = plan.first
            imbalance = second_position - first_position
        if active_disposition is SingleLegDisposition.TAKER_HEDGE:
            captured_book = (hedge_books or {}).get(missing_leg.token_id)
            if captured_book is None or not captured_book.asks:
                action_status = "killed"
                reason_codes.append("missing_taker_hedge_book")
                continue
            book = _copy_depth_book(captured_book)
            result = execute_taker(
                ledger,
                book,
                side=OrderSide.BUY,
                quantity=imbalance,
                limit_price=book.asks[-1].price,
                time_in_force=TimeInForce.FOK,
                fee_schedule=missing_leg.contract.fee_schedule,
                trace=TraceRef(
                    source_event_id=f"{plan.attempt_id}:taker-hedge",
                    decision_id=plan.attempt_id,
                    timestamp_ms=event.timestamp_ms,
                ),
                execution_latency_ms=missing_leg.contract.taker_delay_ms,
                max_book_age_ms=plan.max_book_age_ms,
            )
            taker_hedge_quantity += result.filled_quantity
        else:
            captured_book = (unwind_books or {}).get(excess_leg.token_id)
            if captured_book is None or not captured_book.bids:
                action_status = "killed"
                reason_codes.append("missing_fak_unwind_book")
                continue
            book = _copy_depth_book(captured_book)
            result = execute_taker(
                ledger,
                book,
                side=OrderSide.SELL,
                quantity=imbalance,
                limit_price=book.bids[-1].price,
                time_in_force=TimeInForce.FAK,
                fee_schedule=excess_leg.contract.fee_schedule,
                trace=TraceRef(
                    source_event_id=f"{plan.attempt_id}:fak-unwind",
                    decision_id=plan.attempt_id,
                    timestamp_ms=event.timestamp_ms,
                ),
                execution_latency_ms=excess_leg.contract.taker_delay_ms,
                max_book_age_ms=plan.max_book_age_ms,
            )
            fak_unwind_quantity += result.filled_quantity
        if result.status is ExecutionStatus.FILLED:
            action_status = "filled"
        elif result.status is ExecutionStatus.PARTIAL:
            action_status = "partial"
            reason_codes.append(result.reason or "partial_imbalance_action")
        else:
            action_status = "killed"
            reason_codes.append(result.reason or "imbalance_action_killed")

    for order in (first_order, second_order):
        replay.request_cancel(
            order.order_id,
            now_ms=last_timestamp_ms,
            trace=TraceRef(
                source_event_id=f"{plan.attempt_id}:final-cancel:{order.order_id}",
                decision_id=plan.attempt_id,
                timestamp_ms=last_timestamp_ms,
            ),
        )

    first_position = ledger.position(plan.first.token_id)
    second_position = ledger.position(plan.second.token_id)
    paired_quantity = min(first_position, second_position)
    unpaired_quantity = abs(first_position - second_position)
    terminal_equity = ledger.equity(plan.terminal_payouts)
    return StructuralShadowResult(
        attempt_id=plan.attempt_id,
        expiry_ms=plan.expiry_ms,
        direction=plan.direction,
        scenario=active_scenario,
        disposition=active_disposition,
        first_maker_quantity=first_order.filled_quantity,
        second_maker_quantity=second_order.filled_quantity,
        paired_quantity=paired_quantity,
        unpaired_quantity=unpaired_quantity,
        taker_hedge_quantity=taker_hedge_quantity,
        fak_unwind_quantity=fak_unwind_quantity,
        fees_paid=ledger.fees_paid,
        terminal_equity=terminal_equity,
        net_pnl=terminal_equity - plan.initial_cash,
        action_status=action_status,
        reason_codes=tuple(reason_codes),
    )


@dataclass(frozen=True)
class StructuralShadowRobustness:
    expiry_count: int
    total_net_pnl: Decimal
    without_best_expiry_net_pnl: Decimal
    without_best_direction_net_pnl: Decimal
    minimum_rolling_window_net_pnl: Decimal
    max_single_expiry_concentration: Decimal | None
    passed: bool
    reason_codes: tuple[str, ...]

    def to_document(self) -> dict[str, object]:
        return {
            "expiry_count": self.expiry_count,
            "total_net_pnl": str(self.total_net_pnl),
            "without_best_expiry_net_pnl": str(
                self.without_best_expiry_net_pnl
            ),
            "without_best_direction_net_pnl": str(
                self.without_best_direction_net_pnl
            ),
            "minimum_rolling_window_net_pnl": str(
                self.minimum_rolling_window_net_pnl
            ),
            "max_single_expiry_concentration": (
                None
                if self.max_single_expiry_concentration is None
                else str(self.max_single_expiry_concentration)
            ),
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
        }


def evaluate_shadow_robustness(
    results: Sequence[StructuralShadowResult],
    *,
    rolling_window_size: int,
    minimum_expiries: int = 200,
) -> StructuralShadowRobustness:
    if (
        isinstance(rolling_window_size, bool)
        or not isinstance(rolling_window_size, int)
        or rolling_window_size <= 0
    ):
        raise ValueError("rolling_window_size must be positive")
    if (
        isinstance(minimum_expiries, bool)
        or not isinstance(minimum_expiries, int)
        or minimum_expiries <= 0
    ):
        raise ValueError("minimum_expiries must be positive")
    if any(not isinstance(item, StructuralShadowResult) for item in results):
        raise TypeError("results must contain StructuralShadowResult values")
    ordered = tuple(sorted(results, key=lambda item: (item.expiry_ms, item.attempt_id)))
    ids = [item.attempt_id for item in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("shadow results must use unique attempt ids")
    expiry_ids = [item.expiry_ms for item in ordered]
    if len(expiry_ids) != len(set(expiry_ids)):
        raise ValueError("shadow results must use unique common expiries")
    total = sum((item.net_pnl for item in ordered), ZERO)
    best_expiry = max((item.net_pnl for item in ordered), default=ZERO)
    without_best_expiry = total - best_expiry
    by_direction: dict[str, Decimal] = {}
    for item in ordered:
        by_direction[item.direction] = (
            by_direction.get(item.direction, ZERO) + item.net_pnl
        )
    best_direction = max(by_direction.values(), default=ZERO)
    without_best_direction = total - best_direction
    rolling = tuple(
        sum(
            (item.net_pnl for item in ordered[index : index + rolling_window_size]),
            ZERO,
        )
        for index in range(max(0, len(ordered) - rolling_window_size + 1))
    )
    minimum_rolling = min(rolling, default=ZERO)
    concentration = (
        None
        if total <= ZERO or not ordered
        else max(item.net_pnl for item in ordered) / total
    )
    reasons: list[str] = []
    if len(ordered) < minimum_expiries:
        reasons.append("fewer_than_minimum_expiries")
    if total <= ZERO:
        reasons.append("non_positive_total_net_pnl")
    if without_best_expiry <= ZERO:
        reasons.append("non_positive_without_best_expiry")
    if len(by_direction) < 2:
        reasons.append("fewer_than_two_directions")
    elif without_best_direction <= ZERO:
        reasons.append("non_positive_without_best_direction")
    if len(ordered) < rolling_window_size:
        reasons.append("insufficient_rolling_window_history")
    elif minimum_rolling <= ZERO:
        reasons.append("non_positive_rolling_window")
    if concentration is None:
        reasons.append("single_expiry_concentration_unavailable")
    elif concentration > MAXIMUM_SINGLE_EXPIRY_CONCENTRATION:
        reasons.append("single_expiry_concentration_above_20pct")
    return StructuralShadowRobustness(
        expiry_count=len(ordered),
        total_net_pnl=total,
        without_best_expiry_net_pnl=without_best_expiry,
        without_best_direction_net_pnl=without_best_direction,
        minimum_rolling_window_net_pnl=minimum_rolling,
        max_single_expiry_concentration=concentration,
        passed=not reasons,
        reason_codes=tuple(reasons),
    )


__all__ = [
    "SingleLegDisposition",
    "StructuralDoubleMakerPlan",
    "StructuralMakerLeg",
    "StructuralShadowResult",
    "StructuralShadowRobustness",
    "evaluate_shadow_robustness",
    "replay_structural_double_maker",
]
