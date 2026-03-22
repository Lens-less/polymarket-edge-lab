"""
Gamma API integration for market metadata and liquidity data.

Gamma API provides:
- Market metadata (questions, slugs, end dates)
- Liquidity sorting
- Event information
"""

import json
import time
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
from decimal import Decimal

import requests

from src.utils import setup_logging

logger = setup_logging()


GAMMA_API_BASE = "https://gamma-api.polymarket.com"


@dataclass
class MarketInfo:
    """Market information from Gamma API."""
    token_id: str
    condition_id: str
    market_id: str
    question: str
    slug: str
    description: str
    outcomes: List[Dict[str, Any]]
    active: bool
    closed: bool
    volume: float
    liquidity: float
    end_date: Optional[str]
    category: Optional[str]
    resolution_source: Optional[str]
    image_url: Optional[str]
    raw_data: Dict[str, Any]


class GammaAPI:
    """
    Gamma API client for market metadata.

    Usage:
        gamma = GammaAPI()
        markets = gamma.get_active_markets()
        info = gamma.get_market_info(token_id)
    """

    def __init__(self, base_url: str = GAMMA_API_BASE):
        self.base_url = base_url
        self._cache: Dict[str, tuple] = {}  # key -> (data, timestamp)
        self._cache_ttl = 300  # 5 minutes

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make GET request to Gamma API."""
        cache_key = f"{endpoint}:{json.dumps(params, sort_keys=True) if params else ''}"

        # Check cache
        if cache_key in self._cache:
            data, timestamp = self._cache[cache_key]
            if time.time() - timestamp < self._cache_ttl:
                return data

        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            # Cache result
            self._cache[cache_key] = (data, time.time())
            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Gamma API request failed: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Gamma API JSON decode error: {e}")
            return None

    def get_market_by_condition(self, condition_id: str) -> Optional[Dict]:
        """Get market by condition ID."""
        return self._get(f"/markets/{condition_id}")

    def get_market_by_token(self, token_id: str) -> Optional[MarketInfo]:
        """Get market info by token ID."""
        markets = self._get("/markets", {
            "closed": "false",
            "active": "true",
            "limit": 1000
        })

        if not markets or not isinstance(markets, list):
            return None

        for market in markets:
            outcomes = market.get("outcomes", [])
            if not isinstance(outcomes, list):
                continue

            for outcome in outcomes:
                if outcome.get("token_id") == token_id:
                    return self._parse_market_info(market, outcome)

        return None

    def get_active_markets(self, limit: int = 100,
                           sort_by: str = "liquidity") -> List[MarketInfo]:
        """
        Get active markets sorted by liquidity or volume.

        Args:
            limit: Maximum number of markets
            sort_by: "liquidity" or "volume"

        Returns:
            List of MarketInfo sorted by liquidity/volume
        """
        params = {
            "closed": "false",
            "active": "true",
            "limit": limit * 2,  # Fetch extra for filtering
        }

        markets = self._get("/markets", params)

        if not markets or not isinstance(markets, list):
            return []

        results = []
        for market in markets:
            if not self._is_valid_market(market):
                continue

            outcomes = market.get("outcomes", [])
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                info = self._parse_market_info(market, outcome)
                if info:
                    results.append(info)

        # Sort by requested field
        if sort_by == "liquidity":
            results.sort(key=lambda x: x.liquidity, reverse=True)
        elif sort_by == "volume":
            results.sort(key=lambda x: x.volume, reverse=True)

        return results[:limit]

    def get_liquid_markets(self, min_liquidity: float = 10000.0,
                           min_volume: float = 1000.0,
                           limit: int = 50) -> List[MarketInfo]:
        """
        Get markets with sufficient liquidity and volume.

        Args:
            min_liquidity: Minimum liquidity in USD
            min_volume: Minimum volume in USD
            limit: Maximum number of markets

        Returns:
            List of MarketInfo meeting criteria
        """
        markets = self.get_active_markets(limit=limit * 2)

        filtered = [
            m for m in markets
            if m.liquidity >= min_liquidity and m.volume >= min_volume
        ]

        return filtered[:limit]

    def get_markets_by_category(self, category: str,
                                 limit: int = 50) -> List[MarketInfo]:
        """Get markets by category."""
        params = {
            "closed": "false",
            "active": "true",
            "tag": category,
            "limit": limit,
        }

        markets = self._get("/markets", params)

        if not markets or not isinstance(markets, list):
            return []

        results = []
        for market in markets:
            if not self._is_valid_market(market):
                continue

            outcomes = market.get("outcomes", [])
            for outcome in outcomes:
                info = self._parse_market_info(market, outcome)
                if info:
                    results.append(info)

        return results[:limit]

    def get_trending_markets(self, limit: int = 20) -> List[MarketInfo]:
        """Get trending markets (high recent volume)."""
        params = {
            "closed": "false",
            "active": "true",
            "sort": "volume",
            "order": "desc",
            "limit": limit,
        }

        markets = self._get("/markets", params)

        if not markets or not isinstance(markets, list):
            return []

        results = []
        for market in markets:
            if not self._is_valid_market(market):
                continue

            outcomes = market.get("outcomes", [])
            for outcome in outcomes:
                info = self._parse_market_info(market, outcome)
                if info:
                    results.append(info)

        return results[:limit]

    def get_events(self, limit: int = 100) -> List[Dict]:
        """Get events list."""
        params = {"limit": limit}
        result = self._get("/events", params)
        return result if isinstance(result, list) else []

    def get_event_markets(self, event_id: str) -> List[Dict]:
        """Get markets for a specific event."""
        event = self._get(f"/events/{event_id}")
        if event and isinstance(event, dict):
            return event.get("markets", [])
        return []

    def get_historical_prices(self, token_id: str,
                              start: Optional[str] = None,
                              end: Optional[str] = None,
                              interval: str = "1h") -> List[Dict]:
        """
        Get historical prices for a token.

        Args:
            token_id: Token ID
            start: Start date (ISO format)
            end: End date (ISO format)
            interval: "1m", "5m", "15m", "1h", "4h", "1d"

        Returns:
            List of price candles
        """
        params = {"token_id": token_id, "interval": interval}
        if start:
            params["start_date"] = start
        if end:
            params["end_date"] = end

        result = self._get("/prices/history", params)
        return result if isinstance(result, list) else []

    def get_orderbook_summary(self, token_id: str) -> Optional[Dict]:
        """Get orderbook summary for a token."""
        return self._get(f"/markets/orderbook-summary/{token_id}")

    def _is_valid_market(self, market: Dict) -> bool:
        """Check if market dict is valid."""
        if not isinstance(market, dict):
            return False
        return (
            market.get("conditionId") and
            market.get("outcomes") and
            isinstance(market.get("outcomes"), list)
        )

    def _parse_market_info(self, market: Dict, outcome: Dict) -> Optional[MarketInfo]:
        """Parse market and outcome into MarketInfo."""
        try:
            return MarketInfo(
                token_id=outcome.get("token_id", ""),
                condition_id=market.get("conditionId", ""),
                market_id=market.get("id", ""),
                question=market.get("question", ""),
                slug=market.get("slug", ""),
                description=market.get("description", ""),
                outcomes=market.get("outcomes", []),
                active=market.get("active", True),
                closed=market.get("closed", False),
                volume=float(market.get("volume", 0)),
                liquidity=float(market.get("liquidity", 0)),
                end_date=market.get("endDate"),
                category=market.get("category"),
                resolution_source=market.get("resolutionSource"),
                image_url=market.get("image"),
                raw_data=market
            )
        except Exception as e:
            logger.error(f"Failed to parse market info: {e}")
            return None

    def clear_cache(self):
        """Clear API cache."""
        self._cache.clear()


class MarketFilter:
    """
    Filter and rank markets for trading.

    Usage:
        filter = MarketFilter(gamma_api)
        good_markets = filter.filter_tradeable(
            min_liquidity=50000,
            min_volume=5000
        )
    """

    def __init__(self, gamma_api: GammaAPI):
        self.gamma = gamma_api

    def filter_tradeable(self,
                         min_liquidity: float = 10000.0,
                         min_volume: float = 1000.0,
                         max_spread_pct: Optional[float] = None,
                         exclude_closed: bool = True,
                         limit: int = 50) -> List[MarketInfo]:
        """
        Filter markets suitable for market making.

        Args:
            min_liquidity: Minimum liquidity in USD
            min_volume: Minimum daily volume in USD
            max_spread_pct: Maximum spread as percentage (optional)
            exclude_closed: Exclude closed markets
            limit: Maximum results

        Returns:
            Filtered list of MarketInfo
        """
        markets = self.gamma.get_active_markets(limit=limit * 2)

        results = []
        for market in markets:
            # Basic filters
            if exclude_closed and market.closed:
                continue
            if market.liquidity < min_liquidity:
                continue
            if market.volume < min_volume:
                continue

            results.append(market)

        return results[:limit]

    def rank_by_score(self, markets: List[MarketInfo],
                      liquidity_weight: float = 0.5,
                      volume_weight: float = 0.5) -> List[tuple]:
        """
        Rank markets by composite score.

        Args:
            markets: List of MarketInfo
            liquidity_weight: Weight for liquidity in score
            volume_weight: Weight for volume in score

        Returns:
            List of (MarketInfo, score) tuples, sorted by score
        """
        if not markets:
            return []

        # Normalize metrics
        max_liquidity = max(m.liquidity for m in markets)
        max_volume = max(m.volume for m in markets)

        if max_liquidity == 0:
            max_liquidity = 1
        if max_volume == 0:
            max_volume = 1

        scored = []
        for market in markets:
            liquidity_score = market.liquidity / max_liquidity
            volume_score = market.volume / max_volume

            total_score = (
                liquidity_score * liquidity_weight +
                volume_score * volume_weight
            )
            scored.append((market, total_score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored


# Convenience function for quick access
def get_gamma_api() -> GammaAPI:
    """Get default Gamma API instance."""
    return GammaAPI()
