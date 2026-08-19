import asyncio
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.feed.feed import FeedState, MarketFeed
from src.models import Order, OrderSide, OrderStatus
from src.risk.manager import RiskManager, RiskStatus
from src.trading import OrderCancellationError


def _make_live_order(order_id: str, side: OrderSide, size: str) -> Order:
    return Order(
        id=order_id,
        token_id="token",
        side=side,
        price=Decimal("0.50"),
        size=Decimal(size),
        filled=Decimal("0"),
        status=OrderStatus.LIVE,
        is_simulated=False,
    )


def test_feed_health_requires_fresh_order_book():
    feed = MarketFeed(stale_threshold=1.0)
    feed._data_store.register_token("token")
    feed._data_store.update_book(
        "token",
        [{'price': '0.49', 'size': '10'}],
        [{'price': '0.51', 'size': '10'}],
    )
    feed._data_store.record_message_received()
    feed._data_store.record_ws_message()
    feed._data_store.record_ws_book_message()
    feed._state = FeedState.RUNNING
    feed._data_source = "websocket"
    feed._ws._connected = True
    feed._ws._ws = object()

    assert feed.is_healthy is True

    feed._data_store.get("token").last_book_update -= 5.0
    feed._data_store.update_price("token", 0.50)
    feed._data_store.record_message_received()
    feed._data_store.record_ws_message()

    assert feed.is_healthy is False


def test_feed_connection_lost_resets_ws_timers():
    feed = MarketFeed()
    feed._data_store.record_ws_message()
    feed._data_store.record_ws_book_message()

    feed._handle_connection_lost()

    assert feed._data_store.seconds_since_ws_message() == float("inf")
    assert feed._data_store.seconds_since_ws_book_message() == float("inf")


@pytest.mark.asyncio
async def test_health_monitor_retries_ws_and_waits_for_ws_book_before_cutover():
    class DummyWS:
        def __init__(self):
            self.is_connected = False
            self.is_reconnecting = False
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1
            self.is_connected = True
            return True

    class DummyREST:
        def __init__(self):
            self.is_running = True
            self.stop_calls = 0

        async def stop(self):
            self.stop_calls += 1
            self.is_running = False

        async def start(self, token_ids):
            self.is_running = True

    feed = MarketFeed(stale_threshold=1.0, rest_recovery_delay=0.0)
    feed._state = FeedState.RUNNING
    feed._tokens = ["token"]
    feed._data_source = "rest"
    feed._ws = DummyWS()
    feed._rest = DummyREST()
    feed._last_ws_recovery_attempt = 0.0
    feed._data_store.register_token("token")
    feed._data_store.update_book(
        "token",
        [{'price': '0.49', 'size': '10'}],
        [{'price': '0.51', 'size': '10'}],
    )

    await feed._monitor_health_once()

    assert feed._ws.connect_calls == 1
    assert feed._rest.is_running is True
    assert feed.data_source == "rest"

    feed._data_store.record_ws_message()
    feed._data_store.record_ws_book_message()
    await feed._monitor_health_once()

    assert feed._rest.stop_calls == 1
    assert feed._rest.is_running is False
    assert feed.data_source == "websocket"


@pytest.mark.asyncio
async def test_parity_overpriced_cancels_existing_quotes():
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="yes-token", complement_token_id="no-token")
    mm.feed = MagicMock()
    mm.feed.is_healthy = True
    mm.feed.get_midpoint.side_effect = lambda token_id: 0.60 if token_id == "yes-token" else 0.45
    mm.arb_detector.scan_all = MagicMock(return_value=[])
    mm.trade_logger.log_event = MagicMock()
    mm._cancel_all_quotes = AsyncMock(return_value=True)
    mm.risk.check = MagicMock(return_value=SimpleNamespace(status=RiskStatus.OK))

    await mm._loop_iteration()

    mm._cancel_all_quotes.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_generic_cancel_failure_triggers_kill_switch_and_stop():
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="token")
    mm.risk.record_error = MagicMock()
    mm.risk.kill_switch = MagicMock()
    mm.stop = MagicMock()

    with patch(
        "src.strategy.market_maker.cancel_all_orders",
        side_effect=RuntimeError("cancel transport exploded"),
    ):
        await mm._handle_disconnect_teardown()

    mm.risk.record_error.assert_called()
    mm.risk.kill_switch.assert_called_once_with(
        "Disconnect teardown failed: cancel transport exploded",
        token_id="token",
    )
    mm.stop.assert_called_once()


@pytest.mark.asyncio
async def test_disconnect_order_cancellation_error_stops_before_kill_switch_raises():
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="token")
    mm.risk.record_error = MagicMock()
    mm.risk.kill_switch = MagicMock(
        side_effect=OrderCancellationError("kill switch teardown failed")
    )
    mm.stop = MagicMock()

    with patch(
        "src.strategy.market_maker.cancel_all_orders",
        side_effect=OrderCancellationError("residual live orders remain"),
    ):
        await mm._handle_disconnect_teardown()

    mm.stop.assert_called_once()
    mm.risk.record_error.assert_called()
    mm.risk.kill_switch.assert_called_once_with(
        "Disconnect teardown incomplete: residual live orders remain",
        token_id="token",
    )


@pytest.mark.asyncio
async def test_disconnect_generic_error_stops_before_kill_switch_runtime_error():
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="token")
    mm.risk.record_error = MagicMock()
    mm.risk.kill_switch = MagicMock(
        side_effect=RuntimeError("kill switch runtime failure")
    )
    mm.stop = MagicMock()

    with patch(
        "src.strategy.market_maker.cancel_all_orders",
        side_effect=RuntimeError("cancel transport exploded"),
    ):
        await mm._handle_disconnect_teardown()

    mm.stop.assert_called_once()
    mm.risk.record_error.assert_called()
    mm.risk.kill_switch.assert_called_once_with(
        "Disconnect teardown failed: cancel transport exploded",
        token_id="token",
    )


def test_risk_manager_stops_on_pending_order_exposure():
    manager = RiskManager(max_position=Decimal("100"), enforce=True)

    with patch("src.risk.manager.get_position", return_value=Decimal("80")):
        with patch(
            "src.risk.manager.get_open_orders",
            return_value=[_make_live_order("bid", OrderSide.BUY, "30")],
        ):
            check = manager.check(["token"])

    assert check.status == RiskStatus.STOP
    assert check.details["pending_buy"] == 30.0


def test_risk_manager_uses_dynamic_limit_for_hard_stop():
    manager = RiskManager(max_position=Decimal("100"), enforce=True)
    manager._dynamic_limits.get_limit = MagicMock(return_value=Decimal("60"))

    with patch("src.risk.manager.get_position", return_value=Decimal("70")):
        with patch("src.risk.manager.get_open_orders", return_value=[]):
            check = manager.check(["token"])

    assert check.status == RiskStatus.STOP
    assert check.details["limit"] == 60.0


def test_risk_manager_total_exposure_includes_pending_orders():
    manager = RiskManager(
        max_position=Decimal("100"),
        max_total_exposure=Decimal("100"),
        enforce=True,
    )
    positions = {"t1": Decimal("40"), "t2": Decimal("30")}
    orders = {
        "t1": [_make_live_order("bid-1", OrderSide.BUY, "30")],
        "t2": [_make_live_order("bid-2", OrderSide.BUY, "20")],
    }

    with patch("src.risk.manager.get_position", side_effect=lambda token_id: positions[token_id]):
        with patch(
            "src.risk.manager.get_open_orders",
            side_effect=lambda token_id, raise_on_error=False: orders[token_id],
        ):
            check = manager.check(["t1", "t2"])

    assert check.status == RiskStatus.STOP
    assert check.details["total_exposure"] == 120.0


def test_kill_switch_uses_verified_cancel_all_orders():
    manager = RiskManager(enforce=True)

    with patch("src.risk.manager.cancel_all_orders", return_value=2) as mock_cancel:
        manager.kill_switch("manual")

    mock_cancel.assert_called_once_with(verify=True, raise_on_failure=True)
    assert manager.is_killed is True
    assert manager._kill_reason == "manual"


def test_kill_switch_surfaces_cancel_verification_failure():
    manager = RiskManager(enforce=True)

    with patch(
        "src.risk.manager.cancel_all_orders",
        side_effect=OrderCancellationError("residual live orders remain"),
    ):
        with pytest.raises(OrderCancellationError, match="residual live orders remain"):
            manager.kill_switch("manual")

    assert manager.is_killed is True
    assert manager._kill_reason == "manual"


def test_run_tui_uses_config_defaults():
    import run_tui

    with patch.object(sys, "argv", ["run_tui.py", "--token", "token-1"]):
        with patch("run_tui.DRY_RUN", True):
            with patch("run_tui.validate_config"):
                with patch("run_tui.run_with_tui", new=AsyncMock()) as mock_run:
                    with patch("run_tui.cleanup_orphaned_orders") as mock_cleanup:
                        run_tui.main()

    mock_cleanup.assert_not_called()
    kwargs = mock_run.await_args.kwargs
    assert kwargs["spread"] == float(run_tui.SPREAD_BASE)
    assert kwargs["size"] == float(run_tui.MM_SIZE)
    assert kwargs["position_limit"] == float(run_tui.MM_POSITION_LIMIT)


def test_run_tui_live_mode_requires_credentials():
    import run_tui

    with patch.object(sys, "argv", ["run_tui.py", "--token", "token-1"]):
        with patch("run_tui.DRY_RUN", False):
            with patch("run_tui.validate_config"):
                with patch("run_tui.has_credentials", return_value=False):
                    with patch("builtins.input", side_effect=AssertionError("should not prompt")):
                        with pytest.raises(SystemExit):
                            run_tui.main()


def test_run_tui_live_mode_cleans_orphaned_orders_before_start():
    import run_tui

    with patch.object(sys, "argv", ["run_tui.py", "--token", "token-1"]):
        with patch("run_tui.DRY_RUN", False):
            with patch("run_tui.validate_config"):
                with patch("run_tui.has_credentials", return_value=True):
                    with patch("builtins.input", return_value="y"):
                        with patch("run_tui.cleanup_orphaned_orders", return_value=0) as mock_cleanup:
                            with patch("run_tui.run_with_tui", new=AsyncMock()) as mock_run:
                                run_tui.main()

    mock_cleanup.assert_called_once_with("token-1")
    assert mock_run.await_count == 1


def test_run_tui_aborts_when_orphan_cleanup_cannot_verify_state():
    import run_tui

    with patch.object(sys, "argv", ["run_tui.py", "--token", "token-1"]):
        with patch("run_tui.DRY_RUN", False):
            with patch("run_tui.validate_config"):
                with patch("run_tui.has_credentials", return_value=True):
                    with patch("builtins.input", return_value="y"):
                        with patch("run_tui.cleanup_orphaned_orders", return_value=-1):
                            with patch("run_tui.run_with_tui", new=AsyncMock()) as mock_run:
                                with pytest.raises(SystemExit):
                                    run_tui.main()

    assert mock_run.await_count == 0


def test_shutdown_records_when_verified_cancel_leaves_residual_orders():
    from src.strategy.market_maker import SmartMarketMaker

    mm = SmartMarketMaker(token_id="token")
    mm.feed = MagicMock()
    mm.feed.stop = AsyncMock()
    mm.risk.record_error = MagicMock()

    with patch(
        "src.strategy.market_maker.cancel_all_orders",
        side_effect=OrderCancellationError("residual live orders remain"),
    ):
        shutdown_error = asyncio.run(mm._shutdown())

    mm.feed.stop.assert_awaited_once()
    assert isinstance(shutdown_error, OrderCancellationError)
    mm.risk.record_error.assert_called_once_with(
        "Shutdown cancellation verification failed: residual live orders remain"
    )
