"""
Tests for Phase 1 Safety features.

Tests:
1. Order created_at timestamps
2. Balance check blocks large orders
3. Stale order detection
4. Connection lost callback registration

Startup orphan-order cleanup lived on the deleted generic market-maker
runner and is no longer part of this module.
"""

import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from src.models import Order, OrderSide, OrderStatus
from src.trading import OrderError, check_balance_for_order
from src.simulator import get_simulator, reset_simulator


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
