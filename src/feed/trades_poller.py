"""
Trades poller for real-time trade flow analysis.

Polls the authenticated trades API to get trade details (price, size, side)
for order flow analysis.
"""

import asyncio
from typing import List, Optional, Callable, Dict
from decimal import Decimal
from src.client import get_auth_client
from src.utils import setup_logging

logger = setup_logging()


def _load_trade_params():
    try:
        from py_clob_client.clob_types import TradeParams
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "py-clob-client is not installed. Trade polling is unavailable."
        ) from exc
    return TradeParams


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
            client = get_auth_client()
            TradeParams = _load_trade_params()
            params = TradeParams(asset_id=token_id)

            # Get recent trades (API returns most recent first)
            logger.debug(f"Polling trades for {token_id[:16]}...")
            result = client.get_trades(params=params)
            logger.debug(f"Received trades response for {token_id[:16]}")

            if not result:
                return

            # Process trades in chronological order (oldest first)
            # Result is a dict with 'next_cursor' and 'data' list
            if isinstance(result, dict) and 'data' in result:
                trades = result['data']
            elif isinstance(result, list):
                trades = result
            else:
                logger.warning(f"Unexpected trades response format: {type(result)}")
                return

            # Get last seen trade ID for this token
            last_seen = self._last_trade_ids.get(token_id)

            # Process trades until we hit one we've seen before
            new_trades = []
            for trade in trades:
                trade_id = trade.get('id')
                if trade_id == last_seen:
                    break
                new_trades.append(trade)

            # Update last seen trade ID
            if trades:
                self._last_trade_ids[token_id] = trades[0].get('id')

            # Process new trades in chronological order (reverse of API order)
            for trade in reversed(new_trades):
                self._process_trade(token_id, trade)

        except Exception as e:
            logger.warning(f"Trades API error for {token_id[:16]}: {e}")

    def _process_trade(self, token_id: str, trade: dict):
        """Process a single trade and notify callbacks."""
        try:
            # Extract trade details
            price = trade.get('price')
            size = trade.get('size')
            side = trade.get('side', '').upper()  # "BUY" or "SELL"
            maker_address = trade.get('maker_address', '')
            taker_address = trade.get('taker_address', '')

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
