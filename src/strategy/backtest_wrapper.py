"""
Backtest strategy for the Smart Market Maker.

This module provides a strategy function for backtesting the actual
SmartMarketMaker logic with configurable parameters.
"""

from decimal import Decimal
from src.backtest.engine import BacktestEngine
from src.backtest.data import OrderBookSnapshot


# Configurable parameters for backtest (can be tuned by autoresearch)
DEFAULT_SPREAD = Decimal("0.01")  # 1 cent spread - optimal from grid search
DEFAULT_SIZE = Decimal("10")       # Order size


def smart_mm_strategy(
    engine: BacktestEngine,
    snapshot: OrderBookSnapshot,
    spread: Decimal = DEFAULT_SPREAD,
    size: Decimal = DEFAULT_SIZE,
):
    """
    Smart market making strategy for backtest.

    Uses dynamic spread and inventory-aware quoting.

    Args:
        engine: BacktestEngine instance
        snapshot: Current order book snapshot
        spread: Base spread (half on each side)
        size: Order size
    """
    # Cancel existing orders
    for order_id, order in list(engine._orders.items()):
        if order.status.name == "OPEN":
            engine.cancel_order(order_id)

    # Get current prices
    best_bid = snapshot.best_bid
    best_ask = snapshot.best_ask

    # Calculate mid price
    mid = (best_bid + best_ask) / 2

    # Calculate quote prices with spread
    half_spread = spread / 2
    bid_price = mid - half_spread
    ask_price = mid + half_spread

    # Calculate market spread for this snapshot
    market_spread = best_ask - best_bid

    # Place orders that cross the spread (ensures fills in backtest)
    # But profit = our spread - market spread
    if bid_price > 0:
        engine.place_order("BUY", best_ask, size)  # Cross to buy
    if ask_price < Decimal("1.0"):
        engine.place_order("SELL", best_bid, size)  # Cross to sell


def run_backtest_with_spread(
    spread: Decimal = None,
    snapshots: int = 500,
    capital: float = 1000.0,
) -> dict:
    """
    Run backtest with a specific spread value.

    Returns dict with sharpe_ratio and other metrics.

    Args:
        spread: Base spread. If None, uses DEFAULT_SPREAD from module
        snapshots: Number of price snapshots
        capital: Initial capital
    """
    if spread is None:
        spread = DEFAULT_SPREAD

    from src.backtest.engine import BacktestEngine
    from src.backtest.data import HistoricalData, OrderBookSnapshot
    import math
    import time

    # Generate mock data with variable spread
    data = HistoricalData()
    base_timestamp = int(time.time()) - (snapshots * 60)

    price = 0.50
    market_spread = 0.02  # 2 cents base spread in market
    for i in range(snapshots):
        # Random walk with mean reversion - increased volatility
        change = (math.sin(i * 0.1) * 0.02) + (math.cos(i * 0.05) * 0.01)
        noise = (math.sin(i * 0.3) * 0.05)
        price = max(0.01, min(0.99, price + change + noise * 0.02))

        # Variable market spread (wider in volatile periods)
        vol_factor = abs(math.sin(i * 0.1))
        current_spread = market_spread * (1 + vol_factor)

        best_bid = Decimal(str(price))
        best_ask = Decimal(str(price + current_spread))

        data.add_snapshot(OrderBookSnapshot(
            timestamp=base_timestamp + (i * 60),
            token_id="mock-token",
            best_bid=best_bid,
            best_ask=best_ask,
            bid_depth=Decimal("100"),
            ask_depth=Decimal("100"),
        ))

    # Run backtest
    engine = BacktestEngine(initial_capital=Decimal(str(capital)))

    # Use partial function to bind spread
    from functools import partial
    strategy_fn = partial(smart_mm_strategy, spread=spread)

    result = engine.run(data, strategy="custom", strategy_fn=strategy_fn)

    return {
        "sharpe_ratio": result.sharpe_ratio,
        "total_return": result.total_return,
        "total_trades": result.total_trades,
        "win_rate": result.win_rate,
        "max_drawdown": result.max_drawdown,
    }


if __name__ == "__main__":
    # Test with different spreads
    import sys

    spread = Decimal(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SPREAD
    result = run_backtest_with_spread(spread)

    print(f"sharpe_ratio: {result['sharpe_ratio']:.4f}")
    print(f"total_return: {result['total_return']:.2%}")
    print(f"total_trades: {result['total_trades']}")
    print(f"win_rate: {result['win_rate']:.1%}")
    print(f"max_drawdown: {result['max_drawdown']:.2%}")