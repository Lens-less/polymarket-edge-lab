"""
Tests for Partial Fill Support in Backtest Engine

Tests the new fill models: simple, depth_based, probabilistic
"""

import pytest
from decimal import Decimal
from src.backtest.engine import (
    BacktestEngine,
    BacktestOrder,
    OrderStatus,
)
from src.backtest.data import HistoricalData, OrderBookSnapshot


class TestPartialFillModels:
    """Test different fill models."""

    def test_simple_fill_model_backward_compatible(self):
        """Simple fill model should work like legacy behavior."""
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fill_model="simple",
            partial_fill_enabled=False,
        )

        # Place a buy order
        order_id = engine.place_order("BUY", Decimal("0.50"), Decimal("10"))

        # Process snapshot where ask <= our price
        engine.process_snapshot(OrderBookSnapshot(
            timestamp=1000,
            token_id="t",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.50"),  # Ask at our bid
            bid_depth=Decimal("100"),
            ask_depth=Decimal("100"),
        ))

        order = engine.get_order(order_id)
        assert order.status == OrderStatus.FILLED
        assert order.filled_size == Decimal("10")
        assert order.fill_ratio == 1.0

    def test_depth_based_partial_fill(self):
        """Depth-based model with partial fills."""
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fill_model="depth_based",
            partial_fill_enabled=True,
            queue_position_factor=1.0,  # Full queue access
        )

        order_id = engine.place_order("BUY", Decimal("0.50"), Decimal("100"))

        # Process with low depth - should partial fill
        engine.process_snapshot(OrderBookSnapshot(
            timestamp=1000,
            token_id="t",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.50"),
            bid_depth=Decimal("10"),  # Low depth
            ask_depth=Decimal("10"),
        ))

        order = engine.get_order(order_id)
        # Should be partial filled based on depth
        assert order.status == OrderStatus.PARTIAL
        assert order.filled_size > 0
        assert order.filled_size < Decimal("100")
        assert order.remaining_size > 0

    def test_depth_based_full_fill_when_enough_depth(self):
        """Depth-based model fills fully when depth is sufficient."""
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fill_model="depth_based",
            partial_fill_enabled=True,
            queue_position_factor=1.0,
        )

        order_id = engine.place_order("BUY", Decimal("0.50"), Decimal("10"))

        # Process with high depth
        engine.process_snapshot(OrderBookSnapshot(
            timestamp=1000,
            token_id="t",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.50"),
            bid_depth=Decimal("1000"),  # High depth
            ask_depth=Decimal("1000"),
        ))

        order = engine.get_order(order_id)
        assert order.status == OrderStatus.FILLED
        assert order.filled_size == Decimal("10")

    def test_depth_based_no_partial_when_disabled(self):
        """Depth-based model with partial fills disabled."""
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fill_model="depth_based",
            partial_fill_enabled=False,
            queue_position_factor=1.0,
        )

        order_id = engine.place_order("BUY", Decimal("0.50"), Decimal("100"))

        # Process with low depth - should not fill
        engine.process_snapshot(OrderBookSnapshot(
            timestamp=1000,
            token_id="t",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.50"),
            bid_depth=Decimal("10"),
            ask_depth=Decimal("10"),
        ))

        order = engine.get_order(order_id)
        # Should remain open since partial fills are disabled
        assert order.status == OrderStatus.OPEN
        assert order.filled_size == Decimal("0")

    def test_probabilistic_fill_model(self):
        """Probabilistic fill model produces fills based on probability."""
        # Run multiple times to test probabilistic nature
        fills_observed = 0
        n_runs = 20

        for _ in range(n_runs):
            engine = BacktestEngine(
                initial_capital=Decimal("1000"),
                fill_model="probabilistic",
                partial_fill_enabled=True,
                min_fill_probability=0.5,  # High probability for testing
            )

            order_id = engine.place_order("BUY", Decimal("0.50"), Decimal("100"))

            engine.process_snapshot(OrderBookSnapshot(
                timestamp=1000,
                token_id="t",
                best_bid=Decimal("0.49"),
                best_ask=Decimal("0.50"),
                bid_depth=Decimal("100"),
                ask_depth=Decimal("100"),
            ))

            order = engine.get_order(order_id)
            if order.filled_size > 0:
                fills_observed += 1

        # With 50% probability, we should see some fills
        assert fills_observed > 0
        assert fills_observed <= n_runs


class TestQueuePositionFactor:
    """Test queue position factor calculation."""

    def test_aggressive_order_gets_queue_factor_one(self):
        """Aggressive orders (crossing spread) get queue factor 1.0."""
        engine = BacktestEngine(fill_model="depth_based")

        # Buy at or above ask - aggressive
        factor = engine._calculate_queue_factor(
            Decimal("0.52"), Decimal("0.50"), Decimal("0.51"), "BUY"
        )
        assert factor == 1.0

        # Sell at or below bid - aggressive
        factor = engine._calculate_queue_factor(
            Decimal("0.49"), Decimal("0.50"), Decimal("0.51"), "SELL"
        )
        assert factor == 1.0

    def test_passive_order_queue_factor(self):
        """Passive orders get queue factor based on distance from best."""
        engine = BacktestEngine(fill_model="depth_based")

        # Buy at bid - should get factor based on position
        factor = engine._calculate_queue_factor(
            Decimal("0.50"), Decimal("0.50"), Decimal("0.51"), "BUY"
        )
        assert 0.0 <= factor <= 1.0

        # Buy below bid - lower factor
        factor_lower = engine._calculate_queue_factor(
            Decimal("0.48"), Decimal("0.50"), Decimal("0.52"), "BUY"
        )
        assert 0.0 <= factor_lower < 1.0

    def test_inside_spread_good_position(self):
        """Orders inside spread get good queue position."""
        engine = BacktestEngine(fill_model="depth_based")

        factor = engine._calculate_queue_factor(
            Decimal("0.505"), Decimal("0.50"), Decimal("0.51"), "BUY"
        )
        assert factor >= 0.8  # Should be good position


class TestPartialFillExecution:
    """Test partial fill execution and state management."""

    def test_multiple_partial_fills_accumulate(self):
        """Multiple partial fills accumulate on same order."""
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fill_model="depth_based",
            partial_fill_enabled=True,
            queue_position_factor=0.3,  # Low factor for partial fills
        )

        order_id = engine.place_order("BUY", Decimal("0.50"), Decimal("100"))

        # First partial fill
        engine.process_snapshot(OrderBookSnapshot(
            timestamp=1000,
            token_id="t",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.50"),
            bid_depth=Decimal("20"),
            ask_depth=Decimal("20"),
        ))

        order = engine.get_order(order_id)
        first_fill = order.filled_size
        assert first_fill > 0
        assert order.status == OrderStatus.PARTIAL

        # Second partial fill
        engine.process_snapshot(OrderBookSnapshot(
            timestamp=2000,
            token_id="t",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.50"),
            bid_depth=Decimal("30"),
            ask_depth=Decimal("30"),
        ))

        order = engine.get_order(order_id)
        assert order.filled_size > first_fill  # Accumulated

    def test_partial_fill_trade_record(self):
        """Trades are recorded for partial fills."""
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fill_model="depth_based",
            partial_fill_enabled=True,
            queue_position_factor=0.5,
        )

        order_id = engine.place_order("BUY", Decimal("0.50"), Decimal("100"))

        engine.process_snapshot(OrderBookSnapshot(
            timestamp=1000,
            token_id="t",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.50"),
            bid_depth=Decimal("20"),
            ask_depth=Decimal("20"),
        ))

        order = engine.get_order(order_id)
        if order.filled_size > 0:
            # Check trade record
            trades = [t for t in engine._trades if t["order_id"] == order_id]
            assert len(trades) > 0
            assert trades[0]["partial"] is True
            assert "size" in trades[0]

    def test_sell_partial_fill_pnl(self):
        """P&L calculated correctly on partial sell fills."""
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fill_model="simple",
        )

        # First, buy some position
        buy_id = engine.place_order("BUY", Decimal("0.50"), Decimal("100"))
        engine.process_snapshot(OrderBookSnapshot(
            timestamp=1000,
            token_id="t",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.50"),
            bid_depth=Decimal("1000"),
            ask_depth=Decimal("1000"),
        ))

        assert engine.position == Decimal("100")

        # Then partial sell at profit
        engine.fill_model = "depth_based"
        engine.partial_fill_enabled = True
        engine.queue_position_factor = 0.5

        sell_id = engine.place_order("SELL", Decimal("0.55"), Decimal("100"))
        engine.process_snapshot(OrderBookSnapshot(
            timestamp=2000,
            token_id="t",
            best_bid=Decimal("0.55"),
            best_ask=Decimal("0.56"),
            bid_depth=Decimal("20"),
            ask_depth=Decimal("20"),
        ))

        # Should have some realized P&L
        assert engine.realized_pnl > 0


class TestBacktestOrderProperties:
    """Test BacktestOrder new properties."""

    def test_remaining_size_calculation(self):
        """remaining_size calculated correctly."""
        order = BacktestOrder(
            id="test",
            side="BUY",
            price=Decimal("0.50"),
            size=Decimal("100"),
            filled_size=Decimal("30"),
        )
        assert order.remaining_size == Decimal("70")

    def test_fill_ratio_calculation(self):
        """fill_ratio calculated correctly."""
        order = BacktestOrder(
            id="test",
            side="BUY",
            price=Decimal("0.50"),
            size=Decimal("100"),
            filled_size=Decimal("50"),
        )
        assert order.fill_ratio == 0.5

    def test_fill_ratio_zero_size(self):
        """fill_ratio handles zero size gracefully."""
        order = BacktestOrder(
            id="test",
            side="BUY",
            price=Decimal("0.50"),
            size=Decimal("0"),
            filled_size=Decimal("0"),
        )
        assert order.fill_ratio == 0.0


class TestFillModelRun:
    """Test running backtest with different fill models."""

    @pytest.fixture
    def sample_data(self):
        """Create sample historical data."""
        import math
        data = HistoricalData()
        for i in range(50):
            offset = Decimal(str(math.sin(i * 0.1) * 0.02))
            data.add_snapshot(OrderBookSnapshot(
                timestamp=i * 1000,
                token_id="token-1",
                best_bid=Decimal("0.50") + offset,
                best_ask=Decimal("0.51") + offset,
                bid_depth=Decimal("50") + Decimal(str(i % 20)),
                ask_depth=Decimal("50") + Decimal(str(i % 20)),
            ))
        return data

    def test_run_with_simple_model(self, sample_data):
        """Run backtest with simple model."""
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fill_model="simple",
            partial_fill_enabled=False,
        )

        result = engine.run(sample_data, strategy="simple_mm")

        assert result.total_trades >= 0
        assert result.final_capital is not None

    def test_run_with_depth_based_model(self, sample_data):
        """Run backtest with depth-based model."""
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fill_model="depth_based",
            partial_fill_enabled=True,
        )

        result = engine.run(sample_data, strategy="simple_mm")

        assert result.total_trades >= 0
        assert result.final_capital is not None

    def test_run_with_probabilistic_model(self, sample_data):
        """Run backtest with probabilistic model."""
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fill_model="probabilistic",
            partial_fill_enabled=True,
        )

        result = engine.run(sample_data, strategy="simple_mm")

        assert result.total_trades >= 0
        assert result.final_capital is not None


class TestBackwardCompatibility:
    """Test backward compatibility with existing code."""

    def test_default_constructor_works(self):
        """Default constructor uses simple fill model."""
        engine = BacktestEngine(initial_capital=Decimal("1000"))

        assert engine.fill_model == "simple"
        assert engine.partial_fill_enabled is True

    def test_legacy_place_order_works(self):
        """place_order works as before."""
        engine = BacktestEngine(initial_capital=Decimal("1000"))
        order_id = engine.place_order("BUY", Decimal("0.50"), Decimal("10"))

        order = engine.get_order(order_id)
        assert order.side == "BUY"
        assert order.price == Decimal("0.50")
        assert order.size == Decimal("10")
        assert order.status == OrderStatus.OPEN

    def test_legacy_cancel_order_works(self):
        """cancel_order works as before."""
        engine = BacktestEngine(initial_capital=Decimal("1000"))
        order_id = engine.place_order("BUY", Decimal("0.50"), Decimal("10"))

        engine.cancel_order(order_id)

        order = engine.get_order(order_id)
        assert order.status == OrderStatus.CANCELLED

    def test_legacy_process_snapshot_works(self):
        """process_snapshot works as before."""
        engine = BacktestEngine(initial_capital=Decimal("1000"))
        order_id = engine.place_order("BUY", Decimal("0.50"), Decimal("10"))

        engine.process_snapshot(OrderBookSnapshot(
            timestamp=1000,
            token_id="t",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.50"),
            bid_depth=Decimal("100"),
            ask_depth=Decimal("100"),
        ))

        order = engine.get_order(order_id)
        assert order.is_filled  # Legacy property still works


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
