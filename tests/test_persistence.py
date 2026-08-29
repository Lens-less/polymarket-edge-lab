"""
Tests for persistence module.

Run with: pytest tests/test_persistence.py -v
"""

import time
import tempfile
import shutil
from pathlib import Path
from decimal import Decimal

import pytest
import pandas as pd
import src.feed.persistence as persistence

from src.feed.persistence import (
    SQLiteStore,
    SnapshotRecord,
    TradeRecord,
    PositionRecord,
    MarkoutMetrics,
)
from src.feed.persistent_data_store import PersistentDataStore
from src.models import OrderBook, PriceLevel
from src.feed.gamma_api import GammaAPI, MarketFilter


class TestSQLiteStore:
    """Test SQLite storage."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        self.store = SQLiteStore(str(self.db_path))

    def teardown_method(self):
        self.store.close()
        shutil.rmtree(self.temp_dir)

    def test_save_and_get_snapshot(self):
        """Test saving and retrieving snapshots."""
        snapshot = SnapshotRecord(
            timestamp=time.time(),
            token_id="test-token",
            best_bid=0.5,
            best_ask=0.51,
            spread=0.01,
            midpoint=0.505,
            bid_depth_5=100.0,
            ask_depth_5=50.0,
            bids_json='[{"price": 0.5, "size": 10}]',
            asks_json='[{"price": 0.51, "size": 5}]',
            sequence=1
        )

        self.store.save_snapshot(snapshot)

        # Query snapshots
        df = self.store.get_snapshots("test-token", 0)

        assert len(df) == 1
        assert df.iloc[0]['token_id'] == "test-token"
        assert df.iloc[0]['best_bid'] == 0.5
        assert df.iloc[0]['midpoint'] == 0.505

    def test_save_and_get_trade(self):
        """Test saving and retrieving trades."""
        trade = TradeRecord(
            timestamp=time.time(),
            token_id="test-token",
            price=0.505,
            size=10.0,
            side="BUY",
            is_taker=True,
            trade_id="trade-123"
        )

        self.store.save_trade(trade)

        df = self.store.get_trades("test-token", 0)

        assert len(df) == 1
        assert df.iloc[0]['token_id'] == "test-token"
        assert df.iloc[0]['price'] == 0.505
        assert df.iloc[0]['side'] == "BUY"

    def test_save_and_get_position(self):
        """Test saving and retrieving positions."""
        position = PositionRecord(
            timestamp=time.time(),
            token_id="test-token",
            size=100.0,
            avg_entry_price=0.50,
            unrealized_pnl=5.0,
            realized_pnl=10.0
        )

        self.store.save_position(position)

        df = self.store.get_positions("test-token")

        assert len(df) == 1
        assert df.iloc[0]['size'] == 100.0
        assert df.iloc[0]['unrealized_pnl'] == 5.0

    def test_save_and_get_markout(self):
        """Test saving and retrieving markout metrics."""
        markout = MarkoutMetrics(
            timestamp=time.time(),
            token_id="test-token",
            markout_1s=0.001,
            markout_5s=0.005,
            markout_10s=0.008,
            markout_30s=0.010,
            markout_60s=0.012,
            markout_300s=0.015,
            trade_price=0.50,
            reference_price=0.50
        )

        self.store.save_markout(markout)

        df = self.store.get_markout_metrics("test-token", 0)

        assert len(df) == 1
        assert df.iloc[0]['markout_1s'] == 0.001
        assert df.iloc[0]['markout_300s'] == 0.015

    def test_export_to_parquet(self):
        """Test exporting to Parquet."""
        # Add some data
        snapshot = SnapshotRecord(
            timestamp=time.time(),
            token_id="test-token",
            best_bid=0.5,
            best_ask=0.51,
            midpoint=0.505
        )
        self.store.save_snapshot(snapshot)

        trade = TradeRecord(
            timestamp=time.time(),
            token_id="test-token",
            price=0.505,
            size=10.0,
            side="BUY"
        )
        self.store.save_trade(trade)

        # Export
        output_dir = Path(self.temp_dir) / "parquet"
        if persistence.pa is None or persistence.pq is None:
            with pytest.raises(RuntimeError, match="requires pyarrow"):
                self.store.export_to_parquet(str(output_dir))
            assert not output_dir.exists()
            return

        self.store.export_to_parquet(str(output_dir))

        # Check files exist
        assert (output_dir / "snapshots.parquet").exists()
        assert (output_dir / "trades.parquet").exists()

    def test_export_never_writes_json_with_parquet_suffix(self, monkeypatch):
        """Missing Parquet support must be a hard error with no fake files."""
        snapshot = SnapshotRecord(
            timestamp=time.time(),
            token_id="test-token",
            best_bid=0.5,
            best_ask=0.51,
            midpoint=0.505,
        )
        self.store.save_snapshot(snapshot)
        output_dir = Path(self.temp_dir) / "missing-pyarrow"
        monkeypatch.setattr(persistence, "pa", None)
        monkeypatch.setattr(persistence, "pq", None)

        with pytest.raises(RuntimeError, match="requires pyarrow"):
            self.store.export_to_parquet(str(output_dir))

        assert not output_dir.exists()

    def test_market_metadata(self):
        """Test saving market metadata."""
        metadata = {
            "market_id": "market-123",
            "condition_id": "cond-456",
            "question": "Will it rain?",
            "slug": "rain-today",
            "active": True,
            "volume": 100000.0,
            "liquidity": 50000.0
        }

        self.store.save_market_metadata("token-123", metadata)

        df = self.store.get_market_metadata("token-123")

        assert len(df) == 1
        assert df.iloc[0]['token_id'] == "token-123"
        assert df.iloc[0]['question'] == "Will it rain?"


class TestPersistentDataStore:
    """Test PersistentDataStore."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test.db"
        self.store = PersistentDataStore(
            db_path=str(self.db_path),
            snapshot_interval=0.1  # Short interval for testing
        )

    def teardown_method(self):
        self.store.close()
        shutil.rmtree(self.temp_dir)

    def test_update_book_persists_snapshot(self):
        """Test that update_book persists snapshot."""
        bids = [{"price": "0.5", "size": "10"}]
        asks = [{"price": "0.51", "size": "5"}]

        self.store.update_book("test-token", bids, asks)

        # Wait for persistence
        time.sleep(0.2)

        df = self.store.get_snapshots_df("test-token", 0)
        assert len(df) >= 1

    def test_update_trade_persists(self):
        """Test that update_trade persists."""
        self.store.update_trade("test-token", 0.505, 10.0, "BUY")

        df = self.store.get_trades_df("test-token", 0)
        assert len(df) == 1
        assert df.iloc[0]['price'] == 0.505

    def test_markout_calculation(self):
        """Test markout calculation."""
        future_prices = {
            1: 0.501,
            5: 0.505,
            10: 0.510,
            60: 0.520
        }

        markout = self.store.calculate_and_save_markout(
            "test-token", 0.50, future_prices
        )

        assert markout.markout_1s == 0.001
        assert markout.markout_5s == 0.005
        assert markout.markout_60s == 0.020

        # Verify it was saved
        df = self.store.get_markout_metrics_df("test-token", 0)
        assert len(df) == 1

    def test_export_functionality(self):
        """Test export to parquet."""
        # Add data
        bids = [{"price": "0.5", "size": "10"}]
        asks = [{"price": "0.51", "size": "5"}]
        self.store.update_book("test-token", bids, asks)
        self.store.update_trade("test-token", 0.505, 10.0, "BUY")

        # Export
        output_dir = Path(self.temp_dir) / "export"
        if persistence.pa is None or persistence.pq is None:
            with pytest.raises(RuntimeError, match="requires pyarrow"):
                self.store.export_to_parquet(str(output_dir))
            assert not output_dir.exists()
            return

        path = self.store.export_to_parquet(str(output_dir))

        assert Path(path).exists()

    def test_get_summary(self):
        """Test summary method."""
        # Add data
        bids = [{"price": "0.5", "size": "10"}]
        asks = [{"price": "0.51", "size": "5"}]
        self.store.update_book("test-token", bids, asks)

        summary = self.store.get_summary()

        assert "tokens" in summary
        assert "token_count" in summary
        assert "persisted_tokens" in summary
        assert "time_range" in summary


class TestGammaAPI:
    """Test Gamma API (may require network)."""

    def test_gamma_api_init(self):
        """Test Gamma API initialization."""
        api = GammaAPI()
        assert api.base_url == "https://gamma-api.polymarket.com"

    @pytest.mark.skip(reason="Requires network access")
    def test_get_active_markets(self):
        """Test fetching active markets."""
        api = GammaAPI()
        markets = api.get_active_markets(limit=10)

        assert isinstance(markets, list)
        if markets:
            assert hasattr(markets[0], 'token_id')
            assert hasattr(markets[0], 'liquidity')

    @pytest.mark.skip(reason="Requires network access")
    def test_get_liquid_markets(self):
        """Test getting liquid markets."""
        api = GammaAPI()
        markets = api.get_liquid_markets(
            min_liquidity=100000,
            min_volume=10000,
            limit=10
        )

        for m in markets:
            assert m.liquidity >= 100000
            assert m.volume >= 10000


class TestMarketFilter:
    """Test MarketFilter."""

    def test_filter_init(self):
        """Test MarketFilter initialization."""
        api = GammaAPI()
        filter_obj = MarketFilter(api)
        assert filter_obj.gamma == api


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
