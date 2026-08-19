"""
Risk manager - safety controls for trading.

Tracks P&L, positions, errors, and triggers kill switch when limits breached.

In data-gathering mode (RISK_ENFORCE=false):
- Logs all risk events but doesn't stop trading
- Captures data for later analysis and improvement
- Default in DRY_RUN mode

In enforcement mode (RISK_ENFORCE=true):
- Actually stops trading when limits hit
- Default in LIVE mode (protects real money)
"""

import time
from enum import Enum
from decimal import Decimal
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from collections import deque

from src.config import (
    RISK_ENFORCE,
    RISK_MAX_DAILY_LOSS,
    RISK_MAX_POSITION,
    RISK_MAX_TOTAL_EXPOSURE,
    RISK_ERROR_COOLDOWN,
    RISK_MAX_ERRORS_PER_MINUTE,
    # Phase 3: Risk-Adjusted Returns
    DYNAMIC_LIMIT_FLOOR,
    DYNAMIC_LIMIT_CEILING,
    ADVERSE_LOOKBACK_SECONDS,
    ADVERSE_TOXIC_THRESHOLD,
    KELLY_FRACTION,
    KELLY_MAX_POSITION,
    CORRELATION_THRESHOLD,
    MAX_CORRELATED_EXPOSURE,
)
from src.models import OrderSide
from src.orders import get_open_orders, get_position
from src.trading import cancel_all_orders
from src.utils import setup_logging

# Phase 3 modules
from src.risk.dynamic_limits import DynamicLimitManager, MarketConditions
from src.risk.adverse_selection import AdverseSelectionDetector, AdverseSelectionResponse
from src.risk.kelly import KellyCalculator
from src.risk.correlation import CorrelationTracker, PortfolioRisk

logger = setup_logging()


class RiskStatus(Enum):
    """Risk check result."""
    OK = "OK"           # All clear
    WARN = "WARN"       # Warning, but continue
    STOP = "STOP"       # Kill switch - stop immediately


@dataclass
class RiskCheck:
    """Result of a risk check."""
    status: RiskStatus
    reason: str = ""
    details: Dict = field(default_factory=dict)


@dataclass
class RiskEvent:
    """A logged risk event for later analysis."""
    timestamp: float
    status: str
    reason: str
    details: Dict
    enforced: bool


class RiskManager:
    """
    Manages trading risk.

    Usage:
        risk = RiskManager()

        # In trading loop:
        check = risk.check()
        if check.status == RiskStatus.STOP:
            cancel_all_orders()
            stop_trading()

        # After a trade:
        risk.record_trade(token_id, side, price, size)

        # After an error:
        risk.record_error("Connection failed")

        # Manual kill switch:
        risk.kill_switch("Manual stop requested")

        # Get logged risk events for analysis:
        events = risk.get_risk_events()
    """

    def __init__(
        self,
        max_daily_loss: Decimal = RISK_MAX_DAILY_LOSS,
        max_position: Decimal = RISK_MAX_POSITION,
        max_total_exposure: Decimal = RISK_MAX_TOTAL_EXPOSURE,
        error_cooldown: int = RISK_ERROR_COOLDOWN,
        max_errors_per_minute: int = RISK_MAX_ERRORS_PER_MINUTE,
        enforce: bool = RISK_ENFORCE,
    ):
        self.max_daily_loss = max_daily_loss
        self.max_position = max_position
        self.max_total_exposure = max_total_exposure
        self.error_cooldown = error_cooldown
        self.max_errors_per_minute = max_errors_per_minute
        self.enforce = enforce

        # State
        self._killed = False
        self._kill_reason = ""
        self._start_time = time.time()
        self._errors: deque = deque(maxlen=100)  # Recent errors with timestamps
        self._cooldown_until: float = 0

        # P&L tracking (simple: track entry prices)
        self._trades: List[Dict] = []
        self._daily_pnl = Decimal("0")

        # Risk event log for data gathering
        self._risk_events: List[RiskEvent] = []

        # Volatility-adjusted limits
        self._volatility_multiplier: float = 1.0  # 1.0 = normal, <1 = high vol (reduce limits)
        self._vol_adjusted_position: Decimal = max_position

        # Unrealized P&L tracking
        self._entry_prices: Dict[str, Decimal] = {}  # token_id -> avg entry price
        self._unrealized_pnl: Decimal = Decimal("0")

        # === Phase 3: Risk-Adjusted Returns ===
        # Dynamic position limits
        self._dynamic_limits = DynamicLimitManager(
            base_limit=max_position,
            max_daily_loss=max_daily_loss,
            min_limit=max_position * DYNAMIC_LIMIT_FLOOR,
            max_limit=max_position * DYNAMIC_LIMIT_CEILING,
        )

        # Adverse selection detection
        self._adverse_detector = AdverseSelectionDetector(
            lookback_window=ADVERSE_LOOKBACK_SECONDS
        )
        self._adverse_detector.TOXIC_THRESHOLD = ADVERSE_TOXIC_THRESHOLD

        # Kelly criterion sizing
        self._kelly = KellyCalculator(
            fraction=KELLY_FRACTION,
            max_position_pct=KELLY_MAX_POSITION,
        )

        # Correlation tracking
        self._correlation_tracker = CorrelationTracker()
        self._portfolio_risk = PortfolioRisk(
            max_correlated_exposure=MAX_CORRELATED_EXPOSURE,
            correlation_threshold=CORRELATION_THRESHOLD,
        )

        logger.info(f"RiskManager initialized: enforce={enforce}")

    def check(self, token_ids: Optional[List[str]] = None) -> RiskCheck:
        """
        Run all risk checks.

        Args:
            token_ids: Tokens to check positions for (optional)

        Returns:
            RiskCheck with status and reason

        Note:
            If enforce=False, logs STOP/WARN events but returns OK to continue trading.
            This allows maximum data collection in dry-run mode.
        """
        # Always check kill switch (even in non-enforce mode, manual kills apply)
        if self._killed:
            return RiskCheck(RiskStatus.STOP, f"Kill switch: {self._kill_reason}")

        # Run all checks
        check = self._run_checks(token_ids)

        # If not enforcing and check failed, log but return OK
        if check.status != RiskStatus.OK and not self.enforce:
            self._log_risk_event(check, enforced=False)
            logger.warning(
                f"[DATA MODE] Risk event (not enforced): "
                f"{check.status.value} - {check.reason}"
            )
            return RiskCheck(RiskStatus.OK)

        # If enforcing and check failed, log and return the check
        if check.status != RiskStatus.OK:
            self._log_risk_event(check, enforced=True)

        return check

    def _run_checks(self, token_ids: Optional[List[str]] = None) -> RiskCheck:
        """Run all risk checks and return the result."""
        # Check cooldown
        if time.time() < self._cooldown_until:
            remaining = int(self._cooldown_until - time.time())
            return RiskCheck(RiskStatus.STOP, f"In cooldown for {remaining}s")

        # Check error rate
        check = self._check_error_rate()
        if check.status == RiskStatus.STOP:
            return check

        # Check daily P&L
        pnl_check = self._check_daily_pnl()
        if pnl_check.status == RiskStatus.STOP:
            return pnl_check

        # Check positions
        pos_check = RiskCheck(RiskStatus.OK)
        if token_ids:
            pos_check = self._check_positions(token_ids)
            if pos_check.status == RiskStatus.STOP:
                return pos_check

        # Return worst of P&L and position checks
        # Priority: STOP > WARN > OK
        if pnl_check.status == RiskStatus.WARN or pos_check.status == RiskStatus.WARN:
            # Return the WARN check (prefer P&L warning if both)
            if pnl_check.status == RiskStatus.WARN:
                return pnl_check
            return pos_check

        return RiskCheck(RiskStatus.OK)

    def _log_risk_event(self, check: RiskCheck, enforced: bool):
        """Log a risk event for later analysis."""
        event = RiskEvent(
            timestamp=time.time(),
            status=check.status.value,
            reason=check.reason,
            details=check.details,
            enforced=enforced
        )
        self._risk_events.append(event)

    def _check_error_rate(self) -> RiskCheck:
        """Check if too many errors recently."""
        now = time.time()
        minute_ago = now - 60

        recent_errors = sum(1 for ts, _ in self._errors if ts > minute_ago)

        if recent_errors >= self.max_errors_per_minute:
            self._cooldown_until = now + self.error_cooldown
            return RiskCheck(
                RiskStatus.STOP,
                f"Too many errors ({recent_errors}/min) - cooling down",
                {"error_count": recent_errors}
            )

        return RiskCheck(RiskStatus.OK)

    def _check_daily_pnl(self) -> RiskCheck:
        """Check if daily loss limit exceeded."""
        if self._daily_pnl < -self.max_daily_loss:
            if self.enforce:
                self._killed = True
                self._kill_reason = f"Daily loss limit exceeded: {self._daily_pnl}"
            return RiskCheck(
                RiskStatus.STOP,
                f"Daily loss limit exceeded: {self._daily_pnl}",
                {"daily_pnl": float(self._daily_pnl)}
            )

        # Warning at 80% of limit
        warn_threshold = -self.max_daily_loss * Decimal("0.8")
        if self._daily_pnl < warn_threshold:
            return RiskCheck(
                RiskStatus.WARN,
                f"Approaching daily loss limit: {self._daily_pnl}",
                {"daily_pnl": float(self._daily_pnl)}
            )

        return RiskCheck(RiskStatus.OK)

    def _check_positions(self, token_ids: List[str]) -> RiskCheck:
        """Check projected position limits, including outstanding live orders."""
        total_exposure = Decimal("0")
        dynamic_limit = self.get_dynamic_limit()
        effective_limit = min(self._vol_adjusted_position, dynamic_limit)

        for token_id in token_ids:
            position = get_position(token_id)
            open_orders = get_open_orders(token_id, raise_on_error=True)
            pending_buy = sum(
                order.remaining
                for order in open_orders
                if order.is_live and order.side == OrderSide.BUY
            )
            pending_sell = sum(
                order.remaining
                for order in open_orders
                if order.is_live and order.side == OrderSide.SELL
            )

            projected_long = position + pending_buy
            projected_short = position - pending_sell
            worst_abs_exposure = max(abs(projected_long), abs(projected_short))
            total_exposure += worst_abs_exposure

            if worst_abs_exposure > effective_limit:
                return RiskCheck(
                    RiskStatus.STOP,
                    f"Position limit exceeded for {token_id[:16]}: {worst_abs_exposure} > {effective_limit}",
                    {
                        "token_id": token_id,
                        "position": float(position),
                        "pending_buy": float(pending_buy),
                        "pending_sell": float(pending_sell),
                        "projected_long": float(projected_long),
                        "projected_short": float(projected_short),
                        "limit": float(effective_limit),
                        "dynamic_limit": float(dynamic_limit),
                        "vol_adjusted_limit": float(self._vol_adjusted_position),
                        "vol_adjusted": self._volatility_multiplier < 1.0,
                    }
                )

        if total_exposure > self.max_total_exposure:
            return RiskCheck(
                RiskStatus.STOP,
                f"Total exposure {total_exposure} exceeds limit {self.max_total_exposure}",
                {
                    "total_exposure": float(total_exposure),
                    "max_total_exposure": float(self.max_total_exposure),
                }
            )

        return RiskCheck(RiskStatus.OK)

    def record_trade(
        self,
        token_id: str,
        side: str,
        price: Decimal,
        size: Decimal,
        realized_pnl: Optional[Decimal] = None,
        fee: Decimal = Decimal("0")
    ):
        """
        Record a trade for P&L tracking.

        Args:
            token_id: Token traded
            side: BUY or SELL
            price: Execution price
            size: Trade size
            realized_pnl: Pre-calculated P&L (if available)
            fee: Transaction fee (subtracted from P&L)
        """
        self._trades.append({
            "time": time.time(),
            "token_id": token_id,
            "side": side,
            "price": float(price),
            "size": float(size),
            "fee": float(fee),
            "pnl": float(realized_pnl) if realized_pnl else 0,
        })

        # Feed to adverse selection detector
        self._adverse_detector.record_fill(price, side, size)

        # Feed to dynamic limits
        if realized_pnl is not None:
            self._dynamic_limits.record_pnl(realized_pnl)

        if realized_pnl is not None:
            # Subtract fee from realized P&L
            net_pnl = realized_pnl - fee
            self._daily_pnl += net_pnl
            logger.info(f"P&L update: {net_pnl:+.2f} (gross: {realized_pnl:+.2f}, fee: {fee:.2f}, daily: {self._daily_pnl:+.2f})")
        elif fee > 0:
            # If no realized_pnl but we have a fee, just deduct fee
            self._daily_pnl -= fee
            logger.info(f"Fee deducted: {fee:.2f} (daily: {self._daily_pnl:+.2f})")

    def record_error(self, error: str):
        """Record an error for rate limiting."""
        self._errors.append((time.time(), error))
        logger.warning(f"Error recorded: {error}")

    def kill_switch(
        self,
        reason: str = "Manual",
        *,
        token_id: Optional[str] = None,
    ):
        """
        Trigger kill switch - immediate stop.

        Note: Kill switch is ALWAYS enforced, even in data-gathering mode.
        This is for manual stops or truly critical failures.
        """
        self._killed = True
        self._kill_reason = reason
        logger.critical(f"KILL SWITCH: {reason}")
        try:
            if token_id is None:
                cancel_all_orders(verify=True, raise_on_failure=True)
            else:
                cancel_all_orders(
                    token_id,
                    verify=True,
                    raise_on_failure=True,
                )
        except Exception as e:
            self.record_error(f"Kill switch cancel verification failed: {e}")
            logger.critical(f"KILL SWITCH teardown incomplete: {e}")
            raise

    def reset_kill_switch(self):
        """Reset kill switch (use with caution)."""
        self._killed = False
        self._kill_reason = ""
        logger.info("Kill switch reset")

    def reset_daily_pnl(self):
        """Reset daily P&L (call at start of day)."""
        self._daily_pnl = Decimal("0")
        self._trades.clear()
        logger.info("Daily P&L reset")

    @property
    def is_killed(self) -> bool:
        """Check if kill switch is active."""
        return self._killed

    @property
    def daily_pnl(self) -> Decimal:
        """Get current daily P&L."""
        return self._daily_pnl

    def get_risk_events(self) -> List[RiskEvent]:
        """Get all logged risk events for analysis."""
        return self._risk_events.copy()

    def get_risk_event_summary(self) -> Dict:
        """Get summary of risk events."""
        if not self._risk_events:
            return {"total_events": 0}

        stop_events = [e for e in self._risk_events if e.status == "STOP"]
        warn_events = [e for e in self._risk_events if e.status == "WARN"]
        enforced = [e for e in self._risk_events if e.enforced]

        return {
            "total_events": len(self._risk_events),
            "stop_events": len(stop_events),
            "warn_events": len(warn_events),
            "enforced_events": len(enforced),
            "non_enforced_events": len(self._risk_events) - len(enforced),
        }

    def set_volatility_multiplier(self, multiplier: float):
        """
        Adjust position limits based on volatility.

        In high volatility, reduce position limits to manage risk.

        Args:
            multiplier: Spread multiplier from volatility tracker.
                       1.0 = normal vol -> 100% position limit
                       1.5 = high vol -> 70% position limit
                       2.0 = extreme vol -> 50% position limit
        """
        # Inverse relationship: higher vol multiplier = lower position limit
        # Maps: 0.7->1.0 (calm), 1.0->1.0 (normal), 1.5->0.7, 2.0->0.5
        if multiplier <= 1.0:
            limit_mult = 1.0
        else:
            # Linear reduction: 1.5 -> 0.7, 2.0 -> 0.5
            limit_mult = max(0.5, 1.0 - (multiplier - 1.0) * 0.5)

        self._volatility_multiplier = limit_mult
        self._vol_adjusted_position = self.max_position * Decimal(str(limit_mult))

        if limit_mult < 1.0:
            logger.info(
                f"Vol-adjusted position limit: {self._vol_adjusted_position:.0f} "
                f"({limit_mult:.0%} of {self.max_position})"
            )

    def get_vol_adjusted_position_limit(self) -> Decimal:
        """Get current volatility-adjusted position limit."""
        return self._vol_adjusted_position

    def update_unrealized_pnl(
        self,
        token_id: str,
        position: Decimal,
        current_price: Decimal,
        entry_price: Optional[Decimal] = None,
    ):
        """
        Update unrealized P&L for a position.

        Args:
            token_id: Token with the position
            position: Current position size (positive=long, negative=short)
            current_price: Current mid price
            entry_price: Average entry price (stored if provided)
        """
        if entry_price is not None:
            self._entry_prices[token_id] = entry_price

        stored_entry = self._entry_prices.get(token_id)
        if stored_entry is None or position == 0:
            self._unrealized_pnl = Decimal("0")
            return

        # Unrealized P&L = position * (current - entry)
        # Long: profit if current > entry
        # Short: profit if current < entry
        self._unrealized_pnl = position * (current_price - stored_entry)

    @property
    def unrealized_pnl(self) -> Decimal:
        """Get current unrealized P&L."""
        return self._unrealized_pnl

    @property
    def total_pnl(self) -> Decimal:
        """Get total P&L (realized + unrealized)."""
        return self._daily_pnl + self._unrealized_pnl

    # === Phase 3: Risk-Adjusted Returns Methods ===

    def get_dynamic_limit(self) -> Decimal:
        """Get current dynamic position limit."""
        return self._dynamic_limits.get_limit()

    def update_market_conditions(self, conditions: MarketConditions):
        """Update market conditions for dynamic limits."""
        self._dynamic_limits.set_conditions(conditions)

    def get_adverse_selection_response(self) -> AdverseSelectionResponse:
        """Get recommended response to adverse selection."""
        return self._adverse_detector.get_response()

    def record_price_after_fill(self, fill_id: int, price_after: Decimal):
        """Record price movement after a fill for toxicity analysis."""
        self._adverse_detector.record_price_after(fill_id, price_after)

    def get_toxicity(self, side: Optional[str] = None) -> float:
        """Get current fill toxicity score (0-1)."""
        return self._adverse_detector.get_toxicity(side)

    def get_kelly_size(
        self,
        win_rate: float,
        win_loss_ratio: float,
        price: Decimal,
        bankroll: Optional[Decimal] = None,
    ) -> Decimal:
        """Get Kelly-optimal position size."""
        if bankroll:
            self._kelly.set_bankroll(bankroll)
        return self._kelly.get_position_size(win_rate, win_loss_ratio, price)

    def get_kelly_from_history(self) -> float:
        """Calculate Kelly fraction from trade history."""
        return self._kelly.calculate_from_trades(self._trades)

    def record_market_price(self, market_id: str, price: float):
        """Record price for correlation tracking."""
        self._correlation_tracker.record_price(market_id, price)

    def can_add_correlated_position(
        self,
        market: str,
        size: Decimal,
        existing_positions: Dict[str, Decimal],
    ) -> bool:
        """Check if position can be added within correlation limits."""
        # Update correlations from tracker
        for entry in self._correlation_tracker.get_all_correlations():
            self._portfolio_risk.set_correlation(
                entry.market_a, entry.market_b, entry.correlation
            )
        return self._portfolio_risk.can_add_position(market, size, existing_positions)

    def get_portfolio_beta(self, positions: Dict[str, Decimal]) -> float:
        """Get portfolio beta based on correlations."""
        return self._portfolio_risk.calculate_portfolio_beta(positions)

    def get_status(self) -> Dict:
        """Get current risk status summary."""
        now = time.time()
        minute_ago = now - 60
        recent_errors = sum(1 for ts, _ in self._errors if ts > minute_ago)

        # Get adverse selection response for status
        adverse_response = self._adverse_detector.get_response()

        return {
            "mode": "ENFORCE" if self.enforce else "DATA_GATHER",
            "killed": self._killed,
            "kill_reason": self._kill_reason,
            "daily_pnl": float(self._daily_pnl),
            "unrealized_pnl": float(self._unrealized_pnl),
            "total_pnl": float(self._daily_pnl + self._unrealized_pnl),
            "max_daily_loss": float(self.max_daily_loss),
            "pnl_percent_of_limit": float(abs(self._daily_pnl) / self.max_daily_loss * 100) if self.max_daily_loss else 0,
            "vol_adjusted_position_limit": float(self._vol_adjusted_position),
            "volatility_multiplier": self._volatility_multiplier,
            "errors_last_minute": recent_errors,
            "in_cooldown": time.time() < self._cooldown_until,
            "cooldown_remaining": max(0, int(self._cooldown_until - time.time())),
            "uptime_seconds": int(now - self._start_time),
            "risk_events_logged": len(self._risk_events),
            # Phase 3: Risk-Adjusted Returns
            "dynamic_limit": float(self._dynamic_limits.get_limit()),
            "toxicity": self._adverse_detector.get_toxicity(),
            "toxicity_buy": self._adverse_detector.get_toxicity("BUY"),
            "toxicity_sell": self._adverse_detector.get_toxicity("SELL"),
            "adverse_widen_spread": adverse_response.widen_spread,
            "adverse_spread_mult": adverse_response.spread_multiplier,
            "adverse_size_mult": adverse_response.size_multiplier,
            "kelly_from_history": self.get_kelly_from_history(),
        }


# Global instance
_risk_manager: Optional[RiskManager] = None


def get_risk_manager() -> RiskManager:
    """Get global risk manager instance."""
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = RiskManager()
    return _risk_manager


def reset_risk_manager():
    """Reset global risk manager."""
    global _risk_manager
    _risk_manager = None
