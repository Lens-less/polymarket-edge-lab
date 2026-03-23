#!/usr/bin/env python3
"""
Autoresearch verification script.

Usage:
  python3 scripts/autoresearch_verify.py --spread 0.01
  python3 scripts/autoresearch_verify.py --default
"""

import argparse
import sys
from decimal import Decimal as D
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategy.real_data_backtest import run_backtest, fetch_markets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--spread', type=float, help='Spread value to test')
    parser.add_argument('--default', action='store_true', help='Use default spread from config')
    parser.add_argument('--snapshots', type=int, default=500)
    args = parser.parse_args()

    # Get market
    markets = fetch_markets(min_liquidity=50000)
    if not markets:
        print("ERROR: No markets available", file=sys.stderr)
        sys.exit(1)
    market = markets[0]

    # Determine spread
    if args.default:
        from src.config import SPREAD_BASE
        spread = SPREAD_BASE
    elif args.spread:
        spread = D(str(args.spread))
    else:
        print("ERROR: Must specify --spread or --default", file=sys.stderr)
        sys.exit(1)

    # Run backtest
    result = run_backtest(spread=spread, market=market, snapshots=args.snapshots)

    if 'error' in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"sharpe_ratio: {result['sharpe_ratio']:.4f}")


if __name__ == "__main__":
    main()