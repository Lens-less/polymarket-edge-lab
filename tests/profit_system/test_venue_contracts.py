from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.profit_system.execution import (
    ExecutionMode,
    OrderLifecycleStatus,
    OrderSide,
    QualificationLevel,
    QualificationRecord,
    RiskContext,
    RiskLimits,
    TimeInForce,
    VenueEventKind,
    VenueOrderSpec,
    VenuePreflightRequest,
    VenueReconciliation,
    VenueSubmitBatch,
    VenueSubmitResult,
    VenueUserEvent,
)
from src.profit_system.execution.adapters import (
    PaperOrderBook,
    PaperVenueAdapter,
    PolymarketLiveVenueAdapter,
    ReplayVenueAdapter,
)


def _risk_limits() -> RiskLimits:
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
        allowed_modes=(
            ExecutionMode.REPLAY,
            ExecutionMode.PAPER,
            ExecutionMode.SHADOW,
            ExecutionMode.LIVE_CANARY,
        ),
        market_whitelist=("market-1",),
        risk_limits=_risk_limits(),
        evidence_digest="evidence-1",
    )


def _order(
    *,
    client_order_id: str = "client-1",
    tif: TimeInForce = TimeInForce.GTC,
    post_only: bool = False,
) -> VenueOrderSpec:
    expires_at = datetime.now(tz=UTC) + timedelta(minutes=2) if tif is TimeInForce.GTD else None
    return VenueOrderSpec(
        client_order_id=client_order_id,
        strategy_id="strat-1",
        market_id="market-1",
        token_id="token-1",
        event_id="event-1",
        side=OrderSide.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("0.45"),
        time_in_force=tif,
        post_only=post_only,
        expires_at=expires_at,
    )


def _request() -> VenuePreflightRequest:
    return VenuePreflightRequest(
        mode=ExecutionMode.PAPER,
        qualification=_qualification(),
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
        idempotency_key="preflight-1",
        now=datetime.now(tz=UTC),
    )


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


@dataclass
class _Allowance:
    balance: int
    allowances: dict[str, int]


@dataclass
class _OpenOrder:
    id: str
    market: str
    token_id: str
    side: str
    original_size: Decimal
    size_matched: Decimal
    price: Decimal
    order_type: str
    status: str


@dataclass
class _Accepted:
    ok: bool
    order_id: str
    status: str


def _market() -> SimpleNamespace:
    return SimpleNamespace(
        id="gamma-market-1",
        condition_id="market-1",
        state=SimpleNamespace(
            active=True,
            closed=False,
            archived=False,
            accepting_orders=True,
            enable_order_book=True,
            neg_risk=False,
        ),
        outcomes=SimpleNamespace(
            yes=SimpleNamespace(token_id="token-1"),
            no=SimpleNamespace(token_id="token-2"),
        ),
        trading=SimpleNamespace(
            minimum_order_size=Decimal("1"),
            minimum_tick_size=Decimal("0.01"),
            fees_enabled=True,
            fee_schedule=SimpleNamespace(
                rate=Decimal("0.35"),
                exponent=Decimal("1"),
                taker_only=True,
            ),
        ),
    )


class _FakeLiveClient:
    def __init__(self) -> None:
        self.wallet = "0xwallet"
        self.created_limit: list[dict[str, object]] = []
        self.subscribed = False

    async def get_closed_only_mode(self) -> bool:
        return False

    async def get_balance_allowance(
        self,
        *,
        asset_type: str,
        token_id: str | None = None,
    ) -> _Allowance:
        del token_id
        assert asset_type == "COLLATERAL"
        return _Allowance(
            balance=20_000_000,
            allowances={"0xexchange-a": 20_000_000, "0xexchange-b": 20_000_000},
        )

    def list_markets(self, *, condition_ids, page_size: int):
        del condition_ids, page_size
        return _AsyncList((_market(),))

    async def get_order_book(self, *, token_id: str):
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
        self.created_limit.append(kwargs)
        return {"signed": kwargs}

    async def post_order(self, signed_order):
        del signed_order
        return _Accepted(ok=True, order_id="venue-live-1", status="LIVE")

    async def cancel_order(self, *, order_id: str):
        return {"canceled": (order_id,), "not_canceled": {}}

    async def list_open_orders(self):
        return _AsyncList(
            [
                _OpenOrder(
                    id="venue-live-1",
                    market="market-1",
                    token_id="token-1",
                    side="BUY",
                    original_size=Decimal("2"),
                    size_matched=Decimal("0"),
                    price=Decimal("0.45"),
                    order_type="GTC",
                    status="LIVE",
                )
            ]
        )

    async def list_account_trades(self, *, after=None):
        del after
        return _AsyncList(
            [
                SimpleNamespace(
                    id="fill-1",
                    trader_side="TAKER",
                    taker_order_id="venue-live-1",
                    market="market-1",
                    token_id="token-1",
                    side="BUY",
                    price=Decimal("0.45"),
                    size=Decimal("1"),
                    status="MATCHED",
                    matched_at=datetime.now(tz=UTC),
                    owner="0xwallet",
                    maker_address="0xmaker",
                )
            ]
        )

    async def subscribe(self, spec):
        del spec
        self.subscribed = True
        return _AsyncList(
            [
                {
                    "event_type": "order",
                    "id": "venue-live-1",
                    "owner": "0xowner",
                    "market": "market-1",
                    "asset_id": "token-1",
                    "side": "BUY",
                    "original_size": "2",
                    "size_matched": "0",
                    "price": "0.45",
                    "status": "LIVE",
                    "type": "PLACEMENT",
                }
            ]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_name", ["replay", "paper", "live"])
async def test_venue_adapter_contracts_cover_preflight_submit_cancel_reconcile_and_stream(
    adapter_name: str,
) -> None:
    if adapter_name == "replay":
        adapter = ReplayVenueAdapter(
            scripted_results={
                "submit-1": VenueSubmitBatch(
                    results=(
                        VenueSubmitResult(
                            client_order_id="client-1",
                            status=OrderLifecycleStatus.LIVE,
                            venue_order_id="venue-replay-1",
                        ),
                    ),
                    submitted_at=datetime.now(tz=UTC),
                )
            },
            scripted_reconciliation=VenueReconciliation(
                open_orders=(),
                fills=(),
                as_of=datetime.now(tz=UTC),
            ),
            scripted_events=(
                VenueUserEvent(
                    kind=VenueEventKind.HEARTBEAT,
                    occurred_at=datetime.now(tz=UTC),
                    cursor="replay:heartbeat",
                    detail="heartbeat",
                ),
            ),
        )
    elif adapter_name == "paper":
        adapter = PaperVenueAdapter(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.40"),
                best_ask=Decimal("0.41"),
                bid_size=Decimal("5"),
                ask_size=Decimal("5"),
                as_of=datetime.now(tz=UTC),
            )
        )
    else:
        adapter = PolymarketLiveVenueAdapter(client_factory=_FakeLiveClient)

    preflight = await adapter.preflight(_request())
    assert preflight.ok is True

    batch = await adapter.submit(
        orders=(_order(),),
        mode=ExecutionMode.PAPER,
        idempotency_key="submit-1",
    )
    assert batch.results[0].client_order_id == "client-1"
    assert batch.results[0].status in {
        OrderLifecycleStatus.LIVE,
        OrderLifecycleStatus.FILLED,
    }

    cancel = await adapter.cancel(
        order_ids=tuple(result.venue_order_id for result in batch.results if result.venue_order_id),
        idempotency_key="cancel-1",
        reason="test",
    )
    assert isinstance(cancel.canceled_ids, tuple)

    reconciliation = await adapter.reconcile()
    assert isinstance(reconciliation.open_orders, tuple)
    assert isinstance(reconciliation.resolved_orders, tuple)
    assert isinstance(reconciliation.fills, tuple)

    events = [event async for event in adapter.stream_user_events(markets=("market-1",))]
    assert events[0].kind is VenueEventKind.BACKFILL
