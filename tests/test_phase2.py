"""
Phase 2 Verification Tests
Run with: pytest tests/test_phase2.py -v

Phase 2 is ONLY complete when all tests pass.
"""

import pytest


class TestModels:
    """Test data models"""

    def test_price_level_creation(self):
        """Verify PriceLevel model works"""
        from src.models import PriceLevel

        level = PriceLevel(price=0.55, size=100.0)
        assert level.price == 0.55
        assert level.size == 100.0
        print("✓ PriceLevel model works")

    def test_order_book_properties(self):
        """Verify OrderBook computed properties"""
        from src.models import OrderBook, PriceLevel

        book = OrderBook(
            token_id="test_token",
            bids=[PriceLevel(0.50, 100), PriceLevel(0.49, 200)],
            asks=[PriceLevel(0.52, 150), PriceLevel(0.53, 250)]
        )

        assert book.best_bid == 0.50
        assert book.best_ask == 0.52
        assert book.spread == pytest.approx(0.02)
        assert book.midpoint == pytest.approx(0.51)
        print(f"✓ OrderBook properties: bid={book.best_bid}, ask={book.best_ask}, spread={book.spread}")

    def test_market_model(self):
        """Verify Market model works"""
        from src.models import Market, Outcome

        market = Market(
            condition_id="0x123",
            question="Test question?",
            slug="test-question",
            outcomes=[
                Outcome(name="Yes", token_id="token1"),
                Outcome(name="No", token_id="token2")
            ]
        )

        assert len(market.token_ids) == 2
        assert "token1" in market.token_ids
        print(f"✓ Market model works: {len(market.outcomes)} outcomes")


@pytest.mark.network
class TestMarketDiscovery:
    """Test market discovery from Gamma API"""

    def test_fetch_active_markets(self):
        """Verify we can fetch active markets"""
        from src.markets import fetch_active_markets

        markets = fetch_active_markets(limit=5)

        assert len(markets) > 0, "Should return at least one market"
        assert markets[0].condition_id, "Market should have condition_id"
        assert markets[0].question, "Market should have question"

        print(f"✓ Fetched {len(markets)} active markets")
        print(f"  First market: {markets[0].question[:50]}...")

    def test_market_has_token_ids(self):
        """Verify markets have token IDs for trading"""
        from src.markets import fetch_active_markets

        markets = fetch_active_markets(limit=5)

        # Find a market with token IDs
        market_with_tokens = None
        for m in markets:
            if m.token_ids:
                market_with_tokens = m
                break

        assert market_with_tokens is not None, "Should find at least one market with tokens"
        assert len(market_with_tokens.token_ids) > 0, "Market should have token IDs"

        print(f"✓ Market has {len(market_with_tokens.token_ids)} token(s)")
        print(f"  Token ID: {market_with_tokens.token_ids[0][:20]}...")

    def test_fetch_events(self):
        """Verify we can fetch events"""
        from src.markets import fetch_events

        events = fetch_events(limit=3)

        assert len(events) > 0, "Should return at least one event"
        assert events[0].title, "Event should have title"

        print(f"✓ Fetched {len(events)} events")
        print(f"  First event: {events[0].title[:50]}...")


@pytest.mark.network
class TestPricing:
    """Test pricing and order book fetching"""

    def _get_test_token_id(self):
        """Helper to get a valid token ID for testing"""
        from src.markets import fetch_active_markets

        markets = fetch_active_markets(limit=10)
        for m in markets:
            if m.token_ids:
                return m.token_ids[0]
        pytest.skip("No markets with token IDs found")

    def test_get_midpoint(self):
        """Verify we can get midpoint price"""
        from src.pricing import get_midpoint

        token_id = self._get_test_token_id()
        mid = get_midpoint(token_id)

        # Midpoint might be None for illiquid markets, but function should work
        assert mid is None or (0 <= mid <= 1), "Midpoint should be between 0 and 1"

        print(f"✓ Midpoint: {mid}")

    def test_get_price(self):
        """Verify we can get best price"""
        from src.pricing import get_price

        token_id = self._get_test_token_id()

        buy_price = get_price(token_id, "BUY")
        sell_price = get_price(token_id, "SELL")

        print(f"✓ Prices - Buy: {buy_price}, Sell: {sell_price}")

    def test_get_order_book(self):
        """Verify we can get full order book"""
        from src.pricing import get_order_book

        token_id = self._get_test_token_id()
        book = get_order_book(token_id)

        assert book is not None, "Should return order book"
        assert book.token_id == token_id, "Token ID should match"

        print(f"✓ Order book: {len(book.bids)} bids, {len(book.asks)} asks")
        if book.best_bid and book.best_ask:
            print(f"  Best bid: {book.best_bid}, Best ask: {book.best_ask}")
            print(f"  Spread: {book.spread:.4f}")

    def test_get_spread(self):
        """Verify spread calculation"""
        from src.pricing import get_spread, get_spread_percentage

        token_id = self._get_test_token_id()

        spread = get_spread(token_id)
        spread_pct = get_spread_percentage(token_id)

        print(f"✓ Spread: {spread}, Spread %: {spread_pct}")


@pytest.mark.network
class TestIntegration:
    """Integration tests combining market discovery and pricing"""

    def test_full_market_data_flow(self):
        """Test complete flow: discover market, get prices"""
        from src.markets import fetch_active_markets
        from src.pricing import get_order_book

        # 1. Fetch markets
        markets = fetch_active_markets(limit=3)
        assert len(markets) > 0

        # 2. Find a market with tokens
        test_market = None
        for m in markets:
            if m.token_ids:
                test_market = m
                break

        assert test_market is not None, "Need a market with tokens"

        # 3. Get order book for first token
        token_id = test_market.token_ids[0]
        book = get_order_book(token_id)

        assert book is not None

        print(f"✓ Full flow successful:")
        print(f"  Market: {test_market.question[:40]}...")
        print(f"  Token: {token_id[:20]}...")
        print(f"  Book: {len(book.bids)} bids, {len(book.asks)} asks")
        if book.midpoint:
            print(f"  Midpoint: {book.midpoint:.4f}")
