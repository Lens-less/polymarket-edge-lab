"""Paper-only double-maker replay for shared-terminal structural pairs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from .btc_twap_relative_value import (
    OrderBookSnapshot,
    PairAction,
    PairSettlementState,
    SameExpiryPair,
    TwapMarketContract,
)
from .btc_twap_relative_value_readiness import (
    MINIMUM_STRUCTURAL_SHADOW_EXPIRIES,
    NeutralShadowEvidence,
)
from .btc_twap_relative_value_v07 import (
    SharedTerminalPayoffStructure,
    StrikeOrdering,
)
from .data_store import canonical_json_bytes
from .execution import (
    BASE_UNIT,
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
REGISTERED_ROLLING_WINDOW_EXPIRIES = 20
STRUCTURAL_SHADOW_REPORT_SCHEMA = "btc_twap_structural_shadow_report.v1"
ROLLING_SHADOW_AUDIT_SCHEMA = "btc-5m-15m-readiness-v08-rolling-audit.v1"


def _decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name} must be an exact decimal")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _non_negative_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _bool_flag(value: Any, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


class StructuralDirection(str, Enum):
    FIVE_ABOVE_FIFTEEN = "five_above_fifteen"
    FIVE_BELOW_FIFTEEN = "five_below_fifteen"
    EQUAL_STRIKES = "equal_strikes"
    UNKNOWN = "unknown"


def _structural_direction(value: StructuralDirection | str) -> StructuralDirection:
    return (
        value if isinstance(value, StructuralDirection) else StructuralDirection(value)
    )


def _pair_action(value: PairAction | str) -> PairAction:
    return value if isinstance(value, PairAction) else PairAction(value)


class SingleLegDisposition(str, Enum):
    """Pre-registered response when only one double-maker leg fills."""

    PASSIVE_WAIT = "passive_wait"
    TAKER_HEDGE = "taker_hedge"
    FAK_UNWIND = "fak_unwind"


REGISTERED_NEUTRAL_DISPOSITION = SingleLegDisposition.PASSIVE_WAIT


def _maker_leg_side(contract: TwapMarketContract, token_id: str) -> str:
    return "up" if token_id == contract.up_token_id else "down"


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
        if quantity % BASE_UNIT != ZERO:
            raise ValueError(
                "maker quantity must use non-negative 6-decimal base units"
            )
        if queue < ZERO:
            raise ValueError("visible_queue_ahead cannot be negative")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "visible_queue_ahead", queue)

    def to_document(self) -> dict[str, object]:
        return {
            "market_id": self.contract.market_id,
            "condition_id": self.contract.condition_id,
            "rule_hash": self.contract.rule_hash,
            "token_id": self.token_id,
            "side": _maker_leg_side(self.contract, self.token_id),
            "price": str(self.price),
            "quantity": str(self.quantity),
            "visible_queue_ahead": str(self.visible_queue_ahead),
            "tick_size": str(self.contract.tick_size),
            "minimum_order_size": str(self.contract.minimum_order_size),
        }


@dataclass(frozen=True)
class StructuralDoubleMakerPlan:
    attempt_id: str
    expiry_ms: int
    direction: StructuralDirection
    settlement_state: PairSettlementState
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
        object.__setattr__(self, "direction", _structural_direction(self.direction))
        if not isinstance(self.settlement_state, PairSettlementState):
            raise TypeError("settlement_state must be a PairSettlementState")
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
        state = self.settlement_state
        if (
            state.market_5_rule_hash != self._contract_for_horizon("5m").rule_hash
            or state.market_15_rule_hash != self._contract_for_horizon("15m").rule_hash
        ):
            raise ValueError(
                "settlement state is not bound to both captured rule hashes"
            )
        if (
            state.market_5_open_timestamp_ms
            != self._contract_for_horizon("5m").opens_at_ms
            or state.market_15_open_timestamp_ms
            != self._contract_for_horizon("15m").opens_at_ms
        ):
            raise ValueError("settlement state is not bound to both market opens")
        payoff_structure = SharedTerminalPayoffStructure.from_strikes(
            strike_5=state.strike_5,
            strike_15=state.strike_15,
        )
        expected_direction = {
            StrikeOrdering.FIVE_ABOVE_FIFTEEN: StructuralDirection.FIVE_ABOVE_FIFTEEN,
            StrikeOrdering.FIVE_BELOW_FIFTEEN: StructuralDirection.FIVE_BELOW_FIFTEEN,
            StrikeOrdering.EQUAL: StructuralDirection.EQUAL_STRIKES,
        }[payoff_structure.strike_ordering]
        if self.direction is not expected_direction:
            raise ValueError("direction conflicts with the verified settlement strikes")
        leg_pair = frozenset(
            (
                (
                    self.first.contract.horizon,
                    _maker_leg_side(self.first.contract, self.first.token_id),
                ),
                (
                    self.second.contract.horizon,
                    _maker_leg_side(self.second.contract, self.second.token_id),
                ),
            )
        )
        allowed_pairs = {
            StructuralDirection.FIVE_ABOVE_FIFTEEN: {
                frozenset({("15m", "up"), ("5m", "down")})
            },
            StructuralDirection.FIVE_BELOW_FIFTEEN: {
                frozenset({("5m", "up"), ("15m", "down")})
            },
            StructuralDirection.EQUAL_STRIKES: {
                frozenset({("15m", "up"), ("5m", "down")}),
                frozenset({("5m", "up"), ("15m", "down")}),
            },
        }
        if leg_pair not in allowed_pairs[self.direction]:
            raise ValueError("structural direction conflicts with selected legs")
        action = self._pair_action()
        if payoff_structure.worst_case_payoff(action) < ONE:
            raise ValueError("selected legs do not have a verified structural floor")
        for name in ("expiry_ms", "submitted_at_ms", "book_timestamp_ms"):
            _non_negative_int(getattr(self, name), name=name)
        if not self.book_timestamp_ms <= self.submitted_at_ms < self.expiry_ms:
            raise ValueError("book/submit/expiry timestamps are inconsistent")
        _non_negative_int(self.max_book_age_ms, name="max_book_age_ms")
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
                or payouts[contract.up_token_id] + payouts[contract.down_token_id]
                != ONE
            ):
                raise ValueError("each binary market must have one terminal winner")
        actual_outcome = self._actual_outcome_key(payouts)
        if actual_outcome not in payoff_structure.feasible_outcomes:
            raise ValueError(
                "terminal outcome conflicts with the verified strike ordering"
            )
        if payouts[self.first.token_id] + payouts[self.second.token_id] < ONE:
            raise ValueError("selected legs do not form a structural floor pair")
        object.__setattr__(self, "terminal_payouts", MappingProxyType(payouts))

    def _contract_for_horizon(self, horizon: str) -> TwapMarketContract:
        for contract in (self.first.contract, self.second.contract):
            if contract.horizon == horizon:
                return contract
        raise ValueError(f"double-maker plan is missing its {horizon} contract")

    def _pair_action(self) -> PairAction:
        legs = {
            (
                self.first.contract.horizon,
                _maker_leg_side(self.first.contract, self.first.token_id),
            ),
            (
                self.second.contract.horizon,
                _maker_leg_side(self.second.contract, self.second.token_id),
            ),
        }
        if legs == {("15m", "up"), ("5m", "down")}:
            return PairAction.LONG_15_UP_LONG_5_DOWN
        if legs == {("5m", "up"), ("15m", "down")}:
            return PairAction.LONG_5_UP_LONG_15_DOWN
        raise ValueError("double-maker legs do not map to a supported pair action")

    def _actual_outcome_key(self, payouts: Mapping[str, Decimal]) -> str:
        market_5 = self._contract_for_horizon("5m")
        market_15 = self._contract_for_horizon("15m")
        actual_5_up = payouts[market_5.up_token_id] == ONE
        actual_15_up = payouts[market_15.up_token_id] == ONE
        return (
            ("5up" if actual_5_up else "5down")
            + "_"
            + ("15up" if actual_15_up else "15down")
        )

    def to_document(self) -> dict[str, object]:
        state = self.settlement_state
        return {
            "attempt_id": self.attempt_id,
            "expiry_ms": self.expiry_ms,
            "direction": self.direction.value,
            "action": self._pair_action().value,
            "submitted_at_ms": self.submitted_at_ms,
            "book_timestamp_ms": self.book_timestamp_ms,
            "max_book_age_ms": self.max_book_age_ms,
            "initial_cash": str(self.initial_cash),
            "settlement_state": {
                "market_5_rule_hash": state.market_5_rule_hash,
                "market_15_rule_hash": state.market_15_rule_hash,
                "market_5_open_timestamp_ms": state.market_5_open_timestamp_ms,
                "market_15_open_timestamp_ms": state.market_15_open_timestamp_ms,
                "strike_5": str(state.strike_5),
                "strike_15": str(state.strike_15),
                "opening_5_source_event_id": state.opening_5_source_event_id,
                "opening_15_source_event_id": state.opening_15_source_event_id,
            },
            "first": self.first.to_document(),
            "second": self.second.to_document(),
            "terminal_payouts": {
                token_id: str(value)
                for token_id, value in sorted(self.terminal_payouts.items())
            },
        }


@dataclass(frozen=True)
class StructuralShadowResult:
    attempt_id: str
    expiry_ms: int
    direction: StructuralDirection
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
        _non_negative_int(self.expiry_ms, name="expiry_ms")
        object.__setattr__(self, "direction", _structural_direction(self.direction))
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
        if any(
            not isinstance(reason, str) or not reason for reason in self.reason_codes
        ):
            raise ValueError("reason_codes must contain non-empty strings")

    @classmethod
    def for_robustness(
        cls,
        *,
        attempt_id: str,
        expiry_ms: int,
        direction: StructuralDirection | str,
        net_pnl: Decimal,
    ) -> StructuralShadowResult:
        return cls(
            attempt_id=attempt_id,
            expiry_ms=expiry_ms,
            direction=_structural_direction(direction),
            net_pnl=_decimal(net_pnl, name="net_pnl"),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "expiry_ms": self.expiry_ms,
            "direction": self.direction.value,
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


def _books_for_pair(
    pair: SameExpiryPair,
    books: Mapping[str, OrderBookSnapshot],
) -> Mapping[str, OrderBookSnapshot]:
    if not isinstance(pair, SameExpiryPair):
        raise TypeError("pair must be a SameExpiryPair")
    if not isinstance(books, Mapping):
        raise TypeError("books must be a mapping of token ids to OrderBookSnapshot")
    required = {
        pair.market_5.up_token_id,
        pair.market_5.down_token_id,
        pair.market_15.up_token_id,
        pair.market_15.down_token_id,
    }
    missing = sorted(token_id for token_id in required if token_id not in books)
    if missing:
        raise ValueError(
            "books must cover all four binary tokens; missing " + ", ".join(missing)
        )
    frozen: dict[str, OrderBookSnapshot] = {}
    for token_id in required:
        snapshot = books[token_id]
        if not isinstance(snapshot, OrderBookSnapshot):
            raise TypeError("books must map token ids to OrderBookSnapshot values")
        if snapshot.token_id != token_id:
            raise ValueError("book token id does not match its mapping key")
        frozen[token_id] = snapshot
    return MappingProxyType(frozen)


def _selected_structural_legs(
    pair: SameExpiryPair,
    action: PairAction | str,
) -> tuple[tuple[TwapMarketContract, str], tuple[TwapMarketContract, str]]:
    selected_action = _pair_action(action)
    if selected_action is PairAction.NO_TRADE:
        raise ValueError("structural shadow requires one actionable PairAction")
    if selected_action is PairAction.LONG_15_UP_LONG_5_DOWN:
        return (
            (pair.market_15, pair.market_15.up_token_id),
            (pair.market_5, pair.market_5.down_token_id),
        )
    return (
        (pair.market_5, pair.market_5.up_token_id),
        (pair.market_15, pair.market_15.down_token_id),
    )


def _terminal_payouts_for_pair(
    pair: SameExpiryPair,
    *,
    actual_5_up: bool,
    actual_15_up: bool,
) -> Mapping[str, Decimal]:
    return MappingProxyType(
        {
            pair.market_5.up_token_id: ONE if actual_5_up else ZERO,
            pair.market_5.down_token_id: ZERO if actual_5_up else ONE,
            pair.market_15.up_token_id: ONE if actual_15_up else ZERO,
            pair.market_15.down_token_id: ZERO if actual_15_up else ONE,
        }
    )


def _maker_leg_from_snapshot(
    contract: TwapMarketContract,
    token_id: str,
    *,
    book: OrderBookSnapshot,
    quantity: Decimal,
) -> StructuralMakerLeg:
    if book.token_id != token_id:
        raise ValueError("book token id does not match selected structural leg")
    if book.tick_size != contract.tick_size:
        raise ValueError("book tick size does not match the captured market rule")
    if book.minimum_order_size != contract.minimum_order_size:
        raise ValueError(
            "book minimum order size does not match the captured market rule"
        )
    if not book.bids:
        raise ValueError("selected maker leg is missing visible best-bid depth")
    best_bid = book.bids[0]
    return StructuralMakerLeg(
        contract=contract,
        token_id=token_id,
        price=best_bid.price,
        quantity=quantity,
        visible_queue_ahead=best_bid.size,
    )


def build_structural_double_maker_plan(
    *,
    attempt_id: str,
    pair: SameExpiryPair,
    settlement_state: PairSettlementState,
    books: Mapping[str, OrderBookSnapshot],
    action: PairAction | str,
    quantity: Decimal,
    submitted_at_ms: int,
    actual_5_up: bool,
    actual_15_up: bool,
    max_book_age_ms: int,
    initial_cash: Decimal | None = None,
) -> StructuralDoubleMakerPlan:
    """Bind captured top-of-book evidence to one validated double-maker plan."""

    quantity_decimal = _decimal(quantity, name="quantity")
    all_books = _books_for_pair(pair, books)
    selected_first, selected_second = _selected_structural_legs(pair, action)
    first_book = all_books[selected_first[1]]
    second_book = all_books[selected_second[1]]
    if first_book.timestamp_ms != second_book.timestamp_ms:
        raise ValueError("selected maker books must share one captured timestamp")
    payoff_structure = SharedTerminalPayoffStructure.from_strikes(
        strike_5=settlement_state.strike_5,
        strike_15=settlement_state.strike_15,
    )
    direction = {
        StrikeOrdering.FIVE_ABOVE_FIFTEEN: StructuralDirection.FIVE_ABOVE_FIFTEEN,
        StrikeOrdering.FIVE_BELOW_FIFTEEN: StructuralDirection.FIVE_BELOW_FIFTEEN,
        StrikeOrdering.EQUAL: StructuralDirection.EQUAL_STRIKES,
    }[payoff_structure.strike_ordering]
    effective_initial_cash = (
        quantity_decimal * Decimal(2)
        if initial_cash is None
        else _decimal(initial_cash, name="initial_cash")
    )
    return StructuralDoubleMakerPlan(
        attempt_id=attempt_id,
        expiry_ms=pair.expires_at_ms,
        direction=direction,
        settlement_state=settlement_state,
        first=_maker_leg_from_snapshot(
            selected_first[0],
            selected_first[1],
            book=first_book,
            quantity=quantity_decimal,
        ),
        second=_maker_leg_from_snapshot(
            selected_second[0],
            selected_second[1],
            book=second_book,
            quantity=quantity_decimal,
        ),
        submitted_at_ms=_non_negative_int(submitted_at_ms, name="submitted_at_ms"),
        book_timestamp_ms=first_book.timestamp_ms,
        terminal_payouts=_terminal_payouts_for_pair(
            pair,
            actual_5_up=_bool_flag(actual_5_up, name="actual_5_up"),
            actual_15_up=_bool_flag(actual_15_up, name="actual_15_up"),
        ),
        initial_cash=effective_initial_cash,
        max_book_age_ms=_non_negative_int(max_book_age_ms, name="max_book_age_ms"),
    )


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
                    source_event_id=f"{plan.attempt_id}:cancel:{order.order_id}",
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
            "without_best_expiry_net_pnl": str(self.without_best_expiry_net_pnl),
            "without_best_direction_net_pnl": str(self.without_best_direction_net_pnl),
            "minimum_rolling_window_net_pnl": str(self.minimum_rolling_window_net_pnl),
            "max_single_expiry_concentration": (
                None
                if self.max_single_expiry_concentration is None
                else str(self.max_single_expiry_concentration)
            ),
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class StructuralCaptureVerification:
    attempt_id: str
    capture_attempt_id: str
    capture_root: str
    capture_tree_sha256: str
    manifest_count: int
    record_count: int
    book_event_count: int
    trade_event_count: int
    complete_window: bool
    supporting_capture_roots: tuple[str, ...] = ()
    supporting_capture_tree_sha256: tuple[str, ...] = ()
    supporting_manifest_count: int = 0
    supporting_record_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ValueError("attempt_id must be non-empty")
        if not isinstance(self.capture_attempt_id, str) or not self.capture_attempt_id:
            raise ValueError("capture_attempt_id must be non-empty")
        if not isinstance(self.capture_root, str) or not self.capture_root:
            raise ValueError("capture_root must be non-empty")
        if (
            not isinstance(self.capture_tree_sha256, str)
            or len(self.capture_tree_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.capture_tree_sha256
            )
        ):
            raise ValueError("capture_tree_sha256 must be a lowercase SHA-256")
        for name in (
            "manifest_count",
            "record_count",
            "book_event_count",
            "trade_event_count",
            "supporting_manifest_count",
            "supporting_record_count",
        ):
            _non_negative_int(getattr(self, name), name=name)
        if self.manifest_count == 0 or self.record_count == 0:
            raise ValueError("verified capture must contain manifests and records")
        if self.complete_window is not True:
            raise ValueError("verified capture must cover the complete window")
        roots = tuple(self.supporting_capture_roots)
        digests = tuple(self.supporting_capture_tree_sha256)
        if (
            len(roots) != len(digests)
            or len(roots) != len(set(roots))
            or any(not isinstance(root, str) or not root for root in roots)
            or any(
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                for digest in digests
            )
        ):
            raise ValueError("supporting capture identities are invalid")
        if self.capture_root in roots:
            raise ValueError("primary capture cannot also be a supporting capture")
        if not roots and (
            self.supporting_manifest_count != 0 or self.supporting_record_count != 0
        ):
            raise ValueError("supporting counts require supporting capture roots")
        object.__setattr__(self, "supporting_capture_roots", roots)
        object.__setattr__(self, "supporting_capture_tree_sha256", digests)

    def to_document(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "capture_attempt_id": self.capture_attempt_id,
            "capture_root": self.capture_root,
            "capture_tree_sha256": self.capture_tree_sha256,
            "manifest_count": self.manifest_count,
            "record_count": self.record_count,
            "book_event_count": self.book_event_count,
            "trade_event_count": self.trade_event_count,
            "complete_window": self.complete_window,
            "supporting_captures": [
                {"capture_root": root, "capture_tree_sha256": digest}
                for root, digest in zip(
                    self.supporting_capture_roots,
                    self.supporting_capture_tree_sha256,
                )
            ],
            "supporting_manifest_count": self.supporting_manifest_count,
            "supporting_record_count": self.supporting_record_count,
            "verified_from_finalized_recorder_manifests": True,
        }


@dataclass(frozen=True)
class StructuralShadowAttemptReport:
    attempt_id: str
    expiry_ms: int
    direction: StructuralDirection
    plan: StructuralDoubleMakerPlan
    capture_verification: StructuralCaptureVerification
    locked_action_payload_sha256: str
    source_evidence_sha256: str
    results: tuple[StructuralShadowResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ValueError("attempt_id must be non-empty")
        _non_negative_int(self.expiry_ms, name="expiry_ms")
        object.__setattr__(self, "direction", _structural_direction(self.direction))
        if not isinstance(self.plan, StructuralDoubleMakerPlan):
            raise TypeError("plan must be a StructuralDoubleMakerPlan")
        if not isinstance(self.capture_verification, StructuralCaptureVerification):
            raise TypeError(
                "capture_verification must be a StructuralCaptureVerification"
            )
        if (
            self.plan.attempt_id != self.attempt_id
            or self.plan.expiry_ms != self.expiry_ms
            or self.plan.direction is not self.direction
        ):
            raise ValueError("attempt report identity does not match its plan")
        if self.capture_verification.attempt_id != self.attempt_id:
            raise ValueError("capture verification identity does not match its plan")
        for name in (
            "locked_action_payload_sha256",
            "source_evidence_sha256",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if len(self.results) != len(QueueScenario) * len(SingleLegDisposition):
            raise ValueError("attempt report must contain one complete 3x3 matrix")
        if any(not isinstance(item, StructuralShadowResult) for item in self.results):
            raise TypeError("results must contain StructuralShadowResult values")
        seen = {(result.scenario, result.disposition) for result in self.results}
        if len(seen) != len(self.results):
            raise ValueError(
                "attempt report contains duplicate scenario/disposition rows"
            )

    def result_for(
        self,
        *,
        scenario: QueueScenario | str,
        disposition: SingleLegDisposition | str,
    ) -> StructuralShadowResult:
        active_scenario = (
            scenario if isinstance(scenario, QueueScenario) else QueueScenario(scenario)
        )
        active_disposition = (
            disposition
            if isinstance(disposition, SingleLegDisposition)
            else SingleLegDisposition(disposition)
        )
        for result in self.results:
            if (
                result.scenario is active_scenario
                and result.disposition is active_disposition
            ):
                return result
        raise KeyError("scenario/disposition result is missing from the attempt matrix")

    def to_document(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "expiry_ms": self.expiry_ms,
            "direction": self.direction.value,
            "locked_action_payload_sha256": self.locked_action_payload_sha256,
            "source_evidence_sha256": self.source_evidence_sha256,
            "capture_verification": self.capture_verification.to_document(),
            "plan": self.plan.to_document(),
            "results": [item.to_document() for item in self.results],
        }


ZERO_DENOMINATOR_OUTCOMES = frozenset(
    {
        "no_trade",
        "capture_dirty",
        "rule_or_fee_dirty",
        "rtds_gap_dirty",
    }
)


@dataclass(frozen=True)
class LockedZeroCohort:
    attempt_id: str
    expiry_ms: int
    outcome: str
    clean: bool
    capture_verification: StructuralCaptureVerification
    source_evidence_sha256: str
    dirty_reason_code: str | None = None
    decision_at_ms: int | None = None
    locked_action_payload_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ValueError("attempt_id must be non-empty")
        _non_negative_int(self.expiry_ms, name="expiry_ms")
        if self.outcome not in ZERO_DENOMINATOR_OUTCOMES:
            raise ValueError("zero cohort outcome is not denominator-safe")
        if not isinstance(self.capture_verification, StructuralCaptureVerification):
            raise TypeError(
                "capture_verification must be a StructuralCaptureVerification"
            )
        if self.capture_verification.attempt_id != self.attempt_id:
            raise ValueError("capture verification identity does not match zero cohort")
        expected_clean = self.outcome == "no_trade"
        if self.clean is not expected_clean:
            raise ValueError("zero cohort clean flag conflicts with its outcome")
        if self.clean:
            if self.dirty_reason_code is not None:
                raise ValueError("clean zero cohorts cannot claim a dirty reason")
        elif not isinstance(self.dirty_reason_code, str) or not self.dirty_reason_code:
            raise ValueError("dirty zero cohorts require a verified dirty reason")
        if (
            not isinstance(self.source_evidence_sha256, str)
            or len(self.source_evidence_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.source_evidence_sha256
            )
        ):
            raise ValueError("source_evidence_sha256 must be a lowercase SHA-256")
        if self.outcome == "no_trade":
            if self.decision_at_ms is None:
                raise ValueError("no_trade zero cohort requires its decision time")
            _non_negative_int(self.decision_at_ms, name="decision_at_ms")
            digest = self.locked_action_payload_sha256
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(
                    "no_trade zero cohort requires its locked action payload hash"
                )
        elif (
            self.decision_at_ms is not None
            or self.locked_action_payload_sha256 is not None
        ):
            raise ValueError("dirty zero cohorts cannot claim a verified decision")

    def neutral_result(self) -> StructuralShadowResult:
        return StructuralShadowResult.for_robustness(
            attempt_id=self.attempt_id,
            expiry_ms=self.expiry_ms,
            direction=StructuralDirection.UNKNOWN,
            net_pnl=ZERO,
        )

    def to_document(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "expiry_ms": self.expiry_ms,
            "direction": StructuralDirection.UNKNOWN.value,
            "outcome": self.outcome,
            "clean": self.clean,
            "dirty_reason_code": self.dirty_reason_code,
            "decision_at_ms": self.decision_at_ms,
            "locked_action_payload_sha256": self.locked_action_payload_sha256,
            "source_evidence_sha256": self.source_evidence_sha256,
            "capture_verification": self.capture_verification.to_document(),
            "neutral_net_pnl": "0",
            "reason_code": f"locked_denominator_{self.outcome}",
        }


_BUILDER_CAPABILITY = object()


def _builder_authority_digest(
    *,
    source_input_sha256: str,
    preregistration_sha256: str,
    rolling_audit_sha256: str,
    attempts: Sequence[StructuralShadowAttemptReport],
    locked_zero_cohorts: Sequence[LockedZeroCohort],
) -> str:
    payload = {
        "schema_version": STRUCTURAL_SHADOW_REPORT_SCHEMA,
        "issuer": "btc_twap_structural_shadow.finalized_capture_store_builder",
        "source_input_sha256": source_input_sha256,
        "preregistration_sha256": preregistration_sha256,
        "rolling_audit_sha256": rolling_audit_sha256,
        "attempts": [
            {
                "attempt_id": item.attempt_id,
                "expiry_ms": item.expiry_ms,
                "capture_verification_sha256": sha256(
                    canonical_json_bytes(item.capture_verification.to_document())
                ).hexdigest(),
                "locked_action_payload_sha256": item.locked_action_payload_sha256,
                "source_evidence_sha256": item.source_evidence_sha256,
            }
            for item in attempts
        ],
        "locked_zero_cohorts": [
            {
                "attempt_id": item.attempt_id,
                "expiry_ms": item.expiry_ms,
                "outcome": item.outcome,
                "capture_verification_sha256": sha256(
                    canonical_json_bytes(item.capture_verification.to_document())
                ).hexdigest(),
                "source_evidence_sha256": item.source_evidence_sha256,
                "locked_action_payload_sha256": item.locked_action_payload_sha256,
                "dirty_reason_code": item.dirty_reason_code,
            }
            for item in locked_zero_cohorts
        ],
    }
    return sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class _StructuralShadowBuilderAuthority:
    issuer: str
    authority_kind: str
    authority_sha256: str
    _capability: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._capability is not _BUILDER_CAPABILITY:
            raise ValueError("builder authority must be issued by the finalized builder")
        if self.issuer != "btc_twap_structural_shadow.finalized_capture_store_builder":
            raise ValueError("unsupported builder authority issuer")
        if self.authority_kind != "capture_store_finalized":
            raise ValueError("unsupported builder authority kind")
        if (
            not isinstance(self.authority_sha256, str)
            or len(self.authority_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.authority_sha256)
        ):
            raise ValueError("builder authority digest must be a lowercase SHA-256")

    def to_document(self) -> dict[str, str]:
        return {
            "issuer": self.issuer,
            "authority_kind": self.authority_kind,
            "authority_sha256": self.authority_sha256,
        }


@dataclass(frozen=True)
class StructuralShadowReport:
    attempts: tuple[StructuralShadowAttemptReport, ...]
    locked_zero_cohorts: tuple[LockedZeroCohort, ...]
    neutral_disposition: SingleLegDisposition
    rolling_window_size: int
    minimum_expiries: int
    source_input_sha256: str
    preregistration_sha256: str
    rolling_audit_sha256: str
    rolling_journal_root: str
    receipt_chain_verified: bool
    all_admitted_cohorts_included: bool
    neutral_robustness: StructuralShadowRobustness
    neutral_shadow_evidence: NeutralShadowEvidence
    _builder_authority: _StructuralShadowBuilderAuthority | None = field(
        repr=False,
        compare=False,
        default=None,
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "neutral_disposition",
            (
                self.neutral_disposition
                if isinstance(self.neutral_disposition, SingleLegDisposition)
                else SingleLegDisposition(self.neutral_disposition)
            ),
        )
        if self.neutral_disposition is not REGISTERED_NEUTRAL_DISPOSITION:
            raise ValueError(
                "neutral_disposition must match the registered passive_wait policy"
            )
        if self.rolling_window_size != REGISTERED_ROLLING_WINDOW_EXPIRIES:
            raise ValueError(
                "rolling_window_size must match the registered 20 expiries"
            )
        if (
            isinstance(self.minimum_expiries, bool)
            or not isinstance(self.minimum_expiries, int)
            or self.minimum_expiries < MINIMUM_STRUCTURAL_SHADOW_EXPIRIES
        ):
            raise ValueError(
                "minimum_expiries cannot weaken the registered 200-expiry gate"
            )
        for name in (
            "source_input_sha256",
            "preregistration_sha256",
            "rolling_audit_sha256",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if (
            not isinstance(self.rolling_journal_root, str)
            or not self.rolling_journal_root
        ):
            raise ValueError("rolling_journal_root must be non-empty")
        _bool_flag(self.receipt_chain_verified, name="receipt_chain_verified")
        _bool_flag(
            self.all_admitted_cohorts_included,
            name="all_admitted_cohorts_included",
        )
        if any(
            not isinstance(item, StructuralShadowAttemptReport)
            for item in self.attempts
        ):
            raise TypeError(
                "attempts must contain StructuralShadowAttemptReport values"
            )
        if any(
            not isinstance(item, LockedZeroCohort) for item in self.locked_zero_cohorts
        ):
            raise TypeError("locked_zero_cohorts must contain LockedZeroCohort values")
        combined_ids = [item.attempt_id for item in self.attempts] + [
            item.attempt_id for item in self.locked_zero_cohorts
        ]
        combined_expiries = [item.expiry_ms for item in self.attempts] + [
            item.expiry_ms for item in self.locked_zero_cohorts
        ]
        if len(combined_ids) != len(set(combined_ids)):
            raise ValueError("report cohorts must use unique attempt ids")
        if len(combined_expiries) != len(set(combined_expiries)):
            raise ValueError("report cohorts must use unique common expiries")
        all_capture_verifications = tuple(
            item.capture_verification for item in self.attempts
        ) + tuple(item.capture_verification for item in self.locked_zero_cohorts)
        capture_attempt_ids = [
            item.capture_attempt_id for item in all_capture_verifications
        ]
        capture_roots = [item.capture_root for item in all_capture_verifications]
        if len(capture_attempt_ids) != len(set(capture_attempt_ids)):
            raise ValueError("report cohorts must use unique capture attempts")
        if len(capture_roots) != len(set(capture_roots)):
            raise ValueError("report cohorts must use unique capture roots")
        supporting_roots = {
            root
            for verification in all_capture_verifications
            for root in verification.supporting_capture_roots
        }
        if supporting_roots & set(capture_roots):
            raise ValueError(
                "supporting capture roots cannot also serve as primary capture roots"
            )
        if not isinstance(self.neutral_robustness, StructuralShadowRobustness):
            raise TypeError("neutral_robustness must be StructuralShadowRobustness")
        if not isinstance(self.neutral_shadow_evidence, NeutralShadowEvidence):
            raise TypeError("neutral_shadow_evidence must be NeutralShadowEvidence")
        cohort_count = len(self.attempts) + len(self.locked_zero_cohorts)
        evidence = self.neutral_shadow_evidence
        robustness = self.neutral_robustness
        if (
            robustness.expiry_count != cohort_count
            or evidence.expiry_count != cohort_count
        ):
            raise ValueError("neutral evidence count must include every report cohort")
        if (
            evidence.realized_net_pnl != robustness.total_net_pnl
            or evidence.without_best_expiry_net_pnl
            != robustness.without_best_expiry_net_pnl
            or evidence.without_best_direction_net_pnl
            != robustness.without_best_direction_net_pnl
            or evidence.minimum_rolling_window_net_pnl
            != robustness.minimum_rolling_window_net_pnl
            or evidence.max_single_expiry_pnl_concentration
            != robustness.max_single_expiry_concentration
        ):
            raise ValueError("neutral evidence does not match report robustness")
        if self._builder_authority is None:
            if self.receipt_chain_verified or self.all_admitted_cohorts_included:
                raise ValueError("unverified reports cannot claim complete provenance")
            if evidence.receipt_chain_verified or evidence.all_admitted_cohorts_included:
                raise ValueError(
                    "unverified reports cannot claim verified neutral evidence"
                )
        else:
            expected_authority = _builder_authority_digest(
                source_input_sha256=self.source_input_sha256,
                preregistration_sha256=self.preregistration_sha256,
                rolling_audit_sha256=self.rolling_audit_sha256,
                attempts=self.attempts,
                locked_zero_cohorts=self.locked_zero_cohorts,
            )
            if self._builder_authority.authority_sha256 != expected_authority:
                raise ValueError("builder authority digest does not match the report")

    def to_document(self) -> dict[str, object]:
        evidence = self.neutral_shadow_evidence
        unsigned = {
            "schema_version": STRUCTURAL_SHADOW_REPORT_SCHEMA,
            "builder_authority": (
                None
                if self._builder_authority is None
                else self._builder_authority.to_document()
            ),
            "neutral_disposition": self.neutral_disposition.value,
            "rolling_window_size": self.rolling_window_size,
            "minimum_expiries": self.minimum_expiries,
            "source_input_sha256": self.source_input_sha256,
            "preregistration_sha256": self.preregistration_sha256,
            "rolling_audit_sha256": self.rolling_audit_sha256,
            "rolling_journal_root": self.rolling_journal_root,
            "receipt_chain_verified": self.receipt_chain_verified,
            "all_admitted_cohorts_included": self.all_admitted_cohorts_included,
            "attempt_count": len(self.attempts) + len(self.locked_zero_cohorts),
            "replayed_attempt_count": len(self.attempts),
            "locked_zero_cohort_count": len(self.locked_zero_cohorts),
            "attempts": [item.to_document() for item in self.attempts],
            "locked_zero_cohorts": [
                item.to_document() for item in self.locked_zero_cohorts
            ],
            "neutral_robustness": self.neutral_robustness.to_document(),
            "neutral_shadow_evidence": {
                "realized_net_pnl": (
                    None
                    if evidence.realized_net_pnl is None
                    else str(evidence.realized_net_pnl)
                ),
                "receipt_chain_verified": evidence.receipt_chain_verified,
                "all_admitted_cohorts_included": evidence.all_admitted_cohorts_included,
                "scenario": evidence.scenario,
                "expiry_count": evidence.expiry_count,
                "without_best_expiry_net_pnl": (
                    None
                    if evidence.without_best_expiry_net_pnl is None
                    else str(evidence.without_best_expiry_net_pnl)
                ),
                "without_best_direction_net_pnl": (
                    None
                    if evidence.without_best_direction_net_pnl is None
                    else str(evidence.without_best_direction_net_pnl)
                ),
                "minimum_rolling_window_net_pnl": (
                    None
                    if evidence.minimum_rolling_window_net_pnl is None
                    else str(evidence.minimum_rolling_window_net_pnl)
                ),
                "max_single_expiry_pnl_concentration": (
                    None
                    if evidence.max_single_expiry_pnl_concentration is None
                    else str(evidence.max_single_expiry_pnl_concentration)
                ),
            },
        }
        return {
            **unsigned,
            "report_sha256": sha256(canonical_json_bytes(unsigned)).hexdigest(),
        }


@dataclass(frozen=True)
class RollingShadowAuditBinding:
    preregistration_sha256: str
    rolling_audit_sha256: str
    rolling_journal_root: str


@dataclass(frozen=True)
class _VerifiedFinalizedCaptureBundle:
    capture_verification_by_attempt: Mapping[str, StructuralCaptureVerification]
    locked_action_payload_sha256_by_attempt: Mapping[str, str]
    source_evidence_sha256_by_attempt: Mapping[str, str]
    audit_binding: RollingShadowAuditBinding
    _capability: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._capability is not _BUILDER_CAPABILITY:
            raise ValueError(
                "verified finalized capture bundle requires builder capability"
            )
        if any(
            not isinstance(item, StructuralCaptureVerification)
            for item in self.capture_verification_by_attempt.values()
        ):
            raise TypeError(
                "capture_verification_by_attempt must contain StructuralCaptureVerification values"
            )
        for mapping_name in (
            "locked_action_payload_sha256_by_attempt",
            "source_evidence_sha256_by_attempt",
        ):
            mapping = getattr(self, mapping_name)
            if not isinstance(mapping, Mapping):
                raise TypeError(f"{mapping_name} must be a mapping")
            for value in mapping.values():
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in value
                    )
                ):
                    raise ValueError(
                        f"{mapping_name} values must be lowercase SHA-256"
                    )
        if not isinstance(self.audit_binding, RollingShadowAuditBinding):
            raise TypeError("audit_binding must be RollingShadowAuditBinding")


def _verified_finalized_capture_bundle(
    *,
    capture_verification_by_attempt: Mapping[str, StructuralCaptureVerification],
    locked_action_payload_sha256_by_attempt: Mapping[str, str],
    source_evidence_sha256_by_attempt: Mapping[str, str],
    audit_binding: RollingShadowAuditBinding,
) -> _VerifiedFinalizedCaptureBundle:
    return _VerifiedFinalizedCaptureBundle(
        capture_verification_by_attempt=MappingProxyType(
            dict(capture_verification_by_attempt)
        ),
        locked_action_payload_sha256_by_attempt=MappingProxyType(
            dict(locked_action_payload_sha256_by_attempt)
        ),
        source_evidence_sha256_by_attempt=MappingProxyType(
            dict(source_evidence_sha256_by_attempt)
        ),
        audit_binding=audit_binding,
        _capability=_BUILDER_CAPABILITY,
    )


def _bind_rolling_audit(
    plans: Sequence[StructuralDoubleMakerPlan],
    locked_zero_cohorts: Sequence[LockedZeroCohort],
    *,
    capture_verification_by_attempt: Mapping[str, StructuralCaptureVerification],
    rolling_audit: Mapping[str, Any],
    preregistration_sha256: str,
    locked_action_payload_sha256_by_attempt: Mapping[str, str],
    source_evidence_sha256_by_attempt: Mapping[str, str],
) -> RollingShadowAuditBinding:
    if rolling_audit.get("schema_version") != ROLLING_SHADOW_AUDIT_SCHEMA:
        raise ValueError("rolling shadow audit schema is unsupported")
    if rolling_audit.get("valid") is not True or rolling_audit.get("errors") != []:
        raise ValueError("rolling shadow receipt chain is not valid")
    if rolling_audit.get("denominator_includes_no_trade_no_fill_and_dirty") is not True:
        raise ValueError("rolling audit does not preserve the locked denominator")
    policy = rolling_audit.get("policy")
    if not isinstance(policy, Mapping):
        raise TypeError("rolling audit is missing its locked policy")
    if policy.get("preregistration_sha256") != preregistration_sha256:
        raise ValueError("rolling policy is bound to a different preregistration")
    admissions = rolling_audit.get("admissions")
    decisions = rolling_audit.get("decisions")
    outcomes = rolling_audit.get("outcomes")
    if not all(isinstance(item, Mapping) for item in (admissions, decisions, outcomes)):
        raise ValueError(
            "rolling audit admissions, decisions, and outcomes are required"
        )
    assert isinstance(admissions, Mapping)
    assert isinstance(decisions, Mapping)
    assert isinstance(outcomes, Mapping)
    plan_ids = {plan.attempt_id for plan in plans}
    zero_ids = {cohort.attempt_id for cohort in locked_zero_cohorts}
    if plan_ids & zero_ids:
        raise ValueError("replayed and zero cohorts cannot share attempt ids")
    cohort_ids = plan_ids | zero_ids
    if set(admissions) != cohort_ids or set(outcomes) != cohort_ids:
        raise ValueError(
            "report rows must exactly cover every admitted finalized cohort"
        )
    if set(source_evidence_sha256_by_attempt) != cohort_ids:
        raise ValueError("source evidence hashes must exactly cover every report row")
    expected_locked_action_ids = plan_ids | {
        cohort.attempt_id
        for cohort in locked_zero_cohorts
        if cohort.outcome == "no_trade"
    }
    if set(locked_action_payload_sha256_by_attempt) != expected_locked_action_ids:
        raise ValueError(
            "locked action payload hashes must exactly cover actionable/no-trade rows"
        )
    if (
        rolling_audit.get("admitted_common_expiry_count") != len(cohort_ids)
        or rolling_audit.get("finalized_common_expiry_count") != len(cohort_ids)
        or rolling_audit.get("unfinished_common_expiry_count") != 0
    ):
        raise ValueError("rolling audit cohort counts do not match the report rows")
    decisions_by_attempt: dict[str, list[Mapping[str, Any]]] = {}
    for decision in decisions.values():
        if not isinstance(decision, Mapping):
            raise TypeError("rolling audit contains an invalid decision receipt")
        common_expiry_id = decision.get("common_expiry_id")
        if isinstance(common_expiry_id, str):
            decisions_by_attempt.setdefault(common_expiry_id, []).append(decision)
    for plan in plans:
        admission = admissions[plan.attempt_id]
        outcome = outcomes[plan.attempt_id]
        if not isinstance(admission, Mapping) or not isinstance(outcome, Mapping):
            raise TypeError("rolling audit contains an invalid cohort document")
        market_5 = plan._contract_for_horizon("5m")
        market_15 = plan._contract_for_horizon("15m")
        if (
            admission.get("common_expiry_id") != plan.attempt_id
            or admission.get("capture_attempt_id")
            != capture_verification_by_attempt[plan.attempt_id].capture_attempt_id
            or admission.get("expiry_ms") != plan.expiry_ms
            or admission.get("market_5_id") != market_5.market_id
            or admission.get("market_15_id") != market_15.market_id
            or admission.get("condition_5_id") != market_5.condition_id
            or admission.get("condition_15_id") != market_15.condition_id
        ):
            raise ValueError("rolling cohort admission does not match its report plan")
        matching_decisions = decisions_by_attempt.get(plan.attempt_id, [])
        locked_action_sha256 = locked_action_payload_sha256_by_attempt[plan.attempt_id]
        if not any(
            decision.get("action") == plan._pair_action().value
            and decision.get("decision_at_ms") == plan.submitted_at_ms
            and decision.get("action_payload_sha256") == locked_action_sha256
            for decision in matching_decisions
        ):
            raise ValueError(
                "report plan quote, quantity, time, and action are not bound to "
                "one matching predecision receipt"
            )
        if outcome.get("clean") is not True or outcome.get("outcome") not in {
            "causal_no_fill",
            "complete",
            "partial_unwound",
            "failed_unhedged",
        }:
            raise ValueError("dirty outcomes cannot contribute replay PnL")
        evidence_sha256 = source_evidence_sha256_by_attempt[plan.attempt_id]
        if (
            not isinstance(evidence_sha256, str)
            or len(evidence_sha256) != 64
            or any(character not in "0123456789abcdef" for character in evidence_sha256)
            or outcome.get("evidence_sha256") != evidence_sha256
        ):
            raise ValueError(
                "finalized outcome is not bound to the source replay evidence"
            )
    for cohort in locked_zero_cohorts:
        admission = admissions[cohort.attempt_id]
        outcome = outcomes[cohort.attempt_id]
        if not isinstance(admission, Mapping) or not isinstance(outcome, Mapping):
            raise TypeError("rolling audit contains an invalid zero cohort document")
        if (
            admission.get("common_expiry_id") != cohort.attempt_id
            or admission.get("expiry_ms") != cohort.expiry_ms
            or outcome.get("common_expiry_id") != cohort.attempt_id
            or outcome.get("outcome") != cohort.outcome
            or outcome.get("clean") is not cohort.clean
        ):
            raise ValueError("locked zero cohort conflicts with its rolling receipts")
        recorded_net_pnl = outcome.get("realized_net_pnl")
        if (
            recorded_net_pnl is not None
            and _decimal(
                recorded_net_pnl,
                name="zero cohort realized_net_pnl",
            )
            != ZERO
        ):
            raise ValueError("dirty/no-trade denominator rows cannot contribute PnL")
        evidence_sha256 = source_evidence_sha256_by_attempt[cohort.attempt_id]
        if (
            cohort.source_evidence_sha256 != evidence_sha256
            or outcome.get("evidence_sha256") != evidence_sha256
        ):
            raise ValueError(
                "zero cohort outcome is not bound to its finalized source evidence"
            )
        if cohort.outcome == "no_trade":
            locked_action_sha256 = locked_action_payload_sha256_by_attempt[
                cohort.attempt_id
            ]
            if cohort.locked_action_payload_sha256 != locked_action_sha256:
                raise ValueError(
                    "no_trade row disagrees with its locked action payload hash"
                )
            matching_decisions = decisions_by_attempt.get(cohort.attempt_id, [])
            if not any(
                decision.get("action") == "no_trade"
                and decision.get("decision_at_ms") == cohort.decision_at_ms
                and decision.get("action_payload_sha256") == locked_action_sha256
                for decision in matching_decisions
            ):
                raise ValueError(
                    "no_trade denominator row is not bound to its predecision receipt"
                )
        elif decisions_by_attempt.get(cohort.attempt_id):
            raise ValueError(
                "dirty zero cohorts must not have any decision/action receipts"
            )
    journal_root = rolling_audit.get("journal_root")
    if not isinstance(journal_root, str) or not journal_root:
        raise ValueError("rolling audit journal_root is missing")
    return RollingShadowAuditBinding(
        preregistration_sha256=preregistration_sha256,
        rolling_audit_sha256=sha256(canonical_json_bytes(rolling_audit)).hexdigest(),
        rolling_journal_root=journal_root,
    )


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
    by_direction: dict[StructuralDirection, Decimal] = {}
    for item in ordered:
        if item.direction is StructuralDirection.UNKNOWN:
            continue
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


def build_structural_shadow_report(
    plans: Sequence[StructuralDoubleMakerPlan],
    *,
    locked_zero_cohorts: Sequence[LockedZeroCohort],
    capture_verification_by_attempt: Mapping[str, StructuralCaptureVerification],
    public_events_by_attempt: Mapping[str, Sequence[BookEvent | TradeEvent]],
    neutral_disposition: SingleLegDisposition | str,
    rolling_window_size: int,
    minimum_expiries: int = 200,
    rolling_audit: Mapping[str, Any],
    source_input_sha256: str,
    preregistration_sha256: str,
    locked_action_payload_sha256_by_attempt: Mapping[str, str],
    source_evidence_sha256_by_attempt: Mapping[str, str],
    verified_finalized_bundle: _VerifiedFinalizedCaptureBundle | None = None,
    hedge_books_by_attempt: Mapping[str, Mapping[str, DepthBook]] | None = None,
    unwind_books_by_attempt: Mapping[str, Mapping[str, DepthBook]] | None = None,
) -> StructuralShadowReport:
    if any(not isinstance(item, StructuralDoubleMakerPlan) for item in plans):
        raise TypeError("plans must contain StructuralDoubleMakerPlan values")
    if any(not isinstance(item, LockedZeroCohort) for item in locked_zero_cohorts):
        raise TypeError("locked_zero_cohorts must contain LockedZeroCohort values")
    if not isinstance(capture_verification_by_attempt, Mapping):
        raise TypeError("capture_verification_by_attempt must be a mapping")
    active_disposition = (
        neutral_disposition
        if isinstance(neutral_disposition, SingleLegDisposition)
        else SingleLegDisposition(neutral_disposition)
    )
    if active_disposition is not REGISTERED_NEUTRAL_DISPOSITION:
        raise ValueError(
            "neutral_disposition must match the registered passive_wait policy"
        )
    ordered_plans = tuple(
        sorted(plans, key=lambda item: (item.expiry_ms, item.attempt_id))
    )
    ordered_zero_cohorts = tuple(
        sorted(
            locked_zero_cohorts,
            key=lambda item: (item.expiry_ms, item.attempt_id),
        )
    )
    if len({item.attempt_id for item in ordered_plans}) != len(ordered_plans):
        raise ValueError("plans must use unique attempt ids")
    plan_ids = {item.attempt_id for item in ordered_plans}
    if set(capture_verification_by_attempt) != plan_ids:
        raise ValueError(
            "capture verifications must exactly cover every replayed attempt"
        )
    if any(
        not isinstance(item, StructuralCaptureVerification)
        for item in capture_verification_by_attempt.values()
    ):
        raise TypeError(
            "capture verifications must contain StructuralCaptureVerification values"
        )
    audit_binding = _bind_rolling_audit(
        ordered_plans,
        ordered_zero_cohorts,
        capture_verification_by_attempt=capture_verification_by_attempt,
        rolling_audit=rolling_audit,
        preregistration_sha256=preregistration_sha256,
        locked_action_payload_sha256_by_attempt=(
            locked_action_payload_sha256_by_attempt
        ),
        source_evidence_sha256_by_attempt=source_evidence_sha256_by_attempt,
    )
    if verified_finalized_bundle is not None and (
        dict(verified_finalized_bundle.capture_verification_by_attempt)
        != dict(capture_verification_by_attempt)
        or dict(verified_finalized_bundle.locked_action_payload_sha256_by_attempt)
        != dict(locked_action_payload_sha256_by_attempt)
        or dict(verified_finalized_bundle.source_evidence_sha256_by_attempt)
        != dict(source_evidence_sha256_by_attempt)
        or verified_finalized_bundle.audit_binding != audit_binding
    ):
        raise ValueError(
            "verified finalized capture bundle does not match report inputs"
        )
    attempt_reports: list[StructuralShadowAttemptReport] = []
    neutral_results: list[StructuralShadowResult] = []
    for plan in ordered_plans:
        if plan.attempt_id not in public_events_by_attempt:
            raise ValueError(
                f"public_events_by_attempt is missing captured flow for {plan.attempt_id}"
            )
        matrix_results: list[StructuralShadowResult] = []
        for scenario in QueueScenario:
            for disposition in SingleLegDisposition:
                result = replay_structural_double_maker(
                    plan,
                    public_events=public_events_by_attempt[plan.attempt_id],
                    scenario=scenario,
                    disposition=disposition,
                    hedge_books=(hedge_books_by_attempt or {}).get(plan.attempt_id),
                    unwind_books=(unwind_books_by_attempt or {}).get(plan.attempt_id),
                )
                matrix_results.append(result)
                if (
                    scenario is QueueScenario.NEUTRAL
                    and disposition is active_disposition
                ):
                    audit_outcomes = rolling_audit["outcomes"]
                    assert isinstance(audit_outcomes, Mapping)
                    audit_outcome = audit_outcomes[plan.attempt_id]
                    assert isinstance(audit_outcome, Mapping)
                    recorded_net_pnl = audit_outcome.get("realized_net_pnl")
                    if (
                        recorded_net_pnl is None
                        or _decimal(
                            recorded_net_pnl,
                            name="rolling outcome realized_net_pnl",
                        )
                        != result.net_pnl
                    ):
                        raise ValueError(
                            "rolling outcome PnL does not match the neutral replay"
                        )
                    neutral_results.append(result)
        attempt_reports.append(
            StructuralShadowAttemptReport(
                attempt_id=plan.attempt_id,
                expiry_ms=plan.expiry_ms,
                direction=plan.direction,
                plan=plan,
                capture_verification=capture_verification_by_attempt[plan.attempt_id],
                locked_action_payload_sha256=(
                    locked_action_payload_sha256_by_attempt[plan.attempt_id]
                ),
                source_evidence_sha256=source_evidence_sha256_by_attempt[
                    plan.attempt_id
                ],
                results=tuple(matrix_results),
            )
        )
    neutral_results.extend(cohort.neutral_result() for cohort in ordered_zero_cohorts)
    robustness = evaluate_shadow_robustness(
        neutral_results,
        rolling_window_size=rolling_window_size,
        minimum_expiries=minimum_expiries,
    )
    report_verified = verified_finalized_bundle is not None
    evidence = NeutralShadowEvidence(
        realized_net_pnl=robustness.total_net_pnl,
        receipt_chain_verified=report_verified,
        all_admitted_cohorts_included=report_verified,
        expiry_count=robustness.expiry_count,
        without_best_expiry_net_pnl=robustness.without_best_expiry_net_pnl,
        without_best_direction_net_pnl=robustness.without_best_direction_net_pnl,
        minimum_rolling_window_net_pnl=robustness.minimum_rolling_window_net_pnl,
        max_single_expiry_pnl_concentration=robustness.max_single_expiry_concentration,
    )
    return StructuralShadowReport(
        attempts=tuple(attempt_reports),
        locked_zero_cohorts=ordered_zero_cohorts,
        neutral_disposition=active_disposition,
        rolling_window_size=rolling_window_size,
        minimum_expiries=minimum_expiries,
        source_input_sha256=source_input_sha256,
        preregistration_sha256=audit_binding.preregistration_sha256,
        rolling_audit_sha256=audit_binding.rolling_audit_sha256,
        rolling_journal_root=audit_binding.rolling_journal_root,
        receipt_chain_verified=report_verified,
        all_admitted_cohorts_included=report_verified,
        neutral_robustness=robustness,
        neutral_shadow_evidence=evidence,
        _builder_authority=(
            None
            if not report_verified
            else _StructuralShadowBuilderAuthority(
                issuer="btc_twap_structural_shadow.finalized_capture_store_builder",
                authority_kind="capture_store_finalized",
                authority_sha256=_builder_authority_digest(
                    source_input_sha256=source_input_sha256,
                    preregistration_sha256=audit_binding.preregistration_sha256,
                    rolling_audit_sha256=audit_binding.rolling_audit_sha256,
                    attempts=tuple(attempt_reports),
                    locked_zero_cohorts=ordered_zero_cohorts,
                ),
                _capability=_BUILDER_CAPABILITY,
            )
        ),
    )


__all__ = [
    "REGISTERED_NEUTRAL_DISPOSITION",
    "REGISTERED_ROLLING_WINDOW_EXPIRIES",
    "ROLLING_SHADOW_AUDIT_SCHEMA",
    "STRUCTURAL_SHADOW_REPORT_SCHEMA",
    "SingleLegDisposition",
    "StructuralDirection",
    "StructuralDoubleMakerPlan",
    "StructuralMakerLeg",
    "StructuralShadowAttemptReport",
    "StructuralShadowReport",
    "StructuralShadowResult",
    "StructuralShadowRobustness",
    "build_structural_double_maker_plan",
    "build_structural_shadow_report",
    "evaluate_shadow_robustness",
    "replay_structural_double_maker",
]
