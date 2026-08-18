# Polymarket MM Bot - Developer Context

This document provides deep technical context for developers working on this codebase.

## System Overview

```
                          ┌─────────────────────────────────────────────────────────┐
                          │                    SmartMarketMaker                      │
                          │  - Quote calculation (spread, skew, size)                │
                          │  - Order lifecycle management                            │
                          │  - Integration point for all signals                     │
                          └───────────────────────┬─────────────────────────────────┘
                                                  │
          ┌───────────────────────────────────────┼───────────────────────────────────────┐
          │                                       │                                       │
          ▼                                       ▼                                       ▼
┌─────────────────────┐              ┌─────────────────────┐              ┌─────────────────────┐
│     MarketFeed      │              │     RiskManager     │              │   Alpha Signals     │
│  - WebSocket data   │              │  - Position limits  │              │  - Arbitrage        │
│  - REST fallback    │              │  - Daily P&L        │              │  - Flow analysis    │
│  - Health tracking  │              │  - Kill switch      │              │  - Competitors      │
└─────────────────────┘              │  - Kelly sizing     │              │  - Regime detection │
                                     │  - Adverse select.  │              └─────────────────────┘
                                     └─────────────────────┘
```

## Core Components

### SmartMarketMaker (`src/strategy/market_maker.py`)

The central strategy class that coordinates all components.

**Key Methods:**
- `run()` - Main async loop, handles signals, manages lifecycle
- `_calculate_quotes()` - Compute bid/ask prices with all adjustments
- `_update_quotes()` - Place/cancel orders as needed
- `_should_requote()` - Determine if quotes need updating

**State (`SmartMMState`):**
```python
@dataclass
class SmartMMState:
    running: bool
    token_id: str
    position: Decimal
    bid_order_id: Optional[str]
    ask_order_id: Optional[str]
    current_bid: Optional[Decimal]
    current_ask: Optional[Decimal]
    last_mid: Optional[Decimal]
    vol_multiplier: float
    inventory_skew: Decimal
    timing_mode: str
```

### MarketFeed (`src/feed/feed.py`)

Provides real-time market data with automatic failover.

**State Machine:**
```
STOPPED → STARTING → RUNNING → ERROR
                  ↑          ↓
                  └──────────┘ (via reset())
```

**Key Properties:**
- `is_healthy` - Safe to trade on this data?
- `data_source` - "websocket", "rest", or "none"
- `state` - FeedState enum

**Usage Pattern:**
```python
feed = MarketFeed()
await feed.start(["token_id_1", "token_id_2"])

if feed.is_healthy:
    mid = feed.get_midpoint("token_id_1")
    # Place quotes...
else:
    # Cancel all quotes, wait for recovery
```

### RiskManager (`src/risk/manager.py`)

Central risk control with multiple sub-systems.

**Sub-components:**
- `DynamicLimitManager` - Adjusts limits based on P&L
- `AdverseSelectionDetector` - Tracks toxic flow
- `KellyCalculator` - Optimal position sizing
- `CorrelationTracker` - Cross-market risk

**Key Methods:**
```python
risk = get_risk_manager()

# Pre-trade check
status = risk.check()
if status != RiskStatus.OK:
    # Log and potentially skip order

# Record activity
risk.record_trade(token_id, side, size, price)
risk.record_error()

# Emergency controls
risk.kill_switch()  # Halt all trading
risk.is_killed      # Check kill status
```

### Order Simulator (`src/simulator.py`)

Simulates order matching in DRY_RUN mode.

**Behavior:**
- Maintains virtual order book per token
- Matches orders based on price/time priority
- Simulates partial fills
- Tracks virtual positions and P&L

**Usage:**
```python
sim = get_simulator()

# Place simulated order
order = sim.place_order(token_id, side, price, size)

# Check for fills
fills = sim.process_fills(token_id, current_book)

# Query state
position = sim.get_position(token_id)
orders = sim.get_open_orders(token_id)
```

## Alpha Generation

### ArbitrageDetector (`src/alpha/arbitrage.py`)

Detects YES/NO parity mispricing.

**Signal:**
```python
@dataclass
class ArbitrageSignal:
    type: ArbitrageType  # PARITY or NONE
    profit_bps: int
    direction: str  # "buy_yes" or "buy_no"
    is_actionable: bool
```

**Usage:**
```python
detector = ArbitrageDetector(min_profit_bps=ARB_MIN_PROFIT_BPS)
detector.register_pair(yes_token, no_token, "market-slug")

signal = detector.check_pair(yes_mid, no_mid, "market-slug")
if signal.is_actionable:
    # Adjust quotes or execute arb
```

### FlowAnalyzer (`src/alpha/flow_signals.py`)

Analyzes order flow for informed trading detection.

**Signal:**
```python
class FlowSignal(Enum):
    NEUTRAL = "neutral"
    BULLISH = "bullish"   # Aggressive buying
    BEARISH = "bearish"   # Aggressive selling
```

**Usage:**
```python
analyzer = FlowAnalyzer(window_seconds=60)
analyzer.record_trade(side="buy", size=100, is_aggressive=True)

state = analyzer.get_state()
# state.signal, state.imbalance_ratio, state.recommended_skew
```

### CompetitorDetector (`src/alpha/competitors.py`)

Detects competitor market makers from order patterns.

**Response:**
```python
@dataclass
class StrategyResponse:
    should_back_off: bool
    spread_multiplier: float
    size_multiplier: float
    reason: str
```

### RegimeDetector (`src/alpha/regime.py`)

Classifies market liquidity conditions.

**Regimes:**
- `HIGH_LIQUIDITY` - Tight spreads, deep books
- `NORMAL` - Typical conditions
- `LOW_LIQUIDITY` - Wide spreads, thin books
- `CRISIS` - Severely stressed markets

## Data Models

### Core Models (`src/models.py`)

```python
@dataclass
class PriceLevel:
    price: float
    size: float

@dataclass
class OrderBook:
    token_id: str
    bids: List[PriceLevel]
    asks: List[PriceLevel]
    timestamp: Optional[str] = None

    @property
    def best_bid(self) -> Optional[float]
    @property
    def best_ask(self) -> Optional[float]
    @property
    def midpoint(self) -> Optional[float]
    @property
    def spread(self) -> Optional[float]

@dataclass
class Order:
    id: str
    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    status: OrderStatus
    filled_size: Decimal = Decimal("0")
    created_at: Optional[str] = None

@dataclass
class Market:
    id: str
    question: str
    condition_id: str
    slug: str
    status: str
    outcomes: List[Outcome]
    token_ids: List[str]
    end_date: Optional[str] = None
    volume: float = 0.0
    liquidity: float = 0.0
```

## Configuration System

All configuration via environment variables in `src/config.py`.

**Categories:**
1. **Network**: `CHAIN_ID`, API URLs
2. **Authentication**: `POLY_*` credentials
3. **WebSocket**: Reconnection, heartbeat settings
4. **Trading**: `DRY_RUN`, position limits, order sizes
5. **Market Making**: Spread, requote threshold, timing
6. **Market Selection**: Volume, spread, price filters
7. **Alpha**: Arbitrage, flow, competitor, regime settings
8. **Risk**: Loss limits, Kelly, adverse selection

**Pattern:**
```python
# Decimal for money
RISK_MAX_DAILY_LOSS = Decimal(os.getenv("RISK_MAX_DAILY_LOSS", "50"))

# Float for multipliers/thresholds
VOL_MULT_MAX = float(os.getenv("VOL_MULT_MAX", "3.0"))

# Int for counts
REGIME_WINDOW_SIZE = int(os.getenv("REGIME_WINDOW_SIZE", "50"))
```

## API Integration

### Gamma API (Market Discovery)

```python
from src.markets import fetch_active_markets, fetch_events

markets = fetch_active_markets(limit=100)
events = fetch_events(limit=50)
```

### CLOB API (Trading)

```python
from src.client import get_client, get_auth_client
from src.pricing import get_order_book, get_midpoint
from src.orders import get_open_orders, get_position
from src.trading import place_order, cancel_order

# Read-only (no credentials needed)
client = get_client()
book = get_order_book(token_id)

# Authenticated (requires credentials)
auth_client = get_auth_client()
orders = get_open_orders(token_id)
```

### WebSocket

```python
from src.feed import MarketFeed, FeedState

feed = MarketFeed()
feed.on_book_update = lambda token, book: print(f"Book update: {token}")
feed.on_price_change = lambda token, mid: print(f"Price: {mid}")

await feed.start(["token1", "token2"])
# Feed handles reconnection, failover automatically
await feed.stop()
```

## Testing Patterns

### Unit Tests

```python
# Test with mock feed
from src.feed.mock import MockFeed

feed = MockFeed()
feed.set_midpoint("token", Decimal("0.50"))
feed.set_health(True)

# Test risk manager
from src.risk import get_risk_manager, reset_risk_manager

def test_something():
    reset_risk_manager()  # Fresh instance
    risk = get_risk_manager()
    # ...
```

### Integration Tests

```python
# Test with simulator
from src.simulator import get_simulator, reset_simulator

def test_order_flow():
    reset_simulator()
    sim = get_simulator()

    order = sim.place_order(token_id, OrderSide.BUY, Decimal("0.50"), Decimal("10"))
    assert order is not None
```

### Running Tests

```bash
# All tests
pytest tests/ -v

# Specific module
pytest tests/test_smart_mm.py -v

# With coverage
pytest --cov=src tests/

# Skip slow tests
pytest tests/ -v -m "not slow"
```

## Common Workflows

### Adding a New Alpha Signal

1. Create signal class in `src/alpha/`:
   ```python
   @dataclass
   class MySignal:
       value: float
       confidence: float
       direction: str

   class MyAnalyzer:
       def __init__(self, window_size: int):
           self._data = deque(maxlen=window_size)

       def record(self, value: float):
           self._data.append(value)

       def get_signal(self) -> MySignal:
           # Analysis logic
           pass
   ```

2. Add config variables to `src/config.py`
3. Integrate into SmartMarketMaker's `_calculate_quotes()`
4. Add tests in `tests/test_my_signal.py`
5. Export from `src/alpha/__init__.py`

### Adding a Risk Check

1. Add to `RiskManager._run_checks()`:
   ```python
   def _check_my_condition(self) -> bool:
       """Returns True if risk is OK."""
       if some_condition:
           self._log_risk_event("MY_CHECK", {"details": "..."})
           return False
       return True
   ```

2. Add config threshold to `src/config.py`
3. Add tests

### Modifying Quote Logic

1. All quote calculation in `SmartMarketMaker._calculate_quotes()`
2. Adjustment factors multiply the base spread:
   ```python
   effective_spread = (
       SPREAD_BASE
       * vol_multiplier
       * regime_multiplier
       * competitor_multiplier
   )
   ```
3. Skew shifts the midpoint:
   ```python
   adjusted_mid = mid + inventory_skew + flow_skew
   ```

## Safety Checklist (Live Trading)

Before going live:

- [ ] Test in DRY_RUN with real market data
- [ ] Verify kill switch works (`risk.kill_switch()`)
- [ ] Confirm cancel-on-disconnect fires
- [ ] Check stale order cleanup runs
- [ ] Verify balance monitoring alerts work
- [ ] Start with small `MM_SIZE` and `RISK_MAX_POSITION`
- [ ] Monitor first 24h actively
- [ ] Have manual cancel script ready

## Troubleshooting

### Feed not connecting
- Check `WS_MARKET_URL` is correct
- Verify network connectivity
- Check for rate limiting (429 errors)

### Orders not filling (DRY_RUN)
- Simulator requires price to cross for fills
- Check `sim.process_fills()` is being called

### Orders rejected (LIVE)
- Verify credentials are set
- Check balance/allowance
- Verify tick size (0.01 increments)
- Check position limits

### High adverse selection
- Flow may be informed, widen spread
- Check `ADVERSE_TOXIC_THRESHOLD` setting
- Consider backing off market

## Key Files Reference

| File | Purpose |
|------|---------|
| `run_mm.py` | Main entry point |
| `run_tui.py` | TUI entry point |
| `src/config.py` | All configuration |
| `src/strategy/market_maker.py` | Core strategy |
| `src/strategy/runner.py` | CLI runner with market selection |
| `src/risk/manager.py` | Central risk control |
| `src/feed/feed.py` | Market data feed |
| `src/simulator.py` | DRY_RUN order simulation |
| `src/trading.py` | Order placement |
| `src/orders.py` | Order queries |

## Dependencies

```
polymarket-client # Official unified Polymarket API SDK
python-dotenv     # Environment variables
websockets        # WebSocket connections
pandas            # Data analysis
pytest            # Testing
pytest-asyncio    # Async test support
rich              # TUI rendering
requests          # HTTP client
numpy             # Correlation calculations
```

---

*Last Updated: 2026-01-22*
*Tests: 339 passing*
