"""
CLI runner for the market maker.
"""

import argparse
import asyncio
from decimal import Decimal
from src.config import DRY_RUN, get_mode_string
from src.markets import fetch_active_markets
from src.pricing import get_order_books
from src.strategy.market_maker import SmartMarketMaker
from src.strategy.market_scorer import MarketScorer
from src.orders import get_open_orders
from src.trading import cancel_all_orders
from src.utils import setup_logging

logger = setup_logging()


def cleanup_orphaned_orders(token_id: str) -> int:
    """
    Cancel any existing orders for this token on startup.

    This protects against orphaned orders from crashes.
    Returns count of canceled orders, or -1 on error.
    """
    if DRY_RUN:
        return 0  # Simulator has no persistence across restarts

    logger.info(f"[SAFETY] Checking for orphaned orders on {token_id[:16]}...")

    try:
        orders = get_open_orders(token_id)
        if not orders:
            logger.info("[SAFETY] No orphaned orders found")
            return 0

        logger.warning(f"[SAFETY] Found {len(orders)} orphaned orders - canceling")
        canceled = cancel_all_orders(
            token_id,
            verify=True,
            raise_on_failure=True,
        )
        logger.info(f"[SAFETY] Canceled {canceled} orphaned orders")
        return canceled

    except Exception as e:
        logger.error(f"[SAFETY] Failed to cleanup orders: {e}")
        # Don't block startup, but warn
        return -1


async def log_status_periodically(mm: SmartMarketMaker, interval: float = 30.0):
    """Log status every N seconds."""
    while mm._running:
        await asyncio.sleep(interval)
        if mm._running:
            status = mm.risk.get_status()
            logger.info(
                f"Status: Mode={status['mode']} | "
                f"PnL={status['daily_pnl']:+.2f} ({status['pnl_percent_of_limit']:.0f}% of limit) | "
                f"Events={status['risk_events_logged']}"
            )


def auto_select_market():
    """Auto-select the best market using MarketScorer."""
    print("\nFetching and scoring markets...")
    markets = fetch_active_markets(limit=50)

    if not markets:
        print("No active markets found!")
        return None, None

    # Filter markets with token IDs
    valid_markets = [m for m in markets if m.token_ids]
    if not valid_markets:
        print("No markets with token IDs found!")
        return None, None

    # Fetch order books for scoring
    token_ids = [m.token_ids[0] for m in valid_markets]
    books = get_order_books(token_ids)

    # Filter out markets where CLOB returned None/404
    markets_with_books = []
    for m in valid_markets:
        token_id = m.token_ids[0]
        book = books.get(token_id)
        if book is not None:
            markets_with_books.append(m)
        else:
            print(f"  Skipping {m.question[:40]}... - no CLOB order book")

    if not markets_with_books:
        print("No markets with active CLOB order books found!")
        return None, None

    valid_markets = markets_with_books

    # Build scoring input
    scorer = MarketScorer()
    score_input = []
    for m in valid_markets:
        token_id = m.token_ids[0]
        book = books.get(token_id)
        volume = m.volume or 0
        score_input.append((token_id, m, book, volume))

    # Score and sort
    scores = scorer.score_markets(score_input)

    # Find best non-rejected market
    for score in scores:
        if not score.rejected:
            # Find the market object
            for m in valid_markets:
                if m.token_ids[0] == score.token_id:
                    print(f"\n✓ Auto-selected: {score.market_question[:50]}...")
                    print(f"  Score: {score.total_score:.1f}/100")
                    print(f"  Volume: ${score.volume_24h:,.0f} | Spread: {score.spread:.3f}")
                    return m, score
            break

    # All rejected - show why
    print("\nNo suitable markets found. Top rejections:")
    for score in scores[:3]:
        print(f"  - {score.market_question[:40]}... ({score.reject_reason})")
    return None, None


def select_market_manual():
    """Let user select a market manually."""
    print("\nFetching active markets...")
    markets = fetch_active_markets(limit=20)

    if not markets:
        print("No active markets found!")
        return None

    print("\nAvailable markets:")
    print("-" * 60)

    valid_markets = []
    for i, m in enumerate(markets):
        if not m.token_ids:
            continue
        valid_markets.append(m)
        q = m.question[:50] + "..." if len(m.question) > 50 else m.question
        print(f"  {len(valid_markets)}. {q}")
        print(f"     Volume: ${m.volume:,.0f} | Liquidity: ${m.liquidity:,.0f}")

    print("-" * 60)

    while True:
        try:
            choice = input("\nSelect market number (or 'q' to quit): ").strip()
            if choice.lower() == 'q':
                return None

            idx = int(choice) - 1
            if 0 <= idx < len(valid_markets):
                return valid_markets[idx]
            print("Invalid selection")
        except ValueError:
            print("Enter a number")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Run Polymarket market maker")
    parser.add_argument(
        "--manual", "-m", action="store_true",
        help="Manually select market instead of auto-selecting best"
    )
    parser.add_argument(
        "--spread", "-s", type=float, default=0.02,
        help="Spread to maintain (default: 0.02)"
    )
    parser.add_argument(
        "--size", "-z", type=float, default=10.0,
        help="Quote size (default: 10.0)"
    )
    parser.add_argument(
        "--position-limit", "-p", type=float, default=100.0,
        help="Max position size (default: 100.0)"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  POLYMARKET MARKET MAKER")
    print(f"  Mode: {get_mode_string()}")
    print("  Strategy: SmartMarketMaker")
    print("=" * 60)

    if not DRY_RUN:
        print("\n⚠️  WARNING: LIVE TRADING MODE ⚠️")
        print("Real money will be used!")
        confirm = input("Type 'YES' to continue: ").strip()
        if confirm != "YES":
            print("Aborted.")
            return

    # Select market
    if args.manual:
        market = select_market_manual()
        if not market:
            print("No market selected. Exiting.")
            return
        token_id = market.token_ids[0]
        print(f"\nSelected: {market.question}")
        print(f"Token: {token_id[:20]}...")
    else:
        market, score = auto_select_market()
        if not market:
            print("No suitable market found. Try --manual to select manually.")
            return
        token_id = market.token_ids[0]
    print("\nStarting SmartMarketMaker...")
    print(f"  Spread: {args.spread}")
    print(f"  Size: {args.size}")
    print(f"  Position Limit: {args.position_limit}")
    print("Press Ctrl+C to stop\n")

    # Get complement token for arbitrage
    complement_token_id = None
    if hasattr(market, 'token_ids') and len(market.token_ids) == 2:
        # Binary market with YES/NO tokens
        complement_token_id = [tid for tid in market.token_ids if tid != token_id][0]
        print(f"Found complement token: {complement_token_id[:20]}...")

    # Get market end date for event tracking
    market_end_date = None
    if hasattr(market, 'end_date') and market.end_date:
        from datetime import datetime
        try:
            # Parse ISO format: 2026-02-01T20:00:00Z
            market_end_date = datetime.fromisoformat(market.end_date.replace('Z', '+00:00'))
            print(f"Market ends: {market_end_date.strftime('%Y-%m-%d %H:%M UTC')}")
        except (ValueError, AttributeError) as e:
            logger.warning(f"Could not parse market end_date: {e}")

    # SAFETY: Clean up any orphaned orders before starting
    orphaned = cleanup_orphaned_orders(token_id)
    if orphaned > 0:
        logger.warning(f"[SAFETY] Cleaned up {orphaned} orphaned orders before starting")
    if orphaned < 0:
        logger.error("[SAFETY] Could not verify order state - proceed with caution")

    # Run with auto-retry on CLOB failures
    max_retries = 3
    tried_tokens = set()

    for attempt in range(max_retries):
        tried_tokens.add(token_id)

        mm = SmartMarketMaker(
            token_id=token_id,
            base_spread=Decimal(str(args.spread)),
            size=Decimal(str(args.size)),
            position_limit=Decimal(str(args.position_limit)),
            complement_token_id=complement_token_id,
            market_end_date=market_end_date,
        )

        async def run_with_status():
            """Run market maker with periodic status updates."""
            status_task = asyncio.create_task(log_status_periodically(mm))
            try:
                await mm.run()
            finally:
                status_task.cancel()
                try:
                    await status_task
                except asyncio.CancelledError:
                    pass

        try:
            asyncio.run(run_with_status())
            break  # Normal exit
        except KeyboardInterrupt:
            print("\nInterrupted.")
            break
        except RuntimeError as e:
            if "CLOB" in str(e) and attempt < max_retries - 1:
                print("\n⚠️  Market unavailable on CLOB. Auto-selecting new market...")
                market, score = auto_select_market()
                if not market or market.token_ids[0] in tried_tokens:
                    print("No other suitable markets found. Exiting.")
                    break
                token_id = market.token_ids[0]
                complement_token_id = None
                if len(market.token_ids) == 2:
                    complement_token_id = [t for t in market.token_ids if t != token_id][0]
                market_end_date = None
                if hasattr(market, 'end_date') and market.end_date:
                    try:
                        market_end_date = datetime.fromisoformat(market.end_date.replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        pass
                print(f"Retrying with: {market.question[:50]}...\n")
            else:
                raise


if __name__ == "__main__":
    main()
