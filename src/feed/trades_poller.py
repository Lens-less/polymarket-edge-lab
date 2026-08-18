"""
Trades poller for real-time trade flow analysis.

Polls the authenticated trades API to get trade details (price, size, side)
for order flow analysis.
"""

import asyncio
from typing import List, Optional, Callable, Dict
from decimal import Decimal
from src.client import get_client
from src.utils import setup_logging

logger = setup_logging()

class TradesPoller:
    """
    Polls Polymarket trades API for order flow data.

    Requires authenticated client with API credentials.
    """

    def __init__(
        self,
        poll_interval: float = 5.0
    ):
        """
        Args:
            poll_interval: Seconds between polls (default: 5s)
        """
        self._poll_interval = poll_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._tokens: List[str] = []

        # Track last seen trade IDs to avoid duplicates
        self._last_trade_ids: Dict[str, str] = {}
        self._condition_ids: Dict[str, str] = {}

        # Callbacks: token_id -> list of callbacks
        # Callback signature: (price: Decimal, size: Decimal, side: str, is_taker: bool) -> None
        self._callbacks: Dict[str, List[Callable]] = {}

    @property
    def is_running(self) -> bool:
        return self._running

    def register_callback(self, token_id: str, callback: Callable):
        """Register callback for trade notifications."""
        if token_id not in self._callbacks:
            self._callbacks[token_id] = []
        self._callbacks[token_id].append(callback)

    async def start(self, token_ids: List[str]):
        """Start polling trades."""
        self._tokens = token_ids.copy()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"Trades poller started for {len(token_ids)} tokens (interval: {self._poll_interval}s)")

    async def stop(self):
        """Stop polling."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Trades poller stopped")

    def set_tokens(self, token_ids: List[str]):
        """Update tokens to poll."""
        self._tokens = token_ids.copy()

    async def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_all()
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Trades poll error: {e}")
                await asyncio.sleep(self._poll_interval)

    async def _poll_all(self):
        """Poll trades for all tokens."""
        for token_id in self._tokens:
            if not self._running:
                break

            try:
                await self._poll_token(token_id)
            except Exception as e:
                logger.warning(f"Failed to poll trades for {token_id[:16]}: {e}")

    async def _poll_token(self, token_id: str):
        """Poll trades for a single token."""
        try:
            client = get_client()
            condition_id = self._condition_ids.get(token_id)
            if condition_id is None:
                book = client.get_order_book(token_id=token_id)
                condition_id = str(book.condition_id)
                self._condition_ids[token_id] = condition_id

            # The public Data API returns recent aggressive trades.  Account
            # trades would describe only this wallet and cannot be used as a
            # public order-flow tape.
            logger.debug(f"Polling trades for {token_id[:16]}...")
            result = client.list_trades(
                market=[condition_id],
                taker_only=True,
                page_size=100,
            )
            logger.debug(f"Received trades response for {token_id[:16]}")

            trades = [
                trade
                for trade in result.first_page().items
                if str(getattr(trade, "token_id", "")) == token_id
            ]

            # Get last seen trade ID for this token
            last_seen = self._last_trade_ids.get(token_id)

            # Process trades until we hit one we've seen before
            new_trades = []
            for trade in trades:
                trade_id = self._trade_id(trade)
                if trade_id == last_seen:
                    break
                new_trades.append(trade)

            # Update last seen trade ID
            if trades:
                self._last_trade_ids[token_id] = self._trade_id(trades[0])

            # Process new trades in chronological order (reverse of API order)
            for trade in reversed(new_trades):
                self._process_trade(token_id, trade)

        except Exception as e:
            logger.warning(f"Trades API error for {token_id[:16]}: {e}")

    @staticmethod
    def _trade_id(trade) -> str:
        """Build a stable identity for public trades, which have no REST id."""
        timestamp = getattr(trade, "timestamp", None)
        isoformat = getattr(timestamp, "isoformat", None)
        timestamp_text = isoformat() if callable(isoformat) else str(timestamp)
        return "|".join(
            (
                str(getattr(trade, "transaction_hash", "")),
                str(getattr(trade, "token_id", "")),
                str(getattr(trade, "side", "")),
                str(getattr(trade, "price", "")),
                str(getattr(trade, "size", "")),
                timestamp_text,
            )
        )

    def _process_trade(self, token_id: str, trade):
        """Process a single trade and notify callbacks."""
        try:
            # Extract trade details
            price = getattr(trade, "price", None)
            size = getattr(trade, "size", None)
            side = str(getattr(trade, "side", "")).upper()

            if not price or not size:
                return

            # Convert to Decimal
            price_dec = Decimal(str(price))
            size_dec = Decimal(str(size))

            # Determine if this was an aggressive (taker) trade
            # In Polymarket, the "side" field represents the taker's side
            is_taker = True  # Trades from this API are completed trades (taker orders)

            # Notify callbacks
            if token_id in self._callbacks:
                for callback in self._callbacks[token_id]:
                    try:
                        callback(price_dec, size_dec, side, is_taker)
                    except Exception as e:
                        logger.warning(f"Trade callback error: {e}")

        except Exception as e:
            logger.warning(f"Failed to process trade: {e}")
