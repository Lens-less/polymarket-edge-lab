"""
Phase 3 Verification Tests
Run with: pytest tests/test_phase3.py -v

Phase 3 is ONLY complete when all tests pass.
The default suite uses a deterministic fake at the external WebSocket dialer.
"""

import pytest
import asyncio
import json
from typing import List, Dict, Any


FIXED_TOKEN_ID = "99020283867209289889119738709824148614345922887757122199903300146152690995270"
FIXED_TOKEN_IDS = [
    FIXED_TOKEN_ID,
    "93407877955114874013681720453737167910717495090774867631160491764908235156561",
    "27219179698102068655351512215340723346847782831365166146109682870856219066167",
]


class FakeWebSocketConnection:
    """Deterministic stand-in for the external WebSocket system boundary."""

    def __init__(self) -> None:
        self.sent_messages: List[str] = []
        self.closed = False
        self.connect_args = ()
        self.connect_kwargs: Dict[str, Any] = {}
        self._incoming: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    async def recv(self) -> str:
        return await self._incoming.get()

    async def close(self) -> None:
        self.closed = True

    async def feed(self, message: str) -> None:
        await self._incoming.put(message)


@pytest.fixture
def fake_websocket(monkeypatch: pytest.MonkeyPatch) -> FakeWebSocketConnection:
    """Replace only the external WebSocket dialer; exercise MarketWebSocket itself."""
    import src.websocket_client as websocket_client

    connection = FakeWebSocketConnection()

    async def fake_connect(
        *args: Any,
        **kwargs: Any,
    ) -> FakeWebSocketConnection:
        connection.connect_args = args
        connection.connect_kwargs = kwargs
        return connection

    monkeypatch.setattr(websocket_client.websockets, "connect", fake_connect)
    return connection


class TestWebSocketClient:
    """Test WebSocket client functionality"""

    def _get_test_token_id(self) -> str:
        """Return a stable token identifier without performing market discovery."""
        return FIXED_TOKEN_ID

    def test_import_websocket_client(self):
        """Verify WebSocket client can be imported"""
        from src.websocket_client import MarketWebSocket, ConnectionState

        assert MarketWebSocket is not None
        assert ConnectionState is not None
        print("✓ WebSocket client imported successfully")

    def test_client_instantiation(self):
        """Verify WebSocket client can be instantiated"""
        from src.websocket_client import MarketWebSocket, ConnectionState

        ws = MarketWebSocket()

        assert ws.state == ConnectionState.DISCONNECTED
        assert ws.is_connected == False
        assert len(ws.subscribed_tokens) == 0

        print("✓ WebSocket client instantiated")
        print(f"  Initial state: {ws.state.value}")

    @pytest.mark.asyncio
    async def test_connect_disconnect(self, fake_websocket):
        """Verify connection state and close behavior without external I/O."""
        from src.websocket_client import MarketWebSocket, ConnectionState

        ws = MarketWebSocket()

        # Connect
        result = await ws.connect()
        assert result == True, "Connection should succeed"
        assert ws.state == ConnectionState.CONNECTED
        assert fake_websocket.connect_args == (ws.url,)
        print("✓ Connected to WebSocket server")

        # Disconnect
        await ws.disconnect()
        assert ws.state == ConnectionState.DISCONNECTED
        assert fake_websocket.closed is True
        print("✓ Disconnected from WebSocket server")

    @pytest.mark.asyncio
    async def test_subscribe_to_market(self, fake_websocket):
        """Verify subscription payload and observable state."""
        from src.websocket_client import MarketWebSocket, ConnectionState

        ws = MarketWebSocket()
        token_id = self._get_test_token_id()

        try:
            # Connect
            await ws.connect()
            assert ws.is_connected

            # Subscribe
            result = await ws.subscribe([token_id])
            assert result == True, "Subscription should succeed"
            assert ws.state == ConnectionState.SUBSCRIBED
            assert token_id in ws.subscribed_tokens
            assert json.loads(fake_websocket.sent_messages[-1]) == {
                "type": "market",
                "assets_ids": [token_id],
            }

            print(f"✓ Subscribed to token: {token_id[:20]}...")

        finally:
            await ws.disconnect()
            assert fake_websocket.closed is True

    @pytest.mark.skip(reason="Legacy Phase 3 WebSocket - superseded by Phase 3.5 MarketFeed")
    @pytest.mark.asyncio
    async def test_receive_market_data(self):
        """Verify we receive real-time market data"""
        from src.websocket_client import MarketWebSocket

        ws = MarketWebSocket()
        token_id = self._get_test_token_id()

        received_messages: List[Dict[str, Any]] = []

        def on_any_message(data: Dict[str, Any]):
            received_messages.append(data)
            print(f"  Received: {data.get('event_type')} for {data.get('asset_id', 'unknown')[:15]}...")

        # Set up callbacks for all message types
        ws.on_price_change = on_any_message
        ws.on_book_update = on_any_message
        ws.on_trade = on_any_message

        try:
            await ws.connect()
            await ws.subscribe([token_id])

            # Wait for messages (up to 60 seconds)
            print("  Waiting for market data (up to 60s)...")
            for i in range(60):
                await asyncio.sleep(1)
                if len(received_messages) >= 1:
                    break

            # We should have received at least some data
            # Note: Very illiquid markets might not have activity
            print(f"✓ Received {len(received_messages)} message(s)")

        finally:
            await ws.disconnect()

    @pytest.mark.skip(reason="Legacy Phase 3 WebSocket - superseded by Phase 3.5 MarketFeed")
    @pytest.mark.asyncio
    async def test_order_book_maintenance(self):
        """Verify local order book is maintained"""
        from src.websocket_client import MarketWebSocket

        ws = MarketWebSocket()
        token_id = self._get_test_token_id()

        book_updates = []

        def on_book(data):
            book_updates.append(data)

        ws.on_book_update = on_book

        try:
            await ws.connect()
            await ws.subscribe([token_id])

            # Wait for a book update (up to 60 seconds)
            print("  Waiting for order book update (up to 60s)...")
            for i in range(60):
                await asyncio.sleep(1)
                book = ws.get_order_book(token_id)
                if book and (book.bids or book.asks):
                    print(f"✓ Order book received:")
                    print(f"  Bids: {len(book.bids)}, Asks: {len(book.asks)}")
                    if book.midpoint:
                        print(f"  Midpoint: {book.midpoint:.4f}")
                    if book.spread:
                        print(f"  Spread: {book.spread:.4f}")
                    break

        finally:
            await ws.disconnect()

    @pytest.mark.asyncio
    async def test_callbacks_are_called(self, fake_websocket):
        """Verify callbacks are invoked through the receive loop."""
        from src.websocket_client import MarketWebSocket

        ws = MarketWebSocket()
        token_id = self._get_test_token_id()

        callback_events = {
            "connect": False,
            "disconnect": False,
            "price_change": False,
            "book_update": False,
            "trade": False
        }
        data_callbacks_done = asyncio.Event()

        ws.on_connect = lambda: callback_events.update({"connect": True})
        ws.on_disconnect = lambda: callback_events.update({"disconnect": True})

        def record_callback(name):
            callback_events[name] = True
            if all(callback_events[key] for key in ("price_change", "book_update", "trade")):
                data_callbacks_done.set()

        ws.on_price_change = lambda d: record_callback("price_change")
        ws.on_book_update = lambda d: record_callback("book_update")
        ws.on_trade = lambda d: record_callback("trade")

        try:
            await ws.connect()
            assert callback_events["connect"], "on_connect should be called"
            print("✓ on_connect callback fired")

            await ws.subscribe([token_id])

            # Feed the external boundary so the public receive loop drives callbacks.
            await fake_websocket.feed(json.dumps([
                {
                    "event_type": "book",
                    "asset_id": token_id,
                    "bids": [{"price": "0.45", "size": "100"}],
                    "asks": [{"price": "0.55", "size": "200"}],
                },
                {
                    "market": "test",
                    "price_changes": [
                        {"asset_id": token_id, "price": "0.50"}
                    ]
                },
                {
                    "event_type": "last_trade_price",
                    "asset_id": token_id,
                    "price": "0.50",
                    "size": "10",
                    "side": "BUY",
                }
            ]))
            await asyncio.wait_for(data_callbacks_done.wait(), timeout=1)

            assert callback_events["book_update"], "on_book_update should be called"
            assert callback_events["price_change"], "on_price_change should be called"
            assert callback_events["trade"], "on_trade should be called"

        finally:
            await ws.disconnect()
            assert callback_events["disconnect"], "on_disconnect should be called"
            print("✓ on_disconnect callback fired")

        print(f"✓ Callback status: {callback_events}")

    @pytest.mark.asyncio
    async def test_multiple_subscriptions(self, fake_websocket):
        """Verify one subscription can carry multiple stable token IDs."""
        from src.websocket_client import MarketWebSocket

        token_ids = FIXED_TOKEN_IDS

        ws = MarketWebSocket()

        try:
            await ws.connect()
            result = await ws.subscribe(token_ids)

            assert result == True
            assert len(ws.subscribed_tokens) == len(token_ids)
            assert json.loads(fake_websocket.sent_messages[-1]) == {
                "type": "market",
                "assets_ids": token_ids,
            }

            print(f"✓ Subscribed to {len(token_ids)} tokens simultaneously")

        finally:
            await ws.disconnect()
            assert fake_websocket.closed is True


class TestConnectionState:
    """Test connection state management"""

    def test_state_enum_values(self):
        """Verify connection states are properly defined"""
        from src.websocket_client import ConnectionState

        states = [
            ConnectionState.DISCONNECTED,
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.SUBSCRIBED,
            ConnectionState.RECONNECTING,
            ConnectionState.FAILED
        ]

        for state in states:
            assert state.value is not None

        print(f"✓ All {len(states)} connection states defined")


class TestMarketData:
    """Test MarketData container"""

    def test_market_data_creation(self):
        """Verify MarketData can be created"""
        from src.websocket_client import MarketData

        md = MarketData(token_id="test_token")

        assert md.token_id == "test_token"
        assert md.order_book is None
        assert md.last_price is None
        assert md.is_stale == True  # No updates yet

        print("✓ MarketData container created")

    def test_stale_data_detection(self):
        """Verify stale data detection works"""
        from src.websocket_client import MarketData
        import time

        md = MarketData(token_id="test")
        assert md.is_stale == True  # No data yet

        md.last_update_time = time.time()
        assert md.is_stale == False  # Just updated

        md.last_update_time = time.time() - 120  # 2 minutes ago
        assert md.is_stale == True  # Too old

        print("✓ Stale data detection works")


class TestIntegration:
    """Integration tests for Phase 3"""

    @pytest.mark.asyncio
    async def test_full_websocket_flow(self, fake_websocket):
        """Test complete deterministic WebSocket flow through public behavior."""
        from src.websocket_client import MarketWebSocket, ConnectionState

        token_id = FIXED_TOKEN_ID

        # 2. Set up WebSocket
        ws = MarketWebSocket()
        message_count = 0
        processed = asyncio.Event()

        def count_messages(data):
            nonlocal message_count
            message_count += 1
            if message_count >= 2:
                processed.set()

        ws.on_price_change = count_messages
        ws.on_book_update = count_messages
        ws.on_trade = count_messages

        try:
            # 3. Connect
            connected = await ws.connect()
            assert connected
            assert ws.state == ConnectionState.CONNECTED
            print("  ✓ Connected")

            # 4. Subscribe
            subscribed = await ws.subscribe([token_id])
            assert subscribed
            assert ws.state == ConnectionState.SUBSCRIBED
            print("  ✓ Subscribed")

            # 5. Verify end-to-end processing on a deterministic boundary payload.
            print("  Processing sample market data...")
            await fake_websocket.feed(json.dumps([
                {
                    "event_type": "book",
                    "asset_id": token_id,
                    "bids": [{"price": "0.45", "size": "100"}],
                    "asks": [{"price": "0.55", "size": "200"}],
                },
                {
                    "market": "test",
                    "price_changes": [
                        {"asset_id": token_id, "price": "0.50"}
                    ]
                }
            ]))
            await asyncio.wait_for(processed.wait(), timeout=1)

            # 6. Check we got something
            market_data = ws.get_market_data(token_id)
            assert market_data is not None
            assert market_data.order_book is not None
            assert market_data.last_price == pytest.approx(0.50)
            print(f"  ✓ Processed {message_count} messages")

            if market_data.order_book:
                book = market_data.order_book
                print(f"  ✓ Order book: {len(book.bids)} bids, {len(book.asks)} asks")

        finally:
            # 7. Disconnect
            await ws.disconnect()
            assert ws.state == ConnectionState.DISCONNECTED
            assert fake_websocket.closed is True
            print("  ✓ Disconnected")

        print("✓ Full WebSocket flow completed successfully")
