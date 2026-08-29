from decimal import Decimal
from unittest.mock import patch

from src.feed.feed import FeedState, MarketFeed


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
