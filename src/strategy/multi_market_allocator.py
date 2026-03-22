"""
Multi-Market Capital Allocator

Allocates capital across multiple markets based on:
- Liquidity (liquidity^p factor)
- Quality (markout, fill rate)
- Risk constraints
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from decimal import Decimal
import math


@dataclass
class MarketMetrics:
    """Metrics for a single market."""
    token_id: str
    liquidity: float = 0.0           # Dollar liquidity
    avg_markout: float = 0.0         # Average markout (negative = adverse selection)
    fill_rate: float = 0.0           # Fill rate (0-1)
    recent_volume: float = 0.0       # Recent trading volume
    spread_bps: float = 0.0         # Current spread in basis points


@dataclass
class AllocatorConfig:
    """Configuration for capital allocation."""
    total_capital: Decimal = Decimal("1000")
    min_capital_per_market: Decimal = Decimal("10")
    max_capital_per_market: Decimal = Decimal("200")
    max_markets: int = 10

    # Quality factors
    liquidity_exponent: float = 0.5  # p in liquidity^p
    markout_penalty_weight: float = 2.0  # Penalty for negative markout
    fill_rate_reward_weight: float = 1.0  # Reward for high fill rate


class CapitalAllocator:
    """
    Allocates capital across multiple markets.

    Uses formula: weight = liquidity^p * quality_factor
    where quality_factor penalizes negative markout and rewards high fill rate
    """

    def __init__(self, config: Optional[AllocatorConfig] = None):
        self.config = config or AllocatorConfig()
        self._market_metrics: Dict[str, MarketMetrics] = {}
        self._allocations: Dict[str, Decimal] = {}

    def update_market(self, metrics: MarketMetrics):
        """Update metrics for a market."""
        self._market_metrics[metrics.token_id] = metrics

    def update_market_from_dict(self, token_id: str, **kwargs):
        """Update market metrics from dict."""
        if token_id not in self._market_metrics:
            self._market_metrics[token_id] = MarketMetrics(token_id=token_id)

        for key, value in kwargs.items():
            if hasattr(self._market_metrics[token_id], key):
                setattr(self._market_metrics[token_id], key, value)

    def _calculate_quality_factor(self, metrics: MarketMetrics) -> float:
        """
        Calculate quality factor for a market.

        Positive markout -> reward
        Negative markout -> penalty (exponential)
        High fill rate -> reward
        """
        quality = 1.0

        # Markout penalty (negative markout is bad)
        if metrics.avg_markout < 0:
            # Exponential penalty for adverse selection
            penalty = math.exp(self.config.markout_penalty_weight * metrics.avg_markout)
            quality *= penalty

        # Fill rate reward
        if metrics.fill_rate > 0:
            quality *= (1 + self.config.fill_rate_reward_weight * metrics.fill_rate)

        return max(0.01, quality)  # Minimum quality factor

    def allocate(self, token_ids: Optional[List[str]] = None) -> Dict[str, Decimal]:
        """
        Calculate capital allocation across markets.

        Returns:
            Dict mapping token_id to allocated capital
        """
        if token_ids is None:
            token_ids = list(self._market_metrics.keys())

        # Filter to only markets with metrics
        valid_tokens = [t for t in token_ids if t in self._market_metrics]

        if not valid_tokens:
            return {}

        # Limit number of markets
        if len(valid_tokens) > self.config.max_markets:
            valid_tokens = self._select_top_markets(valid_tokens)

        # Calculate weights
        weights = {}
        total_weight = 0.0

        for token_id in valid_tokens:
            metrics = self._market_metrics[token_id]

            # Liquidity factor: liquidity^p
            liquidity_factor = max(0.01, metrics.liquidity ** self.config.liquidity_exponent)

            # Quality factor
            quality_factor = self._calculate_quality_factor(metrics)

            # Combined weight
            weight = liquidity_factor * quality_factor
            weights[token_id] = weight
            total_weight += weight

        # Normalize and apply capital
        allocations = {}
        if total_weight > 0:
            for token_id in valid_tokens:
                weight_ratio = weights[token_id] / total_weight
                allocated = self.config.total_capital * Decimal(str(weight_ratio))

                # Apply min/max constraints
                allocated = max(
                    self.config.min_capital_per_market,
                    min(allocated, self.config.max_capital_per_market)
                )

                allocations[token_id] = allocated

        self._allocations = allocations
        return allocations

    def _select_top_markets(self, token_ids: List[str]) -> List[str]:
        """Select top markets by liquidity."""
        scored = []
        for token_id in token_ids:
            metrics = self._market_metrics[token_id]
            # Score = liquidity * quality
            quality = self._calculate_quality_factor(metrics)
            score = metrics.liquidity * quality
            scored.append((token_id, score))

        # Sort by score descending
        scored.sort(key=lambda x: x[1], reverse=True)

        # Return top N
        return [t[0] for t in scored[:self.config.max_markets]]

    def get_allocation(self, token_id: str) -> Decimal:
        """Get allocation for a specific market."""
        return self._allocations.get(token_id, Decimal("0"))

    def get_metrics(self, token_id: str) -> Optional[MarketMetrics]:
        """Get metrics for a market."""
        return self._market_metrics.get(token_id)

    def remove_market(self, token_id: str):
        """Remove a market from allocation."""
        self._market_metrics.pop(token_id, None)
        self._allocations.pop(token_id, None)

    def reset(self):
        """Reset allocator state."""
        self._market_metrics.clear()
        self._allocations.clear()

    def get_summary(self) -> dict:
        """Get allocation summary."""
        return {
            "total_capital": self.config.total_capital,
            "allocated": sum(self._allocations.values()),
            "num_markets": len(self._allocations),
            "allocations": {
                k: float(v) for k, v in self._allocations.items()
            },
        }