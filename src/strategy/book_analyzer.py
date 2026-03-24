"""
Order book analyzer - extract trading signals from order book structure.

Analyzes:
- Imbalance: ratio of bid/ask depth (predicts short-term direction)
- Competitive positioning: where to place quotes for fills
- Depth analysis: quality of liquidity around best prices
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, List, Tuple

from src.models import OrderBook, PriceLevel
from src.utils import setup_logging

# Defaults - can be overridden via constructor
DEFAULT_IMBALANCE_THRESHOLD = 0.10  # 10% from balanced
DEFAULT_DEPTH_CENTS = 5.0           # 5 cents from best
DEFAULT_TICK_IMPROVE = 0.01         # 1 cent improvement

logger = setup_logging()


@dataclass
class BookAnalysis:
    """Complete order book analysis."""
    # Imbalance
    imbalance_ratio: float       # 0.0-1.0: 0=all asks, 0.5=balanced, 1=all bids
    imbalance_signal: str        # "BID_HEAVY", "ASK_HEAVY", "BALANCED"
    price_adjustment: Decimal    # Suggested adjustment based on imbalance

    # Depth
    bid_depth: float             # Total $ within BOOK_DEPTH_CENTS of best bid
    ask_depth: float             # Total $ within BOOK_DEPTH_CENTS of best ask
    total_depth: float           # Sum of bid + ask depth

    # Competitive positioning
    bid_wall_price: Optional[Decimal]  # Large bid order to avoid
    ask_wall_price: Optional[Decimal]  # Large ask order to avoid
    suggested_bid: Optional[Decimal]   # Competitive bid price
    suggested_ask: Optional[Decimal]   # Competitive ask price

    # Quality
    depth_quality: str           # "THIN", "NORMAL", "THICK"


class BookAnalyzer:
    """
    Analyzes order book structure for trading signals.

    Usage:
        analyzer = BookAnalyzer()
        analysis = analyzer.analyze(order_book)

        # Use imbalance for skewing
        if analysis.imbalance_signal == "BID_HEAVY":
            # More buyers - expect price up, skew quotes up
            bid_adj = Decimal("0.01")

        # Use competitive positioning
        bid_price = analysis.suggested_bid or (mid - spread/2)
    """

    # Wall detection: consider it a "wall" if single order > X% of nearby depth
    WALL_THRESHOLD = 0.30  # 30% of depth in one order

    # Minimum depth to consider market tradeable
    MIN_TRADEABLE_DEPTH = 50  # $50 each side

    def __init__(
        self,
        imbalance_threshold: float = DEFAULT_IMBALANCE_THRESHOLD,
        depth_cents: float = DEFAULT_DEPTH_CENTS,
        tick_improve: float = DEFAULT_TICK_IMPROVE,
    ):
        """
        Args:
            imbalance_threshold: How far from 0.5 before considered imbalanced (default 0.1)
            depth_cents: How many cents from best to measure depth (default 5)
            tick_improve: How much to improve vs best price (default 0.01)
        """
        self.imbalance_threshold = imbalance_threshold
        self.depth_cents = depth_cents / 100  # Convert to decimal
        self.tick_improve = Decimal(str(tick_improve))

    def analyze(self, order_book: Optional[OrderBook]) -> BookAnalysis:
        """
        Analyze order book and return trading signals.

        Args:
            order_book: Current order book state

        Returns:
            BookAnalysis with imbalance, depth, and positioning info
        """
        if order_book is None or not order_book.bids or not order_book.asks:
            return self._empty_analysis()

        # Calculate depth within range
        best_bid = Decimal(str(order_book.best_bid))
        best_ask = Decimal(str(order_book.best_ask))

        bid_depth, bid_levels = self._calculate_depth(
            order_book.bids, float(best_bid), self.depth_cents
        )
        ask_depth, ask_levels = self._calculate_depth(
            order_book.asks, float(best_ask), self.depth_cents
        )

        total_depth = bid_depth + ask_depth

        # Calculate imbalance
        if total_depth > 0:
            imbalance_ratio = bid_depth / total_depth
        else:
            imbalance_ratio = 0.5

        imbalance_signal = self._classify_imbalance(imbalance_ratio)
        price_adjustment = self._calculate_imbalance_adjustment(imbalance_ratio)

        # Find walls
        bid_wall = self._find_wall(bid_levels, bid_depth)
        ask_wall = self._find_wall(ask_levels, ask_depth)

        # Calculate competitive positions
        suggested_bid, suggested_ask = self._competitive_prices(
            order_book.bids, order_book.asks, bid_wall, ask_wall
        )

        # Classify depth quality
        depth_quality = self._classify_depth(bid_depth, ask_depth)

        return BookAnalysis(
            imbalance_ratio=imbalance_ratio,
            imbalance_signal=imbalance_signal,
            price_adjustment=price_adjustment,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            total_depth=total_depth,
            bid_wall_price=bid_wall,
            ask_wall_price=ask_wall,
            suggested_bid=suggested_bid,
            suggested_ask=suggested_ask,
            depth_quality=depth_quality,
        )

    def get_imbalance_adjustment(
        self,
        order_book: Optional[OrderBook],
        max_adjustment: Decimal = Decimal("0.02"),
    ) -> Decimal:
        """
        Quick method to get just the imbalance-based price adjustment.

        Args:
            order_book: Current order book
            max_adjustment: Maximum adjustment in either direction

        Returns:
            Decimal adjustment to add to both bid and ask prices
            Positive = expect price up, negative = expect down
        """
        if order_book is None:
            return Decimal("0")

        analysis = self.analyze(order_book)
        # Clamp to max adjustment
        adj = analysis.price_adjustment
        if adj > max_adjustment:
            return max_adjustment
        if adj < -max_adjustment:
            return -max_adjustment
        return adj

    def _empty_analysis(self) -> BookAnalysis:
        """Return empty analysis when no book data."""
        return BookAnalysis(
            imbalance_ratio=0.5,
            imbalance_signal="BALANCED",
            price_adjustment=Decimal("0"),
            bid_depth=0.0,
            ask_depth=0.0,
            total_depth=0.0,
            bid_wall_price=None,
            ask_wall_price=None,
            suggested_bid=None,
            suggested_ask=None,
            depth_quality="THIN",
        )

    def _calculate_depth(
        self,
        levels: List[PriceLevel],
        best_price: float,
        within_range: float,
    ) -> Tuple[float, List[PriceLevel]]:
        """Calculate total depth within range of best price."""
        depth = 0.0
        included = []

        for level in levels:
            level_price = Decimal(str(level.price))
            best_price_dec = Decimal(str(best_price))
            if abs(level_price - best_price_dec) <= Decimal(str(within_range)):
                # Depth in $ terms (price * size)
                depth += float(level_price) * float(level.size)
                included.append(level)

        return depth, included

    def _classify_imbalance(self, ratio: float) -> str:
        """Classify imbalance ratio into signal."""
        if ratio > 0.5 + self.imbalance_threshold:
            return "BID_HEAVY"  # More buyers, expect up
        if ratio < 0.5 - self.imbalance_threshold:
            return "ASK_HEAVY"  # More sellers, expect down
        return "BALANCED"

    def _calculate_imbalance_adjustment(self, ratio: float) -> Decimal:
        """
        Calculate price adjustment from imbalance.

        Maps imbalance to adjustment:
        - 0.3 ratio (ask heavy) -> -0.01 (expect down)
        - 0.5 ratio (balanced) -> 0
        - 0.7 ratio (bid heavy) -> +0.01 (expect up)
        """
        # Linear mapping: deviation from 0.5, scaled
        deviation = Decimal(str(ratio)) - Decimal("0.5")  # -0.5 to +0.5
        # Scale: 0.2 deviation = 0.01 adjustment
        adjustment = deviation * Decimal("0.05")  # 0.2 * 0.05 = 0.01
        return adjustment.quantize(Decimal("0.001"))

    def _find_wall(
        self,
        levels: List[PriceLevel],
        total_depth: float,
    ) -> Optional[Decimal]:
        """Find price of a wall (large order) if one exists."""
        if total_depth < self.MIN_TRADEABLE_DEPTH:
            return None

        for level in levels:
            order_value = level.price * level.size
            if float(order_value) / total_depth > float(self.WALL_THRESHOLD):
                return level.price

        return None

    def _competitive_prices(
        self,
        bids: List[PriceLevel],
        asks: List[PriceLevel],
        bid_wall: Optional[Decimal],
        ask_wall: Optional[Decimal],
    ) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """
        Calculate competitive prices that improve on best quotes.

        Avoids placing behind walls.
        """
        if not bids or not asks:
            return None, None

        best_bid = Decimal(str(bids[0].price))
        best_ask = Decimal(str(asks[0].price))

        # For bid: improve by 1 tick unless there's a wall at best
        if bid_wall == best_bid:
            # Don't compete with wall, place behind it
            suggested_bid = best_bid - self.tick_improve
        else:
            # Improve by 1 tick
            suggested_bid = best_bid + self.tick_improve

        # For ask: improve by 1 tick unless there's a wall at best
        if ask_wall == best_ask:
            suggested_ask = best_ask + self.tick_improve
        else:
            suggested_ask = best_ask - self.tick_improve

        # Ensure we don't cross
        if suggested_bid >= suggested_ask:
            # Revert to best prices
            suggested_bid = best_bid
            suggested_ask = best_ask

        # Round to tick
        suggested_bid = (suggested_bid * 100).quantize(Decimal("1")) / 100
        suggested_ask = (suggested_ask * 100).quantize(Decimal("1")) / 100

        return suggested_bid, suggested_ask

    def _classify_depth(self, bid_depth: float, ask_depth: float) -> str:
        """Classify overall depth quality."""
        min_depth = min(bid_depth, ask_depth)

        if min_depth < self.MIN_TRADEABLE_DEPTH:
            return "THIN"
        if min_depth < 200:
            return "NORMAL"
        return "THICK"


def analyze_book(order_book: Optional[OrderBook]) -> BookAnalysis:
    """Convenience function to analyze an order book."""
    analyzer = BookAnalyzer()
    return analyzer.analyze(order_book)
