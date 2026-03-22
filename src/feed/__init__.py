"""
Market data feed with automatic failover.

Simple usage:
    feed = MarketFeed()
    await feed.start(["token1", "token2"])

    if feed.is_healthy:
        price = feed.get_midpoint("token1")

Persistence:
    store = PersistentDataStore(db_path="data/market.db")
    store.update_book(token_id, bids, asks)
    store.export_to_parquet("output/")
"""

from src.feed.feed import MarketFeed, FeedState
from src.feed.persistent_data_store import PersistentDataStore
from src.feed.gamma_api import GammaAPI, MarketFilter
from src.feed.persistence import (
    SQLiteStore,
    SnapshotRecord,
    TradeRecord,
    PositionRecord,
    MarkoutMetrics,
)

__all__ = [
    'MarketFeed', 'FeedState',
    'PersistentDataStore',
    'GammaAPI', 'MarketFilter',
    'SQLiteStore', 'SnapshotRecord', 'TradeRecord',
    'PositionRecord', 'MarkoutMetrics',
]
