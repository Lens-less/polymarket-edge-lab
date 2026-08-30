from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from src.edge_lab.execution import ExecutionFeeSchedule

from .errors import (
    AllowanceError,
    CloseOnlyError,
    GeoblockError,
    IdempotencyConflictError,
    MarketDataStaleError,
    VenuePolicyError,
)
from .models import (
    ExecutionMode,
    FillRecord,
    OrderLifecycleStatus,
    OrderSide,
    PreflightCheck,
    TimeInForce,
    VenueCancelResult,
    VenueEventKind,
    VenueOrderSpec,
    VenueOrderState,
    VenuePreflightRequest,
    VenuePreflightResult,
    VenueReconciliation,
    VenueSubmitBatch,
    VenueSubmitResult,
    VenueUserEvent,
    non_empty,
    to_decimal,
    utc_now,
)


class VenueAdapter(Protocol):
    async def preflight(self, request: VenuePreflightRequest) -> VenuePreflightResult: ...

    async def submit(
        self,
        *,
        orders: Sequence[VenueOrderSpec],
        mode: ExecutionMode,
        idempotency_key: str,
    ) -> VenueSubmitBatch: ...

    async def cancel(
        self,
        *,
        order_ids: Sequence[str],
        idempotency_key: str,
        reason: str | None = None,
    ) -> VenueCancelResult: ...

    async def reconcile(
        self,
        *,
        open_order_ids: Sequence[str] | None = None,
        since_trade_id: str | None = None,
    ) -> VenueReconciliation: ...

    def stream_user_events(
        self,
        *,
        markets: Sequence[str] | None = None,
        since_cursor: str | None = None,
    ) -> AsyncIterator[VenueUserEvent]: ...


@dataclass(frozen=True)
class PaperOrderBook:
    best_bid: Decimal
    best_ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    as_of: datetime


def _coerce_datetime(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime")
    return value.astimezone(UTC)


def _field(subject: object, name: str) -> Any:
    if isinstance(subject, Mapping):
        return subject[name]
    return getattr(subject, name)


def _optional_field(subject: object, name: str, default: Any = None) -> Any:
    if isinstance(subject, Mapping):
        return subject.get(name, default)
    return getattr(subject, name, default)


def _required_decimal_field(subject: object, field_name: str) -> Decimal:
    value = _optional_field(subject, field_name, None)
    if value is None:
        raise VenuePolicyError(f"{field_name} is unavailable")
    try:
        return to_decimal(value, name=field_name)
    except (TypeError, ValueError) as exc:
        raise VenuePolicyError(f"{field_name} is invalid") from exc


def _iter_items(paginator: object) -> AsyncIterator[object]:
    async def generator() -> AsyncIterator[object]:
        if hasattr(paginator, "iter_items"):
            iterator = paginator.iter_items()
        else:
            iterator = paginator
        async for item in cast(AsyncIterator[object], iterator):
            yield item

    return generator()


def _status_from_wire(status: str, *, filled: Decimal, quantity: Decimal) -> OrderLifecycleStatus:
    normalized = status.upper()
    if normalized in {"LIVE", "UNMATCHED"}:
        return OrderLifecycleStatus.LIVE
    if normalized == "DELAYED":
        return OrderLifecycleStatus.DELAYED
    if normalized == "MATCHED":
        return (
            OrderLifecycleStatus.FILLED
            if filled >= quantity
            else OrderLifecycleStatus.PARTIALLY_FILLED
        )
    if normalized == "CANCELED":
        return OrderLifecycleStatus.CANCELED
    return OrderLifecycleStatus.ATTENTION_REQUIRED


class _BaseVenueAdapter(VenueAdapter):
    def __init__(
        self,
        *,
        name: str,
        cash_available: Decimal = Decimal("1000"),
        allowance_available: Decimal = Decimal("1000"),
        geoblocked: bool = False,
        close_only: bool = False,
        market_data_provider: Callable[[VenueOrderSpec], PaperOrderBook | None] | None = None,
        fixed_market_data_age_ms: int = 0,
        max_user_stream_gap_seconds: int = 30,
    ) -> None:
        self.name = non_empty(name, name="name")
        self.cash_available = to_decimal(cash_available, name="cash_available")
        self.allowance_available = to_decimal(
            allowance_available,
            name="allowance_available",
        )
        self.geoblocked = bool(geoblocked)
        self.close_only = bool(close_only)
        self.market_data_provider = market_data_provider
        self.fixed_market_data_age_ms = fixed_market_data_age_ms
        self.max_user_stream_gap_seconds = max_user_stream_gap_seconds
        self._submit_results: dict[str, VenueSubmitBatch] = {}
        self._submit_payloads: dict[str, tuple[tuple[object, ...], ...]] = {}
        self._cancel_results: dict[str, VenueCancelResult] = {}
        self._cancel_payloads: dict[str, tuple[str, ...]] = {}
        self._orders_by_venue_id: dict[str, VenueOrderState] = {}
        self._order_ids_by_client_id: dict[str, str] = {}
        self._fills_by_id: dict[str, FillRecord] = {}
        self._event_queue: deque[VenueUserEvent] = deque()
        self._sequence = 0

    def _heartbeat_event(self, *, occurred_at: datetime, detail: str) -> VenueUserEvent:
        return VenueUserEvent(
            kind=VenueEventKind.HEARTBEAT,
            occurred_at=occurred_at,
            cursor=self._next_cursor(),
            detail=detail,
        )

    def _gap_event(self, *, occurred_at: datetime, gap_seconds: int, detail: str) -> VenueUserEvent:
        return VenueUserEvent(
            kind=VenueEventKind.GAP,
            occurred_at=occurred_at,
            cursor=self._next_cursor(),
            gap_seconds=gap_seconds,
            detail=detail,
        )

    def _next_cursor(self) -> str:
        self._sequence += 1
        return f"{self.name}:{self._sequence}"

    def _remember_submit_payload(
        self,
        idempotency_key: str,
        orders: Sequence[VenueOrderSpec],
    ) -> None:
        payload = tuple(
            (
                order.client_order_id,
                order.strategy_id,
                order.market_id,
                order.token_id,
                order.event_id,
                order.side,
                order.quantity,
                order.limit_price,
                order.time_in_force,
                order.post_only,
                order.expires_at,
                tuple(sorted(order.metadata.items())),
            )
            for order in orders
        )
        previous = self._submit_payloads.get(idempotency_key)
        if previous is not None and previous != payload:
            raise IdempotencyConflictError("submit idempotency key was reused for different orders")
        self._submit_payloads[idempotency_key] = payload

    def _remember_cancel_payload(
        self,
        idempotency_key: str,
        order_ids: Sequence[str],
    ) -> None:
        payload = tuple(sorted(order_ids))
        previous = self._cancel_payloads.get(idempotency_key)
        if previous is not None and previous != payload:
            raise IdempotencyConflictError(
                "cancel idempotency key was reused for different order ids"
            )
        self._cancel_payloads[idempotency_key] = payload

    async def preflight(self, request: VenuePreflightRequest) -> VenuePreflightResult:
        checks: list[PreflightCheck] = []
        checks.append(
            PreflightCheck(
                name="geoblock",
                ok=not self.geoblocked,
                detail="venue geoblock allows new orders"
                if not self.geoblocked
                else "venue is geoblocked",
            )
        )
        checks.append(
            PreflightCheck(
                name="close_only",
                ok=not self.close_only,
                detail="venue accepts opening orders"
                if not self.close_only
                else "venue is close-only",
            )
        )
        total_notional = sum((order.notional for order in request.orders), Decimal("0"))
        checks.append(
            PreflightCheck(
                name="allowance",
                ok=self.allowance_available >= total_notional,
                detail="allowance sufficient"
                if self.allowance_available >= total_notional
                else "allowance insufficient",
            )
        )
        checks.append(
            PreflightCheck(
                name="cash",
                ok=self.cash_available >= total_notional,
                detail="cash sufficient"
                if self.cash_available >= total_notional
                else "cash insufficient",
            )
        )
        market_age_ms = self.fixed_market_data_age_ms or request.risk_context.market_data_age_ms
        checks.append(
            PreflightCheck(
                name="market_data_fresh",
                ok=market_age_ms <= request.qualification.risk_limits.max_market_data_age_ms,
                detail=f"market data age {market_age_ms}ms",
            )
        )
        ok = all(check.ok for check in checks if check.blocking)
        return VenuePreflightResult(
            ok=ok,
            checks=tuple(checks),
            as_of=request.now,
            close_only=self.close_only,
            geoblocked=self.geoblocked,
            allowance_available=self.allowance_available,
            cash_available=self.cash_available,
        )

    async def cancel(
        self,
        *,
        order_ids: Sequence[str],
        idempotency_key: str,
        reason: str | None = None,
    ) -> VenueCancelResult:
        del reason
        self._remember_cancel_payload(idempotency_key, order_ids)
        previous = self._cancel_results.get(idempotency_key)
        if previous is not None:
            return previous
        now = utc_now()
        canceled_ids: list[str] = []
        unresolved_ids: list[str] = []
        for order_id in order_ids:
            state = self._orders_by_venue_id.get(order_id)
            if state is None:
                unresolved_ids.append(order_id)
                continue
            self._orders_by_venue_id[order_id] = VenueOrderState(
                venue_order_id=state.venue_order_id,
                client_order_id=state.client_order_id,
                market_id=state.market_id,
                token_id=state.token_id,
                side=state.side,
                quantity=state.quantity,
                limit_price=state.limit_price,
                filled_quantity=state.filled_quantity,
                status=OrderLifecycleStatus.CANCELED,
                time_in_force=state.time_in_force,
                post_only=state.post_only,
                expires_at=state.expires_at,
                updated_at=now,
            )
            canceled_ids.append(order_id)
            self._event_queue.append(
                VenueUserEvent(
                    kind=VenueEventKind.ORDER,
                    occurred_at=now,
                    cursor=self._next_cursor(),
                    order=self._orders_by_venue_id[order_id],
                )
            )
        result = VenueCancelResult(
            canceled_ids=tuple(canceled_ids),
            unresolved_ids=tuple(unresolved_ids),
            canceled_at=now,
        )
        self._cancel_results[idempotency_key] = result
        return result

    async def reconcile(
        self,
        *,
        open_order_ids: Sequence[str] | None = None,
        since_trade_id: str | None = None,
    ) -> VenueReconciliation:
        del since_trade_id
        open_orders = tuple(
            state
            for state in self._orders_by_venue_id.values()
            if state.status
            in {
                OrderLifecycleStatus.LIVE,
                OrderLifecycleStatus.DELAYED,
                OrderLifecycleStatus.MATCHED,
                OrderLifecycleStatus.PARTIALLY_FILLED,
                OrderLifecycleStatus.CANCEL_REQUESTED,
                OrderLifecycleStatus.RECONCILING,
                OrderLifecycleStatus.ATTENTION_REQUIRED,
                OrderLifecycleStatus.AMBIGUOUS,
            }
            and (open_order_ids is None or state.venue_order_id in set(open_order_ids))
        )
        return VenueReconciliation(
            open_orders=tuple(sorted(open_orders, key=lambda state: state.venue_order_id)),
            fills=tuple(sorted(self._fills_by_id.values(), key=lambda fill: fill.fill_id)),
            as_of=utc_now(),
            cash_balance=self.cash_available,
            allowance_available=self.allowance_available,
            close_only=self.close_only,
            geoblocked=self.geoblocked,
            backfill_complete=True,
        )

    def stream_user_events(
        self,
        *,
        markets: Sequence[str] | None = None,
        since_cursor: str | None = None,
    ) -> AsyncIterator[VenueUserEvent]:
        async def generator() -> AsyncIterator[VenueUserEvent]:
            _ = since_cursor
            reconciliation = await self.reconcile()
            yield VenueUserEvent(
                kind=VenueEventKind.BACKFILL,
                occurred_at=reconciliation.as_of,
                cursor=self._next_cursor(),
                detail="reconciliation_backfill",
            )
            yield self._heartbeat_event(
                occurred_at=reconciliation.as_of,
                detail="reconciliation_backfill_heartbeat",
            )
            allowed = set(markets or ())
            pending = list(self._event_queue)
            self._event_queue.clear()
            for event in pending:
                if event.order is not None and allowed and event.order.market_id not in allowed:
                    continue
                if event.fill is not None and allowed and event.fill.market_id not in allowed:
                    continue
                yield event
                if event.kind in {
                    VenueEventKind.ORDER,
                    VenueEventKind.TRADE,
                    VenueEventKind.STATUS,
                }:
                    yield self._heartbeat_event(
                        occurred_at=event.occurred_at,
                        detail=f"{event.kind.value.lower()}_activity",
                    )

        return generator()

    def _store_order(self, state: VenueOrderState) -> None:
        self._orders_by_venue_id[state.venue_order_id] = state
        self._order_ids_by_client_id[state.client_order_id] = state.venue_order_id

    def _store_fill(self, fill: FillRecord) -> None:
        if fill.fill_id in self._fills_by_id:
            return
        self._fills_by_id[fill.fill_id] = fill
        self._event_queue.append(
            VenueUserEvent(
                kind=VenueEventKind.TRADE,
                occurred_at=fill.occurred_at,
                cursor=self._next_cursor(),
                fill=fill,
            )
        )


class ReplayVenueAdapter(_BaseVenueAdapter):
    def __init__(
        self,
        *,
        scripted_results: Mapping[str, VenueSubmitBatch] | None = None,
        scripted_reconciliation: VenueReconciliation | None = None,
        scripted_events: Sequence[VenueUserEvent] = (),
    ) -> None:
        super().__init__(name="replay")
        self._scripted_results = dict(scripted_results or {})
        self._scripted_reconciliation = scripted_reconciliation
        for event in scripted_events:
            self._event_queue.append(event)

    async def submit(
        self,
        *,
        orders: Sequence[VenueOrderSpec],
        mode: ExecutionMode,
        idempotency_key: str,
    ) -> VenueSubmitBatch:
        del mode
        self._remember_submit_payload(idempotency_key, orders)
        previous = self._submit_results.get(idempotency_key)
        if previous is not None:
            return previous
        scripted = self._scripted_results.get(idempotency_key)
        if scripted is None:
            results = tuple(
                VenueSubmitResult(
                    client_order_id=order.client_order_id,
                    status=OrderLifecycleStatus.LIVE,
                    venue_order_id=f"replay-{order.client_order_id}",
                )
                for order in orders
            )
            scripted = VenueSubmitBatch(results=results, submitted_at=utc_now())
        for order, result in zip(orders, scripted.results, strict=True):
            if result.venue_order_id is None:
                continue
            state = VenueOrderState(
                venue_order_id=result.venue_order_id,
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                quantity=order.quantity,
                limit_price=order.limit_price,
                filled_quantity=result.filled_quantity,
                status=result.status,
                time_in_force=order.time_in_force,
                post_only=order.post_only,
                expires_at=order.expires_at,
                reject_reason=result.reject_reason,
                updated_at=scripted.submitted_at,
            )
            self._store_order(state)
            self._event_queue.append(
                VenueUserEvent(
                    kind=VenueEventKind.ORDER,
                    occurred_at=scripted.submitted_at,
                    cursor=self._next_cursor(),
                    order=state,
                )
            )
        self._submit_results[idempotency_key] = scripted
        return scripted

    async def reconcile(
        self,
        *,
        open_order_ids: Sequence[str] | None = None,
        since_trade_id: str | None = None,
    ) -> VenueReconciliation:
        if self._scripted_reconciliation is not None:
            return self._scripted_reconciliation
        return await super().reconcile(
            open_order_ids=open_order_ids,
            since_trade_id=since_trade_id,
        )


class PaperVenueAdapter(_BaseVenueAdapter):
    def __init__(
        self,
        *,
        market_data_provider: Callable[[VenueOrderSpec], PaperOrderBook | None],
        cash_available: Decimal = Decimal("1000"),
        allowance_available: Decimal = Decimal("1000"),
        fee_schedule: ExecutionFeeSchedule | None = None,
    ) -> None:
        super().__init__(
            name="paper",
            cash_available=cash_available,
            allowance_available=allowance_available,
            market_data_provider=market_data_provider,
        )
        self.fee_schedule = fee_schedule or ExecutionFeeSchedule.fee_exempt(
            reason="paper venue explicit zero-fee sandbox",
            source_ref="profit_system.paper.default",
        )

    async def submit(
        self,
        *,
        orders: Sequence[VenueOrderSpec],
        mode: ExecutionMode,
        idempotency_key: str,
    ) -> VenueSubmitBatch:
        del mode
        self._remember_submit_payload(idempotency_key, orders)
        previous = self._submit_results.get(idempotency_key)
        if previous is not None:
            return previous
        now = utc_now()
        results: list[VenueSubmitResult] = []
        provider = self.market_data_provider
        if provider is None:
            raise RuntimeError("paper venue requires a market_data_provider")
        for order in orders:
            book = provider(order)
            if book is None:
                results.append(
                    VenueSubmitResult(
                        client_order_id=order.client_order_id,
                        status=OrderLifecycleStatus.DELAYED,
                        venue_order_id=f"paper-{order.client_order_id}",
                    )
                )
                continue
            crossing = (order.side is OrderSide.BUY and order.limit_price >= book.best_ask) or (
                order.side is OrderSide.SELL and order.limit_price <= book.best_bid
            )
            if order.post_only and crossing:
                results.append(
                    VenueSubmitResult(
                        client_order_id=order.client_order_id,
                        status=OrderLifecycleStatus.REJECTED,
                        reject_reason="post_only_would_cross",
                    )
                )
                continue
            available = book.ask_size if order.side is OrderSide.BUY else book.bid_size
            if order.time_in_force is TimeInForce.FOK and available < order.quantity:
                results.append(
                    VenueSubmitResult(
                        client_order_id=order.client_order_id,
                        status=OrderLifecycleStatus.REJECTED,
                        reject_reason="fok_insufficient_depth",
                    )
                )
                continue
            if order.time_in_force is TimeInForce.FAK:
                filled = min(order.quantity, available) if crossing else Decimal("0")
                if filled == Decimal("0"):
                    results.append(
                        VenueSubmitResult(
                            client_order_id=order.client_order_id,
                            status=OrderLifecycleStatus.REJECTED,
                            reject_reason="fak_not_marketable",
                        )
                    )
                    continue
                status = (
                    OrderLifecycleStatus.FILLED
                    if filled == order.quantity
                    else OrderLifecycleStatus.PARTIALLY_FILLED
                )
                venue_order_id = f"paper-{order.client_order_id}"
                results.append(
                    VenueSubmitResult(
                        client_order_id=order.client_order_id,
                        status=status,
                        venue_order_id=venue_order_id,
                        filled_quantity=filled,
                    )
                )
                state = VenueOrderState(
                    venue_order_id=venue_order_id,
                    client_order_id=order.client_order_id,
                    market_id=order.market_id,
                    token_id=order.token_id,
                    side=order.side,
                    quantity=order.quantity,
                    limit_price=order.limit_price,
                    filled_quantity=filled,
                    status=status,
                    time_in_force=order.time_in_force,
                    updated_at=now,
                )
                self._store_order(state)
                self._event_queue.append(
                    VenueUserEvent(
                        kind=VenueEventKind.ORDER,
                        occurred_at=now,
                        cursor=self._next_cursor(),
                        order=state,
                    )
                )
                self._store_fill(
                    FillRecord(
                        fill_id=f"{venue_order_id}:1",
                        venue_order_id=venue_order_id,
                        market_id=order.market_id,
                        token_id=order.token_id,
                        side=order.side,
                        price=book.best_ask if order.side is OrderSide.BUY else book.best_bid,
                        quantity=filled,
                        fee=self.fee_schedule.fee(
                            filled,
                            book.best_ask if order.side is OrderSide.BUY else book.best_bid,
                            maker=order.post_only,
                        ),
                        occurred_at=now,
                        raw_status=status.value,
                    )
                )
                continue
            venue_order_id = f"paper-{order.client_order_id}"
            status = OrderLifecycleStatus.FILLED if crossing else OrderLifecycleStatus.LIVE
            filled = order.quantity if crossing else Decimal("0")
            results.append(
                VenueSubmitResult(
                    client_order_id=order.client_order_id,
                    status=status,
                    venue_order_id=venue_order_id,
                    filled_quantity=filled,
                )
            )
            state = VenueOrderState(
                venue_order_id=venue_order_id,
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                quantity=order.quantity,
                limit_price=order.limit_price,
                filled_quantity=filled,
                status=status,
                time_in_force=order.time_in_force,
                post_only=order.post_only,
                expires_at=order.expires_at,
                updated_at=now,
            )
            self._store_order(state)
            self._event_queue.append(
                VenueUserEvent(
                    kind=VenueEventKind.ORDER,
                    occurred_at=now,
                    cursor=self._next_cursor(),
                    order=state,
                )
            )
            if filled:
                self._store_fill(
                    FillRecord(
                        fill_id=f"{venue_order_id}:1",
                        venue_order_id=venue_order_id,
                        market_id=order.market_id,
                        token_id=order.token_id,
                        side=order.side,
                        price=book.best_ask if order.side is OrderSide.BUY else book.best_bid,
                        quantity=filled,
                        fee=self.fee_schedule.fee(
                            filled,
                            book.best_ask if order.side is OrderSide.BUY else book.best_bid,
                            maker=order.post_only,
                        ),
                        occurred_at=now,
                        raw_status=status.value,
                    )
                )
        batch = VenueSubmitBatch(results=tuple(results), submitted_at=now)
        self._submit_results[idempotency_key] = batch
        return batch


class PolymarketLiveVenueAdapter(_BaseVenueAdapter):
    def __init__(
        self,
        *,
        client_factory: Callable[[], Awaitable[object] | object],
        market_client_factory: Callable[[], Awaitable[object] | object] | None = None,
        clock: Callable[[], datetime] = utc_now,
        max_user_stream_gap_seconds: int = 30,
    ) -> None:
        super().__init__(name="live", max_user_stream_gap_seconds=max_user_stream_gap_seconds)
        self._client_factory = client_factory
        self._market_client_factory = market_client_factory or client_factory
        self._clock = clock
        self._client: object | None = None
        self._market_client: object | None = None
        self._fee_schedules_by_market_id: dict[str, ExecutionFeeSchedule] = {}
        self._cancel_all_outcome_unknown_sentinel = "cancel-all:outcome-unknown"

    async def _get_client(self) -> object:
        if self._client is None:
            created = self._client_factory()
            self._client = await created if asyncio.iscoroutine(created) else created
        return self._client

    async def _get_market_client(self) -> object:
        if self._market_client is None:
            if self._market_client_factory is self._client_factory and self._client is not None:
                self._market_client = self._client
            else:
                created = self._market_client_factory()
                self._market_client = await created if asyncio.iscoroutine(created) else created
        return self._market_client

    @staticmethod
    def _aligned_to_tick(price: Decimal, tick_size: Decimal) -> bool:
        if tick_size <= Decimal("0"):
            return False
        return price % tick_size == Decimal("0")

    @staticmethod
    def _market_fee_schedule(market: object, *, market_id: str) -> ExecutionFeeSchedule:
        trading = _field(market, "trading")
        fees_enabled = _optional_field(trading, "fees_enabled", None)
        fee_schedule = _optional_field(trading, "fee_schedule", None)
        if fee_schedule is None:
            if fees_enabled is False:
                return ExecutionFeeSchedule.fee_exempt(
                    reason=f"market {market_id} explicitly reports fees disabled",
                    source_ref=f"market:{market_id}",
                )
            raise VenuePolicyError(f"market {market_id} fee schedule is unavailable")
        rate = to_decimal(_field(fee_schedule, "rate"), name="fee_rate")
        exponent = to_decimal(_field(fee_schedule, "exponent"), name="fee_exponent")
        taker_only = bool(_field(fee_schedule, "taker_only"))
        if rate == Decimal("0"):
            return ExecutionFeeSchedule.fee_exempt(
                reason=f"market {market_id} explicitly reports a zero fee rate",
                source_ref=f"market:{market_id}",
            )
        return ExecutionFeeSchedule(rate=rate, exponent=exponent, taker_only=taker_only)

    @staticmethod
    def _market_token_ids(market: object) -> tuple[str, ...]:
        outcomes = _field(market, "outcomes")
        yes = _field(outcomes, "yes")
        no = _field(outcomes, "no")
        token_ids = tuple(
            token_id
            for token_id in (
                _optional_field(yes, "token_id", None),
                _optional_field(no, "token_id", None),
            )
            if isinstance(token_id, str) and token_id.strip()
        )
        if not token_ids:
            raise VenuePolicyError("market token identifiers are unavailable")
        return token_ids

    @staticmethod
    def _market_state(market: object) -> object:
        state = _optional_field(market, "state", None)
        if state is None:
            raise VenuePolicyError("market state is unavailable")
        return state

    async def _sign_order(
        self,
        *,
        client: object,
        order: VenueOrderSpec,
    ) -> object:
        if order.time_in_force in {TimeInForce.GTC, TimeInForce.GTD}:
            expiration = int(order.expires_at.timestamp()) if order.expires_at is not None else None
            return await cast(Any, client).create_limit_order(
                token_id=order.token_id,
                price=order.limit_price,
                size=order.quantity,
                side=order.side.value,
                post_only=order.post_only,
                expiration=expiration,
            )
        if order.side is OrderSide.BUY:
            notional = order.quantity * order.limit_price
            return await cast(Any, client).create_market_order(
                token_id=order.token_id,
                side=order.side.value,
                amount=notional,
                max_spend=notional,
                max_price=order.limit_price,
                order_type=order.time_in_force.value,
            )
        return await cast(Any, client).create_market_order(
            token_id=order.token_id,
            side=order.side.value,
            shares=order.quantity,
            min_price=order.limit_price,
            order_type=order.time_in_force.value,
        )

    @staticmethod
    def _allowance_snapshot(allowance: object) -> tuple[Decimal, dict[str, Decimal]]:
        balance = to_decimal(_field(allowance, "balance"), name="balance") / Decimal("1000000")
        raw_allowances = _field(allowance, "allowances")
        if not isinstance(raw_allowances, Mapping):
            raise VenuePolicyError("allowance map is unavailable")
        allowances = {
            str(spender): to_decimal(value, name="allowance") / Decimal("1000000")
            for spender, value in raw_allowances.items()
        }
        return balance, allowances

    @staticmethod
    def _allowance_floor(allowances: Mapping[str, Decimal], *, signed_order: object) -> Decimal:
        if not allowances:
            raise VenuePolicyError("allowance map is empty")
        explicit = _optional_field(signed_order, "exchange_address", None)
        if explicit is None:
            explicit = _optional_field(signed_order, "exchange", None)
        if isinstance(explicit, str) and explicit.strip():
            explicit_lower = explicit.casefold()
            for spender in allowances:
                if spender.casefold() == explicit_lower:
                    return allowances[spender]
        return min(allowances.values())

    def _known_live_order_ids(self) -> tuple[str, ...]:
        return tuple(
            order_id
            for order_id, state in self._orders_by_venue_id.items()
            if state.status
            in {
                OrderLifecycleStatus.LIVE,
                OrderLifecycleStatus.DELAYED,
                OrderLifecycleStatus.PARTIALLY_FILLED,
            }
        )

    async def _load_live_market_evidence(
        self,
        request: VenuePreflightRequest,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, ExecutionFeeSchedule]]:
        market_client = await self._get_market_client()
        list_markets = getattr(market_client, "list_markets", None)
        get_order_book = getattr(market_client, "get_order_book", None)
        if not callable(list_markets):
            raise VenuePolicyError("market metadata client does not expose list_markets")
        if not callable(get_order_book):
            raise VenuePolicyError("market metadata client does not expose get_order_book")

        market_ids = tuple(dict.fromkeys(order.market_id for order in request.orders))
        token_ids = tuple(dict.fromkeys(order.token_id for order in request.orders))
        market_docs: dict[str, object] = {}
        order_books: dict[str, object] = {}
        fee_schedules: dict[str, ExecutionFeeSchedule] = {}
        paginator = cast(Any, list_markets)(
            condition_ids=market_ids,
            page_size=max(len(market_ids), 1),
        )
        async for market in _iter_items(paginator):
            condition_id = str(_field(market, "condition_id"))
            if condition_id not in market_ids or condition_id in market_docs:
                continue
            market_docs[condition_id] = market
            fee_schedules[condition_id] = self._market_fee_schedule(
                market,
                market_id=condition_id,
            )
        missing_markets = tuple(
            market_id for market_id in market_ids if market_id not in market_docs
        )
        if missing_markets:
            raise VenuePolicyError(
                f"market metadata is unavailable for {', '.join(missing_markets)}"
            )

        for token_id in token_ids:
            order_books[token_id] = await cast(Any, get_order_book)(token_id=token_id)

        return market_docs, order_books, fee_schedules

    async def preflight(self, request: VenuePreflightRequest) -> VenuePreflightResult:
        client = await self._get_client()
        market_docs, order_books, fee_schedules = await self._load_live_market_evidence(request)
        close_only = bool(await cast(Any, client).get_closed_only_mode())
        allowance_cache: dict[tuple[str, str | None], tuple[Decimal, dict[str, Decimal]]] = {}
        balance_requirements: dict[tuple[str, str | None], Decimal] = {}
        allowance_requirements: dict[tuple[str, str | None], Decimal] = {}
        cash_available = Decimal("0")
        allowance_available = Decimal("0")

        def allowance_key(asset_type: str, token_id: str | None) -> tuple[str, str | None]:
            return (asset_type, token_id)

        for order in request.orders:
            market = market_docs[order.market_id]
            state = self._market_state(market)
            book = order_books[order.token_id]
            market_condition_id = str(_field(market, "condition_id"))
            market_neg_risk = _optional_field(state, "neg_risk", None)
            if not isinstance(market_neg_risk, bool):
                raise VenuePolicyError("market neg_risk state is unavailable")
            if _optional_field(state, "active", None) is not True:
                raise VenuePolicyError("market is not active")
            if _optional_field(state, "closed", None) is not False:
                raise VenuePolicyError("market is closed")
            if _optional_field(state, "archived", None) is not False:
                raise VenuePolicyError("market is archived")
            if _optional_field(state, "accepting_orders", None) is not True:
                raise VenuePolicyError("market is not accepting orders")
            if _optional_field(state, "enable_order_book", None) is not True:
                raise VenuePolicyError("market order book is disabled")

            market_tokens = self._market_token_ids(market)
            if order.token_id not in market_tokens:
                raise VenuePolicyError("order token does not belong to market metadata")

            now = max(request.now.astimezone(UTC), self._clock().astimezone(UTC))
            book_timestamp = _coerce_datetime(_field(book, "timestamp"), name="timestamp")
            age_ms = int((now - book_timestamp).total_seconds() * 1000)
            if age_ms < 0:
                raise MarketDataStaleError("order book timestamp is in the future")
            if age_ms > request.qualification.risk_limits.max_market_data_age_ms:
                raise MarketDataStaleError("order book timestamp is stale")
            book_tick = _required_decimal_field(book, "tick_size")
            book_min_order_size = _required_decimal_field(book, "min_order_size")
            trading = _field(market, "trading")
            market_tick = _required_decimal_field(trading, "minimum_tick_size")
            market_min_order_size = _required_decimal_field(trading, "minimum_order_size")
            if book_tick != market_tick:
                raise VenuePolicyError("order book tick size does not match market metadata")
            if book_min_order_size != market_min_order_size:
                raise VenuePolicyError("order book minimum size does not match market metadata")
            if not self._aligned_to_tick(order.limit_price, book_tick):
                raise VenuePolicyError("order limit price is not aligned to the tick size")
            if order.quantity < book_min_order_size:
                raise VenuePolicyError("order quantity is below the market minimum order size")
            if str(_field(book, "condition_id")) != market_condition_id:
                raise VenuePolicyError("order book condition does not match market metadata")
            if str(_field(book, "market")) != market_condition_id:
                raise VenuePolicyError("order book market does not match market metadata")
            book_neg_risk = _field(book, "neg_risk")
            if not isinstance(book_neg_risk, bool):
                raise VenuePolicyError("order book neg_risk flag is invalid")
            if book_neg_risk is not market_neg_risk:
                raise VenuePolicyError("order book neg_risk does not match market metadata")

            signed_order = await self._sign_order(client=client, order=order)
            requirement_key: tuple[str, str | None]
            if order.side is OrderSide.BUY:
                key = allowance_key("COLLATERAL", None)
                if key not in allowance_cache:
                    allowance_cache[key] = self._allowance_snapshot(
                        await cast(Any, client).get_balance_allowance(asset_type="COLLATERAL")
                    )
                balance, allowances = allowance_cache[key]
                cash_available = balance
                available_allowance = self._allowance_floor(allowances, signed_order=signed_order)
                allowance_available = available_allowance
                requirement_key = ("COLLATERAL", None)
                required_amount = order.quantity * order.limit_price
            else:
                key = allowance_key("CONDITIONAL", order.token_id)
                if key not in allowance_cache:
                    allowance_cache[key] = self._allowance_snapshot(
                        await cast(Any, client).get_balance_allowance(
                            asset_type="CONDITIONAL",
                            token_id=order.token_id,
                        )
                    )
                balance, allowances = allowance_cache[key]
                available_allowance = self._allowance_floor(allowances, signed_order=signed_order)
                requirement_key = ("CONDITIONAL", order.token_id)
                required_amount = order.quantity
            balance_requirements[requirement_key] = (
                balance_requirements.get(
                    requirement_key,
                    Decimal("0"),
                )
                + required_amount
            )
            allowance_requirements[requirement_key] = (
                allowance_requirements.get(
                    requirement_key,
                    Decimal("0"),
                )
                + required_amount
            )
            if balance < balance_requirements[requirement_key]:
                raise AllowanceError("venue balance is insufficient")
            if available_allowance < allowance_requirements[requirement_key]:
                raise AllowanceError("venue allowance is insufficient")

        self.cash_available = cash_available
        self.allowance_available = allowance_available
        self.close_only = close_only
        if (
            request.risk_context.market_data_age_ms
            > request.qualification.risk_limits.max_market_data_age_ms
        ):
            raise MarketDataStaleError("market data is stale")
        if close_only:
            raise CloseOnlyError("venue is close-only")
        if self.geoblocked:
            raise GeoblockError("venue is geoblocked")
        self._fee_schedules_by_market_id = fee_schedules
        return VenuePreflightResult(
            ok=True,
            checks=(
                PreflightCheck(
                    name="geoblock",
                    ok=True,
                    detail="venue geoblock allows new orders",
                ),
                PreflightCheck(
                    name="close_only",
                    ok=True,
                    detail="venue accepts opening orders",
                ),
                PreflightCheck(
                    name="market_data_age",
                    ok=True,
                    detail="market metadata and order books are fresh",
                ),
                PreflightCheck(
                    name="allowance",
                    ok=True,
                    detail="asset balances and allowances cover requested orders",
                ),
            ),
            as_of=request.now.astimezone(UTC),
            close_only=close_only,
            geoblocked=self.geoblocked,
            allowance_available=allowance_available,
            cash_available=cash_available,
        )

    async def submit(
        self,
        *,
        orders: Sequence[VenueOrderSpec],
        mode: ExecutionMode,
        idempotency_key: str,
    ) -> VenueSubmitBatch:
        del mode
        self._remember_submit_payload(idempotency_key, orders)
        previous = self._submit_results.get(idempotency_key)
        if previous is not None:
            return previous
        client = await self._get_client()
        # Signing is a local, pre-mutation operation.  A signing failure cannot
        # mean that an order reached the venue and must never be persisted as an
        # ambiguous acknowledgement.
        try:
            signed_orders = [await self._sign_order(client=client, order=order) for order in orders]
        except Exception:  # noqa: BLE001
            # SDK exceptions may embed credentials or signed payloads.  Keep the
            # public error deterministic and detached from the sensitive cause.
            raise VenuePolicyError("venue order signing failed before submission") from None
        try:
            responses = (
                (await cast(Any, client).post_orders(signed_orders))
                if len(signed_orders) > 1
                else (await cast(Any, client).post_order(signed_orders[0]),)
            )
        except Exception as exc:  # noqa: BLE001
            retry_after = _extract_retry_after(exc)
            if retry_after is not None or _retryable_http_status(exc):
                raise VenuePolicyError(
                    "venue rejected submission with a retryable policy response",
                    retry_after_seconds=retry_after,
                ) from exc
            http_status = _http_status(exc)
            if http_status is not None and http_status != 503:
                raise
            ambiguous = VenueSubmitBatch(
                results=tuple(
                    VenueSubmitResult(
                        client_order_id=order.client_order_id,
                        status=OrderLifecycleStatus.AMBIGUOUS,
                    )
                    for order in orders
                ),
                submitted_at=self._clock(),
            )
            self._submit_results[idempotency_key] = ambiguous
            return ambiguous
        now = self._clock()
        results: list[VenueSubmitResult] = []
        for order, response in zip(orders, responses, strict=True):
            ok = bool(_field(response, "ok"))
            if not ok:
                results.append(
                    VenueSubmitResult(
                        client_order_id=order.client_order_id,
                        status=OrderLifecycleStatus.REJECTED,
                        reject_reason=str(_optional_field(response, "message", "rejected")),
                    )
                )
                continue
            venue_order_id = str(_field(response, "order_id"))
            status = _status_from_wire(
                str(_field(response, "status")),
                filled=Decimal("0"),
                quantity=order.quantity,
            )
            result = VenueSubmitResult(
                client_order_id=order.client_order_id,
                status=status,
                venue_order_id=venue_order_id,
            )
            results.append(result)
            state = VenueOrderState(
                venue_order_id=venue_order_id,
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                quantity=order.quantity,
                limit_price=order.limit_price,
                filled_quantity=Decimal("0"),
                status=status,
                time_in_force=order.time_in_force,
                post_only=order.post_only,
                expires_at=order.expires_at,
                updated_at=now,
            )
            self._store_order(state)
        batch = VenueSubmitBatch(results=tuple(results), submitted_at=now)
        self._submit_results[idempotency_key] = batch
        return batch

    async def cancel(
        self,
        *,
        order_ids: Sequence[str],
        idempotency_key: str,
        reason: str | None = None,
    ) -> VenueCancelResult:
        del reason
        self._remember_cancel_payload(idempotency_key, order_ids)
        previous = self._cancel_results.get(idempotency_key)
        if previous is not None:
            return previous
        client = await self._get_client()
        try:
            if not order_ids:
                response = await cast(Any, client).cancel_all()
            elif len(order_ids) == 1:
                response = await cast(Any, client).cancel_order(order_id=order_ids[0])
            else:
                response = await cast(Any, client).cancel_orders(order_ids=tuple(order_ids))
        except Exception as exc:  # noqa: BLE001
            retry_after = _extract_retry_after(exc)
            if retry_after is not None or _retryable_http_status(exc):
                raise VenuePolicyError(
                    "venue rejected cancel with a retryable policy response",
                    retry_after_seconds=retry_after,
                ) from exc
            if _http_status(exc) == 503:
                unresolved_ids = (
                    tuple(order_ids)
                    if order_ids
                    else self._known_live_order_ids()
                    or (self._cancel_all_outcome_unknown_sentinel,)
                )
                result = VenueCancelResult(
                    canceled_ids=(),
                    unresolved_ids=unresolved_ids,
                    canceled_at=self._clock(),
                )
                self._cancel_results[idempotency_key] = result
                return result
            raise
        now = self._clock()
        canceled_ids = tuple(str(order_id) for order_id in _field(response, "canceled"))
        unresolved_ids = tuple(
            str(order_id) for order_id in _field(response, "not_canceled").keys()
        )
        result = VenueCancelResult(
            canceled_ids=canceled_ids,
            unresolved_ids=unresolved_ids,
            canceled_at=now,
        )
        self._cancel_results[idempotency_key] = result
        for order_id in canceled_ids:
            state = self._orders_by_venue_id.get(order_id)
            if state is None:
                continue
            self._orders_by_venue_id[order_id] = VenueOrderState(
                venue_order_id=state.venue_order_id,
                client_order_id=state.client_order_id,
                market_id=state.market_id,
                token_id=state.token_id,
                side=state.side,
                quantity=state.quantity,
                limit_price=state.limit_price,
                filled_quantity=state.filled_quantity,
                status=OrderLifecycleStatus.CANCELED,
                time_in_force=state.time_in_force,
                post_only=state.post_only,
                expires_at=state.expires_at,
                updated_at=now,
            )
        return result

    async def reconcile(
        self,
        *,
        open_order_ids: Sequence[str] | None = None,
        since_trade_id: str | None = None,
    ) -> VenueReconciliation:
        client = await self._get_client()
        open_orders_iter = await cast(Any, client).list_open_orders()
        open_orders: list[VenueOrderState] = []
        discrepancies: list[str] = []
        async for item in _iter_items(open_orders_iter):
            venue_order_id = str(_field(item, "id"))
            if open_order_ids is not None and venue_order_id not in set(open_order_ids):
                continue
            quantity = to_decimal(_field(item, "original_size"), name="original_size")
            filled = to_decimal(_field(item, "size_matched"), name="size_matched")
            state = VenueOrderState(
                venue_order_id=venue_order_id,
                client_order_id=self._orders_by_venue_id.get(
                    venue_order_id,
                    VenueOrderState(
                        venue_order_id=venue_order_id,
                        client_order_id=venue_order_id,
                        market_id=str(_field(item, "market")),
                        token_id=str(_field(item, "token_id")),
                        side=OrderSide(str(_field(item, "side")).upper()),
                        quantity=quantity,
                        limit_price=to_decimal(_field(item, "price"), name="price"),
                        filled_quantity=filled,
                        status=_status_from_wire(
                            str(_field(item, "status")), filled=filled, quantity=quantity
                        ),
                        time_in_force=TimeInForce(
                            str(_field(item, "order_type")).upper().replace("IOC", "FAK")
                        ),
                    ),
                ).client_order_id,
                market_id=str(_field(item, "market")),
                token_id=str(_field(item, "token_id")),
                side=OrderSide(str(_field(item, "side")).upper()),
                quantity=quantity,
                limit_price=to_decimal(_field(item, "price"), name="price"),
                filled_quantity=filled,
                status=_status_from_wire(
                    str(_field(item, "status")), filled=filled, quantity=quantity
                ),
                time_in_force=TimeInForce(
                    str(_field(item, "order_type")).upper().replace("IOC", "FAK")
                ),
                updated_at=self._clock(),
            )
            self._store_order(state)
            open_orders.append(state)
        trades_iter = await cast(Any, client).list_account_trades(after=since_trade_id)
        async for item in _iter_items(trades_iter):
            try:
                fills = self._trade_fills_from_payload(
                    item,
                    wallet_address=str(getattr(client, "wallet", "")) or None,
                )
            except VenuePolicyError as exc:
                discrepancies.append(str(exc))
                continue
            for fill in fills:
                self._store_fill(fill)
        return VenueReconciliation(
            open_orders=tuple(sorted(open_orders, key=lambda state: state.venue_order_id)),
            fills=tuple(sorted(self._fills_by_id.values(), key=lambda fill: fill.fill_id)),
            as_of=self._clock(),
            cash_balance=self.cash_available,
            allowance_available=self.allowance_available,
            close_only=self.close_only,
            geoblocked=self.geoblocked,
            backfill_complete=True,
            discrepancies=tuple(discrepancies),
        )

    def stream_user_events(
        self,
        *,
        markets: Sequence[str] | None = None,
        since_cursor: str | None = None,
    ) -> AsyncIterator[VenueUserEvent]:
        async def generator() -> AsyncIterator[VenueUserEvent]:
            if since_cursor is not None:
                await self.reconcile(since_trade_id=since_cursor)
            else:
                await self.reconcile()
            client = await self._get_client()
            from polymarket.models.clob.user_events import parse_user_event
            from polymarket.streams import UserSpec

            handle = await cast(Any, client).subscribe(
                UserSpec(markets=tuple(markets) if markets else None)
            )
            now = self._clock()
            yield VenueUserEvent(
                kind=VenueEventKind.BACKFILL,
                occurred_at=now,
                cursor=self._next_cursor(),
                detail="live_backfill_complete",
            )
            yield self._heartbeat_event(
                occurred_at=now,
                detail="live_backfill_heartbeat",
            )
            last_signal_at = self._clock()
            iterator = cast(AsyncIterator[object], handle).__aiter__()
            while True:
                try:
                    raw_event = await asyncio.wait_for(
                        anext(iterator),
                        timeout=max(float(self.max_user_stream_gap_seconds), 0.0),
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    now = self._clock()
                    gap_seconds = max(
                        int((now - last_signal_at).total_seconds()),
                        self.max_user_stream_gap_seconds,
                    )
                    yield self._gap_event(
                        occurred_at=now,
                        gap_seconds=gap_seconds,
                        detail="live_user_stream_timeout",
                    )
                    break
                parsed = parse_user_event(raw_event)
                now = self._clock()
                gap_seconds = int((now - last_signal_at).total_seconds())
                if gap_seconds > 0:
                    yield self._heartbeat_event(
                        occurred_at=now,
                        detail="live_user_stream_poll",
                    )
                if gap_seconds > self.max_user_stream_gap_seconds:
                    yield self._gap_event(
                        occurred_at=now,
                        gap_seconds=gap_seconds,
                        detail="live_user_stream_gap",
                    )
                if parsed.type == "order":
                    payload = parsed.payload
                    quantity = to_decimal(payload.original_size, name="original_size")
                    filled = to_decimal(payload.size_matched, name="size_matched")
                    order_type = str(payload.order_type or "GTC").upper().replace("IOC", "FAK")
                    state = VenueOrderState(
                        venue_order_id=str(payload.id),
                        client_order_id=self._orders_by_venue_id.get(
                            str(payload.id),
                            VenueOrderState(
                                venue_order_id=str(payload.id),
                                client_order_id=str(payload.id),
                                market_id=str(payload.market),
                                token_id=str(payload.token_id),
                                side=OrderSide(str(payload.side).upper()),
                                quantity=quantity,
                                limit_price=to_decimal(payload.price, name="price"),
                                filled_quantity=filled,
                                status=_status_from_wire(
                                    str(payload.status or "LIVE"),
                                    filled=filled,
                                    quantity=quantity,
                                ),
                                time_in_force=TimeInForce(order_type),
                            ),
                        ).client_order_id,
                        market_id=str(payload.market),
                        token_id=str(payload.token_id),
                        side=OrderSide(str(payload.side).upper()),
                        quantity=quantity,
                        limit_price=to_decimal(payload.price, name="price"),
                        filled_quantity=filled,
                        status=_status_from_wire(
                            str(payload.status or "LIVE"),
                            filled=filled,
                            quantity=quantity,
                        ),
                        time_in_force=TimeInForce(order_type),
                        updated_at=now,
                    )
                    self._store_order(state)
                    yield VenueUserEvent(
                        kind=VenueEventKind.ORDER,
                        occurred_at=now,
                        cursor=self._next_cursor(),
                        order=state,
                    )
                else:
                    payload = parsed.payload
                    for fill in self._trade_fills_from_payload(
                        payload,
                        wallet_address=str(getattr(client, "wallet", "")) or None,
                        fallback_occurred_at=now,
                    ):
                        self._store_fill(fill)
                        yield VenueUserEvent(
                            kind=VenueEventKind.TRADE,
                            occurred_at=fill.occurred_at,
                            cursor=self._next_cursor(),
                            fill=fill,
                        )
                last_signal_at = now
                yield self._heartbeat_event(
                    occurred_at=now,
                    detail="live_user_stream_activity",
                )

        return generator()

    def _trade_fills_from_payload(
        self,
        payload: object,
        *,
        wallet_address: str | None,
        fallback_occurred_at: datetime | None = None,
    ) -> tuple[FillRecord, ...]:
        trade_id = str(_field(payload, "id"))
        market_id = str(_field(payload, "market"))
        schedule = self._fee_schedules_by_market_id.get(market_id)
        trader_side = str(_field(payload, "trader_side")).upper()
        occurred_at = _coerce_datetime(
            _optional_field(payload, "matched_at", fallback_occurred_at),
            name="matched_at",
        )
        raw_status = str(_field(payload, "status"))
        wallet_key = (
            wallet_address.casefold()
            if isinstance(wallet_address, str) and wallet_address
            else None
        )
        trade_owner = str(_optional_field(payload, "owner", "")).casefold()
        trade_maker_address = str(_optional_field(payload, "maker_address", "")).casefold()
        if trader_side == "TAKER":
            price = to_decimal(_field(payload, "price"), name="price")
            quantity = to_decimal(_field(payload, "size"), name="size")
            return (
                FillRecord(
                    fill_id=trade_id,
                    venue_order_id=str(_field(payload, "taker_order_id")),
                    market_id=market_id,
                    token_id=str(_field(payload, "token_id")),
                    side=OrderSide(str(_field(payload, "side")).upper()),
                    price=price,
                    quantity=quantity,
                    fee=_trade_fee_from_payload(
                        payload,
                        quantity=quantity,
                        price=price,
                        fee_schedule=schedule,
                        maker=False,
                    ),
                    occurred_at=occurred_at,
                    raw_status=raw_status,
                ),
            )
        if trader_side != "MAKER":
            raise VenuePolicyError(f"unsupported trader_side {trader_side!r} for trade {trade_id}")
        raw_maker_orders = _optional_field(payload, "maker_orders", None)
        if raw_maker_orders is None:
            raise VenuePolicyError("maker trade is missing maker_orders")
        maker_orders = list(raw_maker_orders)
        owned: list[object] = []
        for maker_order in maker_orders:
            maker_order_owner = str(_optional_field(maker_order, "owner", "")).casefold()
            maker_order_address = str(_optional_field(maker_order, "maker_address", "")).casefold()
            if wallet_key is not None and (
                maker_order_owner == wallet_key or maker_order_address == wallet_key
            ):
                owned.append(maker_order)
                continue
            if maker_order_owner and maker_order_owner == trade_owner:
                owned.append(maker_order)
                continue
            if maker_order_address and maker_order_address == trade_maker_address:
                owned.append(maker_order)
        if not owned:
            raise VenuePolicyError("unable to identify account-owned maker order legs")
        fills: list[FillRecord] = []
        for maker_order in owned:
            order_id = str(_field(maker_order, "order_id"))
            price = to_decimal(_field(maker_order, "price"), name="price")
            quantity = to_decimal(_field(maker_order, "matched_amount"), name="matched_amount")
            fills.append(
                FillRecord(
                    fill_id=f"{trade_id}:{order_id}",
                    venue_order_id=order_id,
                    market_id=market_id,
                    token_id=str(_field(maker_order, "token_id")),
                    side=OrderSide(str(_field(maker_order, "side")).upper()),
                    price=price,
                    quantity=quantity,
                    fee=_trade_fee_from_payload(
                        maker_order,
                        quantity=quantity,
                        price=price,
                        fee_schedule=schedule,
                        maker=True,
                    ),
                    occurred_at=occurred_at,
                    raw_status=raw_status,
                )
            )
        return tuple(fills)


def _extract_retry_after(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping) and "Retry-After" in headers:
        try:
            return int(headers["Retry-After"])
        except (TypeError, ValueError):
            return None
    return None


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    for attribute in ("status_code", "status"):
        raw_status = getattr(response, attribute, None)
        if isinstance(raw_status, int):
            return raw_status
    return None


def _retryable_http_status(exc: BaseException) -> bool:
    return _http_status(exc) in {425, 429}


def _trade_fee_from_payload(
    payload: object,
    *,
    quantity: Decimal,
    price: Decimal,
    fee_schedule: ExecutionFeeSchedule | None,
    maker: bool,
) -> Decimal:
    explicit_fee = _optional_field(payload, "fee", None)
    if explicit_fee is None:
        explicit_fee = _optional_field(payload, "fee_amount", None)
    if explicit_fee is not None:
        return to_decimal(explicit_fee, name="fee")
    if fee_schedule is None:
        raise VenuePolicyError(
            "trade fill fee evidence is missing; reconciliation must fail closed"
        )
    return cast(Decimal, fee_schedule.fee(quantity, price, maker=maker))
