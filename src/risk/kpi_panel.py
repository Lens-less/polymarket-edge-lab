"""
Risk KPI Panel

Tracks and monitors key risk metrics for market making:
- Taker rate
- Cancel rate
- Rejection rate
- Balance drawdown
- Position limits
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from decimal import Decimal
from datetime import datetime
import time


@dataclass
class RiskMetrics:
    """Current risk metrics snapshot."""
    # Trade metrics
    total_orders_placed: int = 0
    total_fills: int = 0
    total_cancels: int = 0
    total_rejections: int = 0

    # Rates (0-1)
    taker_rate: float = 0.0
    cancel_rate: float = 0.0
    rejection_rate: float = 0.0

    # P&L metrics
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")

    # Position metrics
    current_position: Decimal = Decimal("0")
    max_position: Decimal = Decimal("0")
    position_limit: Decimal = Decimal("1000")

    # Balance metrics
    initial_balance: Decimal = Decimal("0")
    current_balance: Decimal = Decimal("0")
    max_balance: Decimal = Decimal("0")
    drawdown_pct: float = 0.0

    # Time
    start_time: float = field(default_factory=time.time)


class RiskKPIPanel:
    """
    Risk KPI monitoring panel.

    Tracks key risk metrics and provides alerts when thresholds are exceeded.
    """

    def __init__(
        self,
        initial_balance: Decimal = Decimal("1000"),
        position_limit: Decimal = Decimal("1000"),
        daily_loss_limit: Decimal = Decimal("100"),
        drawdown_limit: float = 0.2,  # 20%
        taker_rate_limit: float = 0.5,
        cancel_rate_limit: float = 0.3,
        rejection_rate_limit: float = 0.1,
    ):
        self.initial_balance = initial_balance
        self.current_balance = initial_balance
        self.max_balance = initial_balance
        self.position_limit = position_limit

        # Thresholds
        self.daily_loss_limit = daily_loss_limit
        self.drawdown_limit = drawdown_limit
        self.taker_rate_limit = taker_rate_limit
        self.cancel_rate_limit = cancel_rate_limit
        self.rejection_rate_limit = rejection_rate_limit

        # Metrics
        self._metrics = RiskMetrics(
            initial_balance=initial_balance,
            current_balance=initial_balance,
            max_balance=initial_balance,
            position_limit=position_limit,
        )

        # Daily tracking
        self._daily_start_pnl = Decimal("0")
        self._last_reset_date = datetime.now().date()

        # Alert callbacks
        self._alert_callbacks: List[callable] = []

    def add_alert_callback(self, callback: callable):
        """Add a callback for risk alerts."""
        self._alert_callbacks.append(callback)

    def record_order_placed(self):
        """Record an order being placed."""
        self._metrics.total_orders_placed += 1

    def record_fill(self, is_taker: bool = False):
        """Record a fill."""
        self._metrics.total_fills += 1
        if is_taker:
            self._metrics.taker_fills += 1

    def record_cancel(self):
        """Record an order cancellation."""
        self._metrics.total_cancels += 1

    def record_rejection(self):
        """Record an order rejection."""
        self._metrics.total_rejections += 1

    def update_pnl(self, realized: Decimal, unrealized: Decimal):
        """Update P&L tracking."""
        self._metrics.realized_pnl = realized
        self._metrics.unrealized_pnl = unrealized
        self._metrics.daily_pnl = realized - self._daily_start_pnl

    def update_balance(self, new_balance: Decimal):
        """Update current balance and drawdown."""
        self.current_balance = new_balance
        self._metrics.current_balance = new_balance

        if new_balance > self.max_balance:
            self.max_balance = new_balance
            self._metrics.max_balance = new_balance

        # Calculate drawdown
        if self.max_balance > 0:
            self._metrics.drawdown_pct = float(
                (self.max_balance - new_balance) / self.max_balance
            )

    def update_position(self, position: Decimal):
        """Update position tracking."""
        self._metrics.current_position = position

        if abs(position) > abs(self._metrics.max_position):
            self._metrics.max_position = position

    def calculate_rates(self):
        """Calculate current rates."""
        placed = self._metrics.total_orders_placed
        if placed > 0:
            self._metrics.cancel_rate = self._metrics.total_cancels / placed
            self._metrics.rejection_rate = self._metrics.total_rejections / placed

        fills = self._metrics.total_fills
        if fills > 0:
            # Estimate taker rate based on fills that crossed spread
            # This would need to be updated when recording fills
            pass

    def check_thresholds(self) -> Dict[str, bool]:
        """Check if any thresholds are exceeded."""
        self.calculate_rates()

        violations = {}

        # Check drawdown
        violations["drawdown"] = self._metrics.drawdown_pct >= self.drawdown_limit

        # Check daily loss
        violations["daily_loss"] = self._metrics.daily_pnl <= -self.daily_loss_limit

        # Check cancel rate
        violations["cancel_rate"] = self._metrics.cancel_rate >= self.cancel_rate_limit

        # Check rejection rate
        violations["rejection_rate"] = self._metrics.rejection_rate >= self.rejection_rate_limit

        # Check position limit
        violations["position_limit"] = (
            abs(self._metrics.current_position) >= self.position_limit
        )

        # Check taker rate (if tracked)
        violations["taker_rate"] = self._metrics.taker_rate >= self.taker_rate_limit

        # Fire alerts for violations
        for key, is_violated in violations.items():
            if is_violated:
                self._fire_alert(key)

        return violations

    def _fire_alert(self, violation_type: str):
        """Fire alert callbacks."""
        alert_msg = f"Risk violation: {violation_type}"
        for callback in self._alert_callbacks:
            try:
                callback(violation_type, alert_msg)
            except Exception:
                pass

    def should_halt(self) -> bool:
        """Check if trading should halt due to risk limits."""
        violations = self.check_thresholds()
        return any(violations.values())

    def get_metrics(self) -> RiskMetrics:
        """Get current risk metrics."""
        self.calculate_rates()
        return self._metrics

    def get_summary(self) -> dict:
        """Get summary dict for display."""
        metrics = self.get_metrics()
        return {
            "orders_placed": metrics.total_orders_placed,
            "fills": metrics.total_fills,
            "cancels": metrics.total_cancels,
            "rejections": metrics.total_rejections,
            "taker_rate": f"{metrics.taker_rate:.1%}",
            "cancel_rate": f"{metrics.cancel_rate:.1%}",
            "rejection_rate": f"{metrics.rejection_rate:.1%}",
            "daily_pnl": f"${metrics.daily_pnl:.2f}",
            "total_pnl": f"${metrics.realized_pnl:.2f}",
            "position": f"${metrics.current_position:.2f}",
            "drawdown": f"{metrics.drawdown_pct:.1%}",
            "balance": f"${metrics.current_balance:.2f}",
            "should_halt": self.should_halt(),
        }

    def reset_daily(self):
        """Reset daily tracking (call at start of each day)."""
        today = datetime.now().date()
        if today != self._last_reset_date:
            self._daily_start_pnl = self._metrics.realized_pnl
            self._last_reset_date = today
            self._metrics.total_orders_placed = 0
            self._metrics.total_fills = 0
            self._metrics.total_cancels = 0
            self._metrics.total_rejections = 0