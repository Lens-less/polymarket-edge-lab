"""
Phase 5 Verification Tests

Run with: pytest tests/test_phase5.py -v

Note: Tests that require authentication will skip if credentials not configured.
"""

import pytest
from decimal import Decimal


class TestOrderModels:
    """Test order-related models."""

    def test_order_status_enum(self):
        """Test OrderStatus enum."""
        from src.models import OrderStatus

        assert OrderStatus.LIVE.value == "LIVE"
        assert OrderStatus.MATCHED.value == "MATCHED"
        assert OrderStatus.CANCELLED.value == "CANCELLED"
        assert OrderStatus.EXPIRED.value == "EXPIRED"

        print("✓ OrderStatus enum defined")

    def test_order_side_enum(self):
        """Test OrderSide enum."""
        from src.models import OrderSide

        assert OrderSide.BUY.value == "BUY"
        assert OrderSide.SELL.value == "SELL"

        print("✓ OrderSide enum defined")

    def test_order_type_enum(self):
        """Test OrderType enum."""
        from src.models import OrderType

        assert OrderType.GTC.value == "GTC"
        assert OrderType.GTD.value == "GTD"
        assert OrderType.FOK.value == "FOK"
        assert OrderType.FAK.value == "FAK"

        print("✓ OrderType enum defined")

    def test_order_dataclass(self):
        """Test Order dataclass."""
        from src.models import Order, OrderStatus, OrderSide

        order = Order(
            id="order123",
            token_id="token456",
            side=OrderSide.BUY,
            price=Decimal("0.55"),
            size=Decimal("100"),
            filled=Decimal("40"),
            status=OrderStatus.LIVE
        )

        assert order.id == "order123"
        assert order.remaining == Decimal("60")
        assert order.is_live == True
        assert order.is_filled == False
        assert order.fill_percent == 40.0

        print("✓ Order dataclass works")

    def test_trade_dataclass(self):
        """Test Trade dataclass."""
        from src.models import Trade, OrderSide

        trade = Trade(
            id="trade789",
            order_id="order123",
            token_id="token456",
            side=OrderSide.BUY,
            price=Decimal("0.55"),
            size=Decimal("50")
        )

        assert trade.id == "trade789"
        assert trade.value == Decimal("27.50")

        print("✓ Trade dataclass works")


class TestOrdersModule:
    """Test orders.py functions."""

    def test_imports(self):
        """Test orders module imports."""
        from src.orders import (
            get_open_orders,
            get_trades,
            get_position,
        )

        print("✓ Orders module imports work")

    def test_get_open_orders_works(self):
        """Test that get_open_orders works."""
        from src.orders import get_open_orders

        # Should return list, even without credentials (in DRY_RUN mode)
        orders = get_open_orders()
        assert isinstance(orders, list)
        print(f"✓ get_open_orders returned {len(orders)} orders")

    def test_get_position(self):
        """Test get_position function."""
        from src.orders import get_position
        from decimal import Decimal

        # Should return Decimal position
        position = get_position("test_token")
        assert isinstance(position, Decimal)

        print(f"✓ get_position returned {position}")

    @pytest.mark.network
    def test_get_trades(self):
        """Test get_trades function."""
        from src.orders import get_trades
        from src.config import has_credentials

        if not has_credentials():
            pytest.skip("Credentials not configured")

        trades = get_trades(limit=10)

        assert isinstance(trades, list)
        print(f"✓ get_trades returned {len(trades)} trades")



class TestIntegration:
    """Integration tests."""

    def test_order_workflow_readonly(self):
        """Test reading orders and trades together."""
        from src.orders import get_open_orders, get_trades, get_position

        # Get current state (works in DRY_RUN mode)
        open_orders = get_open_orders()
        recent_trades = get_trades(limit=5)
        position = get_position("test_token")

        print(f"  Open orders: {len(open_orders)}")
        print(f"  Recent trades: {len(recent_trades)}")
        print(f"  Position: {position}")

        assert isinstance(open_orders, list)
        assert isinstance(recent_trades, list)

        print("✓ Order workflow works")

    @pytest.mark.network
    def test_filter_by_token(self):
        """Test filtering orders by token."""
        from src.orders import get_open_orders, get_trades
        from src.markets import fetch_active_markets

        # Get a token to filter by
        markets = fetch_active_markets(limit=5)
        if not markets or not markets[0].token_ids:
            pytest.skip("No markets found")

        token_id = markets[0].token_ids[0]

        # Filter by token (works in DRY_RUN mode)
        open_orders = get_open_orders(token_id=token_id)
        trades = get_trades(token_id=token_id)

        # All returned orders/trades should be for this token
        for order in open_orders:
            assert order.token_id == token_id
        for trade in trades:
            assert trade.token_id == token_id

        print(f"✓ Token filter works ({len(open_orders)} orders, {len(trades)} trades for token)")
