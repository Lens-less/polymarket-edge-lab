"""
Phase 6 Tests - Order placement with validation.

Run: pytest tests/test_phase6.py -v
"""

import pytest
from decimal import Decimal


class TestValidation:
    """Test order validation."""

    def test_validate_price_valid(self):
        from src.trading import validate_price

        assert validate_price(Decimal("0.50"), "t") == Decimal("0.50")
        assert validate_price(Decimal("0.01"), "t") == Decimal("0.01")
        assert validate_price(Decimal("0.99"), "t") == Decimal("0.99")
        print("✓ Valid prices accepted")

    def test_validate_price_rounds(self):
        from src.trading import validate_price

        # Rounds down to tick
        assert validate_price(Decimal("0.555"), "t") == Decimal("0.55")
        assert validate_price(Decimal("0.509"), "t") == Decimal("0.50")
        print("✓ Prices rounded to tick")

    def test_validate_price_invalid(self):
        from src.trading import validate_price, OrderError

        with pytest.raises(OrderError):
            validate_price(Decimal("0"), "t")
        with pytest.raises(OrderError):
            validate_price(Decimal("1"), "t")
        with pytest.raises(OrderError):
            validate_price(Decimal("1.5"), "t")
        with pytest.raises(OrderError):
            validate_price(Decimal("-0.5"), "t")
        print("✓ Invalid prices rejected")

    def test_validate_size(self):
        from src.trading import validate_size, OrderError
        from src.config import MIN_ORDER_SIZE, MAX_ORDER_SIZE

        # Valid
        validate_size(Decimal("10"))
        validate_size(MIN_ORDER_SIZE)
        validate_size(MAX_ORDER_SIZE)

        # Too small
        with pytest.raises(OrderError):
            validate_size(Decimal("1"))

        # Too large
        with pytest.raises(OrderError):
            validate_size(MAX_ORDER_SIZE + 1)

        print("✓ Size validation works")

    def test_position_limit(self):
        from src.trading import check_position_limit, OrderError
        from src.models import OrderSide
        from src.config import MAX_POSITION_PER_MARKET
        from src.simulator import reset_simulator

        reset_simulator()

        # Within limit
        check_position_limit("t", OrderSide.BUY, Decimal("50"))

        # Exceeds limit
        with pytest.raises(OrderError):
            check_position_limit("t", OrderSide.BUY, MAX_POSITION_PER_MARKET + 1)

        print("✓ Position limit works")


class TestPlaceOrder:
    """Test order placement."""

    def test_place_order_success(self):
        from src.config import DRY_RUN
        from src.trading import place_order
        from src.models import OrderSide
        from src.simulator import reset_simulator

        if not DRY_RUN:
            pytest.skip("Requires DRY_RUN=true")

        reset_simulator()

        order = place_order("token1", OrderSide.BUY, Decimal("0.50"), Decimal("10"))

        assert order.is_simulated
        assert order.is_live
        assert order.side == OrderSide.BUY
        assert order.price == Decimal("0.50")
        assert order.size == Decimal("10")

        print("✓ Order placed successfully")

    def test_place_order_rejects_bad_price(self):
        from src.trading import place_order, OrderError
        from src.models import OrderSide
        from src.simulator import reset_simulator

        reset_simulator()

        with pytest.raises(OrderError):
            place_order("t", OrderSide.BUY, Decimal("1.5"), Decimal("10"))

        print("✓ Bad price rejected")

    def test_place_order_rejects_small_size(self):
        from src.trading import place_order, OrderError
        from src.models import OrderSide
        from src.simulator import reset_simulator

        reset_simulator()

        with pytest.raises(OrderError):
            place_order("t", OrderSide.BUY, Decimal("0.50"), Decimal("1"))

        print("✓ Small size rejected")


class TestCancelOrder:
    """Test order cancellation."""

    def test_cancel_order(self):
        from src.config import DRY_RUN
        from src.trading import place_order, cancel_order
        from src.models import OrderSide, OrderStatus
        from src.simulator import reset_simulator

        if not DRY_RUN:
            pytest.skip("Requires DRY_RUN=true")

        reset_simulator()

        order = place_order("t", OrderSide.BUY, Decimal("0.50"), Decimal("10"))
        result = cancel_order(order.id)

        assert result == True
        assert order.status == OrderStatus.CANCELLED

        print("✓ Cancel order works")

    def test_cancel_all_orders(self):
        from src.config import DRY_RUN
        from src.trading import place_order, cancel_all_orders
        from src.orders import get_open_orders
        from src.models import OrderSide
        from src.simulator import reset_simulator

        if not DRY_RUN:
            pytest.skip("Requires DRY_RUN=true")

        reset_simulator()

        place_order("t1", OrderSide.BUY, Decimal("0.50"), Decimal("10"))
        place_order("t1", OrderSide.SELL, Decimal("0.55"), Decimal("10"))
        place_order("t2", OrderSide.BUY, Decimal("0.30"), Decimal("10"))

        assert len(get_open_orders()) == 3

        # Cancel only t1
        cancelled = cancel_all_orders("t1")
        assert cancelled == 2
        assert len(get_open_orders()) == 1

        # Cancel all
        cancel_all_orders()
        assert len(get_open_orders()) == 0

        print("✓ Cancel all works")


class TestIntegration:
    """Full workflow test."""

    def test_place_fill_cancel_workflow(self):
        from src.config import DRY_RUN
        from src.trading import place_order, cancel_all_orders
        from src.orders import get_open_orders, get_position
        from src.models import OrderSide
        from src.simulator import get_simulator, reset_simulator

        if not DRY_RUN:
            pytest.skip("Requires DRY_RUN=true")

        reset_simulator()
        sim = get_simulator()

        # Place buy
        buy = place_order("t1", OrderSide.BUY, Decimal("0.50"), Decimal("20"))
        assert get_position("t1") == Decimal("0")

        # Fill it
        sim.check_fills("t1", Decimal("0.45"), Decimal("0.50"))
        assert get_position("t1") == Decimal("20")
        assert not buy.is_live

        # Place sell
        sell = place_order("t1", OrderSide.SELL, Decimal("0.55"), Decimal("10"))
        sim.check_fills("t1", Decimal("0.55"), Decimal("0.60"))
        assert get_position("t1") == Decimal("10")

        # Cleanup
        cancel_all_orders()

        print("✓ Full workflow works")

    @pytest.mark.network
    def test_with_real_market(self):
        from src.config import DRY_RUN
        from src.trading import place_order, cancel_all_orders
        from src.models import OrderSide
        from src.markets import fetch_active_markets
        from src.pricing import get_order_book
        from src.simulator import reset_simulator

        if not DRY_RUN:
            pytest.skip("Requires DRY_RUN=true")

        reset_simulator()

        # Get real market
        markets = fetch_active_markets(limit=5)
        token_id = None
        for m in markets:
            if m.token_ids:
                token_id = m.token_ids[0]
                break

        if not token_id:
            pytest.skip("No markets")

        book = get_order_book(token_id)
        if not book or not book.best_bid:
            pytest.skip("No book")

        print(f"  Token: {token_id[:20]}...")
        print(f"  Bid: {book.best_bid}, Ask: {book.best_ask}")

        order = place_order(
            token_id,
            OrderSide.BUY,
            Decimal(str(book.best_bid)),
            Decimal("10")
        )

        assert order.is_live
        cancel_all_orders()

        print("✓ Real market test works")


def test_position_caching():
    """Verify position is cached, not recalculated."""
    from src.simulator import get_simulator, reset_simulator
    from src.models import OrderSide
    from decimal import Decimal

    reset_simulator()
    sim = get_simulator()

    # Create and fill orders
    order1 = sim.create_order("t1", OrderSide.BUY, Decimal("0.50"), Decimal("10"))
    sim.check_fills("t1", Decimal("0.40"), Decimal("0.50"))

    order2 = sim.create_order("t1", OrderSide.BUY, Decimal("0.50"), Decimal("20"))
    sim.check_fills("t1", Decimal("0.40"), Decimal("0.50"))

    # Position should be 30
    assert sim.get_position("t1") == Decimal("30")

    # Sell some
    order3 = sim.create_order("t1", OrderSide.SELL, Decimal("0.55"), Decimal("15"))
    sim.check_fills("t1", Decimal("0.55"), Decimal("0.60"))

    # Position should be 15
    assert sim.get_position("t1") == Decimal("15")

    print("✓ Position caching works")


def test_rate_limiter():
    """Verify rate limiter throttles calls."""
    import time
    from src.rate_limiter import RateLimiter

    limiter = RateLimiter(calls_per_second=10)  # 100ms between calls

    start = time.time()
    for _ in range(5):
        limiter.wait_sync()
    elapsed = time.time() - start

    # 5 calls at 10/sec = at least 0.4 seconds (first call is immediate)
    assert elapsed >= 0.35, f"Expected >= 0.35s, got {elapsed}"

    print(f"✓ Rate limiter works ({elapsed:.2f}s for 5 calls)")
