"""
Backtest Engine

Replays historical data to evaluate strategy performance.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Callable
from enum import Enum
import uuid
import statistics

from src.backtest.data import HistoricalData, OrderBookSnapshot
from src.backtest.fee_model import FeeModel, FeeConfig, FeeScenario

class OrderStatus(Enum):
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"

@dataclass
class BacktestOrder:
    """Order in backtest."""
    id: str
    side: str
    price: Decimal
    size: Decimal
    status: OrderStatus = OrderStatus.OPEN
    filled_size: Decimal = field(default_factory=lambda: Decimal("0"))
    fill_price: Optional[Decimal] = None
    fill_time: Optional[int] = None

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def remaining_size(self) -> Decimal:
        return self.size - self.filled_size

    @property
    def fill_ratio(self) -> float:
        if self.size == 0:
            return 0.0
        return float(self.filled_size / self.size)

@dataclass
class BacktestResult:
    """Result of backtest run."""
    initial_capital: Decimal
    final_capital: Decimal
    total_return: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    sharpe_ratio: float
    max_drawdown: float
    profit_factor: float

class BacktestEngine:
    """
    Backtesting engine for strategy evaluation.

    Usage:
        engine = BacktestEngine(initial_capital=Decimal("1000"))

        data = HistoricalData()
        data.load_from_file("data.csv")

        result = engine.run(data, strategy="smart_mm")
        print(result.sharpe_ratio)
    """

    def __init__(
        self,
        initial_capital: Decimal = Decimal("1000"),
        fill_model: str = "simple",
        partial_fill_enabled: bool = True,
        queue_position_factor: float = 0.5,
        min_fill_probability: float = 0.1,
        fee_config: Optional[FeeConfig] = None,
    ):
        """
        Initialize backtest engine.

        Args:
            initial_capital: Starting capital
            fill_model: Fill model type - "simple" (legacy), "depth_based", "probabilistic"
            partial_fill_enabled: Enable partial fills
            queue_position_factor: Factor for queue position impact (0-1)
            min_fill_probability: Minimum probability for any fill to occur
            fee_config: Fee configuration for cost modeling
        """
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.position = Decimal("0")
        self.realized_pnl = Decimal("0")
        self._avg_entry_price = Decimal("0")

        self._orders: Dict[str, BacktestOrder] = {}
        self._trades: List[dict] = []
        self._equity_curve: List[Decimal] = [initial_capital]

        # Fill model configuration
        self.fill_model = fill_model
        self.partial_fill_enabled = partial_fill_enabled
        self.queue_position_factor = queue_position_factor
        self.min_fill_probability = min_fill_probability

        # Fee model configuration
        self.fee_model = FeeModel(fee_config or FeeConfig())

        # Track partial fill state
        self._partial_fills: Dict[str, Decimal] = {}  # order_id -> filled_amount

    def run(
        self,
        data: HistoricalData,
        strategy: str = "simple_mm",
        strategy_fn: Optional[Callable] = None,
    ) -> BacktestResult:
        """
        Run backtest on historical data.

        Args:
            data: Historical order book data
            strategy: Strategy name or "custom"
            strategy_fn: Custom strategy function

        Returns:
            BacktestResult with performance metrics
        """
        for snapshot in data.iterate():
            # Check for fills
            self._check_fills(snapshot)

            # Run strategy
            if strategy_fn:
                strategy_fn(self, snapshot)
            else:
                self._run_default_strategy(snapshot)

            # Update equity curve
            unrealized = self._calculate_unrealized(snapshot)
            self._equity_curve.append(self.capital + unrealized)

        return self._build_result()

    def place_order(
        self,
        side: str,
        price: Decimal,
        size: Decimal,
    ) -> str:
        """Place an order."""
        order_id = str(uuid.uuid4())[:8]
        order = BacktestOrder(
            id=order_id,
            side=side.upper(),
            price=price,
            size=size,
        )
        self._orders[order_id] = order
        return order_id

    def cancel_order(self, order_id: str):
        """Cancel an order."""
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELLED

    def get_order(self, order_id: str) -> Optional[BacktestOrder]:
        """Get order by ID."""
        return self._orders.get(order_id)

    def process_snapshot(self, snapshot: OrderBookSnapshot):
        """Process a single snapshot (for testing)."""
        self._check_fills(snapshot)

    def generate_report(self) -> dict:
        """Generate performance report as dict."""
        result = self._build_result()
        return {
            "total_return": result.total_return,
            "sharpe_ratio": result.sharpe_ratio,
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "total_trades": result.total_trades,
            "profit_factor": result.profit_factor,
            "initial_capital": result.initial_capital,
            "final_capital": result.final_capital,
        }

    def _build_result(self) -> BacktestResult:
        """Build BacktestResult from current state."""
        final = self._equity_curve[-1] if self._equity_curve else self.initial_capital
        total_return = float((final - self.initial_capital) / self.initial_capital)

        wins = [t for t in self._trades if t["pnl"] > 0]
        losses = [t for t in self._trades if t["pnl"] < 0]

        win_rate = len(wins) / max(1, len(self._trades))
        sharpe = self._calculate_sharpe()
        max_dd = self._calculate_max_drawdown()
        profit_factor = self._calculate_profit_factor()

        return BacktestResult(
            initial_capital=self.initial_capital,
            final_capital=final,
            total_return=total_return,
            total_trades=len(self._trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=win_rate,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            profit_factor=profit_factor,
        )

    def _check_fills(self, snapshot: OrderBookSnapshot):
        """Check if any orders should fill based on fill model."""
        for order_id, order in self._orders.items():
            if order.status not in (OrderStatus.OPEN, OrderStatus.PARTIAL):
                continue

            if self.fill_model == "simple":
                self._check_fill_simple(order, snapshot)
            elif self.fill_model == "depth_based":
                self._check_fill_depth_based(order, snapshot)
            elif self.fill_model == "probabilistic":
                self._check_fill_probabilistic(order, snapshot)
            else:
                self._check_fill_simple(order, snapshot)

    def _check_fill_simple(self, order: BacktestOrder, snapshot: OrderBookSnapshot):
        """Legacy simple fill model - all or nothing."""
        filled = False
        fill_price = Decimal("0")

        if order.side == "BUY":
            if snapshot.best_ask <= order.price:
                filled = True
                fill_price = order.price
        else:  # SELL
            if snapshot.best_bid >= order.price:
                filled = True
                fill_price = order.price

        if filled:
            self._execute_fill(order, order.remaining_size, fill_price, snapshot.timestamp)

    def _check_fill_depth_based(self, order: BacktestOrder, snapshot: OrderBookSnapshot):
        """
        Depth-based fill model with partial fill support.

        Fill probability and size based on order book depth.
        - Better queue position (tighter price) = higher fill probability
        - Larger depth = more partial fill possible
        """
        if order.side == "BUY":
            if snapshot.best_ask > order.price:
                return  # No fill possible

            # Calculate fillable quantity based on depth
            # Depth represents liquidity available at/through our price level
            depth = snapshot.bid_depth if order.price <= snapshot.best_bid else snapshot.ask_depth

            # Queue position factor: better price = better position
            queue_factor = self._calculate_queue_factor(
                order.price, snapshot.best_bid, snapshot.best_ask, order.side
            )

        else:  # SELL
            if snapshot.best_bid < order.price:
                return  # No fill possible

            depth = snapshot.ask_depth if order.price >= snapshot.best_ask else snapshot.bid_depth

            queue_factor = self._calculate_queue_factor(
                order.price, snapshot.best_bid, snapshot.best_ask, order.side
            )

        # Keep fill-size math in Decimal space to avoid Decimal/float type errors.
        available_size = (
            Decimal(str(depth))
            * Decimal(str(queue_factor))
            * Decimal(str(self.queue_position_factor))
        )

        if available_size <= 0:
            return

        if self.partial_fill_enabled:
            # Partial fill: fill up to available size
            fill_size = min(order.remaining_size, available_size)

            # Ensure minimum fill size
            min_fill = order.size * Decimal("0.1")  # At least 10%
            if fill_size < min_fill and fill_size < order.remaining_size:
                return  # Wait for more liquidity
        else:
            # All or nothing: only fill if full size available
            if available_size < order.remaining_size:
                return
            fill_size = order.remaining_size

        fill_price = order.price
        self._execute_fill(order, fill_size, fill_price, snapshot.timestamp)

    def _check_fill_probabilistic(self, order: BacktestOrder, snapshot: OrderBookSnapshot):
        """
        Probabilistic fill model using queue position and depth.

        Simulates realistic queue behavior:
        - Fill probability increases with depth
        - Better queue position = higher probability
        - Random factor simulates queue priority uncertainty
        """
        import random

        if order.side == "BUY":
            if snapshot.best_ask > order.price:
                return
            depth = snapshot.bid_depth if order.price <= snapshot.best_bid else snapshot.ask_depth
            queue_factor = self._calculate_queue_factor(
                order.price, snapshot.best_bid, snapshot.best_ask, order.side
            )
        else:  # SELL
            if snapshot.best_bid < order.price:
                return
            depth = snapshot.ask_depth if order.price >= snapshot.best_ask else snapshot.bid_depth
            queue_factor = self._calculate_queue_factor(
                order.price, snapshot.best_bid, snapshot.best_ask, order.side
            )

        # Calculate fill probability
        # Base probability from depth (normalized)
        depth_factor = min(float(depth) / 100.0, 1.0)  # Cap at 1.0

        # Combined probability
        fill_probability = max(
            self.min_fill_probability,
            depth_factor * queue_factor
        )

        # Determine if fill occurs
        if random.random() > fill_probability:
            return

        # Determine fill size
        if self.partial_fill_enabled:
            # Partial fill based on depth and random factor
            avg_fill_ratio = min(fill_probability * 1.5, 1.0)  # Tend to fill more when prob is high
            fill_variance = random.uniform(0.5, 1.0)
            fill_ratio = avg_fill_ratio * fill_variance
            fill_size = order.remaining_size * Decimal(str(fill_ratio))

            # Ensure reasonable minimum
            min_fill = order.size * Decimal("0.1")
            if fill_size < min_fill:
                fill_size = min_fill
            fill_size = min(fill_size, order.remaining_size)
        else:
            fill_size = order.remaining_size

        fill_price = order.price
        self._execute_fill(order, fill_size, fill_price, snapshot.timestamp)

    def _calculate_queue_factor(
        self,
        order_price: Decimal,
        best_bid: Decimal,
        best_ask: Decimal,
        side: str
    ) -> float:
        """
        Calculate queue position factor (0-1).

        1.0 = best position (at front of queue)
        0.0 = worst position (at back of queue)
        """
        spread = best_ask - best_bid
        if spread <= 0:
            return 0.5  # Neutral if no spread

        if side == "BUY":
            if order_price >= best_ask:
                return 1.0  # Aggressive, crosses spread
            elif order_price <= best_bid:
                # Calculate position in queue (at bid level)
                distance = best_bid - order_price
                queue_factor = max(0.0, 1.0 - float(distance / spread))
                return queue_factor
            else:
                # Inside spread - good position
                return 0.8
        else:  # SELL
            if order_price <= best_bid:
                return 1.0  # Aggressive, crosses spread
            elif order_price >= best_ask:
                distance = order_price - best_ask
                queue_factor = max(0.0, 1.0 - float(distance / spread))
                return queue_factor
            else:
                return 0.8

    def _execute_fill(
        self,
        order: BacktestOrder,
        fill_size: Decimal,
        fill_price: Decimal,
        timestamp: int
    ):
        """Execute a fill (full or partial)."""
        if fill_size <= 0:
            return

        # Update order fill state
        order.filled_size += fill_size
        order.fill_price = fill_price
        order.fill_time = timestamp

        if order.filled_size >= order.size:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIAL

        # Process the fill
        self._process_fill(order, fill_size, fill_price, timestamp)

    def _process_fill(
        self,
        order: BacktestOrder,
        fill_size: Decimal,
        fill_price: Decimal,
        timestamp: int
    ):
        """Process a filled order."""
        pnl = Decimal("0")
        trade_value = fill_size * fill_price

        # Calculate and deduct fees
        fee = self.fee_model.calculate_taker_fee(
            trade_value,
            shares=fill_size,
            price=fill_price,
        )
        self.capital -= fee

        if order.side == "BUY":
            new_position = self.position + fill_size

            # Track entry price for P&L calculation
            if self.position <= 0:
                # Opening or flipping to long
                self._avg_entry_price = fill_price
            elif new_position != 0:
                # Averaging into existing long
                total_cost = self._avg_entry_price * self.position + fill_price * fill_size
                self._avg_entry_price = total_cost / new_position

            self.position = new_position
            self.capital -= fill_size * fill_price
        else:
            # SELL
            new_position = self.position - fill_size

            # Realize P&L if closing long position
            if self.position > 0:
                close_size = min(fill_size, self.position)
                pnl = (fill_price - self._avg_entry_price) * close_size
                self.realized_pnl += pnl

            self.position = new_position
            self.capital += fill_size * fill_price

            # If flipping to short or closing, reset entry
            if self.position <= 0:
                self._avg_entry_price = fill_price if self.position < 0 else Decimal("0")

        # Record trade
        self._trades.append({
            "order_id": order.id,
            "side": order.side,
            "price": fill_price,
            "size": fill_size,
            "timestamp": timestamp,
            "pnl": pnl,
            "fee": fee,
            "partial": order.status == OrderStatus.PARTIAL,
        })

    def _calculate_unrealized(self, snapshot: OrderBookSnapshot) -> Decimal:
        """Calculate unrealized P&L."""
        if self.position == 0:
            return Decimal("0")

        mid = (snapshot.best_bid + snapshot.best_ask) / 2

        if self.position > 0:
            return (mid - self._avg_entry_price) * self.position
        return (self._avg_entry_price - mid) * abs(self.position)

    def _calculate_sharpe(self) -> float:
        """Calculate Sharpe ratio."""
        if len(self._equity_curve) < 2:
            return 0.0

        returns = []
        for i in range(1, len(self._equity_curve)):
            ret = (self._equity_curve[i] - self._equity_curve[i-1]) / self._equity_curve[i-1]
            returns.append(float(ret))

        if not returns:
            return 0.0

        try:
            mean = statistics.mean(returns)
            std = statistics.stdev(returns) if len(returns) > 1 else 1
            return (mean / std) * (252 ** 0.5) if std > 0 else 0  # Annualized
        except Exception:
            return 0.0

    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum drawdown."""
        if not self._equity_curve:
            return 0.0

        peak = self._equity_curve[0]
        max_dd = 0.0

        for value in self._equity_curve:
            if value > peak:
                peak = value
            dd = float((peak - value) / peak)
            max_dd = max(max_dd, dd)

        return max_dd

    def _calculate_profit_factor(self) -> float:
        """Calculate profit factor."""
        gross_profit = sum(t["pnl"] for t in self._trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in self._trades if t["pnl"] < 0))

        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0
        return float(gross_profit / gross_loss)

    def _run_default_strategy(self, snapshot: OrderBookSnapshot):
        """Default simple MM strategy for testing."""
        # Cancel existing open orders
        for order_id, order in list(self._orders.items()):
            if order.status == OrderStatus.OPEN:
                self.cancel_order(order_id)

        # Place orders at market prices (aggressive - will fill on next price movement)
        # BUY at best_ask (crossing the spread to ensure fills)
        # SELL at best_bid (crossing the spread to ensure fills)
        self.place_order("BUY", snapshot.best_ask, Decimal("10"))
        self.place_order("SELL", snapshot.best_bid, Decimal("10"))
