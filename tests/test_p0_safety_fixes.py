import asyncio
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.feed.feed import FeedState, MarketFeed
from src.models import Order, OrderSide, OrderStatus, Trade
from src.risk import reset_risk_manager


def test_feed_unhealthy_immediately_after_connection_lost():
    feed = MarketFeed()
    feed._data_store.register_token("token")
    feed._data_store.update_book(
        "token",
        [{'price': '0.49', 'size': '10'}],
        [{'price': '0.51', 'size': '10'}],
    )
    feed._state = FeedState.RUNNING
    feed._data_source = "websocket"
    feed._ws._connected = True
    feed._ws._ws = object()

    assert feed.is_healthy is True

    feed._handle_connection_lost()

    assert feed.is_healthy is False


def test_get_position_uses_authoritative_conditional_balance():
    from src.orders import get_position

    with patch('src.orders.DRY_RUN', False):
        with patch(
            'src.auth.get_conditional_balance',
            return_value={"balance": Decimal("60")},
        ) as get_balance:
            assert get_position("token") == Decimal("60")
    get_balance.assert_called_once_with("token", raise_on_error=True)


def test_cancel_all_quotes_keeps_references_when_cancel_fails():
    from src.strategy.market_maker import SmartMarketMaker

    reset_risk_manager()
    mm = SmartMarketMaker(token_id="token")
    mm.risk.record_error = MagicMock()
    mm.bid_order = Order(
        id="bid-1",
        token_id="token",
        side=OrderSide.BUY,
        price=Decimal("0.49"),
        size=Decimal("10"),
        filled=Decimal("0"),
        status=OrderStatus.LIVE,
        is_simulated=False,
    )
    mm.ask_order = Order(
        id="ask-1",
        token_id="token",
        side=OrderSide.SELL,
        price=Decimal("0.51"),
        size=Decimal("10"),
        filled=Decimal("0"),
        status=OrderStatus.LIVE,
        is_simulated=False,
    )

    with patch('src.strategy.market_maker.cancel_order', return_value=False):
        result = asyncio.run(mm._cancel_all_quotes())

    assert result is False
    assert mm.bid_order is not None
    assert mm.ask_order is not None


def test_cancel_all_quotes_clears_terminal_race_orders_after_verification():
    from src.strategy.market_maker import SmartMarketMaker

    reset_risk_manager()
    mm = SmartMarketMaker(token_id="token")
    mm.risk.record_error = MagicMock()
    mm.bid_order = Order(
        id="bid-1",
        token_id="token",
        side=OrderSide.BUY,
        price=Decimal("0.49"),
        size=Decimal("10"),
        filled=Decimal("0"),
        status=OrderStatus.LIVE,
        is_simulated=False,
    )

    with patch('src.strategy.market_maker.DRY_RUN', False):
        with patch('src.strategy.market_maker.cancel_order', return_value=False):
            with patch('src.strategy.market_maker.get_open_orders', return_value=[]):
                result = asyncio.run(mm._cancel_all_quotes())

    assert result is True
    assert mm.bid_order is None
    mm.risk.record_error.assert_not_called()


def test_live_sync_records_trade_once_and_clears_filled_order():
    from src.strategy.market_maker import SmartMarketMaker

    reset_risk_manager()
    mm = SmartMarketMaker(token_id="token")
    mm.inventory.record_fill = MagicMock()
    mm.trade_logger.log_trade = MagicMock()
    mm.risk.record_trade = MagicMock()
    mm.bid_order = Order(
        id="bid-1",
        token_id="token",
        side=OrderSide.BUY,
        price=Decimal("0.49"),
        size=Decimal("10"),
        filled=Decimal("0"),
        status=OrderStatus.LIVE,
        is_simulated=False,
    )

    trade = Trade(
        id="trade-1",
        order_id="bid-1",
        token_id="token",
        side=OrderSide.BUY,
        price=Decimal("0.49"),
        size=Decimal("10"),
        fee=Decimal("0.01"),
        is_simulated=False,
    )

    with patch('src.strategy.market_maker.get_trades', return_value=[trade]):
        with patch('src.strategy.market_maker.get_open_orders', return_value=[]):
            mm._sync_live_state()
            mm._sync_live_state()

    assert mm.inventory.record_fill.call_count == 1
    assert mm.risk.record_trade.call_count == 1
    assert mm.bid_order is None
    assert "trade-1" in mm._seen_trade_ids
