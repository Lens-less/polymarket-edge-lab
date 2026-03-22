# Polymarket Market-Making Bot

A production-grade automated market-making system for [Polymarket](https://polymarket.com) prediction markets.

[![Tests](https://img.shields.io/badge/tests-339%20passing-brightgreen)](tests/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Overview

This bot provides two-sided liquidity on Polymarket prediction markets, capturing spread revenue and liquidity rewards. It features adaptive strategies, comprehensive risk management, and market intelligence capabilities.

### Key Features

- **Adaptive Market Making**: Dynamic spread adjustment based on volatility, inventory, and order flow
- **Alpha Generation**: Arbitrage detection, flow analysis, competitor detection, regime detection
- **Risk Management**: Kelly criterion sizing, adverse selection detection, correlation tracking, kill switches
- **Real-time Data**: WebSocket integration with automatic REST fallback
- **Safety Features**: Cancel-on-disconnect, stale order cleanup, balance monitoring
- **TUI Dashboard**: Rich terminal interface for live monitoring
- **Backtesting**: Historical data replay for strategy validation
- **Telemetry**: Trade logging and latency monitoring

## Quick Start

### Prerequisites

- Python 3.11+
- Polymarket account with API credentials (for live trading)

### Installation

```bash
git clone <repo-url>
cd mm-v2
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
# Edit .env with your settings
```

**Required for live trading:**
- `POLY_PRIVATE_KEY` - Ethereum private key
- `POLY_API_KEY` - API key from Polymarket
- `POLY_API_SECRET` - API secret
- `POLY_PASSPHRASE` - API passphrase

### Running

```bash
# Paper trading (default)
python run_mm.py

# With TUI dashboard
python run_tui.py

# Live trading (requires credentials)
DRY_RUN=false python run_mm.py
```

### Testing

```bash
pytest tests/ -v              # All 339 tests
pytest tests/test_safety.py   # Safety feature tests
pytest tests/test_smart_mm.py # Market maker tests
```

## Architecture

```
src/
├── alpha/                    # Alpha generation signals
│   ├── arbitrage.py          # YES/NO parity arbitrage detection
│   ├── competitors.py        # Competitor detection and response
│   ├── events.py             # Market event tracking (resolution, volume spikes)
│   ├── flow_signals.py       # Order flow analysis and imbalance
│   ├── pair_tracker.py       # Token pair management
│   ├── regime.py             # Liquidity regime detection
│   └── time_patterns.py      # Time-of-day pattern analysis
│
├── backtest/                 # Historical backtesting
│   ├── data.py               # Historical data management
│   └── engine.py             # Backtest execution engine
│
├── feed/                     # Market data infrastructure
│   ├── data_store.py         # Local orderbook storage
│   ├── feed.py               # MarketFeed main class
│   ├── fill_feed.py          # Fill notification handling
│   ├── mock.py               # Mock feed for testing
│   ├── rest_poller.py        # REST API fallback
│   ├── trades_poller.py      # Trade history polling
│   └── websocket_conn.py     # WebSocket connection management
│
├── risk/                     # Risk management system
│   ├── adverse_selection.py  # Toxic flow detection
│   ├── correlation.py        # Cross-market correlation tracking
│   ├── dynamic_limits.py     # Adaptive position limits
│   ├── kelly.py              # Kelly criterion position sizing
│   ├── manager.py            # Central RiskManager class
│   └── market_pnl.py         # Per-market P&L tracking
│
├── strategy/                 # Trading strategies
│   ├── allocator.py          # Capital allocation across markets
│   ├── book_analyzer.py      # Order book analysis and imbalance
│   ├── inventory.py          # Inventory management and skewing
│   ├── maker_checker.py      # Ensure maker-only orders
│   ├── market_maker.py       # SmartMarketMaker main class
│   ├── market_scorer.py      # Market selection scoring
│   ├── parity.py             # YES/NO parity validation
│   ├── partial_fill_handler.py # Partial fill response
│   ├── pool.py               # Multi-market pool management
│   ├── queue_optimizer.py    # Queue position optimization
│   ├── runner.py             # CLI entry point
│   ├── timing.py             # Adaptive loop timing
│   └── volatility.py         # Volatility tracking and spread adjustment
│
├── telemetry/                # Observability
│   ├── latency.py            # Latency monitoring and alerts
│   └── trade_logger.py       # JSON trade logging
│
├── tui/                      # Terminal UI
│   ├── collector.py          # State collection from components
│   ├── renderer.py           # Rich console rendering
│   ├── runner.py             # TUI runner integration
│   └── state.py              # Bot state dataclasses
│
├── auth.py                   # Wallet and credential management
├── client.py                 # CLOB API client wrapper
├── config.py                 # Configuration (~80 environment variables)
├── markets.py                # Gamma API market discovery
├── models.py                 # Core data models
├── orders.py                 # Order queries (open orders, positions, trades)
├── pricing.py                # Order book fetching and pricing
├── rate_limiter.py           # API rate limiting
├── simulator.py              # DRY_RUN order simulation
├── trading.py                # Order placement and cancellation
├── utils.py                  # Logging and utilities
└── websocket_client.py       # Legacy WebSocket client

tests/                        # 339 tests across all modules
run_mm.py                     # Main entry point
run_tui.py                    # TUI entry point
```

## Configuration Reference

### Trading Modes

| Variable | Default | Description |
|----------|---------|-------------|
| `DRY_RUN` | `true` | Paper trading mode (no real orders) |
| `RISK_ENFORCE` | `false` (dry) / `true` (live) | Enforce risk limits or log only |

### Market Making

| Variable | Default | Description |
|----------|---------|-------------|
| `MM_SPREAD` | `0.04` | Base spread (4 cents total) |
| `MM_SIZE` | `10` | Order size in contracts |
| `MM_REQUOTE_THRESHOLD` | `0.03` | Requote when mid moves 3 cents |
| `MM_POSITION_LIMIT` | `50` | Max position before skipping side |
| `SPREAD_MIN` | `0.02` | Minimum spread (2 cents) |
| `SPREAD_MAX` | `0.10` | Maximum spread (10 cents) |
| `INVENTORY_SKEW_MAX` | `0.02` | Max inventory skew (2 cents) |

### Risk Management

| Variable | Default | Description |
|----------|---------|-------------|
| `RISK_MAX_DAILY_LOSS` | `50` | Daily loss limit ($) |
| `RISK_MAX_POSITION` | `100` | Max position per token |
| `RISK_MAX_TOTAL_EXPOSURE` | `500` | Total exposure limit ($) |
| `RISK_ERROR_COOLDOWN` | `60` | Pause seconds after errors |
| `KELLY_FRACTION` | `0.25` | Kelly criterion fraction |
| `ADVERSE_TOXIC_THRESHOLD` | `0.4` | Toxic flow threshold |

### Market Selection

| Variable | Default | Description |
|----------|---------|-------------|
| `MARKET_MIN_VOLUME` | `10000` | Minimum 24h volume ($) |
| `MARKET_MIN_SPREAD` | `0.02` | Minimum spread (too tight = competitive) |
| `MARKET_MAX_SPREAD` | `0.15` | Maximum spread (too wide = illiquid) |
| `MARKET_MIN_HOURS_TO_RESOLUTION` | `12` | Avoid near-resolution markets |
| `MARKET_MIN_PRICE` / `MAX_PRICE` | `0.05` / `0.95` | Avoid extreme prices |

### Alpha Generation

| Variable | Default | Description |
|----------|---------|-------------|
| `ARB_MIN_PROFIT_BPS` | `20` | Minimum arbitrage profit (basis points) |
| `FLOW_WINDOW_SECONDS` | `60` | Order flow analysis window |
| `FLOW_IMBALANCE_THRESHOLD` | `0.15` | Flow imbalance to widen spread |
| `REGIME_WINDOW_SIZE` | `50` | Snapshots for regime detection |

See [.env.example](.env.example) for complete configuration reference.

## Safety Features

The bot includes production safety features:

1. **Startup Cleanup**: Cancels orphaned orders from previous sessions
2. **Cancel-on-Disconnect**: Cancels all orders when WebSocket connection drops
3. **Stale Order Cleanup**: Removes orders older than 5 minutes
4. **Balance Monitoring**: Checks balance before orders, alerts on 20%+ drops
5. **Kill Switch**: Automatic trading halt on excessive errors or losses

## Operating Modes

### DRY_RUN Mode (Paper Trading)

- Uses `OrderSimulator` for order matching
- No real API calls for order placement
- Risk limits logged but not enforced (by default)
- Ideal for strategy testing and data gathering

### LIVE Mode

- Real order placement via authenticated CLOB client
- Risk limits enforced
- Balance and position monitoring
- Requires valid API credentials

## API Integration

### Polymarket APIs

| API | URL | Purpose |
|-----|-----|---------|
| Gamma | `gamma-api.polymarket.com` | Market discovery, metadata |
| CLOB | `clob.polymarket.com` | Order books, trading |
| WebSocket | `ws-subscriptions-clob.polymarket.com` | Real-time updates |

### Rate Limits

- Order placement: 40/s sustained, 240/s burst
- Order cancellation: 40/s sustained, 240/s burst
- Book queries: 200/10s
- Market queries: 125/10s

### Fee Structure

- **0% maker fees** - Keep full spread
- **0% taker fees**
- Liquidity rewards available on eligible markets

## Strategy Details

### Quote Calculation

1. Fetch current mid price from order book
2. Apply volatility multiplier to base spread
3. Apply inventory skew (widen on position buildup)
4. Apply order flow adjustment (widen on aggressive flow)
5. Check parity with complement token (YES + NO should sum to ~1.00)

### Inventory Management

- Track position and VWAP per token
- Skew quotes away from inventory direction
- Reduce size as position approaches limits
- Calculate realized P&L on position changes

### Adaptive Timing

| Mode | Interval | Trigger |
|------|----------|---------|
| Normal | 2.0s | Default state |
| Fast | 0.1s | High volatility or recent activity |
| Sleep | 5.0s | Prolonged inactivity (>60s) |

## Development

### Project Structure

```
thoughts/shared/handoffs/    # Session handoff documents
tests/                       # Test suite (339 tests)
tests/unit/                  # Unit tests for alpha components
```

### Running Specific Tests

```bash
pytest tests/test_phase1.py -v     # Environment/connectivity
pytest tests/test_smart_mm.py -v   # Market maker core
pytest tests/test_safety.py -v     # Safety features
pytest tests/test_arbitrage.py -v  # Arbitrage detection
```

### Code Style

- Decimal for all monetary values (not float)
- Dataclasses for data transfer objects
- Type hints throughout
- Comprehensive docstrings

## Disclaimer

This software is for educational and research purposes. Trading prediction markets involves substantial risk of loss. Always:

- Start with DRY_RUN mode
- Use small position sizes initially
- Monitor actively during live trading
- Never use personal wallet keys (create dedicated trading wallet)

## License

MIT License - See [LICENSE](LICENSE)

## Documentation

For detailed guides, see the `docs/` directory:

- [User Documentation](docs/user/) - Getting started, configuration, safety, FAQ
- [Developer Documentation](docs/developer/) - Architecture, setup, contributing
- [Roadmap](docs/roadmap.md) - Project plans and future features

## Links

- [Polymarket](https://polymarket.com)
- [Polymarket API Docs](https://docs.polymarket.com/)
- [py-clob-client](https://github.com/Polymarket/py-clob-client)
