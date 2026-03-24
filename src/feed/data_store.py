"""
Local storage for market data.
"""

import threading
import time
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from src.models import OrderBook, PriceLevel


@dataclass
class TokenData:
    """Data for a single token."""
    token_id: str
    order_book: Optional[OrderBook] = None
    last_price: Optional[float] = None
    last_trade_price: Optional[float] = None
    last_trade_side: Optional[str] = None
    last_trade_size: Optional[float] = None
    last_update: float = 0.0
    last_book_update: float = 0.0

    def update_timestamp(self, book_update: bool = False):
        now = time.time()
        self.last_update = now
        if book_update:
            self.last_book_update = now

    def seconds_since_update(self) -> float:
        if self.last_update == 0:
            return float('inf')
        result = time.time() - self.last_update
        return max(0, result)  # Ensure non-negative

    def seconds_since_book_update(self) -> float:
        if self.last_book_update == 0:
            return float('inf')
        result = time.time() - self.last_book_update
        return max(0, result)


class DataStore:
    """
    Thread-safe storage for market data.

    Updated by WebSocket messages or REST polls.
    Read by the market maker for current prices.
    """

    def __init__(self, stale_threshold: float = 30.0):
        self._lock = threading.RLock()  # Reentrant lock for thread safety
        self._data: Dict[str, TokenData] = {}
        self._stale_threshold = stale_threshold
        self._sequence: Dict[str, int] = {}  # For gap detection
        self._gap_count: Dict[str, int] = {}
        self._last_any_message: float = 0.0
        self._last_ws_message: float = 0.0
        self._last_ws_book_message: float = 0.0

    # === Data Access ===

    def get(self, token_id: str) -> Optional[TokenData]:
        with self._lock:
            return self._data.get(token_id)

    def get_midpoint(self, token_id: str) -> Optional[float]:
        with self._lock:
            data = self._data.get(token_id)
            if data and data.order_book and data.order_book.midpoint is not None:
                return float(data.order_book.midpoint)
            return None

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        with self._lock:
            data = self._data.get(token_id)
            return data.order_book if data else None

    def get_spread(self, token_id: str) -> Optional[float]:
        with self._lock:
            data = self._data.get(token_id)
            if data and data.order_book and data.order_book.spread is not None:
                return float(data.order_book.spread)
            return None

    def get_best_bid(self, token_id: str) -> Optional[float]:
        with self._lock:
            data = self._data.get(token_id)
            if data and data.order_book and data.order_book.best_bid is not None:
                return float(data.order_book.best_bid)
            return None

    def get_best_ask(self, token_id: str) -> Optional[float]:
        with self._lock:
            data = self._data.get(token_id)
            if data and data.order_book and data.order_book.best_ask is not None:
                return float(data.order_book.best_ask)
            return None

    # === Health Checks ===

    def is_fresh(self, token_id: str) -> bool:
        """Check if token has a fresh order book (not just recent trades/prices)."""
        with self._lock:
            data = self._data.get(token_id)
            if not data:
                return False
            if data.order_book is None:
                return False
            return data.seconds_since_book_update() < self._stale_threshold

    def all_fresh(self) -> bool:
        """Check if all tracked tokens have fresh data."""
        with self._lock:
            if not self._data:
                return False
            return all(self.is_fresh(tid) for tid in self._data)

    def has_gaps(self) -> bool:
        """Check if any sequence gaps were detected."""
        with self._lock:
            return any(count > 0 for count in self._gap_count.values())

    # === Updates ===

    def register_token(self, token_id: str):
        """Start tracking a token."""
        with self._lock:
            if token_id not in self._data:
                self._data[token_id] = TokenData(token_id=token_id)
                self._sequence[token_id] = -1
                self._gap_count[token_id] = 0

    def unregister_token(self, token_id: str):
        """Stop tracking a token."""
        with self._lock:
            self._data.pop(token_id, None)
            self._sequence.pop(token_id, None)
            self._gap_count.pop(token_id, None)

    def check_sequence(self, token_id: str, seq: Optional[int]) -> bool:
        """Check message sequence, detect gaps. Returns True if OK, False if gap."""
        with self._lock:
            if seq is None:
                return True
            last = self._sequence.get(token_id, -1)
            if last == -1:
                self._sequence[token_id] = seq
                return True
            expected = last + 1
            if seq == expected:
                self._sequence[token_id] = seq
                return True
            self._gap_count[token_id] = self._gap_count.get(token_id, 0) + 1
            self._sequence[token_id] = seq
            return False

    def clear_gaps(self, token_id: str):
        """Clear gap count after resync."""
        with self._lock:
            self._gap_count[token_id] = 0

    def update_book(self, token_id: str, bids: list, asks: list, timestamp: Optional[str] = None):
        """Update order book from message."""
        with self._lock:
            if token_id not in self._data:
                self._data[token_id] = TokenData(token_id=token_id)
                self._sequence[token_id] = -1
                self._gap_count[token_id] = 0

            data = self._data[token_id]

            # Parse price levels - use Decimal for financial precision
            from decimal import Decimal
            parsed_bids = [
                PriceLevel(price=Decimal(str(b['price'])), size=Decimal(str(b['size'])))
                for b in bids if isinstance(b, dict)
            ]
            parsed_asks = [
                PriceLevel(price=Decimal(str(a['price'])), size=Decimal(str(a['size'])))
                for a in asks if isinstance(a, dict)
            ]

            # Sort: bids descending, asks ascending
            parsed_bids.sort(key=lambda x: x.price, reverse=True)
            parsed_asks.sort(key=lambda x: x.price)

            data.order_book = OrderBook(
                token_id=token_id,
                bids=parsed_bids,
                asks=parsed_asks,
                timestamp=timestamp
            )
            data.update_timestamp(book_update=True)
            self._last_any_message = time.time()

    def update_price(self, token_id: str, price: float):
        """Update last price."""
        with self._lock:
            if token_id not in self._data:
                self._data[token_id] = TokenData(token_id=token_id)
                self._sequence[token_id] = -1
                self._gap_count[token_id] = 0

            self._data[token_id].last_price = price
            self._data[token_id].update_timestamp()
            self._last_any_message = time.time()

    def update_trade(self, token_id: str, price: float, size: Optional[float] = None, side: Optional[str] = None):
        """Update last trade."""
        with self._lock:
            if token_id not in self._data:
                self._data[token_id] = TokenData(token_id=token_id)
                self._sequence[token_id] = -1
                self._gap_count[token_id] = 0

            data = self._data[token_id]
            data.last_trade_price = price
            data.last_trade_size = size
            data.last_trade_side = side
            data.update_timestamp()
            self._last_any_message = time.time()

    def get_token_ids(self) -> List[str]:
        """Get all tracked token IDs."""
        with self._lock:
            return list(self._data.keys())

    def record_message_received(self):
        """Record that any message was received (for heartbeat tracking)."""
        with self._lock:
            self._last_any_message = time.time()

    def seconds_since_any_message(self) -> float:
        """Get seconds since any message was received."""
        with self._lock:
            if self._last_any_message == 0:
                return float('inf')
            return max(0, time.time() - self._last_any_message)

    def record_ws_message(self):
        """Record WebSocket message received."""
        with self._lock:
            self._last_ws_message = time.time()

    def record_ws_book_message(self):
        """Record WebSocket order book message received."""
        with self._lock:
            self._last_ws_book_message = time.time()

    def seconds_since_ws_message(self) -> float:
        """Seconds since last WebSocket message."""
        with self._lock:
            if self._last_ws_message == 0:
                return float('inf')
            return max(0, time.time() - self._last_ws_message)

    def seconds_since_ws_book_message(self) -> float:
        """Seconds since last WebSocket order book update."""
        with self._lock:
            if self._last_ws_book_message == 0:
                return float('inf')
            return max(0, time.time() - self._last_ws_book_message)

    def reset_ws_timers(self):
        """Clear WebSocket heartbeat timers after disconnects."""
        with self._lock:
            self._last_ws_message = 0.0
            self._last_ws_book_message = 0.0

    def clear(self):
        """Clear all data."""
        with self._lock:
            self._data.clear()
            self._sequence.clear()
            self._gap_count.clear()
            self._last_any_message = 0.0
            self._last_ws_message = 0.0
            self._last_ws_book_message = 0.0
