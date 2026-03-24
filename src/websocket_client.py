"""
WebSocket client for real-time Polymarket market data.

⚠️ DEPRECATED: This is the Phase 3 WebSocket implementation.

Use MarketFeed from src/feed/ instead (Phase 3.5+), which provides:
- Better error handling
- Automatic REST fallback
- Health monitoring
- Simplified interface

This module is kept for historical/educational purposes only.
Production code should use: from src.feed import MarketFeed
"""

import json
import asyncio
import time
import ssl
from enum import Enum
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass
import certifi
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK
)

from src.config import (
    WS_MARKET_URL,
    WS_RECONNECT_ATTEMPTS,
    WS_RECONNECT_BASE_DELAY,
    WS_RECONNECT_MAX_DELAY,
    WS_HEARTBEAT_INTERVAL,
    WS_STALE_DATA_THRESHOLD
)
from src.models import OrderBook, PriceLevel
from src.utils import setup_logging

logger = setup_logging()


class ConnectionState(Enum):
    """WebSocket connection states"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    SUBSCRIBED = "subscribed"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class MarketData:
    """Container for real-time market data"""
    token_id: str
    order_book: Optional[OrderBook] = None
    last_price: Optional[float] = None
    last_trade_price: Optional[float] = None
    last_trade_side: Optional[str] = None
    last_trade_size: Optional[float] = None
    last_update_time: float = 0.0
    tick_size: str = "0.01"

    @property
    def is_stale(self) -> bool:
        """Check if data is stale (no updates for threshold period)"""
        if self.last_update_time == 0:
            return True
        return (time.time() - self.last_update_time) > WS_STALE_DATA_THRESHOLD


class MarketWebSocket:
    """
    WebSocket client for Polymarket market data.

    Handles:
    - Connection management with automatic reconnection
    - Market channel subscriptions
    - Real-time order book updates
    - Price change notifications
    - Trade notifications

    Usage:
        ws = MarketWebSocket()
        ws.on_price_change = my_price_handler
        ws.on_book_update = my_book_handler

        await ws.connect()
        await ws.subscribe(["token_id_1", "token_id_2"])

        # ... run your logic ...

        await ws.disconnect()
    """

    def __init__(self, url: str = WS_MARKET_URL):
        self.url = url
        self._ws: Optional[ClientConnection] = None
        self._state = ConnectionState.DISCONNECTED
        self._subscribed_tokens: List[str] = []
        self._market_data: Dict[str, MarketData] = {}

        # Reconnection state
        self._reconnect_attempt = 0
        self._should_reconnect = True

        # Background tasks
        self._receive_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

        # Callbacks (user-provided)
        self.on_price_change: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_book_update: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_trade: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_tick_size_change: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None
        self.on_connect: Optional[Callable[[], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None

    @property
    def state(self) -> ConnectionState:
        """Current connection state"""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected and subscribed"""
        return self._state in (ConnectionState.CONNECTED, ConnectionState.SUBSCRIBED)

    @property
    def subscribed_tokens(self) -> List[str]:
        """List of currently subscribed token IDs"""
        return self._subscribed_tokens.copy()

    def get_market_data(self, token_id: str) -> Optional[MarketData]:
        """Get current market data for a token"""
        return self._market_data.get(token_id)

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Get current order book for a token"""
        data = self._market_data.get(token_id)
        return data.order_book if data else None

    async def connect(self) -> bool:
        """
        Establish WebSocket connection.

        Returns:
            True if connection successful, False otherwise
        """
        if self._state in (ConnectionState.CONNECTED, ConnectionState.SUBSCRIBED):
            logger.warning("Already connected")
            return True

        self._state = ConnectionState.CONNECTING
        self._should_reconnect = True

        try:
            logger.info(f"Connecting to {self.url}")
            # Create SSL context with certifi certificates for macOS compatibility
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            self._ws = await websockets.connect(
                self.url,
                ssl=ssl_context,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            )

            self._state = ConnectionState.CONNECTED
            self._reconnect_attempt = 0

            # Start background tasks
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            logger.info("WebSocket connected")

            if self.on_connect:
                self.on_connect()

            return True

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._state = ConnectionState.FAILED

            if self.on_error:
                self.on_error(e)

            return False

    async def disconnect(self):
        """Disconnect WebSocket and cleanup"""
        logger.info("Disconnecting WebSocket")
        self._should_reconnect = False

        # Cancel background tasks
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Close WebSocket
        if self._ws:
            try:
                await self._ws.close()
            except Exception as e:
                logger.debug(f"Error closing WebSocket: {e}")

        self._ws = None
        self._state = ConnectionState.DISCONNECTED
        self._subscribed_tokens = []

        if self.on_disconnect:
            self.on_disconnect()

        logger.info("WebSocket disconnected")

    async def subscribe(self, token_ids: List[str]) -> bool:
        """
        Subscribe to market data for given tokens.

        Args:
            token_ids: List of token IDs to subscribe to

        Returns:
            True if subscription sent successfully
        """
        if not self.is_connected:
            logger.error("Cannot subscribe: not connected")
            return False

        if not token_ids:
            logger.warning("No token IDs provided")
            return False

        # Build subscription message
        message = {
            "type": "market",
            "assets_ids": token_ids
        }

        try:
            if self._ws is None:
                logger.error("WebSocket not connected")
                return False
            await self._ws.send(json.dumps(message))

            # Initialize market data containers
            for token_id in token_ids:
                if token_id not in self._market_data:
                    self._market_data[token_id] = MarketData(token_id=token_id)
                if token_id not in self._subscribed_tokens:
                    self._subscribed_tokens.append(token_id)

            self._state = ConnectionState.SUBSCRIBED
            logger.info(f"Subscribed to {len(token_ids)} token(s)")

            return True

        except Exception as e:
            logger.error(f"Subscription failed: {e}")
            return False

    async def unsubscribe(self, token_ids: List[str]) -> bool:
        """
        Unsubscribe from specific tokens.

        Args:
            token_ids: List of token IDs to unsubscribe from

        Returns:
            True if successful
        """
        # Remove from tracking
        for token_id in token_ids:
            if token_id in self._subscribed_tokens:
                self._subscribed_tokens.remove(token_id)
            if token_id in self._market_data:
                del self._market_data[token_id]

        # Re-subscribe with remaining tokens
        if self._subscribed_tokens:
            return await self.subscribe(self._subscribed_tokens)

        return True

    async def _receive_loop(self):
        """Background task to receive and process messages"""
        while self._should_reconnect:
            try:
                if not self._ws:
                    await asyncio.sleep(0.1)
                    continue

                raw_message = await self._ws.recv()
                # Handle both str and bytes messages
                message_str: str = raw_message.decode("utf-8") if isinstance(raw_message, bytes) else raw_message
                await self._handle_message(message_str)

            except ConnectionClosedOK:
                logger.info("WebSocket closed normally")
                break

            except (ConnectionClosed, ConnectionClosedError) as e:
                logger.warning(f"Connection closed: {e}")
                if self._should_reconnect:
                    await self._reconnect()
                break

            except asyncio.CancelledError:
                break

            except Exception as e:
                logger.error(f"Error in receive loop: {e}")
                if self.on_error:
                    self.on_error(e)

    async def _heartbeat_loop(self):
        """Background task to send periodic heartbeats"""
        while self._should_reconnect:
            try:
                await asyncio.sleep(WS_HEARTBEAT_INTERVAL)

                if self._ws and self.is_connected:
                    # WebSockets library handles ping/pong automatically
                    # but we can check for stale connections here
                    stale_tokens = [
                        t for t, d in self._market_data.items()
                        if d.is_stale
                    ]
                    if stale_tokens:
                        logger.warning(f"Stale data detected for {len(stale_tokens)} token(s)")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Heartbeat error: {e}")

    async def _reconnect(self):
        """Attempt to reconnect with exponential backoff"""
        if not self._should_reconnect:
            return

        self._state = ConnectionState.RECONNECTING

        while self._reconnect_attempt < WS_RECONNECT_ATTEMPTS:
            self._reconnect_attempt += 1

            # Calculate delay with exponential backoff
            delay = min(
                WS_RECONNECT_BASE_DELAY * (2 ** (self._reconnect_attempt - 1)),
                WS_RECONNECT_MAX_DELAY
            )

            logger.info(
                f"Reconnect attempt {self._reconnect_attempt}/{WS_RECONNECT_ATTEMPTS} "
                f"in {delay:.1f}s"
            )

            await asyncio.sleep(delay)

            if not self._should_reconnect:
                return

            # Try to reconnect
            try:
                ssl_context = ssl.create_default_context(cafile=certifi.where())
                self._ws = await websockets.connect(
                    self.url,
                    ssl=ssl_context,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                )

                self._state = ConnectionState.CONNECTED
                self._reconnect_attempt = 0

                logger.info("Reconnected successfully")

                # Re-subscribe to previous tokens
                if self._subscribed_tokens:
                    await self.subscribe(self._subscribed_tokens)

                # Restart receive loop
                self._receive_task = asyncio.create_task(self._receive_loop())

                if self.on_connect:
                    self.on_connect()

                return

            except Exception as e:
                logger.warning(f"Reconnect attempt failed: {e}")

        # Max retries exceeded
        logger.error("Max reconnection attempts exceeded")
        self._state = ConnectionState.FAILED

        if self.on_error:
            self.on_error(Exception("Max reconnection attempts exceeded"))

    async def _handle_message(self, raw_message: str):
        """Parse and route incoming messages"""
        try:
            data = json.loads(raw_message)
            await self._process_message_payload(data, raw_message)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse message: {e}")
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    async def _process_message_payload(self, data: Any, raw_message: str = ""):
        """Handle the Polymarket WS payload shapes we see in production."""
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._process_message_payload(item, raw_message)
            return

        if not isinstance(data, dict):
            logger.debug(f"Unknown message format: {raw_message[:100]}")
            return

        price_changes = data.get("price_changes")
        if isinstance(price_changes, list):
            for change in price_changes:
                if not isinstance(change, dict):
                    continue
                normalized_change = dict(change)
                normalized_change.setdefault("event_type", "price_change")
                await self._process_message_payload(normalized_change, raw_message)
            return

        event_type = data.get("event_type")
        asset_id = data.get("asset_id")

        if not event_type:
            logger.debug(f"Unknown message format: {raw_message[:100]}")
            return

        # Update last update time
        if asset_id and asset_id in self._market_data:
            self._market_data[asset_id].last_update_time = time.time()

        # Route to appropriate handler
        if event_type == "price_change":
            self._handle_price_change(data)
        elif event_type == "book":
            self._handle_book_update(data)
        elif event_type == "last_trade_price":
            self._handle_trade(data)
        elif event_type == "tick_size_change":
            self._handle_tick_size_change(data)
        else:
            logger.debug(f"Unhandled event type: {event_type}")

    def _handle_price_change(self, data: Dict[str, Any]):
        """Handle price_change events"""
        asset_id = data.get("asset_id")

        if asset_id and asset_id in self._market_data:
            price = data.get("price")
            if price:
                self._market_data[asset_id].last_price = float(price)

        if self.on_price_change:
            self.on_price_change(data)

    def _handle_book_update(self, data: Dict[str, Any]):
        """Handle book events (order book updates)"""
        asset_id = data.get("asset_id")

        if asset_id and asset_id in self._market_data:
            # Parse order book
            bids = []
            asks = []

            for bid in data.get("bids", []):
                if isinstance(bid, dict):
                    bids.append(PriceLevel(
                        price=float(bid.get("price", 0)),
                        size=float(bid.get("size", 0))
                    ))

            for ask in data.get("asks", []):
                if isinstance(ask, dict):
                    asks.append(PriceLevel(
                        price=float(ask.get("price", 0)),
                        size=float(ask.get("size", 0))
                    ))

            # Sort properly
            bids.sort(key=lambda x: x.price, reverse=True)
            asks.sort(key=lambda x: x.price)

            self._market_data[asset_id].order_book = OrderBook(
                token_id=asset_id,
                bids=bids,
                asks=asks,
                timestamp=data.get("timestamp")
            )

        if self.on_book_update:
            self.on_book_update(data)

    def _handle_trade(self, data: Dict[str, Any]):
        """Handle last_trade_price events"""
        asset_id = data.get("asset_id")

        if asset_id and asset_id in self._market_data:
            md = self._market_data[asset_id]
            md.last_trade_price = float(data.get("price", 0))
            md.last_trade_side = data.get("side")
            md.last_trade_size = float(data.get("size", 0)) if data.get("size") else None

        if self.on_trade:
            self.on_trade(data)

    def _handle_tick_size_change(self, data: Dict[str, Any]):
        """Handle tick_size_change events"""
        asset_id = data.get("asset_id")

        if asset_id and asset_id in self._market_data:
            new_tick = data.get("new_tick_size")
            if new_tick:
                self._market_data[asset_id].tick_size = new_tick

        if self.on_tick_size_change:
            self.on_tick_size_change(data)
