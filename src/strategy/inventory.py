"""
Inventory manager - gradual position skewing and P&L tracking.

Instead of binary position limits (stop quoting at limit),
gradually skew prices and reduce sizes as inventory grows.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Dict

from src.orders import get_position
from src.utils import setup_logging

logger = setup_logging()

# Defaults - can be overridden via constructor
DEFAULT_POSITION_LIMIT = Decimal("100")   # Max position size
DEFAULT_SKEW_MAX = Decimal("0.02")        # Max price skew (2 cents)
DEFAULT_SIZE_REDUCTION_START = Decimal("0.5")  # Start reducing at 50% of limit


@dataclass
class InventoryState:
    """Current inventory state for display and calculations."""
    position: Decimal              # Authoritative wallet balance/exposure
    strategy_position: Decimal     # Net position attributable to this session
    position_limit: Decimal        # Max allowed position
    position_pct: float            # Position as % of limit (-100 to +100)

    # Skew values
    bid_skew: Decimal              # Price adjustment for bids (negative = lower bid)
    ask_skew: Decimal              # Price adjustment for asks (positive = higher ask)

    # Size adjustments
    bid_size_mult: float           # Size multiplier for bids (0.0-1.0)
    ask_size_mult: float           # Size multiplier for asks (0.0-1.0)

    # P&L tracking
    vwap_entry: Optional[Decimal]  # Volume-weighted average entry price
    unrealized_pnl: Decimal        # Unrealized P&L at current mid
    realized_pnl: Decimal          # Realized P&L from closed trades

    # Signals
    inventory_level: str           # "NEUTRAL", "LONG", "SHORT", "MAX_LONG", "MAX_SHORT"


@dataclass
class TradeRecord:
    """Record of a single trade for VWAP calculation."""
    price: Decimal
    size: Decimal
    side: str  # "BUY" or "SELL"


class InventoryManager:
    """
    Manages inventory risk through gradual skewing.

    Instead of hard stops at position limits:
    - Gradually skew prices to encourage mean reversion
    - Reduce order sizes as inventory grows
    - Track VWAP entry for P&L calculation

    Usage:
        inv = InventoryManager(token_id="abc123")

        # Get adjustments for quoting
        state = inv.get_state(mid_price=0.55)
        bid_price = mid - spread/2 + state.bid_skew
        ask_price = mid + spread/2 + state.ask_skew
        bid_size = base_size * state.bid_size_mult
        ask_size = base_size * state.ask_size_mult

        # Record fills
        inv.record_fill(price=0.54, size=10, side="BUY")
    """

    def __init__(
        self,
        token_id: str,
        position_limit: Decimal = DEFAULT_POSITION_LIMIT,
        skew_max: Decimal = DEFAULT_SKEW_MAX,
        size_reduction_start: Decimal = DEFAULT_SIZE_REDUCTION_START,
        min_size_mult: float = 0.2,  # Minimum size multiplier
    ):
        """
        Args:
            token_id: Token being managed
            position_limit: Maximum position size
            skew_max: Maximum price skew at full inventory
            size_reduction_start: Position % at which to start reducing size
            min_size_mult: Minimum size multiplier (never go below this)
        """
        self.token_id = token_id
        self.position_limit = position_limit
        self.skew_max = skew_max
        self.size_reduction_start = size_reduction_start
        self.min_size_mult = min_size_mult

        # P&L tracking
        self._trades: list[TradeRecord] = []
        self._realized_pnl = Decimal("0")
        self._total_bought = Decimal("0")
        self._total_bought_value = Decimal("0")
        self._total_sold = Decimal("0")
        self._total_sold_value = Decimal("0")
        self._strategy_position = Decimal("0")

    def get_state(
        self,
        mid_price: Optional[Decimal] = None,
        *,
        wallet_position: Optional[Decimal] = None,
    ) -> InventoryState:
        """
        Get current inventory state with all adjustments calculated.

        Args:
            mid_price: Current mid price for unrealized P&L calc

        Returns:
            InventoryState with skews, size multipliers, P&L
        """
        position = (
            get_position(self.token_id)
            if wallet_position is None
            else wallet_position
        )
        position = Decimal(str(position))

        # Calculate position percentage (-100% to +100%)
        if self.position_limit > 0:
            position_pct = float(position / self.position_limit * 100)
        else:
            position_pct = 0.0

        # Clamp to ±100%
        position_pct = max(-100, min(100, position_pct))

        # Calculate skews based on inventory
        bid_skew, ask_skew = self._calculate_skews(position)

        # Calculate size multipliers
        bid_size_mult, ask_size_mult = self._calculate_size_multipliers(position)

        # Calculate P&L
        vwap_entry = self._calculate_vwap()
        unrealized_pnl = Decimal("0")
        if (
            vwap_entry is not None
            and mid_price is not None
            and self._strategy_position != 0
        ):
            # Long position: profit if price > entry
            # Short position: profit if price < entry
            unrealized_pnl = self._strategy_position * (mid_price - vwap_entry)

        # Classify inventory level
        level = self._classify_level(position_pct)

        return InventoryState(
            position=position,
            strategy_position=self._strategy_position,
            position_limit=self.position_limit,
            position_pct=position_pct,
            bid_skew=bid_skew,
            ask_skew=ask_skew,
            bid_size_mult=bid_size_mult,
            ask_size_mult=ask_size_mult,
            vwap_entry=vwap_entry,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=self._realized_pnl,
            inventory_level=level,
        )

    def record_fill(self, price: Decimal, size: Decimal, side: str):
        """
        Record a trade fill for P&L tracking.

        Args:
            price: Fill price
            size: Fill size
            side: "BUY" or "SELL"
        """
        self._trades.append(TradeRecord(price=price, size=size, side=side))

        if side == "BUY":
            self._strategy_position += size
            self._total_bought += size
            self._total_bought_value += price * size
        else:
            self._strategy_position -= size
            self._total_sold += size
            self._total_sold_value += price * size

        # Calculate realized P&L when position crosses zero
        self._update_realized_pnl()

        logger.debug(
            f"Fill recorded: {side} {size} @ {price}, "
            f"VWAP: {self._calculate_vwap()}"
        )

    def reset(self):
        """Reset P&L tracking (e.g., at start of new session)."""
        self._trades.clear()
        self._realized_pnl = Decimal("0")
        self._total_bought = Decimal("0")
        self._total_bought_value = Decimal("0")
        self._total_sold = Decimal("0")
        self._total_sold_value = Decimal("0")
        self._strategy_position = Decimal("0")

    def _calculate_skews(self, position: Decimal) -> tuple[Decimal, Decimal]:
        """
        Calculate bid and ask price skews based on inventory.

        When long (positive position):
        - Shift both quotes lower (less aggressive buying, more aggressive selling)

        When short (negative position):
        - Shift both quotes higher (more aggressive buying, less aggressive selling)

        Skew increases linearly with position size.
        """
        if self.position_limit == 0:
            return Decimal("0"), Decimal("0")

        # Position as fraction of limit (-1 to +1)
        inv_ratio = position / self.position_limit
        inv_ratio = max(Decimal("-1"), min(Decimal("1"), inv_ratio))

        # Shift the quote centre rather than widening only the inventory-
        # building side.  Final prices are quantized once, using the venue's
        # actual tick size, by SmartMarketMaker._calculate_quotes().
        centre_skew = -self.skew_max * inv_ratio
        return centre_skew, centre_skew

    def _calculate_size_multipliers(self, position: Decimal) -> tuple[float, float]:
        """
        Calculate size multipliers based on inventory.

        Reduce size on the "building" side as inventory grows.
        Keep size unchanged on the "reducing" side.

        At 50% of limit: 70% size on building side
        At 80% of limit: 40% size on building side
        At 100% of limit: min_size_mult
        """
        if self.position_limit == 0:
            return 1.0, 1.0

        # Position as fraction of limit
        inv_ratio = abs(position) / self.position_limit
        inv_ratio = min(Decimal("1"), inv_ratio)

        if inv_ratio < self.size_reduction_start:
            # Below threshold - full size both sides
            return 1.0, 1.0

        # Calculate reduction
        # Linear from 1.0 at start threshold to min_size_mult at 100%
        reduction_range = Decimal("1") - self.size_reduction_start
        if reduction_range == 0:
            mult = self.min_size_mult
        else:
            progress = (inv_ratio - self.size_reduction_start) / reduction_range
            mult = float(1.0 - float(progress) * (1.0 - self.min_size_mult))

        if position > 0:
            # Long - reduce bid size
            return mult, 1.0
        elif position < 0:
            # Short - reduce ask size
            return 1.0, mult
        else:
            return 1.0, 1.0

    def _calculate_vwap(self) -> Optional[Decimal]:
        """Calculate session-attributable volume-weighted average entry price."""
        position = self._strategy_position

        if position == 0:
            return None

        if position > 0:
            # Long position - VWAP is average buy price
            if self._total_bought == 0:
                return None
            return self._total_bought_value / self._total_bought
        else:
            # Short position - VWAP is average sell price
            if self._total_sold == 0:
                return None
            return self._total_sold_value / self._total_sold

    def _update_realized_pnl(self):
        """
        Update realized P&L when trades close out positions.

        This is simplified - assumes FIFO matching.
        """
        # For now, keep it simple: realized P&L is calculated
        # when position goes from positive to negative or vice versa
        pass  # TODO: Implement proper FIFO matching if needed

    def _classify_level(self, position_pct: float) -> str:
        """Classify inventory level for display."""
        if position_pct >= 90:
            return "MAX_LONG"
        if position_pct <= -90:
            return "MAX_SHORT"
        if position_pct >= 30:
            return "LONG"
        if position_pct <= -30:
            return "SHORT"
        return "NEUTRAL"


class MultiTokenInventoryManager:
    """
    Manage inventory for multiple tokens.

    Usage:
        mgr = MultiTokenInventoryManager()
        state1 = mgr.get_state("token1", mid_price=0.55)
        state2 = mgr.get_state("token2", mid_price=0.40)
    """

    def __init__(self, **kwargs):
        """kwargs are passed to each InventoryManager."""
        self._managers: Dict[str, InventoryManager] = {}
        self._kwargs = kwargs

    def get_state(
        self,
        token_id: str,
        mid_price: Optional[Decimal] = None,
    ) -> InventoryState:
        """Get inventory state for a token."""
        if token_id not in self._managers:
            self._managers[token_id] = InventoryManager(token_id, **self._kwargs)
        return self._managers[token_id].get_state(mid_price)

    def record_fill(self, token_id: str, price: Decimal, size: Decimal, side: str):
        """Record a fill for a token."""
        if token_id not in self._managers:
            self._managers[token_id] = InventoryManager(token_id, **self._kwargs)
        self._managers[token_id].record_fill(price, size, side)

    def get_manager(self, token_id: str) -> InventoryManager:
        """Get or create manager for a token."""
        if token_id not in self._managers:
            self._managers[token_id] = InventoryManager(token_id, **self._kwargs)
        return self._managers[token_id]
