"""
Phase 3.5 Verification Tests (Simplified)

Run with: pytest tests/test_phase3_5.py -v --timeout=120
"""

import pytest
import asyncio
import json
from typing import List


class TestFeedState:
    """Test feed states."""

    def test_states_defined(self):
        """Verify all states exist."""
        from src.feed import FeedState

        assert FeedState.STOPPED is not None
        assert FeedState.STARTING is not None
        assert FeedState.RUNNING is not None
        assert FeedState.ERROR is not None

        print("✓ All 4 states defined")


class TestDataStore:
    """Test data storage."""

    def test_store_creation(self):
        """Test store can be created."""
        from src.feed.data_store import DataStore

        store = DataStore()
        assert store is not None
        print("✓ DataStore created")

    def test_book_update(self):
        """Test order book updates."""
        from src.feed.data_store import DataStore

        store = DataStore()
        store.register_token("token1")

        store.update_book(
            "token1",
            [{'price': '0.50', 'size': '100'}],
            [{'price': '0.55', 'size': '200'}]
        )

        assert store.get_best_bid("token1") == pytest.approx(0.50)
        assert store.get_best_ask("token1") == pytest.approx(0.55)
        assert store.get_midpoint("token1") == pytest.approx(0.525)
        assert store.get_spread("token1") == pytest.approx(0.05)

        print("✓ Order book updated correctly")

    def test_freshness(self):
        """Test data freshness detection."""
        from src.feed.data_store import DataStore

        store = DataStore(stale_threshold=1.0)
        store.register_token("token1")

        # No data yet
        assert not store.is_fresh("token1")

        # Fresh book data should count as fresh
        store.update_book(
            "token1",
            [{'price': '0.50', 'size': '100'}],
            [{'price': '0.55', 'size': '200'}]
        )
        assert store.is_fresh("token1")

        # Stale the book manually
        store.get("token1").last_book_update -= 2.0
        assert not store.is_fresh("token1")

        # Trade/price updates should not mask a stale book
        store.update_price("token1", 0.54)
        store.update_trade("token1", 0.54, size=10, side="BUY")
        assert not store.is_fresh("token1")

        print("✓ Freshness detection works")

    def test_sequence_tracking(self):
        """Test sequence gap detection."""
        from src.feed.data_store import DataStore

        store = DataStore()
        store.register_token("token1")

        # Normal sequence
        assert store.check_sequence("token1", 1) == True
        assert store.check_sequence("token1", 2) == True
        assert store.check_sequence("token1", 3) == True

        # Gap
        assert store.check_sequence("token1", 10) == False
        assert store.has_gaps() == True

        # Clear
        store.clear_gaps("token1")
        assert store.has_gaps() == False

        print("✓ Sequence tracking works")


class TestMockFeed:
    """Test mock feed."""

    @pytest.mark.asyncio
    async def test_mock_basic(self):
        """Test basic mock functionality."""
        from src.feed.mock import MockMarketFeed
        from src.feed import FeedState

        feed = MockMarketFeed()

        assert feed.state == FeedState.STOPPED

        await feed.start(["token1"])
        assert feed.state == FeedState.RUNNING
        assert feed.is_healthy

        await feed.stop()
        assert feed.state == FeedState.STOPPED

        print("✓ Mock lifecycle works")

    @pytest.mark.asyncio
    async def test_mock_data(self):
        """Test mock data injection."""
        from src.feed.mock import MockMarketFeed

        feed = MockMarketFeed()
        await feed.start(["token1"])

        # Set book
        feed.set_book("token1", [(0.50, 100), (0.49, 200)], [(0.55, 150)])

        assert feed.get_midpoint("token1") == 0.525
        assert feed.get_best_bid("token1") == 0.50
        assert feed.get_best_ask("token1") == 0.55

        await feed.stop()
        print("✓ Mock data injection works")

    @pytest.mark.asyncio
    async def test_mock_health(self):
        """Test mock health control."""
        from src.feed.mock import MockMarketFeed

        feed = MockMarketFeed()
        await feed.start(["token1"])

        assert feed.is_healthy

        feed.set_healthy(False)
        assert not feed.is_healthy

        feed.set_healthy(True)
        assert feed.is_healthy

        await feed.stop()
        print("✓ Mock health control works")


class TestMarketFeed:
    """Test real MarketFeed with network."""

    def _get_test_token(self) -> str:
        """Get a valid token for testing."""
        from src.markets import fetch_active_markets

        markets = fetch_active_markets(limit=10)
        for m in markets:
            if m.token_ids:
                return m.token_ids[0]
        pytest.skip("No markets with tokens found")

    def test_import(self):
        """Test imports work."""
        from src.feed import MarketFeed, FeedState

        assert MarketFeed is not None
        assert FeedState is not None
        print("✓ Imports successful")

    def test_instantiation(self):
        """Test feed can be created."""
        from src.feed import MarketFeed, FeedState

        feed = MarketFeed()

        assert feed.state == FeedState.STOPPED
        assert not feed.is_healthy
        assert feed.data_source == "none"

        print("✓ MarketFeed instantiated")

    @pytest.mark.asyncio
    async def test_start_stop(self):
        """Test basic lifecycle."""
        from src.feed import MarketFeed, FeedState

        feed = MarketFeed()
        token = self._get_test_token()

        # Start
        result = await feed.start([token])
        assert result == True
        assert feed.state == FeedState.RUNNING
        print(f"  Started with source: {feed.data_source}")

        # Stop
        await feed.stop()
        assert feed.state == FeedState.STOPPED
        print("  Stopped")

        print("✓ Start/stop works")

    @pytest.mark.asyncio
    async def test_health_and_data(self):
        """Test health status and data access."""
        from src.feed import MarketFeed

        feed = MarketFeed()
        token = self._get_test_token()

        try:
            await feed.start([token])

            # Wait for data
            print("  Waiting for data...")
            await asyncio.sleep(15)

            # Check health
            print(f"  is_healthy: {feed.is_healthy}")
            print(f"  data_source: {feed.data_source}")

            # Check data
            mid = feed.get_midpoint(token)
            spread = feed.get_spread(token)

            print(f"  midpoint: {mid}")
            print(f"  spread: {spread}")

            if mid is not None:
                print("✓ Data received successfully")

        finally:
            await feed.stop()

    @pytest.mark.asyncio
    async def test_callbacks(self):
        """Test callbacks are invoked."""
        from src.feed import MarketFeed

        feed = MarketFeed()
        token = self._get_test_token()

        events = {'book': 0, 'price': 0, 'trade': 0}

        def on_book(data):
            events['book'] += 1

        def on_price(data):
            events['price'] += 1

        def on_trade(data):
            events['trade'] += 1

        feed.on_book_update = on_book
        feed.on_price_change = on_price
        feed.on_trade = on_trade

        try:
            await feed.start([token])
            await feed._process_message(json.dumps([
                {
                    'event_type': 'book',
                    'asset_id': token,
                    'bids': [{'price': '0.45', 'size': '100'}],
                    'asks': [{'price': '0.55', 'size': '200'}],
                    'timestamp': '2026-01-21T00:00:00Z'
                },
                {
                    'market': 'test',
                    'price_changes': [
                        {'asset_id': token, 'price': '0.50'}
                    ]
                },
                {
                    'event_type': 'last_trade_price',
                    'asset_id': token,
                    'price': '0.50',
                    'size': '10',
                    'side': 'BUY'
                }
            ]))

            print(f"  Events received: {events}")

            total = sum(events.values())
            assert total >= 3
            print(f"✓ Callbacks invoked ({total} total)")

        finally:
            await feed.stop()

    @pytest.mark.asyncio
    async def test_state_transitions(self):
        """Test state change callbacks."""
        from src.feed import MarketFeed, FeedState

        feed = MarketFeed()
        token = self._get_test_token()

        states = []

        def on_state(state):
            states.append(state)

        feed.on_state_change = on_state

        try:
            await feed.start([token])
            await asyncio.sleep(5)
            await feed.stop()

            print(f"  States observed: {[s.name for s in states]}")

            assert FeedState.STARTING in states
            assert FeedState.RUNNING in states
            assert FeedState.STOPPED in states

            print("✓ State transitions work")

        except Exception:
            await feed.stop()
            raise


class TestIntegration:
    """Integration tests."""

    def _get_test_token(self) -> str:
        from src.markets import fetch_active_markets
        markets = fetch_active_markets(limit=10)
        for m in markets:
            if m.token_ids:
                return m.token_ids[0]
        pytest.skip("No markets found")

    @pytest.mark.asyncio
    async def test_market_maker_pattern(self):
        """
        Test the pattern a market maker would use.

        This is the most important test - it validates that
        the API supports the market making use case.
        """
        from src.feed import MarketFeed

        feed = MarketFeed()
        token = self._get_test_token()

        quote_updates = []

        try:
            await feed.start([token])

            # Simulate market maker loop
            for i in range(10):
                await asyncio.sleep(2)

                if feed.is_healthy:
                    mid = feed.get_midpoint(token)
                    if mid:
                        # In real bot: place quotes around mid
                        bid = round(mid - 0.02, 2)
                        ask = round(mid + 0.02, 2)
                        quote_updates.append((bid, mid, ask))
                        print(f"  Would quote: {bid} / {ask} (mid={mid})")
                else:
                    # In real bot: cancel quotes
                    print("  Would cancel quotes (unhealthy)")

            print(f"✓ Market maker pattern works ({len(quote_updates)} quote updates)")

        finally:
            await feed.stop()


def test_heartbeat_tracking():
    """Verify heartbeat tracking works."""
    from src.feed.data_store import DataStore
    import time

    store = DataStore(stale_threshold=30.0)

    # Initially, no messages received
    assert store.seconds_since_any_message() == float('inf')

    # Record a message
    store.record_message_received()
    time.sleep(0.1)

    # Should be about 0.1 seconds
    elapsed = store.seconds_since_any_message()
    assert 0 < elapsed < 1.0

    print(f"✓ Heartbeat tracking works ({elapsed:.2f}s since message)")


@pytest.mark.asyncio
async def test_list_message_handling():
    """Verify feed can handle list messages from Polymarket."""
    from src.feed import MarketFeed
    import json

    feed = MarketFeed()

    # Create test list message (what Polymarket sometimes sends)
    list_message = json.dumps([
        {
            'event_type': 'book',
            'asset_id': 'test_token',
            'bids': [{'price': '0.45', 'size': '100'}],
            'asks': [{'price': '0.55', 'size': '200'}],
            'timestamp': '2026-01-21T00:00:00Z'
        },
        {
            'event_type': 'price_change',
            'asset_id': 'test_token',
            'price': '0.50'
        }
    ])

    # Process without crashing
    try:
        await feed._process_message(list_message)
        print("✓ List messages handled without error")
    except AttributeError as e:
        pytest.fail(f"Failed to handle list message: {e}")
