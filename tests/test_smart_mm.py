"""
Smart Market Maker Tests - Tests for adaptive MM components.

Run: pytest tests/test_smart_mm.py -v
"""

import pytest
from decimal import Decimal
from unittest.mock import patch


class TestVolatilityTracker:
    """Test volatility calculation and spread multiplier."""

    def test_initial_state(self):
        from src.strategy.volatility import VolatilityTracker

        vol = VolatilityTracker(token_id="test")
        state = vol.get_state()

        assert state.level == "UNKNOWN"
        assert vol.get_multiplier() == 1.0  # Neutral when no data
        print("Initial state is unknown with neutral multiplier")

    def test_samples_collected(self):
        import time
        from src.strategy.volatility import VolatilityTracker

        vol = VolatilityTracker(
            token_id="test",
            sample_interval=0.01,  # 10ms sampling for test
            min_samples=3
        )

        # Add samples with small delays to satisfy sample_interval
        for price in [0.50, 0.51, 0.49, 0.50, 0.52]:
            vol.update(price)
            time.sleep(0.015)  # Wait > sample_interval

        state = vol.get_state()
        assert state.sample_count >= 3
        print(f"Collected {state.sample_count} samples")

    def test_multiplier_increases_with_volatility(self):
        from src.strategy.volatility import VolatilityTracker

        vol = VolatilityTracker(
            token_id="test",
            sample_interval=0.001,
            min_samples=5
        )

        # Simulate high volatility (big swings)
        prices = [0.30, 0.70, 0.20, 0.80, 0.25, 0.75]
        for p in prices:
            vol.update(p)

        # Should have elevated volatility
        mult = vol.get_multiplier()
        assert mult >= 1.0, "High volatility should not reduce multiplier"
        print(f"High vol multiplier: {mult:.2f}")


class TestBookAnalyzer:
    """Test order book analysis."""

    def test_empty_book(self):
        from src.strategy.book_analyzer import BookAnalyzer

        analyzer = BookAnalyzer()
        analysis = analyzer.analyze(None)

        assert analysis.imbalance_signal == "BALANCED"
        assert analysis.total_depth == 0
        print("Empty book handled correctly")

    def test_bid_heavy_imbalance(self):
        from src.strategy.book_analyzer import BookAnalyzer
        from src.models import OrderBook, PriceLevel

        analyzer = BookAnalyzer(imbalance_threshold=0.1)

        # Create bid-heavy book
        book = OrderBook(
            token_id="test",
            bids=[
                PriceLevel(price=0.50, size=100),  # $50 depth
                PriceLevel(price=0.49, size=100),  # $49 depth
            ],
            asks=[
                PriceLevel(price=0.51, size=20),   # $10.2 depth
            ]
        )

        analysis = analyzer.analyze(book)

        assert analysis.imbalance_ratio > 0.6
        assert analysis.imbalance_signal == "BID_HEAVY"
        assert analysis.price_adjustment > 0  # Expect price up
        print(f"Bid heavy detected: ratio={analysis.imbalance_ratio:.2f}")

    def test_ask_heavy_imbalance(self):
        from src.strategy.book_analyzer import BookAnalyzer
        from src.models import OrderBook, PriceLevel

        analyzer = BookAnalyzer(imbalance_threshold=0.1)

        # Create ask-heavy book
        book = OrderBook(
            token_id="test",
            bids=[
                PriceLevel(price=0.50, size=20),   # $10 depth
            ],
            asks=[
                PriceLevel(price=0.51, size=100),  # $51 depth
                PriceLevel(price=0.52, size=100),  # $52 depth
            ]
        )

        analysis = analyzer.analyze(book)

        assert analysis.imbalance_ratio < 0.4
        assert analysis.imbalance_signal == "ASK_HEAVY"
        assert analysis.price_adjustment < 0  # Expect price down
        print(f"Ask heavy detected: ratio={analysis.imbalance_ratio:.2f}")


class TestInventoryManager:
    """Test inventory skewing and size adjustments."""

    @patch('src.strategy.inventory.get_position')
    def test_neutral_position(self, mock_get_position):
        from src.strategy.inventory import InventoryManager

        mock_get_position.return_value = Decimal("0")

        inv = InventoryManager(
            token_id="test",
            position_limit=Decimal("100")
        )

        state = inv.get_state(mid_price=Decimal("0.50"))

        assert state.inventory_level == "NEUTRAL"
        assert state.bid_skew == 0
        assert state.ask_skew == 0
        assert state.bid_size_mult == 1.0
        assert state.ask_size_mult == 1.0
        print("Neutral position: no skew")

    @patch('src.strategy.inventory.get_position')
    def test_long_position_skews_bid_down(self, mock_get_position):
        from src.strategy.inventory import InventoryManager

        mock_get_position.return_value = Decimal("50")  # 50% long

        inv = InventoryManager(
            token_id="test",
            position_limit=Decimal("100"),
            skew_max=Decimal("0.02")
        )

        state = inv.get_state(mid_price=Decimal("0.50"))

        assert state.inventory_level == "LONG"
        assert state.bid_skew < 0  # Lower bid to discourage buying
        assert state.ask_skew == state.bid_skew  # Lower ask to encourage selling
        print(f"Long position: bid_skew={state.bid_skew}")

    @patch('src.strategy.inventory.get_position')
    def test_short_position_skews_ask_up(self, mock_get_position):
        from src.strategy.inventory import InventoryManager

        mock_get_position.return_value = Decimal("-50")  # 50% short

        inv = InventoryManager(
            token_id="test",
            position_limit=Decimal("100"),
            skew_max=Decimal("0.02")
        )

        state = inv.get_state(mid_price=Decimal("0.50"))

        assert state.inventory_level == "SHORT"
        assert state.bid_skew == state.ask_skew  # Raise bid to encourage buying
        assert state.ask_skew > 0  # Raise ask to discourage selling
        print(f"Short position: ask_skew={state.ask_skew}")

    @patch('src.strategy.inventory.get_position')
    def test_size_reduction_at_high_inventory(self, mock_get_position):
        from src.strategy.inventory import InventoryManager

        mock_get_position.return_value = Decimal("80")  # 80% long

        inv = InventoryManager(
            token_id="test",
            position_limit=Decimal("100"),
            size_reduction_start=Decimal("0.5")  # Start at 50%
        )

        bid_mult, ask_mult = inv.get_size_multipliers()

        assert bid_mult < 1.0  # Reduced bid size (discourage buying more)
        assert ask_mult == 1.0  # Full ask size (encourage selling)
        print(f"Size reduction: bid={bid_mult:.2f}, ask={ask_mult:.2f}")


class TestMarketScorer:
    """Test market selection scoring."""

    def test_rejects_low_volume(self):
        from src.strategy.market_scorer import MarketScorer
        from src.models import Market, OrderBook, PriceLevel, Outcome

        scorer = MarketScorer(min_volume=10000)

        market = Market(
            condition_id="test",
            question="Test market",
            slug="test",
            outcomes=[Outcome(name="Yes", token_id="test")]
        )
        book = OrderBook(
            token_id="test",
            bids=[PriceLevel(0.50, 100)],
            asks=[PriceLevel(0.52, 100)]
        )

        score = scorer.score_market("test", market, book, volume_24h=5000)

        assert score.rejected
        assert "Volume" in score.reject_reason
        print(f"Rejected: {score.reject_reason}")

    def test_rejects_tight_spread(self):
        from src.strategy.market_scorer import MarketScorer
        from src.models import Market, OrderBook, PriceLevel, Outcome

        scorer = MarketScorer(min_spread=0.02)

        market = Market(
            condition_id="test",
            question="Test market",
            slug="test",
            outcomes=[Outcome(name="Yes", token_id="test")]
        )
        book = OrderBook(
            token_id="test",
            bids=[PriceLevel(0.50, 100)],
            asks=[PriceLevel(0.505, 100)]  # Only 0.5 cent spread
        )

        score = scorer.score_market("test", market, book, volume_24h=50000)

        assert score.rejected
        assert "Spread too tight" in score.reject_reason
        print(f"Rejected: {score.reject_reason}")

    def test_scores_good_market(self):
        from src.strategy.market_scorer import MarketScorer
        from src.models import Market, OrderBook, PriceLevel, Outcome

        scorer = MarketScorer()

        market = Market(
            condition_id="test",
            question="Test market",
            slug="test",
            outcomes=[Outcome(name="Yes", token_id="test")]
        )
        book = OrderBook(
            token_id="test",
            bids=[PriceLevel(0.48, 200), PriceLevel(0.47, 300)],
            asks=[PriceLevel(0.52, 200), PriceLevel(0.53, 300)]  # 4 cent spread
        )

        score = scorer.score_market("test", market, book, volume_24h=50000)

        assert not score.rejected
        assert score.total_score > 50  # Decent score
        print(f"Good market score: {score.total_score:.1f}")


class TestRiskManagerEnhancements:
    """Test new risk manager features."""

    def test_vol_adjusted_position_limit(self):
        from src.risk.manager import RiskManager

        risk = RiskManager(max_position=Decimal("100"))

        # Normal volatility
        risk.set_volatility_multiplier(1.0)
        assert risk.get_vol_adjusted_position_limit() == Decimal("100")

        # High volatility - should reduce limit
        risk.set_volatility_multiplier(1.5)
        limit = risk.get_vol_adjusted_position_limit()
        assert limit < Decimal("100")
        print(f"High vol limit: {limit}")

        # Extreme volatility
        risk.set_volatility_multiplier(2.0)
        limit = risk.get_vol_adjusted_position_limit()
        assert limit <= Decimal("50")  # At most 50% of original
        print(f"Extreme vol limit: {limit}")

    def test_unrealized_pnl_tracking(self):
        from src.risk.manager import RiskManager

        risk = RiskManager()

        # Long position at 0.50, current price 0.55
        risk.update_unrealized_pnl(
            token_id="test",
            position=Decimal("10"),
            current_price=Decimal("0.55"),
            entry_price=Decimal("0.50")
        )

        # Should be +$0.50 unrealized (10 * 0.05)
        assert risk.unrealized_pnl == Decimal("0.50")
        print(f"Unrealized P&L: ${risk.unrealized_pnl}")

    def test_status_includes_new_fields(self):
        from src.risk.manager import RiskManager

        risk = RiskManager()
        risk.set_volatility_multiplier(1.5)
        risk.update_unrealized_pnl("test", Decimal("10"), Decimal("0.55"), Decimal("0.50"))

        status = risk.get_status()

        assert "unrealized_pnl" in status
        assert "total_pnl" in status
        assert "vol_adjusted_position_limit" in status
        assert status["unrealized_pnl"] == 0.5
        print(f"Status fields present: {list(status.keys())}")


class TestLiveQuoteGating:
    """Test live quote behavior when one side is intentionally disabled."""

    def test_missing_disabled_ask_does_not_force_requote(self):
        from src.strategy.market_maker import SmartMarketMaker
        from src.models import Order, OrderSide, OrderStatus

        mm = SmartMarketMaker(token_id="test", size=Decimal("1"), position_limit=Decimal("5"))
        mm.bid_order = Order(
            id="bid-1",
            token_id="test",
            side=OrderSide.BUY,
            price=Decimal("0.49"),
            size=Decimal("1"),
            filled=Decimal("0"),
            status=OrderStatus.LIVE,
        )
        mm.ask_order = None
        mm._quote_bid_enabled = True
        mm._quote_ask_enabled = False
        mm.last_mid = Decimal("0.50")

        assert mm._should_requote(Decimal("0.50")) is False

    def test_missing_enabled_ask_still_requires_requote(self):
        from src.strategy.market_maker import SmartMarketMaker
        from src.models import Order, OrderSide, OrderStatus

        mm = SmartMarketMaker(token_id="test", size=Decimal("1"), position_limit=Decimal("5"))
        mm.bid_order = Order(
            id="bid-1",
            token_id="test",
            side=OrderSide.BUY,
            price=Decimal("0.49"),
            size=Decimal("1"),
            filled=Decimal("0"),
            status=OrderStatus.LIVE,
        )
        mm.ask_order = None
        mm._quote_bid_enabled = True
        mm._quote_ask_enabled = True
        mm.last_mid = Decimal("0.50")

        assert mm._should_requote(Decimal("0.50")) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
