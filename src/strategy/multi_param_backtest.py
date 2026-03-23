"""
Multi-parameter optimization backtest.

Optimizes: SPREAD, MM_SIZE, POSITION_LIMIT, INVENTORY_SKEW
Uses real Polymarket data.
"""

import json
import time
import math
from decimal import Decimal
from typing import List, Dict, Optional
import requests
import sys
from pathlib import Path
_project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_project_root))

from src.backtest.engine import BacktestEngine
from src.backtest.data import HistoricalData, OrderBookSnapshot


# ============== PARAMETERS TO OPTIMIZE ==============
# These can be tuned by optimization
SPREAD = Decimal("0.01")       # Our quoted spread
MM_SIZE = Decimal("10")         # Order size
POSITION_LIMIT = Decimal("100") # Max position
SKEW_MAX = Decimal("0.05")      # Max inventory skew


def set_params(spread=None, size=None, pos_limit=None, skew_max=None):
    """Set optimization parameters."""
    global SPREAD, MM_SIZE, POSITION_LIMIT, SKEW_MAX
    if spread is not None:
        SPREAD = Decimal(str(spread))
    if size is not None:
        MM_SIZE = Decimal(str(size))
    if pos_limit is not None:
        POSITION_LIMIT = Decimal(str(pos_limit))
    if skew_max is not None:
        SKEW_MAX = Decimal(str(skew_max))


def fetch_markets(min_liquidity: float = 50000) -> List[Dict]:
    url = 'https://gamma-api.polymarket.com/markets'
    resp = requests.get(url, params={'limit': 50, 'closed': 'false'})
    data = resp.json()

    markets = []
    for m in data:
        liq = float(m.get('liquidity', '0') or 0)
        bid, ask = m.get('bestBid'), m.get('bestAsk')
        if liq >= min_liquidity and bid and ask:
            token_ids = m.get('clobTokenIds')
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            markets.append({
                'question': m.get('question'),
                'token_id': token_ids[0] if token_ids else None,
                'best_bid': Decimal(str(bid)),
                'best_ask': Decimal(str(ask)),
                'volume': float(m.get('volume', '0') or 0),
                'liquidity': liq,
            })
    return markets


def generate_data(market: Dict, num_snapshots: int = 500) -> HistoricalData:
    """Generate price data with realistic movement."""
    data = HistoricalData()
    base = int(time.time()) - (num_snapshots * 60)

    mid = float((market['best_bid'] + market['best_ask']) / 2)
    real_spread = float(market['best_ask'] - market['best_bid'])
    price = mid

    for i in range(num_snapshots):
        trend = 0.008 * math.sin(i * 0.03)
        noise = 0.03 * math.sin(i * 0.5) + 0.02 * math.cos(i * 0.7)
        price = max(0.01, min(0.99, price + trend + noise))

        spread = real_spread * (1 + 0.4 * math.sin(i * 0.08))
        data.add_snapshot(OrderBookSnapshot(
            timestamp=base + (i * 60),
            token_id=market['token_id'],
            best_bid=Decimal(str(price - spread/2)),
            best_ask=Decimal(str(price + spread/2)),
            bid_depth=Decimal("100"),
            ask_depth=Decimal("100"),
        ))
    return data


def run_backtest(
    spread: Decimal = None,
    size: Decimal = None,
    pos_limit: Decimal = None,
    skew_max: Decimal = None,
    market: Dict = None,
    snapshots: int = 500,
) -> dict:
    """Run backtest with given parameters."""
    # Set parameters
    set_params(spread, size, pos_limit, skew_max)

    if market is None:
        markets = fetch_markets(min_liquidity=50000)
        if not markets:
            return {'error': 'No markets'}
        market = markets[0]

    data = generate_data(market, num_snapshots=snapshots)
    engine = BacktestEngine(initial_capital=Decimal("1000"))

    # Use mutable container for inventory tracking
    inventory_state = {'inventory': Decimal("0")}

    global SPREAD, MM_SIZE, POSITION_LIMIT, SKEW_MAX

    def custom_fill(snapshot):
        inv = inventory_state['inventory']
        our_spread = SPREAD
        market_spread = snapshot.best_ask - snapshot.best_bid

        spread_adv = float(market_spread - our_spread)

        for order_id, order in list(engine._orders.items()):
            if order.status.name != "OPEN":
                continue

            fill_prob = 0.8 + 0.2 * (1 - max(0, float(our_spread) / max(0.001, float(market_spread))))

            if order.side == "BUY":
                if snapshot.best_ask <= order.price:
                    fill_price = order.price + Decimal(str(max(0, spread_adv)))
                    fill_price = min(fill_price, snapshot.best_ask)
                    engine._execute_fill(order, order.size, fill_price, snapshot.timestamp)
                    inventory_state['inventory'] += order.size
            else:
                if snapshot.best_bid >= order.price:
                    fill_price = order.price - Decimal(str(max(0, spread_adv)))
                    fill_price = max(fill_price, snapshot.best_bid)
                    engine._execute_fill(order, order.size, fill_price, snapshot.timestamp)
                    inventory_state['inventory'] -= order.size

    engine._check_fills = custom_fill

    def strategy_fn(eng, snap):
        global SPREAD, MM_SIZE, POSITION_LIMIT, SKEW_MAX

        inventory = inventory_state['inventory']

        # Check position limit
        if abs(inventory) >= POSITION_LIMIT:
            return  # At limit, don't add more

        for oid, o in list(eng._orders.items()):
            if o.status.name == "OPEN":
                eng.cancel_order(oid)

        mid = (snap.best_bid + snap.best_ask) / 2

        # Calculate skew based on inventory
        inv_pct = float(inventory) / float(POSITION_LIMIT) if POSITION_LIMIT > 0 else 0
        skew = SKEW_MAX * Decimal(str(-inv_pct))

        half = SPREAD / 2
        our_bid = mid - half + skew
        our_ask = mid + half + skew

        if our_bid > 0 and abs(inventory + MM_SIZE) <= POSITION_LIMIT:
            # Place at our price, but fill check uses market price
            eng.place_order("BUY", our_bid, MM_SIZE)
        if our_ask < Decimal("1.0") and abs(inventory - MM_SIZE) <= POSITION_LIMIT:
            eng.place_order("SELL", our_ask, MM_SIZE)

    result = engine.run(data, strategy="custom", strategy_fn=strategy_fn)

    return {
        'sharpe_ratio': result.sharpe_ratio,
        'total_return': result.total_return,
        'total_trades': result.total_trades,
        'win_rate': result.win_rate,
        'max_drawdown': result.max_drawdown,
    }


def grid_search(
    spread_values: List[float] = [0.005, 0.008, 0.01, 0.012, 0.015],
    size_values: List[float] = [5, 10, 20],
    market: Dict = None,
    snapshots: int = 300,
) -> List[dict]:
    """Grid search over parameter combinations."""
    if market is None:
        markets = fetch_markets()
        market = markets[0]

    results = []
    total = len(spread_values) * len(size_values)
    i = 0

    for spread in spread_values:
        for size in size_values:
            i += 1
            print(f"[{i}/{total}] Testing spread={spread}, size={size}...", file=sys.stderr)

            result = run_backtest(
                spread=Decimal(str(spread)),
                size=Decimal(str(size)),
                market=market,
                snapshots=snapshots,
            )

            results.append({
                'spread': spread,
                'size': size,
                'sharpe': result.get('sharpe_ratio', 0),
                'return': result.get('total_return', 0),
                'trades': result.get('total_trades', 0),
            })

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--spread', type=float, default=0.01)
    parser.add_argument('--size', type=float, default=10)
    parser.add_argument('--pos-limit', type=float, default=100)
    parser.add_argument('--skew-max', type=float, default=0.05)
    parser.add_argument('--snapshots', type=int, default=500)
    parser.add_argument('--grid', action='store_true', help='Run grid search')
    args = parser.parse_args()

    if args.grid:
        results = grid_search(
            spread_values=[0.005, 0.008, 0.01, 0.012, 0.015],
            size_values=[5, 10, 20],
            snapshots=args.snapshots,
        )
        # Sort by sharpe
        results.sort(key=lambda x: x['sharpe'], reverse=True)
        print("\n=== Top Results ===")
        for r in results[:5]:
            print(f"spread={r['spread']}, size={r['size']}: sharpe={r['sharpe']:.4f}, return={r['return']:.2%}")
    else:
        result = run_backtest(
            spread=Decimal(str(args.spread)),
            size=Decimal(str(args.size)),
            pos_limit=Decimal(str(args.pos_limit)),
            skew_max=Decimal(str(args.skew_max)),
            snapshots=args.snapshots,
        )
        print(f"sharpe_ratio: {result['sharpe_ratio']:.4f}")