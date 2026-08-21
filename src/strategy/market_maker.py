"""
Market maker for Polymarket.

SmartMarketMaker: Dynamic spread, inventory skewing, volatility-aware
"""

import asyncio
import signal
import time
import traceback
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Optional
from dataclasses import dataclass

# Safety constants
STALE_ORDER_THRESHOLD_SECONDS = 300  # 5 minutes
BALANCE_CHECK_INTERVAL = 60  # seconds
BALANCE_DROP_ALERT_PCT = Decimal("0.20")  # Alert if drops 20%
ORDER_TEARDOWN_TIMEOUT_SECONDS = 10.0
LIVE_ACCOUNT_CACHE_SECONDS = 1.0
LIVE_TRADE_POLL_SECONDS = 1.0
LIVE_TRADE_OVERLAP_SECONDS = 60
# How far the local clock is allowed to look ahead of Polymarket's clock
# before the live trade sync refuses to trust it. A fast local clock shifts
# `after` into the future and can silently skip real fills; a slow local
# clock only ever widens the query window, which is safe. Kept well under
# LIVE_TRADE_OVERLAP_SECONDS so a drift big enough to matter is caught
# before it could plausibly outrun the overlap.
LIVE_CLOCK_SKEW_TOLERANCE_SECONDS = 30
# Consecutive trade-sync rounds allowed to report zero new fills while a
# tracked open order's size_matched keeps growing before that is treated as
# a desynced feed rather than a coincidence.
LIVE_TRADE_SYNC_DESYNC_ROUNDS = 3

from src.config import (
    DRY_RUN,
    MM_SIZE,
    MM_REQUOTE_THRESHOLD,
    MM_POSITION_LIMIT,
    MM_LOOP_INTERVAL,
    SPREAD_BASE,
    SPREAD_MIN,
    SPREAD_MAX,
    INVENTORY_SKEW_MAX,
    TIMING_BASE_INTERVAL,
    TIMING_FAST_INTERVAL,
    TIMING_SLEEP_INTERVAL,
    TIMING_VOL_THRESHOLD,
    TIMING_INACTIVITY_THRESHOLD,
    TIMING_FAST_MODE_DURATION,
    MARKET_MIN_PRICE,
    MARKET_MAX_PRICE,
)
from src.models import Order, OrderSide, OrderStatus, Trade
from src.auth import PolymarketAdapterError
from src.trading import (
    place_order,
    cancel_order,
    cancel_all_orders,
    get_tick_size,
    round_to_tick,
    OrderCancellationError,
    OrderError,
)
from src.orders import get_open_orders, get_trades
from src.feed import MarketFeed
from src.pricing import get_order_book
from src.risk import RiskStatus, get_risk_manager
from src.simulator import get_simulator
from src.utils import setup_logging
from src.strategy.volatility import VolatilityTracker
from src.strategy.book_analyzer import BookAnalyzer
from src.strategy.inventory import InventoryManager, InventoryState
from src.strategy.parity import check_parity, ParityStatus
from src.strategy.timing import AdaptiveTimer
from src.risk.market_pnl import MarketPnLTracker
from src.telemetry.trade_logger import TradeLogger
from src.alpha import (
    ArbitrageDetector,
    PairTracker,
    FlowAnalyzer,
    EventTracker,
    TokenPair,
)
from src.config import (
    ARB_MIN_PROFIT_BPS,
    FLOW_WINDOW_SECONDS,
)
from src.edge_lab.compatibility import LiveExecutionBlocked

logger = setup_logging()


@dataclass
class SmartMMState:
    """State snapshot for SmartMarketMaker (for TUI display)."""
    # Spread
    base_spread: Decimal
    vol_multiplier: float
    inv_multiplier: float
    final_spread: Decimal

    # Volatility
    volatility_level: str
    realized_vol: float

    # Inventory
    inventory_pct: float
    inventory_level: str
    bid_skew: Decimal
    ask_skew: Decimal

    # Book
    imbalance_signal: str
    imbalance_adjustment: Decimal

    # P&L
    unrealized_pnl: Decimal
    vwap_entry: Optional[Decimal]


@dataclass(frozen=True)
class LiveAccountSnapshot:
    """One internally consistent, short-lived view of live account state."""

    captured_at: float
    balance: Decimal
    allowance: Decimal
    sellable: Decimal
    open_orders: tuple[Order, ...]


class SmartMarketMaker:
    """
    Adaptive market maker with dynamic spread and inventory management.

    Features:
    - Dynamic spread based on volatility
    - Gradual inventory skewing (not hard stops)
    - Order book imbalance awareness
    - Competitive quote positioning
    - Unrealized P&L tracking

    Usage:
        mm = SmartMarketMaker(token_id="abc123")
        await mm.run()
    """

    def __init__(
        self,
        token_id: str,
        base_spread: Decimal = SPREAD_BASE,
        min_spread: Decimal = SPREAD_MIN,
        max_spread: Decimal = SPREAD_MAX,
        size: Decimal = MM_SIZE,
        requote_threshold: Decimal = MM_REQUOTE_THRESHOLD,
        position_limit: Decimal = MM_POSITION_LIMIT,
        loop_interval: float = MM_LOOP_INTERVAL,
        skew_max: Decimal = INVENTORY_SKEW_MAX,
        complement_token_id: Optional[str] = None,
        market_end_date: Optional[datetime] = None,
    ):
        self.token_id = token_id
        self.base_spread = base_spread
        self.min_spread = min_spread
        self.max_spread = max_spread
        self.size = size
        self.requote_threshold = requote_threshold
        self.position_limit = position_limit
        self.loop_interval = loop_interval

        # Components
        self.volatility = VolatilityTracker(token_id)
        self.book_analyzer = BookAnalyzer()
        self.inventory = InventoryManager(
            token_id,
            position_limit=position_limit,
            skew_max=skew_max,
        )
        self.pnl_tracker = MarketPnLTracker()
        self.trade_logger = TradeLogger(log_file=f"logs/trades_{token_id[:8]}.jsonl")
        self.complement_token_id = complement_token_id

        # Adaptive timing
        self.timer = AdaptiveTimer(
            base_interval=TIMING_BASE_INTERVAL,
            fast_interval=TIMING_FAST_INTERVAL,
            sleep_interval=TIMING_SLEEP_INTERVAL,
            volatility_threshold=TIMING_VOL_THRESHOLD,
            inactivity_threshold=TIMING_INACTIVITY_THRESHOLD,
            fast_mode_duration=TIMING_FAST_MODE_DURATION,
        )

        # Alpha modules
        self.arb_detector = ArbitrageDetector(min_profit_bps=ARB_MIN_PROFIT_BPS)
        self.pair_tracker = PairTracker()
        self.flow_analyzer = FlowAnalyzer(
            token_id=token_id,
            window_seconds=FLOW_WINDOW_SECONDS,
        )
        self.event_tracker = EventTracker()

        # Register YES/NO pair for arbitrage detection
        if market_end_date:
            self.event_tracker.set_market_metadata(token_id, {
                'resolution_time': market_end_date.timestamp(),
            })
            hours_to_resolution = (market_end_date.timestamp() - datetime.now().timestamp()) / 3600
            logger.info(f"Event tracker configured: {hours_to_resolution:.1f} hours to resolution")

        # Register YES/NO pair for arbitrage detection
        if self.complement_token_id:
            pair = TokenPair(
                condition_id=f"pair-{token_id[:8]}",
                yes_token_id=token_id,
                no_token_id=self.complement_token_id,
                market_slug="",
            )
            self.arb_detector.register_pair(pair)
            self.pair_tracker._pairs[pair.condition_id] = pair
            logger.info(f"Registered arbitrage pair: {token_id[:8]} <-> {self.complement_token_id[:8]}")

        # State
        self.feed: Optional[MarketFeed] = None
        self.bid_order: Optional[Order] = None
        self.ask_order: Optional[Order] = None
        self._quote_bid_enabled = True
        self._quote_ask_enabled = True
        self.last_mid: Optional[Decimal] = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self.risk = get_risk_manager()
        self._loop_count = 0
        self._last_heartbeat = 0.0

        # Balance monitoring (safety)
        self._initial_balance: Optional[Decimal] = None
        self._last_balance_check: float = 0.0

        # Computed values (for TUI display)
        self._last_state: Optional[SmartMMState] = None
        self._seen_trade_ids: set[str] = set()
        self._disconnect_teardown_task: Optional[asyncio.Task[None]] = None
        self._shutdown_cancel_task: Optional[asyncio.Task[int]] = None
        self._live_account_snapshot: Optional[LiveAccountSnapshot] = None
        self._latest_inventory_state: Optional[InventoryState] = None
        self._last_live_trade_poll: float = 0.0
        self._trade_sync_watermark: Optional[int] = None
        self._seen_trade_timestamps: dict[str, int] = {}
        self._clock_skew_seconds: int = 0
        self._empty_trade_sync_rounds: int = 0
        self._last_known_open_order_filled: dict[str, Decimal] = {}

    async def run(self, install_signals: bool = True):
        """Main loop. Runs until stopped."""
        logger.info(f"Starting SMART market maker for {self.token_id[:16]}...")
        logger.info(f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
        logger.info(f"Base spread: {self.base_spread}, Size: {self.size}")

        loop = asyncio.get_event_loop()
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._handle_signal)

        primary_error: BaseException | None = None
        try:
            self.feed = MarketFeed()
            # Subscribe to both tokens if we have a complement (for arbitrage)
            tokens_to_watch = [self.token_id]
            if self.complement_token_id:
                tokens_to_watch.append(self.complement_token_id)
                logger.info("Subscribing to YES + NO tokens for arbitrage")
            await self.feed.start(tokens_to_watch)

            # SAFETY: Register callback to cancel orders on disconnect
            def on_ws_disconnect():
                if loop.is_closed():
                    logger.critical(
                        "[SAFETY] Disconnect callback arrived after the strategy loop closed"
                    )
                    return
                if (
                    self._disconnect_teardown_task is not None
                    and not self._disconnect_teardown_task.done()
                ):
                    logger.warning("[SAFETY] Disconnect teardown already in progress")
                    return
                self._disconnect_teardown_task = loop.create_task(
                    self._handle_disconnect_teardown()
                )

            self.feed.register_connection_lost_callback(on_ws_disconnect)

            await self._wait_for_data()

            # Register flow analyzer callback
            def flow_callback(price, size, side, is_taker):
                self.flow_analyzer.record_trade(price, size, side, is_taker)

            self.feed.register_flow_callback(self.token_id, flow_callback)

            if not DRY_RUN:
                self._prime_live_trade_state()

            self._running = True
            logger.info("Smart market maker running. Press Ctrl+C to stop.")

            while self._running and not self._shutdown_event.is_set():
                try:
                    await self._loop_iteration()
                except LiveExecutionBlocked as error:
                    self.risk.record_error(f"Live execution blocked: {error}")
                    logger.critical("[SAFETY] Live execution blocked: %s", error)
                    self.stop()
                    break
                except Exception as e:
                    self.risk.record_error(f"Loop error: {e}")
                    logger.error(f"Loop error: {e}")
                    traceback.print_exc()

                # Use adaptive interval instead of fixed
                interval = self.timer.get_interval()
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=interval
                    )
                except asyncio.TimeoutError:
                    pass

        except BaseException as error:
            primary_error = error
            raise
        finally:
            shutdown_error = await self._shutdown()
            if primary_error is None and shutdown_error is not None:
                raise shutdown_error

    def stop(self):
        """Signal the market maker to stop."""
        logger.info("Stop requested...")
        self._running = False
        self._shutdown_event.set()

    async def _handle_disconnect_teardown(self) -> None:
        """Cancel and reconcile orders without blocking the feed event loop."""
        logger.warning("[SAFETY] WebSocket disconnected - canceling all orders")
        self._invalidate_live_account_snapshot()
        try:
            cancelled = await asyncio.to_thread(
                cancel_all_orders,
                self.token_id,
                verify=True,
                raise_on_failure=True,
            )
            await asyncio.to_thread(self._sync_live_orders)
            if cancelled:
                logger.warning(
                    "[SAFETY] Disconnect teardown cancelled %s live order(s)",
                    cancelled,
                )
        except OrderCancellationError as error:
            reason = f"Disconnect teardown incomplete: {error}"
            self.risk.record_error(f"Disconnect teardown left live orders: {error}")
            self.stop()
            try:
                await asyncio.to_thread(
                    self.risk.kill_switch,
                    reason,
                    token_id=self.token_id,
                )
            except Exception as kill_error:
                logger.critical("[SAFETY] Kill-switch teardown also failed: %s", kill_error)
            logger.critical("[SAFETY] %s", error)
        except Exception as error:
            reason = f"Disconnect teardown failed: {error}"
            self.risk.record_error(reason)
            self.stop()
            try:
                await asyncio.to_thread(
                    self.risk.kill_switch,
                    reason,
                    token_id=self.token_id,
                )
            except Exception as kill_error:
                logger.critical("[SAFETY] Kill-switch teardown also failed: %s", kill_error)
            logger.error("[SAFETY] Failed to cancel orders on disconnect: %s", error)

    def get_state_for_tui(self) -> dict:
        """Get current state for TUI rendering."""
        pnl_stats = self.pnl_tracker.get_market_stats(self.token_id)
        return {
            'bid_order': self.bid_order,
            'ask_order': self.ask_order,
            'last_mid': self.last_mid,
            'running': self._running,
            'smart_state': self._last_state,
            'realized_pnl': pnl_stats.realized_pnl if pnl_stats else Decimal("0"),
            'trade_count': pnl_stats.trade_count if pnl_stats else 0,
            'win_rate': pnl_stats.win_rate if pnl_stats else 0.0,
        }

    def _handle_signal(self):
        """Handle shutdown signals."""
        self.stop()

    def _get_live_account_snapshot(
        self,
        *,
        force_refresh: bool = False,
    ) -> LiveAccountSnapshot:
        """Fetch or reuse one short-lived balance/order view for a loop."""
        now = time.monotonic()
        cached = self._live_account_snapshot
        if (
            not force_refresh
            and cached is not None
            and now - cached.captured_at < LIVE_ACCOUNT_CACHE_SECONDS
        ):
            return cached

        from src.auth import get_conditional_balance

        # Read open orders before the balance.  A fill racing this pair can
        # then make projected exposure conservative (old open order plus new
        # balance) rather than hiding a just-filled position.
        open_orders = tuple(get_open_orders(self.token_id, raise_on_error=True))
        conditional = get_conditional_balance(
            self.token_id,
            raise_on_error=True,
        )
        snapshot = LiveAccountSnapshot(
            captured_at=time.monotonic(),
            balance=Decimal(str(conditional["balance"])),
            allowance=Decimal(str(conditional["allowance"])),
            sellable=Decimal(str(conditional["sellable"])),
            open_orders=open_orders,
        )
        self._live_account_snapshot = snapshot
        return snapshot

    def _invalidate_live_account_snapshot(self) -> None:
        """Force the next live read after a local order-state mutation."""
        self._live_account_snapshot = None

    async def _wait_for_data(self, timeout: float = 30.0):
        """Wait for feed to have data, bootstrapping via REST if needed."""
        logger.info("Waiting for market data...")
        start = asyncio.get_event_loop().time()
        bootstrapped = False

        while asyncio.get_event_loop().time() - start < timeout:
            if self.feed:
                mid = self.feed.get_midpoint(self.token_id)
                is_healthy = self.feed.is_healthy

                # Debug: log why we're not ready
                if not is_healthy or mid is None:
                    elapsed = asyncio.get_event_loop().time() - start
                    if int(elapsed) % 5 == 0 and elapsed > 0:  # Log every 5 seconds
                        book = self.feed.get_order_book(self.token_id)
                        has_bids = bool(book and book.bids) if book else False
                        has_asks = bool(book and book.asks) if book else False
                        logger.debug(
                            f"Waiting: healthy={is_healthy}, mid={mid}, "
                            f"has_bids={has_bids}, has_asks={has_asks}, "
                            f"state={self.feed.state.name}, "
                            f"all_fresh={self.feed._data_store.all_fresh()}"
                        )

                if is_healthy and mid is not None:
                    logger.info(f"Got initial mid: {mid}")
                    # Runtime price check - reject extreme prices
                    if mid < MARKET_MIN_PRICE:
                        raise RuntimeError(
                            f"Price too low for safe MM: {mid:.4f} < {MARKET_MIN_PRICE:.2f}. "
                            f"Select a market with mid price between {MARKET_MIN_PRICE:.0%} and {MARKET_MAX_PRICE:.0%}."
                        )
                    if mid > MARKET_MAX_PRICE:
                        raise RuntimeError(
                            f"Price too high for safe MM: {mid:.4f} > {MARKET_MAX_PRICE:.2f}. "
                            f"Select a market with mid price between {MARKET_MIN_PRICE:.0%} and {MARKET_MAX_PRICE:.0%}."
                        )
                    return

            # Bootstrap via REST if no WS data after 3 seconds
            elapsed = asyncio.get_event_loop().time() - start
            if not bootstrapped and elapsed > 3.0:
                logger.info("No WS data yet, bootstrapping via REST...")
                await self._bootstrap_order_books()
                bootstrapped = True

                # Fail fast if bootstrap found no CLOB data
                if getattr(self, '_bootstrap_failed', False):
                    raise RuntimeError(
                        "Market not available on CLOB. Select a different market."
                    )

            await asyncio.sleep(0.5)

        raise RuntimeError("Timeout waiting for market data")

    async def _bootstrap_order_books(self):
        """Fetch initial order book data via REST API."""
        tokens = [self.token_id]
        if self.complement_token_id:
            tokens.append(self.complement_token_id)

        loop = asyncio.get_event_loop()
        bootstrapped_any = False
        for token_id in tokens:
            try:
                book = await loop.run_in_executor(None, get_order_book, token_id)
                if book is None:
                    logger.error(
                        f"No CLOB order book for {token_id[:16]}... - market may not be active on CLOB"
                    )
                    continue
                if self.feed:
                    self.feed._data_store.update_book(
                        token_id,
                        [{'price': str(b.price), 'size': str(b.size)} for b in book.bids],
                        [{'price': str(a.price), 'size': str(a.size)} for a in book.asks]
                    )
                    logger.info(f"Bootstrapped order book for {token_id[:16]}...")
                    bootstrapped_any = True
            except Exception as e:
                logger.warning(f"Failed to bootstrap {token_id[:16]}...: {e}")

        # Set flag if primary token has no CLOB data
        self._bootstrap_failed = not bootstrapped_any

    async def _loop_iteration(self):
        """Single iteration of the smart market making loop."""
        import time
        self._loop_count += 1

        # SAFETY: Check for stale orders every 10 iterations
        if self._loop_count % 10 == 0:
            self._cleanup_stale_orders()

        # SAFETY: Periodic balance check
        now = time.time()
        if now - self._last_balance_check > BALANCE_CHECK_INTERVAL:
            self._check_balance()
            self._last_balance_check = now

        # Heartbeat every 30 seconds
        if now - self._last_heartbeat >= 30:
            pnl_stats = self.pnl_tracker.get_market_stats(self.token_id)
            pnl_str = f"${pnl_stats.realized_pnl:.2f}" if pnl_stats else "$0.00"
            fills = pnl_stats.trade_count if pnl_stats else 0
            timer_mode = self.timer.get_mode().value
            timer_interval = self.timer.get_interval()
            logger.info(
                f"[HEARTBEAT] Loop #{self._loop_count} | "
                f"Mid: {self.last_mid or 'N/A'} | "
                f"Fills: {fills} | P&L: {pnl_str} | "
                f"Timer: {timer_mode} ({timer_interval:.1f}s)"
            )
            self._last_heartbeat = now

        live_snapshot: LiveAccountSnapshot | None = None
        if not DRY_RUN:
            try:
                live_snapshot = self._get_live_account_snapshot()
                self._sync_live_state(live_snapshot)
            except PolymarketAdapterError as error:
                self.risk.record_error(f"Live trade adapter contract failed: {error}")
                logger.critical("[SAFETY] Live trade adapter contract failed: %s", error)
                self.stop()
                await self._cancel_all_quotes()
                return
            except Exception as e:
                self.risk.record_error(f"Live sync failed: {e}")
                logger.error(f"Live sync failed: {e}")
                await self._cancel_all_quotes()
                return

        # Risk check
        if live_snapshot is None:
            check = self.risk.check([self.token_id])
        else:
            check = self.risk.check(
                [self.token_id],
                position_snapshots={self.token_id: live_snapshot.balance},
                open_order_snapshots={self.token_id: live_snapshot.open_orders},
            )
        if check.status == RiskStatus.STOP:
            logger.error(f"Risk stop: {check.reason}")
            await self._cancel_all_quotes()
            self.stop()
            return

        if check.status == RiskStatus.WARN:
            logger.warning(f"Risk warning: {check.reason}")

        # Feed health
        if not self.feed or not self.feed.is_healthy:
            logger.warning("Feed unhealthy - cancelling quotes")
            await self._cancel_all_quotes()
            return

        # Scan for arbitrage opportunities
        if self.complement_token_id and self.feed:
            def price_getter(token_id: str) -> Optional[Decimal]:
                if not self.feed:
                    return None
                price = self.feed.get_midpoint(token_id)
                return Decimal(str(price)) if price is not None else None

            # Get prices for debugging
            yes_price = price_getter(self.token_id)
            no_price = price_getter(self.complement_token_id)

            signals = self.arb_detector.scan_all(price_getter)

            # Debug log on first loop
            if self._loop_count == 1 and yes_price and no_price:
                logger.info(
                    f"[ARB] Scanning: YES={yes_price:.4f} NO={no_price:.4f} "
                    f"Sum={yes_price + no_price:.4f}"
                )

            if signals:
                for signal in signals:
                    logger.info(
                        f"[ARB] {signal.type.value}: {signal.recommended_action} "
                        f"({signal.profit_bps}bps)"
                    )

        # Get market data
        mid = self.feed.get_midpoint(self.token_id)
        if mid is None:
            logger.warning("No midpoint available")
            return
        mid = Decimal(str(mid))

        # Check YES/NO parity for arbitrage detection
        if self.complement_token_id:
            no_mid = self.feed.get_midpoint(self.complement_token_id)
            if no_mid is not None:
                parity = check_parity(mid, Decimal(str(no_mid)))
                if parity == ParityStatus.OVERPRICED:
                    logger.warning(
                        f"Arbitrage opportunity: YES+NO = {mid + Decimal(str(no_mid)):.3f} "
                        "(overpriced, skipping quotes)"
                    )
                    self.trade_logger.log_event(
                        "arbitrage_detected",
                        yes_price=str(mid),
                        no_price=str(no_mid),
                        status=parity.value,
                    )
                    await self._cancel_all_quotes()
                    return
                elif parity == ParityStatus.NEAR_ARBITRAGE:
                    logger.info(f"Near-arbitrage: YES+NO = {mid + Decimal(str(no_mid)):.3f}")

        # Update volatility tracker
        self.volatility.update(float(mid))

        # Update adaptive timer with price observation
        self.timer.update_from_price(float(mid))

        # Get order book for analysis
        order_book = None
        if hasattr(self.feed, '_data_store'):
            order_book = self.feed._data_store.get_order_book(self.token_id)

        # Check for simulated fills in DRY_RUN mode
        if DRY_RUN:
            bid = self.feed.get_best_bid(self.token_id)
            ask = self.feed.get_best_ask(self.token_id)
            if bid is not None and ask is not None:
                sim = get_simulator()
                trades_before = len(sim.get_trades(self.token_id))
                filled = sim.check_fills(
                    self.token_id, Decimal(str(bid)), Decimal(str(ask))
                )
                if filled:
                    logger.info(f"[SIM] {filled} order(s) filled")
                    # Record new fills in inventory manager for VWAP tracking
                    all_trades = sim.get_trades(self.token_id)
                    new_trades = all_trades[trades_before:]
                    for trade in new_trades:
                        self._record_trade_fill(trade, fill_type="maker")

        # Calculate dynamic spread and quotes
        result = self._calculate_quotes(
            mid,
            order_book,
            wallet_position=(
                live_snapshot.balance if live_snapshot is not None else None
            ),
        )
        if result is None:
            # Event signal says not to trade - cancel quotes
            await self._cancel_all_quotes()
            return

        bid_price, ask_price, state = result

        # Store state for TUI
        self._last_state = state

        # Check if requote needed
        if self._should_requote(mid):
            if await self._update_quotes(
                mid,
                bid_price,
                ask_price,
                live_snapshot=live_snapshot,
                inventory_state=self._latest_inventory_state,
            ):
                self.last_mid = mid

    def _calculate_quotes(
        self,
        mid: Decimal,
        order_book,
        *,
        wallet_position: Decimal | None = None,
    ) -> Optional[tuple[Decimal, Decimal, SmartMMState]]:
        """Calculate optimal bid/ask prices using all signals. Returns None if should not quote."""
        # Check event signal first - may prohibit trading
        event_signal = self.event_tracker.get_signal(self.token_id)
        if not event_signal.should_trade:
            logger.warning(f"[EVENT] {event_signal.reason} - not quoting")
            return None

        # 1. Volatility multiplier
        vol_mult = self.volatility.get_multiplier()
        vol_state = self.volatility.get_state()

        # 2. Inventory state and skews
        inv_state = self.inventory.get_state(
            mid,
            wallet_position=wallet_position,
        )
        self._latest_inventory_state = inv_state

        # Inventory multiplier: widen spread when inventory is high
        inv_mult = 1.0 + abs(inv_state.position_pct) / 200  # +50% at max inventory

        # 3. Book imbalance
        book_analysis = self.book_analyzer.analyze(order_book)
        imbalance_adj = book_analysis.price_adjustment

        # 4. Calculate final spread
        spread = self.base_spread * Decimal(str(vol_mult)) * Decimal(str(inv_mult))
        spread = max(self.min_spread, min(self.max_spread, spread))

        # 5. Event tracker spread adjustment (should_trade already checked).
        # Apply this before prices are built so later alpha adjustments cannot
        # be overwritten by a second base-price calculation.
        if event_signal.spread_multiplier != 1.0:
            logger.info(
                f"[EVENT] Spread multiplier: {event_signal.spread_multiplier:.2f}x - {event_signal.reason}"
            )
            spread = spread * Decimal(str(event_signal.spread_multiplier))

        # 6. Build prices once from all risk adjustments, then apply alpha.
        half_spread = spread / 2
        bid_price = mid - half_spread + inv_state.bid_skew + imbalance_adj
        ask_price = mid + half_spread + inv_state.ask_skew + imbalance_adj
        bid_price, ask_price = self.arb_detector.get_quote_adjustment(
            self.token_id, bid_price, ask_price
        )

        # Use the venue tick for both quote construction and order validation.
        # Bids round down and asks round up so quantization never narrows the
        # intended spread or accidentally crosses the market.
        tick_size = get_tick_size(self.token_id)
        bid_price = round_to_tick(bid_price, tick_size, rounding=ROUND_DOWN)
        ask_price = round_to_tick(ask_price, tick_size, rounding=ROUND_UP)

        minimum_price = tick_size
        maximum_price = Decimal("1") - tick_size
        bid_price = max(minimum_price, min(maximum_price, bid_price))
        ask_price = max(minimum_price, min(maximum_price, ask_price))
        if bid_price >= ask_price:
            # Revert to the risk-adjusted spread around mid.  If the requested
            # spread is smaller than one tick, enforce one full tick.
            bid_price = mid - half_spread
            ask_price = mid + half_spread
            bid_price = round_to_tick(bid_price, tick_size, rounding=ROUND_DOWN)
            ask_price = round_to_tick(ask_price, tick_size, rounding=ROUND_UP)
            bid_price = max(minimum_price, min(maximum_price, bid_price))
            ask_price = max(minimum_price, min(maximum_price, ask_price))
            if bid_price >= ask_price:
                bid_price = min(bid_price, Decimal("1") - (tick_size * 2))
                ask_price = bid_price + tick_size

        # Build state for TUI
        state = SmartMMState(
            base_spread=self.base_spread,
            vol_multiplier=vol_mult,
            inv_multiplier=inv_mult,
            final_spread=spread,
            volatility_level=vol_state.level,
            realized_vol=vol_state.realized_vol,
            inventory_pct=inv_state.position_pct,
            inventory_level=inv_state.inventory_level,
            bid_skew=inv_state.bid_skew,
            ask_skew=inv_state.ask_skew,
            imbalance_signal=book_analysis.imbalance_signal,
            imbalance_adjustment=imbalance_adj,
            unrealized_pnl=inv_state.unrealized_pnl,
            vwap_entry=inv_state.vwap_entry,
        )

        return bid_price, ask_price, state

    def _should_requote(self, mid: Decimal) -> bool:
        """Check if quotes need updating."""
        if self.bid_order is not None and not self.bid_order.is_live:
            self.bid_order = None
        if self.ask_order is not None and not self.ask_order.is_live:
            self.ask_order = None

        if self._quote_bid_enabled and self.bid_order is None:
            return True
        if self._quote_ask_enabled and self.ask_order is None:
            return True
        if not self._quote_bid_enabled and self.bid_order is not None:
            return True
        if not self._quote_ask_enabled and self.ask_order is not None:
            return True

        if self.last_mid is not None:
            move = abs(mid - self.last_mid)
            if move >= self.requote_threshold:
                logger.info(f"Mid moved {move:.4f} - requoting")
                return True

        return False

    async def _update_quotes(
        self,
        mid: Decimal,
        bid_price: Decimal,
        ask_price: Decimal,
        *,
        live_snapshot: LiveAccountSnapshot | None = None,
        inventory_state: InventoryState | None = None,
    ) -> bool:
        """Reconcile each quote independently, preserving unchanged queue priority."""
        logger.info(f"Mid: {mid:.2f} -> Bid: {bid_price:.2f}, Ask: {ask_price:.2f}")

        if self._last_state:
            logger.info(
                f"  Spread: {self._last_state.final_spread:.3f} "
                f"(vol={self._last_state.vol_multiplier:.2f}x, "
                f"inv={self._last_state.inv_multiplier:.2f}x)"
            )

        if inventory_state is None:
            inventory_state = self.inventory.get_state(
                wallet_position=(
                    live_snapshot.balance if live_snapshot is not None else None
                )
            )

        bid_size_mult = inventory_state.bid_size_mult
        ask_size_mult = inventory_state.ask_size_mult

        bid_size = self.size * Decimal(str(bid_size_mult))
        ask_size = self.size * Decimal(str(ask_size_mult))

        # Round sizes
        bid_size = bid_size.quantize(Decimal("0.01"))
        ask_size = ask_size.quantize(Decimal("0.01"))

        # Ensure minimum size
        from src.config import MIN_ORDER_SIZE
        bid_size = max(MIN_ORDER_SIZE, bid_size)
        ask_size = max(MIN_ORDER_SIZE, ask_size)

        if not DRY_RUN:
            if live_snapshot is None:
                from src.auth import get_conditional_balance

                conditional = get_conditional_balance(
                    self.token_id,
                    raise_on_error=True,
                )
                sellable = Decimal(str(conditional["sellable"]))
            else:
                sellable = live_snapshot.sellable
            ask_size = min(ask_size, sellable)

        # Log quote update
        self.trade_logger.log_quote(
            market_id=self.token_id,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
            spread=ask_price - bid_price,
            mid=mid,
        )

        # Place quotes (respecting inventory limits via size reduction, not hard stops)
        inv_state = inventory_state
        self._quote_bid_enabled = inv_state.inventory_level != "MAX_LONG"
        has_sellable_ask = ask_size >= MIN_ORDER_SIZE
        self._quote_ask_enabled = (
            inv_state.inventory_level != "MAX_SHORT" and has_sellable_ask
        )

        # A zero-inventory live start cannot offer a real ask.  Do not turn
        # that condition into a directional long strategy by leaving only the
        # bid active.  True dual-outcome quoting needs an explicitly modelled
        # complementary-token leg; until then, this state fails closed.
        if not DRY_RUN and not has_sellable_ask:
            self._quote_bid_enabled = False

        if not self._quote_bid_enabled:
            if inv_state.inventory_level == "MAX_LONG":
                logger.info("MAX_LONG - skipping bid")
            else:
                logger.warning(
                    "[SAFETY] Skipping bid because a two-sided live quote cannot be funded"
                )
        if not self._quote_ask_enabled:
            if inv_state.inventory_level == "MAX_SHORT":
                logger.info("MAX_SHORT - skipping ask")
            else:
                logger.info("Skipping ask: no sellable outcome token balance/allowance")

        bid_ok = self._reconcile_quote(
            attr_name="bid_order",
            label="bid",
            enabled=self._quote_bid_enabled,
            side=OrderSide.BUY,
            price=bid_price,
            size=bid_size,
        )
        ask_ok = self._reconcile_quote(
            attr_name="ask_order",
            label="ask",
            enabled=self._quote_ask_enabled,
            side=OrderSide.SELL,
            price=ask_price,
            size=ask_size,
        )
        return bid_ok and ask_ok

    def _reconcile_quote(
        self,
        *,
        attr_name: str,
        label: str,
        enabled: bool,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
    ) -> bool:
        """Cancel/replace one side only when its desired order changed."""
        order = getattr(self, attr_name)
        if order is not None and not order.is_live:
            setattr(self, attr_name, None)
            order = None

        if (
            order is not None
            and enabled
            and order.token_id == self.token_id
            and order.side == side
            and order.price == price
            and order.size == size
        ):
            logger.debug("Keeping unchanged %s order %s in queue", label, order.id[:16])
            return True

        if order is not None:
            try:
                cancelled = cancel_order(order.id)
            except Exception as error:
                cancelled = False
                logger.error("Failed to cancel %s order: %s", label, error)

            if not cancelled and not DRY_RUN:
                try:
                    open_ids = {
                        item.id
                        for item in get_open_orders(
                            order.token_id,
                            raise_on_error=True,
                        )
                        if item.is_live
                    }
                    cancelled = order.id not in open_ids
                except Exception as error:
                    logger.error(
                        "Cancel verification failed for %s order %s: %s",
                        label,
                        order.id,
                        error,
                    )

            if not cancelled:
                self.risk.record_error(f"Failed to cancel {label} order {order.id}")
                return False
            setattr(self, attr_name, None)
            self._invalidate_live_account_snapshot()

        if enabled:
            replacement = self._place_quote(side, price, size)
            setattr(self, attr_name, replacement)
            return replacement is not None
        return True

    def _place_quote(self, side: OrderSide, price: Decimal, size: Decimal) -> Optional[Order]:
        """Place a single quote."""
        try:
            order = place_order(
                token_id=self.token_id,
                side=side,
                price=price,
                size=size
            )
            logger.info(f"Placed {side.value} {size} @ {price}: {order.id}")
            self._invalidate_live_account_snapshot()
            return order
        except OrderError as e:
            self.risk.record_error(f"Failed to place {side.value}: {e}")
            logger.error(f"Failed to place {side.value}: {e}")
            return None

    async def _cancel_all_quotes(self):
        """Cancel all our quotes."""
        return self._cancel_all_quotes_sync()

    def _cancel_all_quotes_sync(self) -> bool:
        """Cancel tracked quotes and preserve local state if cancel fails."""
        if self.bid_order is not None or self.ask_order is not None:
            self._invalidate_live_account_snapshot()
        live_orders: list[tuple[str, str, Order]] = []

        for attr_name, label in (("bid_order", "bid"), ("ask_order", "ask")):
            order = getattr(self, attr_name)
            if order is None:
                continue

            if not order.is_live:
                setattr(self, attr_name, None)
                continue

            try:
                cancelled = cancel_order(order.id)
            except Exception as e:
                cancelled = False
                logger.error(f"Failed to cancel {label} order: {e}")

            if cancelled:
                setattr(self, attr_name, None)
            else:
                live_orders.append((attr_name, label, order))

        if not live_orders or DRY_RUN:
            for _attr_name, label, order in live_orders:
                self.risk.record_error(f"Failed to cancel {label} order {order.id}")
                logger.error(f"Cancel request did not complete for {label} order {order.id}")
            return not live_orders

        try:
            open_order_ids = {
                order.id
                for order in get_open_orders(self.token_id, raise_on_error=True)
                if order.is_live
            }
        except Exception as e:
            for _attr_name, label, order in live_orders:
                self.risk.record_error(f"Failed to cancel {label} order {order.id}")
                logger.error(f"Cancel verification failed for {label} order {order.id}: {e}")
            return False

        success = True
        for attr_name, label, order in live_orders:
            if order.id not in open_order_ids:
                logger.info(
                    "%s order %s already absent after cancel verification",
                    label.upper(),
                    order.id[:16],
                )
                setattr(self, attr_name, None)
                continue
            success = False
            self.risk.record_error(f"Failed to cancel {label} order {order.id}")
            logger.error(f"Cancel request did not complete for {label} order {order.id}")

        return success

    def _cleanup_stale_orders(self):
        """Cancel orders older than threshold (safety check)."""
        now = datetime.now(timezone.utc)
        stale_threshold = timedelta(seconds=STALE_ORDER_THRESHOLD_SECONDS)

        for order, name in [(self.bid_order, "bid"), (self.ask_order, "ask")]:
            if order and order.created_at:
                try:
                    # Parse ISO format timestamp
                    created = datetime.fromisoformat(order.created_at.replace('Z', '+00:00'))
                    age = now - created
                    if age > stale_threshold:
                        logger.warning(
                            f"[SAFETY] {name.upper()} order {order.id[:16]} is {age.seconds}s old - canceling"
                        )
                        cancel_order(order.id)
                        if name == "bid":
                            self.bid_order = None
                        else:
                            self.ask_order = None
                except Exception as e:
                    logger.error(f"Stale order check failed for {name}: {e}")

    def _check_balance(self):
        """Monitor balance for unexpected drops (safety check)."""
        if DRY_RUN:
            return

        try:
            from src.auth import get_balances
            balances = get_balances()
            current = balances.get('usdc_allowance', Decimal('0'))

            if self._initial_balance is None:
                self._initial_balance = current
                logger.info(f"[BALANCE] Initial balance: ${current:.2f}")
                return

            if self._initial_balance > 0:
                drop_pct = (self._initial_balance - current) / self._initial_balance
                if drop_pct > BALANCE_DROP_ALERT_PCT:
                    logger.error(
                        f"[SAFETY] Balance dropped {drop_pct:.1%}: "
                        f"${self._initial_balance:.2f} -> ${current:.2f} - TRIGGERING KILL SWITCH"
                    )
                    self.stop()
                    try:
                        self.risk.kill_switch(
                            "Balance dropped >20%",
                            token_id=self.token_id,
                        )
                    except Exception as error:
                        logger.critical(
                            "[SAFETY] Balance kill-switch teardown incomplete: %s",
                            error,
                        )

        except Exception as e:
            logger.warning(f"Balance check failed: {e}")

    @staticmethod
    def _trade_timestamp_seconds(trade: Trade) -> int | None:
        """Normalize a trade's matched timestamp to Unix seconds."""
        if not trade.timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(str(trade.timestamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())

    def _remember_trade(self, trade: Trade, *, fallback_timestamp: int) -> None:
        if not trade.id:
            return
        self._seen_trade_ids.add(trade.id)
        self._seen_trade_timestamps[trade.id] = (
            self._trade_timestamp_seconds(trade) or fallback_timestamp
        )

    def _prune_seen_trades(self, *, oldest_retained_timestamp: int) -> None:
        expired_ids = {
            trade_id
            for trade_id, timestamp in self._seen_trade_timestamps.items()
            if timestamp < oldest_retained_timestamp
        }
        for trade_id in expired_ids:
            self._seen_trade_timestamps.pop(trade_id, None)
            self._seen_trade_ids.discard(trade_id)

    def _clock_skew_bound_from_trades(
        self, trades: list[Trade], *, local_now: int
    ) -> Optional[int]:
        """Bound how far the local clock might be running ahead of Polymarket's.

        ClobTrade.matched_at is a server timestamp for an event that already
        happened, so local_now - matched_at is a valid (if possibly loose)
        upper bound on "local ahead of server" skew: server_now is at least
        matched_at, so server_now = local_now - skew >= matched_at implies
        skew <= local_now - matched_at. A slow local clock only produces a
        small/zero bound here, which is fine -- only a fast local clock can
        push after=now-OVERLAP past real fills and silently miss them, so
        that is the only direction this needs to catch.

        Returns None (never a default of zero) when none of `trades` has a
        parseable matched_at to anchor the estimate on.
        """
        timestamps = [
            timestamp
            for trade in trades
            if (timestamp := self._trade_timestamp_seconds(trade)) is not None
        ]
        if not timestamps:
            return None
        return max(0, local_now - max(timestamps))

    def _raise_if_skew_exceeds_tolerance(self, skew: int, *, context: str) -> None:
        if skew <= LIVE_CLOCK_SKEW_TOLERANCE_SECONDS:
            return
        raise PolymarketAdapterError(
            f"Local clock appears at least {skew}s ahead of Polymarket's "
            f"clock ({context}, inferred from account trade history). That "
            f"exceeds the {LIVE_CLOCK_SKEW_TOLERANCE_SECONDS}s tolerance -- "
            "fix host time sync before trusting live trade sync; a live "
            "loop cannot safely auto-correct a drift this large."
        )

    def _prime_live_trade_state(self):
        """Prime only a bounded overlap window so old history is never replayed."""
        local_now = int(time.time())
        after = max(0, local_now - LIVE_TRADE_OVERLAP_SECONDS)
        recent_trades = get_trades(
            self.token_id,
            limit=None,
            raise_on_error=True,
            after=str(after),
        )
        skew = self._clock_skew_bound_from_trades(recent_trades, local_now=local_now)
        if skew is None:
            raise PolymarketAdapterError(
                f"Cannot estimate live clock skew for {self.token_id!r}: no "
                "account trade with a parseable matched_at in the last "
                f"{LIVE_TRADE_OVERLAP_SECONDS}s to anchor the estimate "
                "against. Refusing to default to zero skew and start live "
                "trade sync unguarded."
            )
        self._raise_if_skew_exceeds_tolerance(skew, context="startup prime")
        self._clock_skew_seconds = skew
        watermark = local_now - skew
        self._seen_trade_ids.clear()
        self._seen_trade_timestamps.clear()
        for trade in recent_trades:
            self._remember_trade(trade, fallback_timestamp=watermark)
        self._trade_sync_watermark = watermark
        self._last_live_trade_poll = time.monotonic()
        self._empty_trade_sync_rounds = 0
        self._last_known_open_order_filled = {}

    def _sync_live_state(
        self,
        snapshot: LiveAccountSnapshot | None = None,
    ) -> None:
        """Sync live trades and open orders into local state."""
        new_trade_count = self._sync_live_trades()
        open_orders = self._sync_live_orders(
            snapshot.open_orders if snapshot is not None else None
        )
        if new_trade_count is not None:
            self._check_trade_sync_desync(
                open_orders, found_new_trades=new_trade_count > 0
            )

    def _check_trade_sync_desync(
        self,
        open_orders: tuple[Order, ...],
        *,
        found_new_trades: bool,
    ) -> None:
        """Flag a desynced trade feed from a live symptom, not clock math.

        A local clock running fast enough shifts `after` into the future and
        get_trades() can silently return nothing even though our own
        resting orders are filling. size_matched growing on a tracked open
        order while consecutive trade-sync rounds report zero new fills is
        that symptom -- it needs no clock-skew estimate and so still catches
        drift the startup estimate missed or that developed afterward.
        """
        current_filled = {order.id: order.filled for order in open_orders}
        previous_filled = self._last_known_open_order_filled
        grew = any(
            order_id in previous_filled
            and current_filled[order_id] > previous_filled[order_id]
            for order_id in current_filled
        )
        self._last_known_open_order_filled = current_filled

        if found_new_trades or not grew:
            self._empty_trade_sync_rounds = 0
            return

        self._empty_trade_sync_rounds += 1
        if self._empty_trade_sync_rounds >= LIVE_TRADE_SYNC_DESYNC_ROUNDS:
            raise PolymarketAdapterError(
                "Live trade sync looks desynced: open-order size_matched "
                f"grew for {self._empty_trade_sync_rounds} consecutive "
                "trade-sync rounds while get_trades() reported no new "
                "fills. Refusing to continue unguarded -- check for local/"
                "server clock drift or an adapter contract change."
            )

    def _sync_live_trades(self) -> Optional[int]:
        """Process newly observed live trades.

        Returns the number of new trades processed, or None if this call
        was skipped by the LIVE_TRADE_POLL_SECONDS throttle -- callers use
        None to distinguish "did not check" from "checked, found nothing".
        """
        now = time.monotonic()
        if now - self._last_live_trade_poll < LIVE_TRADE_POLL_SECONDS:
            return None
        wall_time = int(time.time())
        watermark = self._trade_sync_watermark
        if watermark is None:
            watermark = wall_time
        after = max(0, watermark - LIVE_TRADE_OVERLAP_SECONDS)
        recent_trades = get_trades(
            self.token_id,
            limit=None,
            raise_on_error=True,
            after=str(after),
        )
        self._last_live_trade_poll = now
        new_trades = [
            trade for trade in recent_trades
            if trade.id and trade.id not in self._seen_trade_ids
        ]

        if new_trades:
            # A fresh, first-time-seen fill is the best low-noise clock-skew
            # calibration point available in steady state: unlike a trade
            # re-seen inside the overlap window (which can be arbitrarily
            # old on a quiet market), one just discovered for the first
            # time should have matched_at close to wall_time under synced
            # clocks. Missing/unparseable timestamps are skipped rather
            # than treated as a failure -- this is a best-effort recheck,
            # not the one-time startup requirement to have some anchor.
            skew = self._clock_skew_bound_from_trades(new_trades, local_now=wall_time)
            if skew is not None:
                self._raise_if_skew_exceeds_tolerance(skew, context="trade-sync round")
                self._clock_skew_seconds = skew

        for trade in new_trades:
            self._record_trade_fill(trade, fill_type="live")
        for trade in recent_trades:
            self._remember_trade(trade, fallback_timestamp=wall_time)

        trade_timestamps = [
            timestamp
            for trade in recent_trades
            if (timestamp := self._trade_timestamp_seconds(trade)) is not None
        ]
        self._trade_sync_watermark = max(
            watermark,
            wall_time,
            max(trade_timestamps, default=watermark),
        )
        self._prune_seen_trades(
            oldest_retained_timestamp=(
                self._trade_sync_watermark - LIVE_TRADE_OVERLAP_SECONDS
            )
        )
        return len(new_trades)

    def _sync_live_orders(
        self,
        open_orders: tuple[Order, ...] | None = None,
    ) -> tuple[Order, ...]:
        """Reconcile tracked quote references with exchange open orders."""
        if open_orders is None:
            open_orders = tuple(
                get_open_orders(self.token_id, raise_on_error=True)
            )
        open_by_id = {order.id: order for order in open_orders}

        for attr_name, label in (("bid_order", "bid"), ("ask_order", "ask")):
            order = getattr(self, attr_name)
            if order is None:
                continue

            live_order = open_by_id.get(order.id)
            if live_order is None:
                if order.is_live:
                    logger.info(f"{label.upper()} order {order.id[:16]} no longer open")
                    order.status = OrderStatus.CANCELLED
                setattr(self, attr_name, None)
                continue

            order.filled = live_order.filled
            order.status = live_order.status

        return open_orders

    def _record_trade_fill(self, trade: Trade, fill_type: str):
        """Apply a fill to inventory, P&L, and risk state exactly once."""
        side = trade.side.value if isinstance(trade.side, OrderSide) else str(trade.side).upper()
        fee = trade.fee if isinstance(trade.fee, Decimal) else Decimal(str(trade.fee))

        self.inventory.record_fill(
            price=trade.price,
            size=trade.size,
            side=side,
        )

        stats_before = self.pnl_tracker.get_market_stats(self.token_id)
        realized_before = stats_before.realized_pnl if stats_before else Decimal("0")

        self.pnl_tracker.record_trade(
            market_id=self.token_id,
            side=side,
            price=trade.price,
            size=trade.size,
        )

        stats_after = self.pnl_tracker.get_market_stats(self.token_id)
        realized_after = stats_after.realized_pnl if stats_after else Decimal("0")
        realized_delta = realized_after - realized_before

        self.risk.record_trade(
            token_id=self.token_id,
            side=side,
            price=trade.price,
            size=trade.size,
            realized_pnl=realized_delta if realized_delta != 0 else None,
            fee=fee,
        )

        self.trade_logger.log_trade(
            market_id=self.token_id,
            side=side,
            price=trade.price,
            size=trade.size,
            fill_type=fill_type,
            order_id=trade.order_id or None,
        )

        self._mark_order_fill(trade.order_id, trade.size)

    def _mark_order_fill(self, order_id: str, fill_size: Decimal):
        """Advance local tracked order state after a fill."""
        if not order_id:
            return

        for attr_name in ("bid_order", "ask_order"):
            order = getattr(self, attr_name)
            if order is None or order.id != order_id:
                continue

            order.filled += fill_size
            if order.filled >= order.size:
                order.status = OrderStatus.MATCHED
                setattr(self, attr_name, None)
            break

    async def _shutdown(self) -> Exception | None:
        """Clean shutdown and return teardown errors without masking callers."""
        logger.info("Shutting down smart market maker...")
        shutdown_error: Exception | None = None
        cancellation_still_running = False
        disconnect_task = self._disconnect_teardown_task
        if (
            disconnect_task is not None
            and disconnect_task is not asyncio.current_task()
            and not disconnect_task.done()
        ):
            logger.info("Waiting for disconnect teardown to finish...")
            try:
                await asyncio.wait_for(
                    asyncio.shield(disconnect_task),
                    timeout=ORDER_TEARDOWN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                cancellation_still_running = True
                shutdown_error = OrderCancellationError(
                    "Disconnect teardown exceeded "
                    f"{ORDER_TEARDOWN_TIMEOUT_SECONDS:g}s; live order cancellation "
                    "is still running in the background"
                )
                self.risk.record_error(str(shutdown_error))
                logger.critical("[SAFETY] %s", shutdown_error)

        summary = self.risk.get_risk_event_summary()
        if summary["total_events"] > 0:
            logger.info("Risk Event Summary:")
            logger.info(f"  Total events: {summary['total_events']}")
            logger.info(f"  STOP events: {summary['stop_events']} (enforced: {summary['enforced_events']})")
            logger.info(f"  WARN events: {summary['warn_events']}")
            logger.info(f"  Final P&L: {self.risk.daily_pnl}")

        # Log smart MM specific stats
        if self._last_state:
            logger.info("Final state:")
            logger.info(f"  Volatility: {self._last_state.volatility_level} ({self._last_state.realized_vol:.1%})")
            logger.info(f"  Inventory: {self._last_state.inventory_level} ({self._last_state.inventory_pct:.1f}%)")
            logger.info(f"  Unrealized P&L: {self._last_state.unrealized_pnl}")

        if not cancellation_still_running:
            logger.info("Cancelling all orders...")
            try:
                self._shutdown_cancel_task = asyncio.create_task(
                    asyncio.to_thread(
                        cancel_all_orders,
                        self.token_id,
                        verify=True,
                        raise_on_failure=True,
                    )
                )
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_cancel_task),
                    timeout=ORDER_TEARDOWN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                error = OrderCancellationError(
                    "Shutdown cancellation exceeded "
                    f"{ORDER_TEARDOWN_TIMEOUT_SECONDS:g}s and is still running "
                    "in the background"
                )
                if shutdown_error is None:
                    shutdown_error = error
                self.risk.record_error(str(error))
                logger.critical("[SAFETY] %s", error)
            except Exception as e:
                if shutdown_error is None:
                    shutdown_error = e
                self.risk.record_error(f"Shutdown cancellation verification failed: {e}")
                logger.critical("[SAFETY] %s", e)

        if self.feed:
            logger.info("Stopping feed...")
            try:
                await self.feed.stop()
            except Exception as error:
                if shutdown_error is None:
                    shutdown_error = error
                self.risk.record_error(f"Feed shutdown failed: {error}")
                logger.critical("[SAFETY] Feed shutdown failed: %s", error)

        logger.info("Smart market maker stopped.")
        return shutdown_error


async def run_smart_market_maker(token_id: str, **kwargs):
    """
    Convenience function to run the smart market maker.

    Usage:
        asyncio.run(run_smart_market_maker("token123"))
    """
    mm = SmartMarketMaker(token_id, **kwargs)
    await mm.run()
