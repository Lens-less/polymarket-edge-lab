"""
Tests for Phase 1 Safety features.

Tests:
1. Order created_at timestamps
2. Balance check blocks large orders
3. Startup cleanup detects orphaned orders
4. Stale order detection
5. Connection lost callback registration
"""

import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from src.models import Order, OrderSide, OrderStatus
from src.trading import OrderError, check_balance_for_order
from src.simulator import get_simulator, reset_simulator
from src.strategy.runner import cleanup_orphaned_orders


class TestOrderTimestamps:
    """Test that orders have created_at timestamps."""

    def setup_method(self):
        reset_simulator()

    def test_simulated_order_has_created_at(self):
        """Simulated orders should have created_at set."""
        sim = get_simulator()
        order = sim.create_order(
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("10")
        )
        assert order.created_at is not None
        # Should be parseable ISO format
        created = datetime.fromisoformat(order.created_at.replace('Z', '+00:00'))
        assert created is not None


class TestBalanceCheck:
    """Test balance validation before orders."""

    def test_balance_check_skipped_in_dry_run(self):
        """Balance check should pass in DRY_RUN mode."""
        with patch('src.trading.DRY_RUN', True):
            # Should not raise even with large order
            check_balance_for_order(Decimal("0.99"), Decimal("1000"))

    def test_balance_check_blocks_when_too_low(self):
        """Should reject order when balance below minimum."""
        with patch('src.trading.DRY_RUN', False):
            with patch('src.trading._cached_balance', Decimal("0.50")):
                with patch('src.trading._last_balance_check', float('inf')):
                    with pytest.raises(OrderError, match="Balance too low"):
                        check_balance_for_order(Decimal("0.50"), Decimal("1"))

    def test_balance_check_blocks_large_order(self):
        """Should reject order exceeding 50% of balance."""
        with patch('src.trading.DRY_RUN', False):
            with patch('src.trading._cached_balance', Decimal("10.00")):
                with patch('src.trading._last_balance_check', float('inf')):
                    # Order cost = 0.60 * 10 = $6 which is > 50% of $10
                    with pytest.raises(OrderError, match="exceeds 50%"):
                        check_balance_for_order(Decimal("0.60"), Decimal("10"))

    def test_balance_check_fails_closed_when_pusd_cannot_be_verified(self):
        """A read error must never authorize an otherwise unchecked order."""
        with patch('src.trading.DRY_RUN', False):
            with patch('src.trading._cached_balance', None):
                with patch('src.trading._last_balance_check', 0):
                    with patch(
                        'src.auth.get_balances',
                        side_effect=RuntimeError("unavailable"),
                    ):
                        with pytest.raises(OrderError, match="verify pUSD"):
                            check_balance_for_order(Decimal("0.50"), Decimal("5"))


class TestStartupCleanup:
    """Test orphaned order cleanup on startup."""

    def test_cleanup_returns_zero_in_dry_run(self):
        """Cleanup should return 0 in DRY_RUN mode (no persistence)."""
        with patch('src.strategy.runner.DRY_RUN', True):
            result = cleanup_orphaned_orders("test_token")
            assert result == 0

    def test_cleanup_handles_no_orders(self):
        """Cleanup should handle case of no existing orders."""
        with patch('src.strategy.runner.DRY_RUN', False):
            with patch('src.strategy.runner.get_open_orders', return_value=[]):
                result = cleanup_orphaned_orders("test_token")
                assert result == 0

    def test_cleanup_cancels_orphaned_orders(self):
        """Cleanup should cancel existing orders."""
        mock_orders = [
            Order(
                id="order1",
                token_id="test_token",
                side=OrderSide.BUY,
                price=Decimal("0.50"),
                size=Decimal("10"),
                filled=Decimal("0"),
                status=OrderStatus.LIVE,
                is_simulated=False,
            ),
            Order(
                id="order2",
                token_id="test_token",
                side=OrderSide.SELL,
                price=Decimal("0.60"),
                size=Decimal("10"),
                filled=Decimal("0"),
                status=OrderStatus.LIVE,
                is_simulated=False,
            ),
        ]

        with patch('src.strategy.runner.DRY_RUN', False):
            with patch('src.strategy.runner.get_open_orders', return_value=mock_orders):
                with patch('src.strategy.runner.cancel_all_orders', return_value=2) as mock_cancel:
                    result = cleanup_orphaned_orders("test_token")
                    assert result == 2
                    mock_cancel.assert_called_once_with(
                        "test_token",
                        verify=True,
                        raise_on_failure=True,
                    )

    def test_cleanup_returns_minus_one_when_cancel_verification_fails(self):
        with patch('src.strategy.runner.DRY_RUN', False):
            with patch(
                'src.strategy.runner.get_open_orders',
                return_value=[
                    Order(
                        id="order1",
                        token_id="test_token",
                        side=OrderSide.BUY,
                        price=Decimal("0.50"),
                        size=Decimal("10"),
                        filled=Decimal("0"),
                        status=OrderStatus.LIVE,
                        is_simulated=False,
                    )
                ],
            ):
                with patch(
                    'src.strategy.runner.cancel_all_orders',
                    side_effect=RuntimeError("residual live orders remain"),
                ):
                    assert cleanup_orphaned_orders("test_token") == -1


class TestStaleOrderDetection:
    """Test stale order cleanup."""

    def test_order_age_calculation(self):
        """Test that order age is calculated correctly."""
        # Create order with known timestamp
        old_time = datetime.now(timezone.utc) - timedelta(seconds=600)  # 10 minutes ago
        order = Order(
            id="old_order",
            token_id="test_token",
            side=OrderSide.BUY,
            price=Decimal("0.50"),
            size=Decimal("10"),
            filled=Decimal("0"),
            status=OrderStatus.LIVE,
            is_simulated=True,
            created_at=old_time.isoformat(),
        )

        # Parse and check age
        assert order.created_at is not None
        created = datetime.fromisoformat(order.created_at.replace('Z', '+00:00'))
        age = datetime.now(timezone.utc) - created
        assert age.total_seconds() >= 600


class TestConnectionLostCallback:
    """Test WebSocket disconnect callback."""

    def test_feed_callback_registration(self):
        """Test that MarketFeed can register connection lost callback."""
        from src.feed import MarketFeed

        feed = MarketFeed()
        callback_called = False

        def on_disconnect():
            nonlocal callback_called
            callback_called = True

        feed.register_connection_lost_callback(on_disconnect)

        # Simulate connection lost
        feed._handle_connection_lost()

        assert callback_called

    def test_websocket_callback_attribute(self):
        """Test that WebSocketConnection has on_connection_lost callback."""
        from src.feed.websocket_conn import WebSocketConnection

        ws = WebSocketConnection()
        assert hasattr(ws, 'on_connection_lost')

        callback_called = False

        def on_lost():
            nonlocal callback_called
            callback_called = True

        ws.on_connection_lost = on_lost

        # Manually invoke (normally called in _receive_loop)
        if ws.on_connection_lost:
            ws.on_connection_lost()

        assert callback_called


class TestBalanceMonitoring:
    """Test periodic balance monitoring."""

    def test_balance_drop_detection(self):
        """Test that large balance drops trigger kill switch."""
        from src.strategy.market_maker import SmartMarketMaker

        mm = SmartMarketMaker(token_id="test_token")

        # Set initial balance
        mm._initial_balance = Decimal("100.00")

        # Mock get_balances to return dropped balance
        with patch('src.strategy.market_maker.DRY_RUN', False):
            with patch('src.auth.get_balances', return_value={'usdc_allowance': Decimal("70.00")}):
                with patch.object(mm.risk, 'kill_switch') as mock_kill:
                    with patch.object(mm, 'stop') as mock_stop:
                        mm._check_balance()
                        # 30% drop > 20% threshold
                        mock_kill.assert_called_once()
                        mock_stop.assert_called_once()

    def test_balance_no_alert_on_small_drop(self):
        """Test that small balance drops don't trigger alert."""
        from src.strategy.market_maker import SmartMarketMaker

        mm = SmartMarketMaker(token_id="test_token")
        mm._initial_balance = Decimal("100.00")

        with patch('src.strategy.market_maker.DRY_RUN', False):
            with patch('src.auth.get_balances', return_value={'usdc_allowance': Decimal("90.00")}):
                with patch.object(mm.risk, 'kill_switch') as mock_kill:
                    mm._check_balance()
                    # 10% drop < 20% threshold
                    mock_kill.assert_not_called()
