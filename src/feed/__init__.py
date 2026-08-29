"""Feed package with lazy imports for optional runtime dependencies."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "MarketFeed": ("src.feed.feed", "MarketFeed"),
    "FeedState": ("src.feed.feed", "FeedState"),
    "PersistentDataStore": ("src.feed.persistent_data_store", "PersistentDataStore"),
    "GammaAPI": ("src.feed.gamma_api", "GammaAPI"),
    "MarketFilter": ("src.feed.gamma_api", "MarketFilter"),
    "SQLiteStore": ("src.feed.persistence", "SQLiteStore"),
    "SnapshotRecord": ("src.feed.persistence", "SnapshotRecord"),
    "TradeRecord": ("src.feed.persistence", "TradeRecord"),
    "PositionRecord": ("src.feed.persistence", "PositionRecord"),
    "MarkoutMetrics": ("src.feed.persistence", "MarkoutMetrics"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


__all__ = list(_LAZY_EXPORTS)
