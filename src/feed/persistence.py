"""
Persistence layer for market data.

Provides SQLite storage and Parquet export for:
- Order book snapshots
- Trades
- Positions
- Markout metrics
"""

import sqlite3
import json
import time
import asyncio
import gc
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from contextlib import contextmanager
import logging

import pandas as pd
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ModuleNotFoundError:  # pragma: no cover - environment dependent
    pa = None
    pq = None

from src.models import OrderBook, PriceLevel, OrderSide

logger = logging.getLogger(__name__)


def _write_frame(path: Path, df: pd.DataFrame) -> None:
    if pa is not None and pq is not None:
        pq.write_table(pa.Table.from_pandas(df), str(path))
        return
    logger.warning(
        "pyarrow is unavailable; writing line-delimited JSON fallback to %s",
        path,
    )
    path.write_text(df.to_json(orient="records", lines=True), encoding="utf-8")


@dataclass
class SnapshotRecord:
    """Order book snapshot for persistence."""
    timestamp: float
    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    midpoint: Optional[float] = None
    bid_depth_5: float = 0.0  # Sum of top 5 bid sizes
    ask_depth_5: float = 0.0  # Sum of top 5 ask sizes
    bids_json: str = ""  # Full order book as JSON
    asks_json: str = ""  # Full order book as JSON
    sequence: Optional[int] = None


@dataclass
class TradeRecord:
    """Trade record for persistence."""
    timestamp: float
    token_id: str
    price: float
    size: float
    side: str
    is_taker: bool = False
    trade_id: Optional[str] = None


@dataclass
class PositionRecord:
    """Position record for persistence."""
    timestamp: float
    token_id: str
    size: float
    avg_entry_price: float
    unrealized_pnl: float
    realized_pnl: float


@dataclass
class MarkoutMetrics:
    """Markout metrics for persistence."""
    timestamp: float
    token_id: str
    markout_1s: Optional[float] = None
    markout_5s: Optional[float] = None
    markout_10s: Optional[float] = None
    markout_30s: Optional[float] = None
    markout_60s: Optional[float] = None
    markout_300s: Optional[float] = None
    trade_price: float = 0.0
    reference_price: float = 0.0


class SQLiteStore:
    """
    SQLite storage for market data.

    Tables:
    - snapshots: Order book snapshots
    - trades: Trade records
    - positions: Position snapshots
    - markout_metrics: Markout analysis data
    - metadata: Market metadata
    """

    def __init__(self, db_path: str = "data/market_data.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.db_path))
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        """Initialize database tables."""
        with sqlite3.connect(str(self.db_path)) as conn:
            # Snapshots table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    token_id TEXT NOT NULL,
                    best_bid REAL,
                    best_ask REAL,
                    spread REAL,
                    midpoint REAL,
                    bid_depth_5 REAL DEFAULT 0.0,
                    ask_depth_5 REAL DEFAULT 0.0,
                    bids_json TEXT,
                    asks_json TEXT,
                    sequence INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Trades table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    token_id TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    side TEXT NOT NULL,
                    is_taker BOOLEAN DEFAULT 0,
                    trade_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Positions table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    token_id TEXT NOT NULL,
                    size REAL NOT NULL,
                    avg_entry_price REAL NOT NULL,
                    unrealized_pnl REAL DEFAULT 0.0,
                    realized_pnl REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Markout metrics table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS markout_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    token_id TEXT NOT NULL,
                    markout_1s REAL,
                    markout_5s REAL,
                    markout_10s REAL,
                    markout_30s REAL,
                    markout_60s REAL,
                    markout_300s REAL,
                    trade_price REAL NOT NULL,
                    reference_price REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Metadata table for markets
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_metadata (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id TEXT UNIQUE NOT NULL,
                    market_id TEXT,
                    condition_id TEXT,
                    question TEXT,
                    slug TEXT,
                    active BOOLEAN DEFAULT 1,
                    volume REAL DEFAULT 0.0,
                    liquidity REAL DEFAULT 0.0,
                    end_date TEXT,
                    outcome_name TEXT,
                    metadata_json TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Indexes for faster queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_snapshots_token_time
                ON snapshots(token_id, timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_token_time
                ON trades(token_id, timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_positions_token_time
                ON positions(token_id, timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_markout_token_time
                ON markout_metrics(token_id, timestamp)
            """)

            conn.commit()

    def save_snapshot(self, snapshot: SnapshotRecord):
        """Save order book snapshot."""
        self._conn.execute("""
            INSERT INTO snapshots
            (timestamp, token_id, best_bid, best_ask, spread, midpoint,
             bid_depth_5, ask_depth_5, bids_json, asks_json, sequence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot.timestamp, snapshot.token_id,
            snapshot.best_bid, snapshot.best_ask, snapshot.spread, snapshot.midpoint,
            snapshot.bid_depth_5, snapshot.ask_depth_5,
            snapshot.bids_json, snapshot.asks_json,
            snapshot.sequence
        ))
        self._conn.commit()

    def save_trade(self, trade: TradeRecord):
        """Save trade record."""
        self._conn.execute("""
            INSERT INTO trades
            (timestamp, token_id, price, size, side, is_taker, trade_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.timestamp, trade.token_id, trade.price, trade.size,
            trade.side, trade.is_taker, trade.trade_id
        ))
        self._conn.commit()

    def save_position(self, position: PositionRecord):
        """Save position record."""
        self._conn.execute("""
            INSERT INTO positions
            (timestamp, token_id, size, avg_entry_price, unrealized_pnl, realized_pnl)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            position.timestamp, position.token_id, position.size,
            position.avg_entry_price, position.unrealized_pnl, position.realized_pnl
        ))
        self._conn.commit()

    def save_markout(self, markout: MarkoutMetrics):
        """Save markout metrics."""
        self._conn.execute("""
            INSERT INTO markout_metrics
            (timestamp, token_id, markout_1s, markout_5s, markout_10s,
             markout_30s, markout_60s, markout_300s, trade_price, reference_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            markout.timestamp, markout.token_id,
            markout.markout_1s, markout.markout_5s, markout.markout_10s,
            markout.markout_30s, markout.markout_60s, markout.markout_300s,
            markout.trade_price, markout.reference_price
        ))
        self._conn.commit()

    def save_market_metadata(self, token_id: str, metadata: Dict[str, Any]):
        """Save or update market metadata."""
        self._conn.execute("""
            INSERT OR REPLACE INTO market_metadata
            (token_id, market_id, condition_id, question, slug, active,
             volume, liquidity, end_date, outcome_name, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            token_id,
            metadata.get('market_id'),
            metadata.get('condition_id'),
            metadata.get('question'),
            metadata.get('slug'),
            metadata.get('active', True),
            metadata.get('volume', 0.0),
            metadata.get('liquidity', 0.0),
            metadata.get('end_date'),
            metadata.get('outcome_name'),
            json.dumps(metadata)
        ))
        self._conn.commit()

    def get_snapshots(self, token_id: str, start_time: float,
                      end_time: Optional[float] = None) -> pd.DataFrame:
        """Get snapshots for a token in time range."""
        end_time = end_time or time.time()
        cursor = self._conn.execute("""
            SELECT * FROM snapshots
            WHERE token_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp
        """, (token_id, start_time, end_time))
        rows = cursor.fetchall()
        return pd.DataFrame(rows, columns=[c[0] for c in cursor.description])

    def get_trades(self, token_id: str, start_time: float,
                   end_time: Optional[float] = None) -> pd.DataFrame:
        """Get trades for a token in time range."""
        end_time = end_time or time.time()
        cursor = self._conn.execute("""
            SELECT * FROM trades
            WHERE token_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp
        """, (token_id, start_time, end_time))
        rows = cursor.fetchall()
        return pd.DataFrame(rows, columns=[c[0] for c in cursor.description])

    def get_positions(self, token_id: Optional[str] = None) -> pd.DataFrame:
        """Get position history."""
        if token_id:
            cursor = self._conn.execute("""
                SELECT * FROM positions WHERE token_id = ? ORDER BY timestamp
            """, (token_id,))
        else:
            cursor = self._conn.execute("""
                SELECT * FROM positions ORDER BY timestamp
            """)
        rows = cursor.fetchall()
        return pd.DataFrame(rows, columns=[c[0] for c in cursor.description])

    def get_markout_metrics(self, token_id: str, start_time: float,
                            end_time: Optional[float] = None) -> pd.DataFrame:
        """Get markout metrics for a token."""
        end_time = end_time or time.time()
        cursor = self._conn.execute("""
            SELECT * FROM markout_metrics
            WHERE token_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp
        """, (token_id, start_time, end_time))
        rows = cursor.fetchall()
        return pd.DataFrame(rows, columns=[c[0] for c in cursor.description])

    def get_market_metadata(self, token_id: Optional[str] = None) -> pd.DataFrame:
        """Get market metadata."""
        if token_id:
            cursor = self._conn.execute("""
                SELECT * FROM market_metadata WHERE token_id = ?
            """, (token_id,))
        else:
            cursor = self._conn.execute("SELECT * FROM market_metadata")
        rows = cursor.fetchall()
        return pd.DataFrame(rows, columns=[c[0] for c in cursor.description])

    def get_all_tokens(self) -> List[str]:
        """Get all tracked token IDs."""
        cursor = self._conn.execute("""
            SELECT DISTINCT token_id FROM snapshots
            UNION
            SELECT DISTINCT token_id FROM trades
            ORDER BY token_id
        """)
        return [row[0] for row in cursor.fetchall()]

    def get_time_range(self) -> tuple:
        """Get min/max timestamp across all tables."""
        cursor = self._conn.execute("""
            SELECT MIN(timestamp), MAX(timestamp) FROM (
                SELECT timestamp FROM snapshots
                UNION ALL SELECT timestamp FROM trades
                UNION ALL SELECT timestamp FROM positions
            )
        """)
        row = cursor.fetchone()
        return (row[0], row[1]) if row else (None, None)

    def export_to_parquet(self, output_dir: str,
                          start_time: Optional[float] = None,
                          end_time: Optional[float] = None):
        """Export all data to Parquet files."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        start_time = start_time or 0
        end_time = end_time or time.time()

        # Tables with timestamp column
        tables_with_time = ['snapshots', 'trades', 'positions', 'markout_metrics']

        for table in tables_with_time:
            try:
                cursor = self._conn.execute(f"""
                    SELECT * FROM {table}
                    WHERE timestamp >= ? AND timestamp <= ?
                """, (start_time, end_time))
                rows = cursor.fetchall()
                if rows:
                    df = pd.DataFrame(rows, columns=[c[0] for c in cursor.description])
                    _write_frame(output_path / f"{table}.parquet", df)
                    logger.info(f"Exported {len(rows)} rows to {table}.parquet")
            except Exception as e:
                logger.error(f"Failed to export {table}: {e}")

        # market_metadata doesn't have timestamp, export all
        try:
            cursor = self._conn.execute("SELECT * FROM market_metadata")
            rows = cursor.fetchall()
            if rows:
                df = pd.DataFrame(rows, columns=[c[0] for c in cursor.description])
                _write_frame(output_path / "market_metadata.parquet", df)
                logger.info(f"Exported {len(rows)} rows to market_metadata.parquet")
        except Exception as e:
            logger.error(f"Failed to export market_metadata: {e}")

    def close(self):
        """Close database connection."""
        connection = getattr(self._local, "conn", None)
        if connection is not None:
            connection.close()
            self._local.conn = None
            gc.collect()


import threading


class DataStorePersistenceMixin:
    """
    Mixin to add persistence capabilities to DataStore.

    Usage:
        from src.feed.data_store import DataStore

        class PersistentDataStore(DataStore, DataStorePersistenceMixin):
            def __init__(self, *args, **kwargs):
                DataStore.__init__(self, *args, **kwargs)
                DataStorePersistenceMixin.__init__(self, db_path="data/market.db")
    """

    def __init__(self, db_path: str = "data/market_data.db",
                 snapshot_interval: float = 1.0,  # Save snapshot every second
                 enable_snapshots: bool = True,
                 enable_trades: bool = True):
        self._sqlite = SQLiteStore(db_path)
        self._snapshot_interval = snapshot_interval
        self._enable_snapshots = enable_snapshots
        self._enable_trades = enable_trades
        self._last_snapshot_time: Dict[str, float] = {}
        self._markout_buffer: Dict[str, List[tuple]] = {}  # For markout calculation

    def persist_snapshot(self, token_id: str, order_book: OrderBook):
        """Persist order book snapshot if interval has passed."""
        if not self._enable_snapshots:
            return

        now = time.time()
        last = self._last_snapshot_time.get(token_id, 0)

        if now - last < self._snapshot_interval:
            return

        self._last_snapshot_time[token_id] = now

        # Calculate depth
        bid_depth = sum(b.size for b in order_book.bids[:5])
        ask_depth = sum(a.size for a in order_book.asks[:5])

        snapshot = SnapshotRecord(
            timestamp=now,
            token_id=token_id,
            best_bid=order_book.best_bid,
            best_ask=order_book.best_ask,
            spread=order_book.spread,
            midpoint=order_book.midpoint,
            bid_depth_5=bid_depth,
            ask_depth_5=ask_depth,
            bids_json=json.dumps([{'price': b.price, 'size': b.size} for b in order_book.bids]),
            asks_json=json.dumps([{'price': a.price, 'size': a.size} for a in order_book.asks]),
        )

        try:
            self._sqlite.save_snapshot(snapshot)
        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")

    def persist_trade(self, token_id: str, price: float, size: float,
                      side: str, is_taker: bool = False, trade_id: Optional[str] = None):
        """Persist trade record."""
        if not self._enable_trades:
            return

        trade = TradeRecord(
            timestamp=time.time(),
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            is_taker=is_taker,
            trade_id=trade_id
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

    def persist_markout(self, token_id: str, markout: MarkoutMetrics):
        """Persist markout metrics."""
        try:
            self._sqlite.save_markout(markout)
        except Exception as e:
            logger.error(f"Failed to save markout: {e}")

    def calculate_markout(self, token_id: str, trade_price: float,
                          future_prices: Dict[int, float]) -> MarkoutMetrics:
        """
        Calculate markout metrics from future prices.

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

        return markout

    def export_to_parquet(self, output_dir: str,
                          start_time: Optional[float] = None,
                          end_time: Optional[float] = None) -> str:
        """Export all data to Parquet files."""
        self._sqlite.export_to_parquet(output_dir, start_time, end_time)
        return output_dir

    def get_snapshots_df(self, token_id: str, start_time: float,
                         end_time: Optional[float] = None) -> pd.DataFrame:
        """Get snapshots as DataFrame."""
        return self._sqlite.get_snapshots(token_id, start_time, end_time)

    def get_trades_df(self, token_id: str, start_time: float,
                      end_time: Optional[float] = None) -> pd.DataFrame:
        """Get trades as DataFrame."""
        return self._sqlite.get_trades(token_id, start_time, end_time)

    def close(self):
        """Close persistence layer."""
        self._sqlite.close()


class PersistentDataStore:
    """
    DataStore with SQLite persistence.

    Extends the original DataStore with persistence capabilities.
    """

    def __init__(self, stale_threshold: float = 30.0,
                 db_path: str = "data/market_data.db",
                 snapshot_interval: float = 1.0,
                 enable_snapshots: bool = True,
                 enable_trades: bool = True):
        # Import here to avoid circular import
        from src.feed.data_store import DataStore

        self._data_store = DataStore(stale_threshold=stale_threshold)
        self._persistence = DataStorePersistenceMixin()
        self._persistence.__init__(
            db_path=db_path,
            snapshot_interval=snapshot_interval,
            enable_snapshots=enable_snapshots,
            enable_trades=enable_trades
        )

    def __getattr__(self, name):
        """Delegate to underlying DataStore."""
        return getattr(self._data_store, name)

    def update_book(self, token_id: str, bids: list, asks: list,
                    timestamp: Optional[str] = None):
        """Update order book and persist snapshot."""
        # Call original method
        self._data_store.update_book(token_id, bids, asks, timestamp)

        # Persist if we have order book
        order_book = self._data_store.get_order_book(token_id)
        if order_book:
            self._persistence.persist_snapshot(token_id, order_book)

    def update_trade(self, token_id: str, price: float,
                     size: Optional[float] = None, side: Optional[str] = None):
        """Update trade and persist."""
        # Call original method
        self._data_store.update_trade(token_id, price, size, side)

        # Persist trade
        if size:
            self._persistence.persist_trade(
                token_id, price, size, side or "UNKNOWN", False
            )

    def persist_position(self, token_id: str, size: float,
                         avg_entry_price: float, unrealized_pnl: float = 0.0,
                         realized_pnl: float = 0.0):
        """Persist position snapshot."""
        self._persistence.persist_position(
            token_id, size, avg_entry_price, unrealized_pnl, realized_pnl
        )

    def export_to_parquet(self, output_dir: str,
                          start_time: Optional[float] = None,
                          end_time: Optional[float] = None) -> str:
        """Export all data to Parquet."""
        return self._persistence.export_to_parquet(output_dir, start_time, end_time)

    def close(self):
        """Close data store and persistence."""
        self._persistence.close()
