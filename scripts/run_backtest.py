#!/usr/bin/env python3
"""
Minimal Backtest Runner

Runs backtest using BacktestEngine with either:
- Generated mock data
- SQLite data from persistence layer
- Parquet files

Usage:
    python scripts/run_backtest.py --mock
    python scripts/run_backtest.py --sqlite data/market_data.db --token <token_id>
    python scripts/run_backtest.py --parquet data/backtest/
"""

import argparse
import math
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.backtest.engine import BacktestEngine, BacktestResult
from src.backtest.data import HistoricalData, OrderBookSnapshot


def generate_mock_data(
    token_id: str = "mock-token-1",
    num_snapshots: int = 500,
    start_price: float = 0.50,
    volatility: float = 0.02,
    spread: float = 0.01,
) -> HistoricalData:
    """
    Generate mock historical order book data.

    Creates price series with random walk + mean reversion to simulate
    realistic market behavior for backtesting.
    """
    data = HistoricalData()
    base_timestamp = int(time.time()) - (num_snapshots * 60)  # Start in past

    price = start_price
    for i in range(num_snapshots):
        # Random walk with mean reversion
        change = (math.sin(i * 0.1) * 0.005) + (math.cos(i * 0.05) * 0.003)
        noise = (math.sin(i * 0.3) * volatility * 0.5)
        price = max(0.01, min(0.99, price + change + noise * 0.01))

        best_bid = Decimal(str(price))
        best_ask = Decimal(str(price + spread))

        data.add_snapshot(OrderBookSnapshot(
            timestamp=base_timestamp + (i * 60),  # 1 minute intervals
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_depth=Decimal(str(100 + math.sin(i * 0.2) * 50)),
            ask_depth=Decimal(str(100 + math.cos(i * 0.2) * 50)),
        ))

    return data


def load_from_sqlite(db_path: str, token_id: str) -> Optional[HistoricalData]:
    """Load historical data from SQLite database."""
    try:
        import sqlite3
        from src.feed.persistence import SQLiteStore

        store = SQLiteStore(db_path)

        # Get all snapshots for token
        df = store.get_snapshots(token_id, start_time=0)
        if df.empty:
            print(f"No data found for token {token_id}")
            return None

        data = HistoricalData()
        for _, row in df.iterrows():
            data.add_snapshot(OrderBookSnapshot(
                timestamp=int(row['timestamp']),
                token_id=token_id,
                best_bid=Decimal(str(row['best_bid'])) if pd.notna(row['best_bid']) else Decimal("0"),
                best_ask=Decimal(str(row['best_ask'])) if pd.notna(row['best_ask']) else Decimal("0"),
                bid_depth=Decimal(str(row['bid_depth_5'])) if 'bid_depth_5' in row and pd.notna(row['bid_depth_5']) else Decimal("100"),
                ask_depth=Decimal(str(row['ask_depth_5'])) if 'ask_depth_5' in row and pd.notna(row['ask_depth_5']) else Decimal("100"),
            ))

        store.close()
        return data

    except Exception as e:
        print(f"Failed to load from SQLite: {e}")
        return None


def load_from_parquet(parquet_dir: str, token_id: str) -> Optional[HistoricalData]:
    """Load historical data from Parquet files."""
    try:
        import pandas as pd

        snapshots_path = Path(parquet_dir) / "snapshots.parquet"
        if not snapshots_path.exists():
            print(f"No snapshots.parquet found in {parquet_dir}")
            return None

        df = pd.read_parquet(snapshots_path)
        df = df[df['token_id'] == token_id]

        if df.empty:
            print(f"No data found for token {token_id}")
            return None

        data = HistoricalData()
        for _, row in df.iterrows():
            data.add_snapshot(OrderBookSnapshot(
                timestamp=int(row['timestamp']),
                token_id=token_id,
                best_bid=Decimal(str(row['best_bid'])) if pd.notna(row['best_bid']) else Decimal("0"),
                best_ask=Decimal(str(row['best_ask'])) if pd.notna(row['best_ask']) else Decimal("0"),
                bid_depth=Decimal(str(row['bid_depth_5'])) if 'bid_depth_5' in row and pd.notna(row['bid_depth_5']) else Decimal("100"),
                ask_depth=Decimal(str(row['ask_depth_5'])) if 'ask_depth_5' in row and pd.notna(row['ask_depth_5']) else Decimal("100"),
            ))

        return data

    except Exception as e:
        print(f"Failed to load from Parquet: {e}")
        return None


def simple_mm_strategy(engine: BacktestEngine, snapshot: OrderBookSnapshot):
    """
    Simple market making strategy for backtest.

    Places bid at best_bid and ask at best_ask, requoting each snapshot.
    """
    # Cancel existing orders
    for order_id, order in list(engine._orders.items()):
        if order.status.name == "OPEN":
            engine.cancel_order(order_id)

    # Place new orders
    # Buy at best_ask (aggressive to ensure fills in backtest)
    # Sell at best_bid (aggressive to ensure fills in backtest)
    engine.place_order("BUY", snapshot.best_ask, Decimal("10"))
    engine.place_order("SELL", snapshot.best_bid, Decimal("10"))


def print_results(result: BacktestResult):
    """Print formatted backtest results."""
    print("\n" + "=" * 50)
    print("BACKTEST RESULTS")
    print("=" * 50)
    print(f"Initial Capital:    ${result.initial_capital:,.2f}")
    print(f"Final Capital:      ${result.final_capital:,.2f}")
    print(f"Total Return:       {result.total_return:+.2%}")
    print(f"Total Trades:       {result.total_trades}")
    print(f"Winning Trades:     {result.winning_trades}")
    print(f"Losing Trades:      {result.losing_trades}")
    print(f"Win Rate:           {result.win_rate:.1%}")
    print(f"Sharpe Ratio:       {result.sharpe_ratio:.2f}")
    print(f"Max Drawdown:       {result.max_drawdown:.2%}")
    print(f"Profit Factor:      {result.profit_factor:.2f}")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="Run backtest on historical data")
    parser.add_argument("--mock", action="store_true", help="Use generated mock data")
    parser.add_argument("--sqlite", type=str, help="Path to SQLite database")
    parser.add_argument("--parquet", type=str, help="Path to Parquet directory")
    parser.add_argument("--token", type=str, default="mock-token-1", help="Token ID to backtest")
    parser.add_argument("--capital", type=float, default=1000.0, help="Initial capital")
    parser.add_argument("--snapshots", type=int, default=500, help="Number of mock snapshots")

    args = parser.parse_args()

    # Determine data source
    if args.mock:
        print(f"Generating mock data: {args.snapshots} snapshots...")
        data = generate_mock_data(
            token_id=args.token,
            num_snapshots=args.snapshots,
        )
    elif args.sqlite:
        print(f"Loading from SQLite: {args.sqlite}...")
        data = load_from_sqlite(args.sqlite, args.token)
        if data is None:
            sys.exit(1)
    elif args.parquet:
        print(f"Loading from Parquet: {args.parquet}...")
        data = load_from_parquet(args.parquet, args.token)
        if data is None:
            sys.exit(1)
    else:
        # Default to mock data
        print(f"No data source specified, using mock data...")
        data = generate_mock_data(token_id=args.token, num_snapshots=args.snapshots)

    print(f"Loaded {len(data)} snapshots for token: {args.token}")

    # Run backtest
    print(f"\nRunning backtest with ${args.capital:.2f} initial capital...")
    engine = BacktestEngine(initial_capital=Decimal(str(args.capital)))
    result = engine.run(data, strategy="custom", strategy_fn=simple_mm_strategy)

    # Print results
    print_results(result)

    # Optional: save equity curve
    report = engine.generate_report()
    print("\nDetailed Report:")
    for key, value in report.items():
        if isinstance(value, float):
            print(f"  {key}: {value:.4f}")
        else:
            print(f"  {key}: {value}")

    return result


if __name__ == "__main__":
    main()
