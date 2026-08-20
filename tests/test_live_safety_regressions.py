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


def test_cancel_all_reports_typed_error_when_preflight_fails_but_verify_finds_order(
    monkeypatch,
) -> None:
    list_calls = 0

    class Client:
        @staticmethod
        def list_open_orders(**kwargs):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 1:
                raise ConnectionError("preflight REST down")
            return _Paginator(
                (
                    SimpleNamespace(
                        id="still-live",
                        token_id="token-1",
                        side="BUY",
                        price=Decimal("0.40"),
                        original_size=Decimal(5),
                        size_matched=Decimal(0),
                        status="LIVE",
                    ),
                )
            )

        @staticmethod
        def cancel_market_orders(**kwargs):
            return SimpleNamespace(canceled=(), not_canceled={})

        @staticmethod
        def cancel_order(**kwargs):
            return SimpleNamespace(canceled=(), not_canceled={"still-live": "busy"})

    _configure_live_adapters(monkeypatch, Client())

    with pytest.raises(
        trading.OrderCancellationError,
        match="preflight count: unknown",
    ):
        trading.cancel_all_orders("token-1", verify=True)


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
    fee_rate_bps: Decimal = Decimal(0),
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
        fee_rate_bps=fee_rate_bps,
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


def test_get_trades_charges_reported_taker_fee_but_not_maker_fee(monkeypatch) -> None:
    maker_order = SimpleNamespace(
        order_id="maker-order-1",
        token_id="token-1",
        maker_address="0xme",
        owner="owner-me",
        side="BUY",
        price=Decimal("0.40"),
        matched_amount=Decimal(3),
        fee_rate_bps=Decimal(700),
    )
    items = (
        _sdk_trade(
            "taker-trade",
            status="CONFIRMED",
            trader_side="TAKER",
            fee_rate_bps=Decimal(700),
        ),
        _sdk_trade(
            "maker-trade",
            status="CONFIRMED",
            trader_side="MAKER",
            fee_rate_bps=Decimal(700),
            maker_orders=(maker_order,),
        ),
    )

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator(items)

    _configure_live_adapters(monkeypatch, Client())

    result = orders.get_trades("token-1", limit=None, raise_on_error=True)
    by_id = {trade.id: trade for trade in result}

    assert by_id["taker-trade"].fee == Decimal("0.05040")
    assert by_id["maker-trade:maker-order-1"].fee == Decimal(0)


def test_get_trades_logs_maker_identity_payload_before_adapter_failure(
    monkeypatch,
) -> None:
    maker_orders = tuple(
        SimpleNamespace(
            order_id=f"maker-order-{index}",
            token_id="token-1",
            maker_address=f"0xother{index}",
            owner=f"owner-other-{index}",
            side="BUY",
            price=Decimal("0.40"),
            matched_amount=Decimal(index),
            fee_rate_bps=Decimal(0),
        )
        for index in (1, 2)
    )
    item = _sdk_trade(
        "ambiguous-maker-trade",
        status="CONFIRMED",
        trader_side="MAKER",
        maker_orders=maker_orders,
    )

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator((item,))

    _configure_live_adapters(monkeypatch, Client())

    with (
        patch.object(orders.logger, "error") as error_log,
        pytest.raises(orders.PolymarketAdapterError, match="ambiguous-maker-trade"),
    ):
        orders.get_trades("token-1", limit=None, raise_on_error=True)

    logged = str(error_log.call_args)
    assert "0xme" in logged
    assert "owner-me" in logged
    assert "maker-order-1" in logged
    assert "0xother2" in logged
    assert "owner-other-2" in logged


def test_get_trades_sorts_before_applying_recent_trade_limit(monkeypatch) -> None:
    items = (
        _sdk_trade(
            "oldest",
            status="CONFIRMED",
            matched_at=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
        ),
        _sdk_trade(
            "newest",
            status="CONFIRMED",
            matched_at=datetime(2026, 8, 20, 0, 2, tzinfo=UTC),
        ),
        _sdk_trade(
            "middle",
            status="CONFIRMED",
            matched_at=datetime(2026, 8, 20, 0, 1, tzinfo=UTC),
        ),
    )

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator(items)

    _configure_live_adapters(monkeypatch, Client())

    result = orders.get_trades("token-1", limit=2, raise_on_error=True)

    assert [trade.id for trade in result] == ["middle", "newest"]


def test_get_trades_forwards_incremental_after_watermark(monkeypatch) -> None:
    observed_kwargs: dict[str, object] = {}

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            observed_kwargs.update(kwargs)
            return _Paginator(())

    _configure_live_adapters(monkeypatch, Client())

    assert orders.get_trades(
        "token-1",
        limit=None,
        raise_on_error=True,
        after="1787184000",
    ) == []
    assert observed_kwargs == {
        "token_id": "token-1",
        "after": "1787184000",
    }


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

    def fake_get_trades(
        token_id,
        limit=50,
        raise_on_error=False,
        *,
        after=None,
    ):
        return history if limit is None else history[:limit]

    with (
        patch(
            "src.strategy.market_maker.get_trades",
            side_effect=fake_get_trades,
        ) as get_trades,
        patch("src.strategy.market_maker.time.time", return_value=1060),
        patch("src.strategy.market_maker.time.monotonic", return_value=100),
    ):
        mm._sync_live_trades()

    get_trades.assert_called_once_with(
        "token-1",
        limit=None,
        raise_on_error=True,
        after="1000",
    )
    mm._record_trade_fill.assert_called_once_with(history[100], fill_type="live")


def test_live_trade_sync_uses_overlapping_incremental_watermark() -> None:
    from src.strategy.market_maker import SmartMarketMaker

    old_trade = Trade(
        id="old-trade",
        order_id="old-order",
        token_id="token-1",
        side=OrderSide.BUY,
        price=Decimal("0.40"),
        size=Decimal(1),
        timestamp=datetime.fromtimestamp(1059, UTC).isoformat(),
    )
    new_trade = Trade(
        id="new-trade",
        order_id="new-order",
        token_id="token-1",
        side=OrderSide.BUY,
        price=Decimal("0.41"),
        size=Decimal(1),
        timestamp=datetime.fromtimestamp(1061, UTC).isoformat(),
    )
    responses = iter(([old_trade], [old_trade, new_trade]))
    calls: list[dict[str, object]] = []

    def fake_get_trades(token_id, limit=50, raise_on_error=False, **kwargs):
        calls.append(
            {
                "token_id": token_id,
                "limit": limit,
                "raise_on_error": raise_on_error,
                **kwargs,
            }
        )
        return next(responses)

    mm = SmartMarketMaker(token_id="token-1")
    mm._record_trade_fill = MagicMock()

    with (
        patch(
            "src.strategy.market_maker.get_trades",
            side_effect=fake_get_trades,
        ),
        patch(
            "src.strategy.market_maker.time.time",
            side_effect=(1060, 1062),
        ),
        patch(
            "src.strategy.market_maker.time.monotonic",
            side_effect=(100, 102),
        ),
    ):
        mm._prime_live_trade_state()
        mm._sync_live_trades()

    assert calls == [
        {
            "token_id": "token-1",
            "limit": None,
            "raise_on_error": True,
            "after": "1000",
        },
        {
            "token_id": "token-1",
            "limit": None,
            "raise_on_error": True,
            "after": "1000",
        },
    ]
    mm._record_trade_fill.assert_called_once_with(new_trade, fill_type="live")


@pytest.mark.asyncio
async def test_live_loop_reuses_one_short_lived_account_snapshot(monkeypatch) -> None:
    from src.risk.manager import RiskManager
    from src.strategy.market_maker import SmartMarketMaker

    class Client:
        def __init__(self) -> None:
            self.balance_calls = 0
            self.open_order_calls = 0
            self.trade_calls = 0

        def get_balance_allowance(self, **kwargs):
            self.balance_calls += 1
            return SimpleNamespace(
                balance=55_000_000,
                allowances={"exchange": 55_000_000},
            )

        def list_open_orders(self, **kwargs):
            self.open_order_calls += 1
            return _Paginator(())

        def list_account_trades(self, **kwargs):
            self.trade_calls += 1
            return _Paginator(())

    class FeedStub:
        is_healthy = True

        @staticmethod
        def get_midpoint(token_id):
            return Decimal("0.50")

    client = Client()
    _configure_live_adapters(monkeypatch, client)
    mm = SmartMarketMaker(token_id="token-1")
    mm.feed = FeedStub()
    mm.risk = RiskManager(enforce=True)
    mm._last_balance_check = time.time()
    mm._last_heartbeat = time.time()
    mm._should_requote = MagicMock(return_value=False)

    with (
        patch("src.strategy.market_maker.DRY_RUN", False),
        patch("src.strategy.market_maker.get_tick_size", return_value=Decimal("0.01")),
    ):
        await mm._loop_iteration()
        await mm._loop_iteration()

    assert client.balance_calls == 1
    assert client.open_order_calls == 1
    assert client.trade_calls == 1


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


def test_inventory_pnl_uses_session_position_not_preloaded_wallet_balance() -> None:
    from src.strategy.inventory import InventoryManager

    inventory = InventoryManager(token_id="token-1", position_limit=Decimal(100))
    inventory.record_fill(price=Decimal("0.40"), size=Decimal(5), side="BUY")

    with patch(
        "src.strategy.inventory.get_position",
        return_value=Decimal(55),
    ) as wallet_position:
        state = inventory.get_state(mid_price=Decimal("0.50"))

    assert state.position == Decimal(55)
    assert state.strategy_position == Decimal(5)
    assert state.vwap_entry == Decimal("0.40")
    assert state.unrealized_pnl == Decimal("0.50")
    wallet_position.assert_called_once_with("token-1")


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
    mm._shutdown = AsyncMock(return_value=None)
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
async def test_live_execution_block_stops_the_strategy_after_one_loop() -> None:
    from src.edge_lab.compatibility import LiveExecutionBlocked
    from src.strategy.market_maker import SmartMarketMaker

    class FeedStub:
        async def start(self, token_ids):
            return None

        def register_connection_lost_callback(self, callback):
            return None

        def register_flow_callback(self, token_id, callback):
            return None

    mm = SmartMarketMaker(token_id="token-1")
    mm._wait_for_data = AsyncMock()
    mm._shutdown = AsyncMock(return_value=None)
    mm.timer.get_interval = MagicMock(return_value=0)
    loop_calls = 0

    async def block_then_guard_against_an_infinite_loop():
        nonlocal loop_calls
        loop_calls += 1
        if loop_calls == 1:
            raise LiveExecutionBlocked("live release boundary is closed")
        mm.stop()

    mm._loop_iteration = AsyncMock(
        side_effect=block_then_guard_against_an_infinite_loop
    )

    with (
        patch("src.strategy.market_maker.DRY_RUN", True),
        patch("src.strategy.market_maker.MarketFeed", return_value=FeedStub()),
    ):
        await mm.run(install_signals=False)

    assert loop_calls == 1
    assert mm._shutdown_event.is_set()


@pytest.mark.asyncio
async def test_live_trade_adapter_contract_failure_cancels_and_stops() -> None:
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="token-1")
    mm._sync_live_state = MagicMock(
        side_effect=orders.PolymarketAdapterError("maker payload mismatch")
    )
    mm._get_live_account_snapshot = MagicMock(
        return_value=SimpleNamespace(balance=Decimal(0), open_orders=())
    )
    mm._cancel_all_quotes = AsyncMock()
    mm.stop = MagicMock()

    with patch("src.strategy.market_maker.DRY_RUN", False):
        await mm._loop_iteration()

    mm._cancel_all_quotes.assert_awaited_once_with()
    mm.stop.assert_called_once_with()


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
async def test_shutdown_bounds_wait_for_running_disconnect_teardown() -> None:
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="token-1")
    never_finishes = asyncio.Event()
    disconnect_task = asyncio.create_task(never_finishes.wait())
    mm._disconnect_teardown_task = disconnect_task

    try:
        with (
            patch(
                "src.strategy.market_maker.ORDER_TEARDOWN_TIMEOUT_SECONDS",
                0.01,
                create=True,
            ),
            patch("src.strategy.market_maker.cancel_all_orders") as cancel,
        ):
            error = await asyncio.wait_for(mm._shutdown(), timeout=0.1)

        assert isinstance(error, trading.OrderCancellationError)
        assert "still running in the background" in str(error)
        assert not disconnect_task.done()
        cancel.assert_not_called()
    finally:
        disconnect_task.cancel()
        await asyncio.gather(disconnect_task, return_exceptions=True)


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
    mm.inventory.get_state = MagicMock(
        return_value=SimpleNamespace(
            inventory_level="NEUTRAL",
            bid_size_mult=1.0,
            ask_size_mult=1.0,
        )
    )

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
    mm.inventory.get_state = MagicMock(
        return_value=SimpleNamespace(
            inventory_level="NEUTRAL",
            bid_size_mult=1.0,
            ask_size_mult=1.0,
        )
    )

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
