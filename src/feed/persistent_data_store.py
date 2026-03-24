"""
Extended DataStore with persistence capabilities.

This module extends the base DataStore with SQLite and Parquet export.
"""

import time
import json
from typing import Dict, Optional, List, Any
from pathlib import Path

from src.feed.data_store import DataStore, TokenData
from src.feed.persistence import (
    SQLiteStore,
    SnapshotRecord,
    TradeRecord,
    PositionRecord,
    MarkoutMetrics,
    DataStorePersistenceMixin
)
from src.utils import setup_logging

logger = setup_logging()


class PersistentDataStore(DataStore):
    """
    DataStore with SQLite persistence and Parquet export.

    Extends the base DataStore to add:
    - Automatic order book snapshot persistence
    - Trade logging
    - Position tracking
    - Markout metrics calculation
    - Parquet export for offline analysis

    Usage:
        store = PersistentDataStore(
            db_path="data/market.db",
            snapshot_interval=1.0,  # Save every second
            enable_snapshots=True,
            enable_trades=True
        )

        # Use like normal DataStore
        store.update_book(token_id, bids, asks)
        store.update_trade(token_id, price, size, side)

        # Export to Parquet
        store.export_to_parquet("output/2024-01-01")
    """

    def __init__(self,
                 stale_threshold: float = 30.0,
                 db_path: str = "data/market_data.db",
                 snapshot_interval: float = 1.0,
                 enable_snapshots: bool = True,
                 enable_trades: bool = True):
        super().__init__(stale_threshold=stale_threshold)

        self._sqlite = SQLiteStore(db_path)
        self._snapshot_interval = snapshot_interval
        self._enable_snapshots = enable_snapshots
        self._enable_trades = enable_trades
        self._last_snapshot_time: Dict[str, float] = {}
        self._markout_buffer: Dict[str, List[tuple]] = {}

    def update_book(self, token_id: str, bids: list, asks: list,
                    timestamp: Optional[str] = None):
        """Update order book and persist snapshot."""
        super().update_book(token_id, bids, asks, timestamp)

        # Persist if enabled
        if self._enable_snapshots:
            self._persist_snapshot_if_needed(token_id)

    def _persist_snapshot_if_needed(self, token_id: str):
        """Persist snapshot if interval has passed."""
        now = time.time()
        last = self._last_snapshot_time.get(token_id, 0)

        if now - last < self._snapshot_interval:
            return

        self._last_snapshot_time[token_id] = now

        data = self._data.get(token_id)
        if not data or not data.order_book:
            return

        order_book = data.order_book

        # Persist floats, because SQLite/JSON cannot serialize Decimal directly.
        bid_depth = float(sum(b.size for b in order_book.bids[:5]))
        ask_depth = float(sum(a.size for a in order_book.asks[:5]))

        snapshot = SnapshotRecord(
            timestamp=now,
            token_id=token_id,
            best_bid=float(order_book.best_bid) if order_book.best_bid is not None else None,
            best_ask=float(order_book.best_ask) if order_book.best_ask is not None else None,
            spread=float(order_book.spread) if order_book.spread is not None else None,
            midpoint=float(order_book.midpoint) if order_book.midpoint is not None else None,
            bid_depth_5=bid_depth,
            ask_depth_5=ask_depth,
            bids_json=json.dumps(
                [{"price": float(b.price), "size": float(b.size)} for b in order_book.bids]
            ),
            asks_json=json.dumps(
                [{"price": float(a.price), "size": float(a.size)} for a in order_book.asks]
            ),
            sequence=self._sequence.get(token_id)
        )

        try:
            self._sqlite.save_snapshot(snapshot)
        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")

    def update_trade(self, token_id: str, price: float,
                     size: Optional[float] = None, side: Optional[str] = None):
        """Update trade and persist."""
        super().update_trade(token_id, price, size, side)

        if self._enable_trades and size:
            self._persist_trade(token_id, price, size, side or "UNKNOWN")

    def _persist_trade(self, token_id: str, price: float,
                       size: float, side: str):
        """Persist trade record."""
        trade = TradeRecord(
            timestamp=time.time(),
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            is_taker=False,  # Unknown from this context
            trade_id=None
        )

        try:
            self._sqlite.save_trade(trade)
        except Exception as e:
            logger.error(f"Failed to save trade: {e}")

    def persist_position(self, token_id: str, size: float,
                         avg_entry_price: float, unrealized_pnl: float = 0.0,
                         realized_pnl: float = 0.0):
        """Persist position snapshot."""
        position = PositionRecord(
            timestamp=time.time(),
            token_id=token_id,
            size=size,
            avg_entry_price=avg_entry_price,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl
        )

        try:
            self._sqlite.save_position(position)
        except Exception as e:
            logger.error(f"Failed to save position: {e}")

    def persist_markout(self, markout: MarkoutMetrics):
        """Persist markout metrics."""
        try:
            self._sqlite.save_markout(markout)
        except Exception as e:
            logger.error(f"Failed to save markout: {e}")

    def calculate_and_save_markout(self, token_id: str, trade_price: float,
                                   future_prices: Dict[int, float]) -> MarkoutMetrics:
        """
        Calculate markout metrics and save.

        Args:
            token_id: Token ID
            trade_price: Price of the trade
            future_prices: Dict of seconds -> expected future price

        Returns:
            MarkoutMetrics with calculated markouts
        """
        now = time.time()

        markout = MarkoutMetrics(
            timestamp=now,
            token_id=token_id,
            trade_price=trade_price,
            reference_price=trade_price
        )

        for seconds, price in future_prices.items():
            pnl = round(price - trade_price, 6)
            if seconds == 1:
                markout.markout_1s = pnl
            elif seconds == 5:
                markout.markout_5s = pnl
            elif seconds == 10:
                markout.markout_10s = pnl
            elif seconds == 30:
                markout.markout_30s = pnl
            elif seconds == 60:
                markout.markout_60s = pnl
            elif seconds == 300:
                markout.markout_300s = pnl

        self.persist_markout(markout)
        return markout

    def export_to_parquet(self, output_dir: str,
                          start_time: Optional[float] = None,
                          end_time: Optional[float] = None) -> str:
        """
        Export all data to Parquet files.

        Args:
            output_dir: Directory to save Parquet files
            start_time: Optional start timestamp
            end_time: Optional end timestamp

        Returns:
            Path to output directory
        """
        self._sqlite.export_to_parquet(output_dir, start_time, end_time)
        return output_dir

    def get_snapshots_df(self, token_id: str, start_time: float,
                         end_time: Optional[float] = None):
        """
        Get snapshots as pandas DataFrame.

        Args:
            token_id: Token ID
            start_time: Start timestamp
            end_time: Optional end timestamp

        Returns:
            pandas DataFrame with snapshots
        """
        return self._sqlite.get_snapshots(token_id, start_time, end_time)

    def get_trades_df(self, token_id: str, start_time: float,
                      end_time: Optional[float] = None):
        """
        Get trades as pandas DataFrame.

        Args:
            token_id: Token ID
            start_time: Start timestamp
            end_time: Optional end timestamp

        Returns:
            pandas DataFrame with trades
        """
        return self._sqlite.get_trades(token_id, start_time, end_time)

    def get_positions_df(self, token_id: Optional[str] = None):
        """
        Get positions as pandas DataFrame.

        Args:
            token_id: Optional token ID filter

        Returns:
            pandas DataFrame with positions
        """
        return self._sqlite.get_positions(token_id)

    def get_markout_metrics_df(self, token_id: str, start_time: float,
                               end_time: Optional[float] = None):
        """
        Get markout metrics as pandas DataFrame.

        Args:
            token_id: Token ID
            start_time: Start timestamp
            end_time: Optional end timestamp

        Returns:
            pandas DataFrame with markout metrics
        """
        return self._sqlite.get_markout_metrics(token_id, start_time, end_time)

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of stored data."""
        min_ts, max_ts = self._sqlite.get_time_range()

        return {
            "tokens": self._sqlite.get_all_tokens(),
            "token_count": len(self._data),
            "persisted_tokens": self._sqlite.get_all_tokens(),
            "time_range": {
                "start": min_ts,
                "end": max_ts,
                "duration_hours": (max_ts - min_ts) / 3600 if min_ts and max_ts else 0
            }
        }

    def close(self):
        """Close data store and persistence."""
        self._sqlite.close()
