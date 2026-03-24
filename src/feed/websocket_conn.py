"""
WebSocket connection management.

Internal component - use MarketFeed instead.
"""

import json
import asyncio
import ssl
import certifi
from typing import Optional, List, Callable, Dict, Any
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK

from src.config import (
    WS_MARKET_URL,
    WS_RECONNECT_ATTEMPTS,
    WS_RECONNECT_BASE_DELAY,
    WS_RECONNECT_MAX_DELAY,
)
from src.utils import setup_logging

logger = setup_logging()


class WebSocketConnection:
    """
    Low-level WebSocket connection handler.

    Responsibilities:
    - Connect/disconnect
    - Send subscription requests
    - Receive messages and pass to callback
    - Auto-reconnect with exponential backoff
    """

    def __init__(self, url: str = WS_MARKET_URL):
        self.url = url
        self._ws: Optional[ClientConnection] = None
        self._connected = False
        self._should_run = False
        self._receive_task: Optional[asyncio.Task] = None
        self._reconnect_count = 0

        # Callbacks
        self.on_message: Optional[Callable[[str], None]] = None
        self.on_connect: Optional[Callable[[], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None
        self.on_max_retries: Optional[Callable[[], None]] = None
        self.on_connection_lost: Optional[Callable[[], None]] = None  # Fires BEFORE reconnect

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    @property
    def is_reconnecting(self) -> bool:
        """Whether the background receive loop is currently handling reconnects."""
        return (
            self._should_run
            and not self._connected
            and self._receive_task is not None
            and not self._receive_task.done()
        )

    async def connect(self) -> bool:
        """Establish WebSocket connection."""
        self._should_run = True
        self._reconnect_count = 0

        try:
            logger.info(f"Connecting to {self.url}")

            # Create SSL context with certifi for macOS compatibility
            ssl_context = ssl.create_default_context(cafile=certifi.where())

            self._ws = await websockets.connect(
                self.url,
                ssl=ssl_context,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            )
            self._connected = True
            logger.info("WebSocket connected")

            # Start receive loop
            self._receive_task = asyncio.create_task(self._receive_loop())

            if self.on_connect:
                self.on_connect()

            return True

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self._connected = False
            if self.on_error:
                self.on_error(e)
            return False

    async def disconnect(self):
        """Disconnect cleanly."""
        logger.info("Disconnecting WebSocket")
        self._should_run = False
        self._connected = False

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        if self.on_disconnect:
            self.on_disconnect()

    async def subscribe(self, token_ids: List[str]) -> bool:
        """Send subscription request."""
        if not self.is_connected:
            logger.error("Cannot subscribe: not connected")
            return False

        message = {
            "type": "market",
            "assets_ids": token_ids
        }

        try:
            await self._ws.send(json.dumps(message))
            logger.info(f"Subscribed to {len(token_ids)} token(s)")
            return True
        except Exception as e:
            logger.error(f"Subscribe failed: {e}")
            return False

    async def _receive_loop(self):
        """Receive messages and handle reconnection."""
        while self._should_run:
            try:
                if not self._ws:
                    await asyncio.sleep(0.1)
                    continue

                message = await self._ws.recv()

                # Handle bytes or str
                if isinstance(message, bytes):
                    message = message.decode('utf-8')

                if self.on_message:
                    self.on_message(message)

            except ConnectionClosedOK:
                logger.info("WebSocket closed normally")
                self._connected = False
                break

            except (ConnectionClosed, ConnectionClosedError) as e:
                logger.warning(f"Connection lost: {e}")
                self._connected = False

                # SAFETY: Fire callback BEFORE reconnect attempts
                if self.on_connection_lost:
                    try:
                        self.on_connection_lost()
                    except Exception as cb_err:
                        logger.error(f"Connection lost callback error: {cb_err}")

                if self._should_run:
                    await self._reconnect()
                break

            except asyncio.CancelledError:
                break

            except Exception as e:
                logger.error(f"Receive error: {e}")
                if self.on_error:
                    self.on_error(e)

    async def _reconnect(self):
        """Reconnect with exponential backoff."""
        while self._should_run and self._reconnect_count < WS_RECONNECT_ATTEMPTS:
            self._reconnect_count += 1

            delay = min(
                WS_RECONNECT_BASE_DELAY * (2 ** (self._reconnect_count - 1)),
                WS_RECONNECT_MAX_DELAY
            )

            logger.info(f"Reconnecting ({self._reconnect_count}/{WS_RECONNECT_ATTEMPTS}) in {delay:.1f}s")
            await asyncio.sleep(delay)

            if not self._should_run:
                return

            try:
                # Create SSL context with certifi for macOS compatibility
                ssl_context = ssl.create_default_context(cafile=certifi.where())

                self._ws = await websockets.connect(
                    self.url,
                    ssl=ssl_context,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                )
                self._connected = True
                self._reconnect_count = 0
                logger.info("Reconnected successfully")

                # Restart receive loop
                self._receive_task = asyncio.create_task(self._receive_loop())

                if self.on_connect:
                    self.on_connect()

                return

            except Exception as e:
                logger.warning(f"Reconnect failed: {e}")

        # Max retries exceeded
        if self._should_run:
            logger.error("Max reconnection attempts exceeded")
            if self.on_max_retries:
                self.on_max_retries()
