from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src import orders, trading
from src.models import Order, OrderSide, OrderStatus, Trade
from src.risk.manager import RiskManager


class _Paginator:
    def __init__(self, items) -> None:
        self._items = tuple(items)

    def iter_items(self):
        yield from self._items


def _configure_live_adapters(monkeypatch, client) -> None:
    monkeypatch.setattr(trading, "DRY_RUN", False)
    monkeypatch.setattr(trading, "has_credentials", lambda: True)
    monkeypatch.setattr(orders, "DRY_RUN", False)
    monkeypatch.setattr(orders, "has_credentials", lambda: True)
    monkeypatch.setattr("src.client.get_auth_client", lambda: client)
    monkeypatch.setattr("src.auth.get_auth_client", lambda: client)


def test_cancel_all_still_sends_bulk_cancel_when_open_order_listing_raises(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Client:
        @staticmethod
        def list_open_orders(**kwargs):
            raise ConnectionError("REST down")

        @staticmethod
        def cancel_market_orders(**kwargs):
            calls.append("cancel_market_orders")
            return SimpleNamespace(canceled=("orphan-order",), not_canceled={})

    _configure_live_adapters(monkeypatch, Client())

    assert trading.cancel_all_orders("token-1") == 1
    assert calls
    assert set(calls) == {"cancel_market_orders"}


def test_cancel_all_still_sends_bulk_cancel_when_open_order_listing_is_empty(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Client:
        @staticmethod
        def list_open_orders(**kwargs):
            return _Paginator(())

        @staticmethod
        def cancel_market_orders(**kwargs):
            calls.append("cancel_market_orders")
            return SimpleNamespace(canceled=("eventually-consistent-order",), not_canceled={})

    _configure_live_adapters(monkeypatch, Client())

    assert trading.cancel_all_orders("token-1") == 1
    assert calls == ["cancel_market_orders"]


def test_get_position_fails_closed_when_live_balance_and_trade_apis_fail(
    monkeypatch,
) -> None:
    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            raise ConnectionError("trade REST down")

        @staticmethod
        def get_balance_allowance(**kwargs):
            raise ConnectionError("balance REST down")

    _configure_live_adapters(monkeypatch, Client())

    with pytest.raises(ConnectionError, match="REST down"):
        orders.get_position("token-1")


def _sdk_trade(
    trade_id: str,
    *,
    status: str,
    trader_side: str = "TAKER",
    side: str = "BUY",
    matched_at: datetime | None = None,
    maker_orders=(),
):
    return SimpleNamespace(
        id=trade_id,
        taker_order_id=f"taker-{trade_id}",
        token_id="token-1",
        side=side,
        trader_side=trader_side,
        price=Decimal("0.40"),
        size=Decimal(3),
        status=status,
        matched_at=matched_at or datetime(2026, 8, 20, tzinfo=UTC),
        maker_address="0xme",
        owner="owner-me",
        maker_orders=tuple(maker_orders),
    )


def test_get_trades_uses_owned_maker_order_and_filters_non_fills(monkeypatch) -> None:
    owned_maker_order = SimpleNamespace(
        order_id="maker-order-1",
        token_id="token-1",
        maker_address="0xme",
        owner="owner-me",
        side="BUY",
        price=Decimal("0.41"),
        matched_amount=Decimal(2),
    )
    items = (
        _sdk_trade(
            "maker-trade",
            status="CONFIRMED",
            trader_side="MAKER",
            side="SELL",
            maker_orders=(owned_maker_order,),
        ),
        _sdk_trade("failed-trade", status="FAILED"),
        _sdk_trade("retrying-trade", status="RETRYING"),
    )

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator(items)

    _configure_live_adapters(monkeypatch, Client())

    result = orders.get_trades("token-1", limit=None, raise_on_error=True)

    assert len(result) == 1
    assert result[0].side is OrderSide.BUY
    assert result[0].order_id == "maker-order-1"
    assert result[0].price == Decimal("0.41")
    assert result[0].size == Decimal(2)


def test_live_trade_sync_does_not_drop_the_101st_trade() -> None:
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="token-1")
    history = [
        Trade(
            id=f"trade-{index}",
            order_id=f"order-{index}",
            token_id="token-1",
            side=OrderSide.BUY,
            price=Decimal("0.40"),
            size=Decimal(1),
        )
        for index in range(101)
    ]
    mm._seen_trade_ids = {trade.id for trade in history[:100]}
    mm._record_trade_fill = MagicMock()

    def fake_get_trades(token_id, limit=50, raise_on_error=False):
        return history if limit is None else history[:limit]

    with patch("src.strategy.market_maker.get_trades", side_effect=fake_get_trades) as get_trades:
        mm._sync_live_trades()

    get_trades.assert_called_once_with("token-1", limit=None, raise_on_error=True)
    mm._record_trade_fill.assert_called_once_with(history[100], fill_type="live")


def _configure_quote_components(mm) -> None:
    mm.volatility.get_multiplier = MagicMock(return_value=1.0)
    mm.volatility.get_state = MagicMock(
        return_value=SimpleNamespace(level="NORMAL", realized_vol=0.0)
    )
    mm.inventory.get_state = MagicMock(
        return_value=SimpleNamespace(
            position=Decimal(0),
            position_pct=0.0,
            bid_skew=Decimal(0),
            ask_skew=Decimal(0),
            bid_size_mult=1.0,
            ask_size_mult=1.0,
            vwap_entry=None,
            unrealized_pnl=Decimal(0),
            inventory_level="NEUTRAL",
        )
    )
    mm.book_analyzer.analyze = MagicMock(
        return_value=SimpleNamespace(
            price_adjustment=Decimal(0),
            imbalance_signal="BALANCED",
        )
    )
    mm.event_tracker.get_signal = MagicMock(
        return_value=SimpleNamespace(
            should_trade=True,
            spread_multiplier=1.0,
            reason="normal",
        )
    )


def test_quote_calculation_preserves_arb_adjustment_and_uses_exchange_tick() -> None:
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(
        token_id="yes-token",
        complement_token_id="no-token",
        base_spread=Decimal("0.02"),
        min_spread=Decimal("0.001"),
        max_spread=Decimal("0.20"),
    )
    _configure_quote_components(mm)
    mm.arb_detector.get_quote_adjustment = MagicMock(
        return_value=(Decimal("0.4936"), Decimal("0.5074"))
    )

    with patch(
        "src.strategy.market_maker.get_tick_size",
        return_value=Decimal("0.001"),
        create=True,
    ):
        bid, ask, _state = mm._calculate_quotes(Decimal("0.50"), None)

    assert bid == Decimal("0.493")
    assert ask == Decimal("0.508")


def test_inventory_skew_moves_both_quotes_and_keeps_sub_cent_precision() -> None:
    from src.strategy.inventory import InventoryManager

    inventory = InventoryManager(
        token_id="token-1",
        position_limit=Decimal(100),
        skew_max=Decimal("0.02"),
    )

    long_bid, long_ask = inventory._calculate_skews(Decimal(5))
    short_bid, short_ask = inventory._calculate_skews(Decimal(-5))

    assert long_bid == long_ask == Decimal("-0.001")
    assert short_bid == short_ask == Decimal("0.001")


def test_kill_switch_can_scope_cancellation_to_one_strategy_token() -> None:
    manager = RiskManager(enforce=True)

    with patch("src.risk.manager.cancel_all_orders", return_value=2) as cancel:
        manager.kill_switch("strategy stop", token_id="token-1")

    cancel.assert_called_once_with("token-1", verify=True, raise_on_failure=True)


def test_balance_drop_stops_immediately_even_if_kill_switch_cancel_fails() -> None:
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="token-1")
    mm._initial_balance = Decimal(100)
    mm.stop = MagicMock()
    mm.risk.kill_switch = MagicMock(side_effect=RuntimeError("cancel verification failed"))

    with (
        patch("src.strategy.market_maker.DRY_RUN", False),
        patch(
            "src.auth.get_balances",
            return_value={"usdc_allowance": Decimal(70)},
        ),
    ):
        mm._check_balance()

    mm.stop.assert_called_once_with()
    mm.risk.kill_switch.assert_called_once_with(
        "Balance dropped >20%",
        token_id="token-1",
    )


@pytest.mark.asyncio
async def test_disconnect_callback_does_not_block_the_event_loop() -> None:
    from src.strategy.market_maker import SmartMarketMaker

    callback = None

    class FeedStub:
        async def start(self, token_ids):
            return None

        async def stop(self):
            return None

        def register_connection_lost_callback(self, cb):
            nonlocal callback
            callback = cb

        def register_flow_callback(self, token_id, cb):
            return None

    mm = SmartMarketMaker(token_id="token-1")
    mm._wait_for_data = AsyncMock()
    mm._shutdown = AsyncMock()
    mm._sync_live_orders = MagicMock()

    async def stop_after_one_iteration():
        mm.stop()

    mm._loop_iteration = AsyncMock(side_effect=stop_after_one_iteration)

    with patch("src.strategy.market_maker.MarketFeed", return_value=FeedStub()):
        await mm.run(install_signals=False)

    assert callable(callback)

    def slow_cancel(*args, **kwargs):
        time.sleep(0.15)
        return 1

    with patch("src.strategy.market_maker.cancel_all_orders", side_effect=slow_cancel):
        started = asyncio.get_running_loop().time()
        callback()
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 0.05
        await mm._disconnect_teardown_task


@pytest.mark.asyncio
async def test_shutdown_cancel_failure_does_not_mask_primary_run_error() -> None:
    from src.strategy.market_maker import SmartMarketMaker
    from src.trading import OrderCancellationError

    class FeedStub:
        async def start(self, token_ids):
            raise RuntimeError("primary feed failure")

        async def stop(self):
            return None

    mm = SmartMarketMaker(token_id="token-1")

    with (
        patch("src.strategy.market_maker.MarketFeed", return_value=FeedStub()),
        patch(
            "src.strategy.market_maker.cancel_all_orders",
            side_effect=OrderCancellationError("shutdown cancel failure"),
        ),
        pytest.raises(RuntimeError, match="primary feed failure"),
    ):
        await mm.run(install_signals=False)


@pytest.mark.asyncio
async def test_requote_preserves_an_unchanged_side_queue_position() -> None:
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="token-1", size=Decimal(5))
    original_bid = Order(
        id="bid-old",
        token_id="token-1",
        side=OrderSide.BUY,
        price=Decimal("0.490"),
        size=Decimal("5.00"),
        filled=Decimal(0),
        status=OrderStatus.LIVE,
    )
    original_ask = Order(
        id="ask-old",
        token_id="token-1",
        side=OrderSide.SELL,
        price=Decimal("0.510"),
        size=Decimal("5.00"),
        filled=Decimal(0),
        status=OrderStatus.LIVE,
    )
    mm.bid_order = original_bid
    mm.ask_order = original_ask
    mm.trade_logger.log_quote = MagicMock()
    mm.inventory.get_size_multipliers = MagicMock(return_value=(1.0, 1.0))
    mm.inventory.get_state = MagicMock(return_value=SimpleNamespace(inventory_level="NEUTRAL"))

    def make_order(token_id, side, price, size):
        return Order(
            id=f"new-{side.value.lower()}",
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            filled=Decimal(0),
            status=OrderStatus.LIVE,
        )

    with (
        patch("src.strategy.market_maker.DRY_RUN", True),
        patch("src.strategy.market_maker.cancel_order", return_value=True) as cancel,
        patch(
            "src.strategy.market_maker.place_order",
            side_effect=make_order,
        ) as place,
    ):
        await mm._update_quotes(
            Decimal("0.50"),
            Decimal("0.490"),
            Decimal("0.520"),
        )

    assert cancel.call_args_list == [call("ask-old")]
    assert mm.bid_order is original_bid
    assert place.call_count == 1
    assert place.call_args.kwargs["side"] is OrderSide.SELL


@pytest.mark.asyncio
async def test_live_cold_start_refuses_to_accumulate_from_a_one_sided_bid() -> None:
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="token-1", size=Decimal(5))
    mm.trade_logger.log_quote = MagicMock()
    mm.inventory.get_size_multipliers = MagicMock(return_value=(1.0, 1.0))
    mm.inventory.get_state = MagicMock(return_value=SimpleNamespace(inventory_level="NEUTRAL"))

    with (
        patch("src.strategy.market_maker.DRY_RUN", False),
        patch(
            "src.auth.get_conditional_balance",
            return_value={
                "balance": Decimal(0),
                "allowance": Decimal(0),
                "sellable": Decimal(0),
            },
        ),
        patch("src.strategy.market_maker.place_order") as place,
    ):
        await mm._update_quotes(
            Decimal("0.50"),
            Decimal("0.490"),
            Decimal("0.510"),
        )

    place.assert_not_called()
    assert mm.bid_order is None
    assert mm.ask_order is None


def test_example_env_does_not_disable_live_risk_enforcement() -> None:
    active_lines = [
        line.strip()
        for line in Path(".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert "RISK_ENFORCE=false" not in active_lines
