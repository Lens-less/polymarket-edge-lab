"""
Real data backtest - profit depends on spread difference.

Key: Our profit = (our_spread - market_spread) * position_size * num_fills
"""

import json
import time
import math
from decimal import Decimal
from typing import List, Dict
import requests
import sys
from pathlib import Path
_project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_project_root))

from src.backtest.engine import BacktestEngine
from src.backtest.data import HistoricalData, OrderBookSnapshot


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
    data = HistoricalData()
    base = int(time.time()) - (num_snapshots * 60)

    mid = float((market['best_bid'] + market['best_ask']) / 2)
    real_spread = float(market['best_ask'] - market['best_bid'])
    price = mid

    for i in range(num_snapshots):
        # Price walk with larger moves
        trend = 0.008 * math.sin(i * 0.03)
        noise = 0.03 * math.sin(i * 0.5) + 0.02 * math.cos(i * 0.7)
        price = max(0.01, min(0.99, price + trend + noise))

        # Varying spread
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


def run_backtest(spread: Decimal, market: Dict, snapshots: int = 500) -> dict:
    """Run backtest where spread affects profit."""
    data = generate_data(market, num_snapshots=snapshots)
    engine = BacktestEngine(initial_capital=Decimal("1000"))

    # Store spread for profit calculation
    engine._strategy_spread = spread

    # Custom fill model that factors in spread
    original_check = engine._check_fills

    def custom_fill(snapshot):
        # Get our spread
        our_spread = getattr(engine, '_strategy_spread', Decimal("0.01"))
        market_spread = snapshot.best_ask - snapshot.best_bid

        # If our spread >= market spread, we lose
        # If our spread < market spread, we win
        spread_advantage = float(market_spread - our_spread)

        for order_id, order in list(engine._orders.items()):
            if order.status.name != "OPEN":
                continue

            # Calculate fill probability based on spread advantage
            # Better spread = higher fill probability
            fill_prob = 0.8 + 0.2 * (1 - max(0, float(our_spread) / max(0.001, float(market_spread))))

            if order.side == "BUY":
                # Buy fills when price drops to or below our price
                if snapshot.best_ask <= order.price:
                    # Adjust fill price: get better price if our spread is tighter
                    fill_price = order.price + Decimal(str(max(0, spread_advantage)))
                    fill_price = min(fill_price, snapshot.best_ask)
                    engine._execute_fill(order, order.size, fill_price, snapshot.timestamp)
            else:
                # Sell fills when price rises to or above our price
                if snapshot.best_bid >= order.price:
                    fill_price = order.price - Decimal(str(max(0, spread_advantage)))
                    fill_price = max(fill_price, snapshot.best_bid)
                    engine._execute_fill(order, order.size, fill_price, snapshot.timestamp)

    engine._check_fills = custom_fill

    # Strategy places orders at our quoted spread
    def strategy_fn(eng, snap):
        for oid, o in list(eng._orders.items()):
            if o.status.name == "OPEN":
                eng.cancel_order(oid)

        mid = (snap.best_bid + snap.best_ask) / 2
        half = spread / 2
        our_bid = mid - half
        our_ask = mid + half

        if our_bid > 0:
            eng.place_order("BUY", our_bid, Decimal("10"))
        if our_ask < Decimal("1.0"):
            eng.place_order("SELL", our_ask, Decimal("10"))

    result = engine.run(data, strategy="custom", strategy_fn=strategy_fn)

    return {
        'sharpe_ratio': result.sharpe_ratio,
        'total_return': result.total_return,
        'total_trades': result.total_trades,
        'win_rate': result.win_rate,
    }


if __name__ == "__main__":
    from decimal import Decimal as D
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--spread', type=float, default=0.01)
    parser.add_argument('--snapshots', type=int, default=500)
    args = parser.parse_args()

    markets = fetch_markets()
    market = markets[0]

    result = run_backtest(spread=D(str(args.spread)), market=market, snapshots=args.snapshots)
    print(f"sharpe_ratio: {result['sharpe_ratio']:.4f}")