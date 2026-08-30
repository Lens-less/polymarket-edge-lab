from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.profit_system.execution import (
    ExecutionMode,
    IdempotencyConflictError,
    OrderLifecycleStatus,
    OrderSide,
    QualificationLevel,
    QualificationRecord,
    RiskContext,
    RiskLimits,
    TimeInForce,
    VenueEventKind,
    VenueOrderSpec,
    VenuePolicyError,
    VenuePreflightRequest,
)
from src.profit_system.execution.adapters import PolymarketLiveVenueAdapter


def _limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("100"),
        max_market_exposure=Decimal("100"),
        max_event_exposure=Decimal("100"),
        max_strategy_exposure=Decimal("100"),
        max_total_exposure=Decimal("100"),
        max_daily_loss=Decimal("20"),
        max_drawdown=Decimal("20"),
        max_open_orders=10,
        max_unmatched_leg_seconds=30,
        max_market_data_age_ms=500,
        max_user_stream_gap_seconds=30,
        max_order_rate=5,
    )


def _qualification() -> QualificationRecord:
    return QualificationRecord(
        strategy_id="strat-1",
        qualification=QualificationLevel.PROBE_ALLOWED,
        allowed_modes=(ExecutionMode.LIVE_CANARY,),
        market_whitelist=("market-1",),
        risk_limits=_limits(),
        evidence_digest="digest-1",
    )


def _request(*orders: VenueOrderSpec) -> VenuePreflightRequest:
    return VenuePreflightRequest(
        mode=ExecutionMode.LIVE_CANARY,
        qualification=_qualification(),
        orders=orders or (_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
        idempotency_key="preflight-1",
        now=datetime.now(tz=UTC),
    )


def _order(
    *,
    client_order_id: str = "client-1",
    tif: TimeInForce = TimeInForce.GTC,
    side: OrderSide = OrderSide.BUY,
    token_id: str = "token-1",
    quantity: Decimal = Decimal("2"),
    limit_price: Decimal = Decimal("0.40"),
) -> VenueOrderSpec:
    expires_at = datetime.now(tz=UTC) + timedelta(minutes=5) if tif is TimeInForce.GTD else None
    return VenueOrderSpec(
        client_order_id=client_order_id,
        strategy_id="strat-1",
        market_id="market-1",
        token_id=token_id,
        event_id="event-1",
        side=side,
        quantity=quantity,
        limit_price=limit_price,
        time_in_force=tif,
        expires_at=expires_at,
    )


@dataclass
class _Allowance:
    balance: int
    allowances: dict[str, int]


class _AsyncList:
    def __init__(self, items) -> None:
        self._items = tuple(items)

    def __aiter__(self) -> AsyncIterator[object]:
        async def iterate() -> AsyncIterator[object]:
            for item in self._items:
                yield item

        return iterate()

    def iter_items(self) -> AsyncIterator[object]:
        return self.__aiter__()


def _market(
    *,
    minimum_order_size: Decimal | None = Decimal("1"),
    minimum_tick_size: Decimal | None = Decimal("0.01"),
    neg_risk: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="gamma-market-1",
        condition_id="market-1",
        state=SimpleNamespace(
            active=True,
            closed=False,
            archived=False,
            accepting_orders=True,
            enable_order_book=True,
            neg_risk=neg_risk,
        ),
        outcomes=SimpleNamespace(
            yes=SimpleNamespace(token_id="token-1"),
            no=SimpleNamespace(token_id="token-2"),
        ),
        trading=SimpleNamespace(
            minimum_order_size=minimum_order_size,
            minimum_tick_size=minimum_tick_size,
            fees_enabled=True,
            fee_schedule=SimpleNamespace(
                rate=Decimal("0.35"),
                exponent=Decimal("1"),
                taker_only=True,
            ),
        ),
    )


class _LiveClient:
    def __init__(self) -> None:
        self.wallet = "0xwallet"
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.market = _market()

    async def get_closed_only_mode(self) -> bool:
        self.calls.append(("get_closed_only_mode", None))
        return False

    async def get_balance_allowance(
        self,
        *,
        asset_type: str,
        token_id: str | None = None,
    ) -> _Allowance:
        self.calls.append(
            ("get_balance_allowance", {"asset_type": asset_type, "token_id": token_id})
        )
        if asset_type == "CONDITIONAL":
            return _Allowance(balance=4_000_000, allowances={"0xexchange-a": 8_000_000})
        return _Allowance(
            balance=25_000_000,
            allowances={"0xexchange-a": 25_000_000, "0xexchange-b": 30_000_000},
        )

    def list_markets(self, *, condition_ids, page_size: int):
        self.calls.append(
            ("list_markets", {"condition_ids": tuple(condition_ids), "page_size": page_size})
        )
        return _AsyncList((self.market,))

    async def get_order_book(self, *, token_id: str):
        self.calls.append(("get_order_book", {"token_id": token_id}))
        return SimpleNamespace(
            market="market-1",
            condition_id="market-1",
            token_id=token_id,
            timestamp=datetime.now(tz=UTC),
            bids=(),
            asks=(),
            min_order_size=Decimal("1"),
            tick_size=Decimal("0.01"),
            neg_risk=False,
        )

    async def create_limit_order(self, **kwargs):
        self.calls.append(("create_limit_order", kwargs))
        return {"signed": kwargs}

    async def create_market_order(self, **kwargs):
        self.calls.append(("create_market_order", kwargs))
        return {"signed": kwargs}

    async def post_order(self, signed_order):
        self.calls.append(("post_order", {"signed_order": signed_order}))
        return {"ok": True, "order_id": "venue-1", "status": "LIVE"}

    async def post_orders(self, signed_orders):
        self.calls.append(("post_orders", {"count": len(tuple(signed_orders))}))
        return (
            {"ok": True, "order_id": "venue-1", "status": "LIVE"},
            {"ok": False, "code": "REJECTED", "message": "crossed"},
        )

    async def cancel_orders(self, *, order_ids):
        self.calls.append(("cancel_orders", {"order_ids": tuple(order_ids)}))
        return {"canceled": tuple(order_ids), "not_canceled": {}}

    async def list_open_orders(self):
        return _AsyncList([])

    async def list_account_trades(self, *, after=None):
        del after
        return _AsyncList(
            [
                SimpleNamespace(
                    id="maker-trade-1",
                    trader_side="MAKER",
                    taker_order_id="taker-ignored",
                    market="market-1",
                    token_id="token-1",
                    side="BUY",
                    size=Decimal("1"),
                    price=Decimal("0.40"),
                    status="MATCHED",
                    matched_at=datetime.now(tz=UTC),
                    owner="0xother",
                    maker_address="0xother-maker",
                    maker_orders=(
                        SimpleNamespace(
                            order_id="venue-maker-1",
                            token_id="token-1",
                            side="SELL",
                            price=Decimal("0.41"),
                            matched_amount=Decimal("1"),
                            owner=self.wallet,
                            maker_address="0xwallet",
                        ),
                    ),
                )
            ]
        )

    async def subscribe(self, spec):
        self.calls.append(("subscribe", {"markets": getattr(spec, "markets", None)}))
        return _AsyncList(
            [
                {
                    "event_type": "trade",
                    "id": "fill-1",
                    "taker_order_id": "venue-1",
                    "trader_side": "TAKER",
                    "owner": "0xowner",
                    "maker_address": "0xmaker",
                    "market": "market-1",
                    "asset_id": "token-1",
                    "side": "BUY",
                    "size": "1",
                    "price": "0.40",
                    "status": "MATCHED",
                    "match_time": 1_785_208_414,
                }
            ]
        )


@pytest.mark.asyncio
async def test_live_adapter_lazy_loads_client_and_maps_limit_and_market_orders() -> None:
    client = _LiveClient()
    constructed = {"count": 0}

    def factory():
        constructed["count"] += 1
        return client

    adapter = PolymarketLiveVenueAdapter(client_factory=factory)
    assert constructed["count"] == 0

    preflight = await adapter.preflight(_request())
    assert preflight.ok is True
    assert constructed["count"] == 1

    limit_batch = await adapter.submit(
        orders=(_order(tif=TimeInForce.GTD),),
        mode=ExecutionMode.LIVE_CANARY,
        idempotency_key="submit-limit",
    )
    market_batch = await adapter.submit(
        orders=(
            _order(
                client_order_id="client-2",
                tif=TimeInForce.FAK,
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                limit_price=Decimal("0.44"),
            ),
        ),
        mode=ExecutionMode.LIVE_CANARY,
        idempotency_key="submit-market",
    )
    sell_batch = await adapter.submit(
        orders=(
            _order(
                client_order_id="client-3",
                tif=TimeInForce.FAK,
                side=OrderSide.SELL,
                token_id="token-2",
                quantity=Decimal("3"),
                limit_price=Decimal("0.55"),
            ),
        ),
        mode=ExecutionMode.LIVE_CANARY,
        idempotency_key="submit-sell-market",
    )

    assert limit_batch.results[0].status is OrderLifecycleStatus.LIVE
    assert market_batch.results[0].status is OrderLifecycleStatus.LIVE
    assert sell_batch.results[0].status is OrderLifecycleStatus.LIVE

    limit_calls = [kwargs for name, kwargs in client.calls if name == "create_limit_order"]
    market_calls = [kwargs for name, kwargs in client.calls if name == "create_market_order"]
    assert limit_calls[0]["size"] == Decimal("2")
    assert market_calls[0]["amount"] == Decimal("0.44")
    assert market_calls[0].get("shares") is None
    assert market_calls[0]["max_spend"] == Decimal("0.44")
    assert market_calls[0]["max_price"] == Decimal("0.44")
    assert market_calls[0].get("min_price") is None
    assert market_calls[1].get("amount") is None
    assert market_calls[1]["shares"] == Decimal("3")
    assert market_calls[1].get("max_spend") is None
    assert market_calls[1].get("max_price") is None
    assert market_calls[1]["min_price"] == Decimal("0.55")


@pytest.mark.asyncio
async def test_live_adapter_idempotency_binds_complete_order_economics() -> None:
    client = _LiveClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    await adapter.submit(
        orders=(_order(quantity=Decimal("2"), limit_price=Decimal("0.40")),),
        mode=ExecutionMode.LIVE_CANARY,
        idempotency_key="submit-complete-payload",
    )

    with pytest.raises(IdempotencyConflictError, match="different orders"):
        await adapter.submit(
            orders=(_order(quantity=Decimal("9"), limit_price=Decimal("0.41")),),
            mode=ExecutionMode.LIVE_CANARY,
            idempotency_key="submit-complete-payload",
        )

    assert sum(name == "post_order" for name, _ in client.calls) == 1


@pytest.mark.asyncio
async def test_live_adapter_rejects_unproven_single_maker_leg_ownership() -> None:
    client = _LiveClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)
    trades = await client.list_account_trades()
    payload = await anext(trades.__aiter__())

    with pytest.raises(VenuePolicyError, match="unable to identify account-owned maker"):
        adapter._trade_fills_from_payload(payload, wallet_address="0xnot-the-maker")


@pytest.mark.asyncio
async def test_live_adapter_preflight_uses_conservative_multi_spender_allowance_floor() -> None:
    client = _LiveClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    preflight = await adapter.preflight(_request())

    assert preflight.ok is True
    assert adapter.allowance_available == Decimal("25")


@pytest.mark.asyncio
async def test_live_adapter_preflight_uses_conditional_balances_for_sell_orders() -> None:
    client = _LiveClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    preflight = await adapter.preflight(
        _request(
            _order(
                side=OrderSide.SELL,
                token_id="token-2",
                tif=TimeInForce.FAK,
                quantity=Decimal("3"),
                limit_price=Decimal("0.51"),
            )
        )
    )

    assert preflight.ok is True
    assert (
        "get_balance_allowance",
        {"asset_type": "CONDITIONAL", "token_id": "token-2"},
    ) in client.calls


@pytest.mark.asyncio
async def test_live_adapter_aggregates_buy_requirements_across_two_orders() -> None:
    client = _LiveClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    with pytest.raises(VenuePolicyError, match="balance is insufficient"):
        await adapter.preflight(
            _request(
                _order(
                    client_order_id="client-1",
                    quantity=Decimal("30"),
                    limit_price=Decimal("0.60"),
                ),
                _order(
                    client_order_id="client-2",
                    quantity=Decimal("20"),
                    limit_price=Decimal("0.60"),
                ),
            )
        )


@pytest.mark.asyncio
async def test_live_adapter_aggregates_sell_requirements_across_two_orders() -> None:
    client = _LiveClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    with pytest.raises(VenuePolicyError, match="balance is insufficient"):
        await adapter.preflight(
            _request(
                _order(
                    client_order_id="client-1",
                    side=OrderSide.SELL,
                    token_id="token-2",
                    tif=TimeInForce.FAK,
                    quantity=Decimal("3"),
                    limit_price=Decimal("0.51"),
                ),
                _order(
                    client_order_id="client-2",
                    side=OrderSide.SELL,
                    token_id="token-2",
                    tif=TimeInForce.FAK,
                    quantity=Decimal("2"),
                    limit_price=Decimal("0.50"),
                ),
            )
        )


@pytest.mark.asyncio
async def test_live_adapter_rejects_market_token_mismatch_during_preflight() -> None:
    client = _LiveClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    with pytest.raises(VenuePolicyError, match="does not belong to market metadata"):
        await adapter.preflight(_request(_order(token_id="token-x")))


@pytest.mark.asyncio
async def test_live_adapter_rejects_missing_market_constraints_during_preflight() -> None:
    client = _LiveClient()
    client.market = _market(minimum_order_size=None)
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    with pytest.raises(VenuePolicyError, match="minimum_order_size"):
        await adapter.preflight(_request())


@pytest.mark.asyncio
async def test_live_adapter_batch_results_and_rate_limit_surface_cleanly() -> None:
    client = _LiveClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    batch = await adapter.submit(
        orders=(_order(client_order_id="client-1"), _order(client_order_id="client-2")),
        mode=ExecutionMode.LIVE_CANARY,
        idempotency_key="batch-1",
    )
    assert batch.results[0].status is OrderLifecycleStatus.LIVE
    assert batch.results[1].status is OrderLifecycleStatus.REJECTED

    class RetryAfterError(RuntimeError):
        def __init__(self) -> None:
            self.response = type("Response", (), {"headers": {"Retry-After": "7"}})()

    async def boom(*args, **kwargs):
        del args, kwargs
        raise RetryAfterError()

    client.cancel_orders = boom
    with pytest.raises(VenuePolicyError) as exc:
        await adapter.cancel(
            order_ids=("venue-1", "venue-2"),
            idempotency_key="cancel-1",
            reason="test",
        )
    assert exc.value.retry_after_seconds == 7


@pytest.mark.asyncio
async def test_live_adapter_reconciliation_maps_maker_trade_to_owned_order_leg() -> None:
    client = _LiveClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    await adapter.preflight(_request())
    reconciliation = await adapter.reconcile()

    assert reconciliation.fills[0].fill_id == "maker-trade-1:venue-maker-1"
    assert reconciliation.fills[0].venue_order_id == "venue-maker-1"
    assert reconciliation.fills[0].side is OrderSide.SELL


@pytest.mark.asyncio
async def test_live_adapter_stream_user_events_backfills_before_live_events() -> None:
    client = _LiveClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)
    await adapter.preflight(_request())

    events = [event async for event in adapter.stream_user_events(markets=("market-1",))]

    assert events[0].kind is VenueEventKind.BACKFILL
    assert events[1].kind is VenueEventKind.HEARTBEAT
    assert any(event.kind is VenueEventKind.TRADE for event in events)


@pytest.mark.asyncio
async def test_live_adapter_stream_times_out_silent_subscription_with_gap() -> None:
    class SilentStream:
        def __aiter__(self) -> SilentStream:
            return self

        async def __anext__(self) -> object:
            await asyncio.Future()
            raise StopAsyncIteration

    class SilentClient(_LiveClient):
        async def subscribe(self, spec):
            self.calls.append(("subscribe", {"markets": getattr(spec, "markets", None)}))
            return SilentStream()

    client = SilentClient()
    adapter = PolymarketLiveVenueAdapter(
        client_factory=lambda: client,
        max_user_stream_gap_seconds=0,
    )
    await adapter.preflight(_request())

    events = [event async for event in adapter.stream_user_events(markets=("market-1",))]

    assert [event.kind for event in events] == [
        VenueEventKind.BACKFILL,
        VenueEventKind.HEARTBEAT,
        VenueEventKind.GAP,
    ]


@pytest.mark.asyncio
async def test_live_adapter_stream_does_not_emit_health_before_subscribe_succeeds() -> None:
    class BrokenSubscribeClient(_LiveClient):
        async def subscribe(self, spec):
            del spec
            raise RuntimeError("subscribe failed")

    client = BrokenSubscribeClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)
    await adapter.preflight(_request())

    events: list[object] = []
    with pytest.raises(RuntimeError, match="subscribe failed"):
        async for event in adapter.stream_user_events(markets=("market-1",)):
            events.append(event)

    assert events == []
