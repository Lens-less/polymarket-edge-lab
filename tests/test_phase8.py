"""
Phase 8 Tests - Risk management.

Run: pytest tests/test_phase8.py -v
"""

import pytest
import time
from decimal import Decimal


class TestRiskStatus:
    """Test risk status checks."""

    def test_ok_by_default(self):
        from src.risk.manager import RiskManager, RiskStatus

        risk = RiskManager()
        check = risk.check()

        assert check.status == RiskStatus.OK
        print("✓ Default status is OK")

    def test_kill_switch(self):
        from src.risk.manager import RiskManager, RiskStatus

        risk = RiskManager()

        risk.kill_switch("Test stop")
        check = risk.check()

        assert check.status == RiskStatus.STOP
        assert "Test stop" in check.reason
        assert risk.is_killed

        print("✓ Kill switch works")

    def test_reset_kill_switch(self):
        from src.risk.manager import RiskManager, RiskStatus

        risk = RiskManager()

        risk.kill_switch("Test")
        assert risk.is_killed

        risk.reset_kill_switch()
        assert not risk.is_killed

        check = risk.check()
        assert check.status == RiskStatus.OK

        print("✓ Kill switch reset works")


class TestEnforceMode:
    """Test enforce vs data-gathering mode."""

    def test_enforce_true_stops(self):
        from src.risk.manager import RiskManager, RiskStatus

        risk = RiskManager(max_daily_loss=Decimal("50"), enforce=True)

        # Record a big loss
        risk.record_trade("t1", "SELL", Decimal("0.50"), Decimal("10"),
                         realized_pnl=Decimal("-60"))

        check = risk.check()

        assert check.status == RiskStatus.STOP
        print("✓ Enforce=True stops trading")

    def test_enforce_false_continues(self):
        from src.risk.manager import RiskManager, RiskStatus

        risk = RiskManager(max_daily_loss=Decimal("50"), enforce=False)

        # Record a big loss
        risk.record_trade("t1", "SELL", Decimal("0.50"), Decimal("10"),
                         realized_pnl=Decimal("-60"))

        check = risk.check()

        # Should return OK even though limit exceeded
        assert check.status == RiskStatus.OK

        # But event should be logged
        events = risk.get_risk_events()
        assert len(events) == 1
        assert events[0].status == "STOP"
        assert events[0].enforced == False

        print("✓ Enforce=False continues trading but logs event")

    def test_kill_switch_always_enforced(self):
        from src.risk.manager import RiskManager, RiskStatus

        # Even with enforce=False, manual kill switch works
        risk = RiskManager(enforce=False)

        risk.kill_switch("Emergency")
        check = risk.check()

        assert check.status == RiskStatus.STOP
        print("✓ Kill switch always enforced")


class TestRiskEventLogging:
    """Test risk event logging for data gathering."""

    def test_events_logged(self):
        from src.risk.manager import RiskManager

        risk = RiskManager(
            max_daily_loss=Decimal("50"),
            max_position=Decimal("30"),
            enforce=False
        )

        # Trigger multiple events
        risk.record_trade("t1", "BUY", Decimal("0.50"), Decimal("10"),
                         realized_pnl=Decimal("-45"))  # Warning
        risk.check(["t1"])

        risk.record_trade("t1", "BUY", Decimal("0.50"), Decimal("10"),
                         realized_pnl=Decimal("-20"))  # Stop (exceeded)
        risk.check(["t1"])

        events = risk.get_risk_events()
        assert len(events) >= 2

        summary = risk.get_risk_event_summary()
        assert summary["total_events"] >= 2
        assert summary["non_enforced_events"] >= 2

        print(f"✓ Risk events logged: {summary}")

    def test_event_details_captured(self):
        from src.risk.manager import RiskManager

        risk = RiskManager(max_daily_loss=Decimal("50"), enforce=False)

        risk.record_trade("t1", "SELL", Decimal("0.50"), Decimal("10"),
                         realized_pnl=Decimal("-60"))
        risk.check()

        events = risk.get_risk_events()
        event = events[0]

        assert event.status == "STOP"
        assert "loss" in event.reason.lower()
        assert "daily_pnl" in event.details
        assert event.enforced == False
        assert event.timestamp > 0

        print("✓ Event details captured correctly")


class TestDailyLoss:
    """Test daily loss limit."""

    def test_loss_limit_stop(self):
        from src.risk.manager import RiskManager, RiskStatus

        risk = RiskManager(max_daily_loss=Decimal("50"), enforce=True)

        # Record a big loss
        risk.record_trade("t1", "SELL", Decimal("0.50"), Decimal("10"),
                         realized_pnl=Decimal("-60"))

        check = risk.check()

        assert check.status == RiskStatus.STOP
        assert "loss limit" in check.reason.lower()

        print("✓ Daily loss limit triggers stop")

    def test_loss_warning(self):
        from src.risk.manager import RiskManager, RiskStatus

        risk = RiskManager(max_daily_loss=Decimal("50"), enforce=True)

        # Record loss at 80% of limit
        risk.record_trade("t1", "SELL", Decimal("0.50"), Decimal("10"),
                         realized_pnl=Decimal("-42"))

        check = risk.check()

        assert check.status == RiskStatus.WARN

        print("✓ Approaching loss limit triggers warning")

    def test_reset_daily_pnl(self):
        from src.risk.manager import RiskManager, RiskStatus

        risk = RiskManager(max_daily_loss=Decimal("50"))

        risk.record_trade("t1", "SELL", Decimal("0.50"), Decimal("10"),
                         realized_pnl=Decimal("-30"))

        assert risk.daily_pnl == Decimal("-30")

        risk.reset_daily_pnl()

        assert risk.daily_pnl == Decimal("0")

        print("✓ Daily P&L reset works")


class TestErrorRate:
    """Test error rate limiting."""

    def test_error_cooldown(self):
        from src.risk.manager import RiskManager, RiskStatus

        risk = RiskManager(max_errors_per_minute=3, error_cooldown=5, enforce=True)

        # Record enough errors
        for i in range(5):
            risk.record_error(f"Error {i}")

        check = risk.check()

        assert check.status == RiskStatus.STOP
        assert "error" in check.reason.lower() or "cooldown" in check.reason.lower()

        print("✓ Error rate triggers cooldown")


class TestPositionLimits:
    """Test position limit checks."""

    def test_position_stop(self):
        from src.risk.manager import RiskManager, RiskStatus
        from src.simulator import get_simulator, reset_simulator
        from src.models import OrderSide
        from src.config import DRY_RUN

        if not DRY_RUN:
            pytest.skip("Requires DRY_RUN=true")

        reset_simulator()
        sim = get_simulator()

        # Build up a large position
        order = sim.create_order("t1", OrderSide.BUY, Decimal("0.50"), Decimal("150"))
        sim.check_fills("t1", Decimal("0.45"), Decimal("0.50"))

        risk = RiskManager(max_position=Decimal("100"), enforce=True)
        check = risk.check(["t1"])

        assert check.status == RiskStatus.STOP
        assert "position" in check.reason.lower()

        print("✓ Position limit triggers stop")


class TestGetStatus:
    """Test status reporting."""

    def test_get_status(self):
        from src.risk.manager import RiskManager

        risk = RiskManager(max_daily_loss=Decimal("100"), enforce=False)

        risk.record_trade("t1", "BUY", Decimal("0.50"), Decimal("10"),
                         realized_pnl=Decimal("-20"))
        risk.record_error("Test error")
        risk.check()  # Log an event

        status = risk.get_status()

        assert "mode" in status
        assert status["mode"] == "DATA_GATHER"
        assert "killed" in status
        assert "daily_pnl" in status
        assert "errors_last_minute" in status
        assert "risk_events_logged" in status

        assert status["daily_pnl"] == -20.0
        assert status["errors_last_minute"] >= 1

        print(f"✓ Status: {status}")


class TestGlobalInstance:
    """Test global risk manager."""

    def test_global_instance(self):
        from src.risk.manager import get_risk_manager, reset_risk_manager

        reset_risk_manager()

        rm1 = get_risk_manager()
        rm2 = get_risk_manager()

        assert rm1 is rm2  # Same instance

        reset_risk_manager()
        rm3 = get_risk_manager()

        assert rm3 is not rm1  # New instance after reset

        print("✓ Global instance works")


class TestIntegration:
    """Integration tests."""

    def test_data_gathering_workflow(self):
        """Test full data gathering workflow in non-enforce mode."""
        from src.risk.manager import RiskManager, RiskStatus
        from src.simulator import reset_simulator
        from src.config import DRY_RUN

        if not DRY_RUN:
            pytest.skip("Requires DRY_RUN=true")

        reset_simulator()

        # Data gathering mode - don't enforce limits
        risk = RiskManager(
            max_daily_loss=Decimal("50"),
            max_position=Decimal("50"),
            enforce=False
        )

        # Simulate a full trading session with big losses
        trades = [
            ("t1", "BUY", Decimal("10")),
            ("t1", "SELL", Decimal("-20")),  # loss
            ("t1", "BUY", Decimal("-15")),   # loss
            ("t1", "SELL", Decimal("-30")),  # loss - exceeds limit
            ("t1", "BUY", Decimal("-25")),   # more loss
        ]

        for token, side, pnl in trades:
            risk.record_trade(token, side, Decimal("0.50"), Decimal("10"),
                            realized_pnl=pnl)
            check = risk.check([token])
            # Should always return OK in data gather mode
            assert check.status == RiskStatus.OK

        # Total P&L should be tracked
        assert risk.daily_pnl == Decimal("-80")  # 10 - 20 - 15 - 30 - 25

        # Events should be logged
        events = risk.get_risk_events()
        assert len(events) > 0

        summary = risk.get_risk_event_summary()
        print(f"  Total events: {summary['total_events']}")
        print(f"  Stop events: {summary['stop_events']}")
        print(f"  Warn events: {summary['warn_events']}")
        print(f"  Final P&L: {risk.daily_pnl}")

        # All events should be non-enforced
        assert summary["enforced_events"] == 0

        print("✓ Data gathering workflow works")


def test_fee_tracking():
    """Verify fees are included in P&L."""
    from src.risk.manager import RiskManager
    from decimal import Decimal

    risk = RiskManager()

    # Record a trade with fee
    risk.record_trade(
        "t1", "BUY", Decimal("0.50"), Decimal("100"),
        realized_pnl=Decimal("10"),  # $10 gross profit
        fee=Decimal("0.50")  # $0.50 fee
    )

    # Net P&L should be 10 - 0.50 = 9.50
    assert risk.daily_pnl == Decimal("9.50"), f"Expected 9.50, got {risk.daily_pnl}"

    print("✓ Fee tracking works")
