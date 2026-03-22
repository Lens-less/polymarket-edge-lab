# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Polymarket Market-Making Bot - a production-grade automated market-making system for Polymarket prediction markets. Provides two-sided liquidity on YES/NO prediction markets, capturing spread revenue and liquidity rewards.

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run in paper trading mode (default)
python run_mm.py

# Run with TUI dashboard
python run_tui.py

# Run in live trading mode (requires API credentials)
DRY_RUN=false python run_mm.py

# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_smart_mm.py -v
pytest tests/test_safety.py -v

# Run backtest
python scripts/run_backtest.py
```

## Architecture

The codebase is organized into functional modules:

- **src/strategy/** - Core market-making logic: `SmartMarketMaker` class manages the main trading loop, quote calculation, inventory skew, and order placement
- **src/feed/** - Market data infrastructure: WebSocket + REST fallback for order book and trade data via `MarketFeed`
- **src/risk/** - Risk management: `RiskManager` enforces position limits, Kelly sizing, adverse selection detection, and kill switches
- **src/alpha/** - Alpha generation: arbitrage detection, competitor analysis, order flow signals, regime detection
- **src/backtest/** - Historical backtesting engine for strategy validation
- **src/tui/** - Rich terminal dashboard for live monitoring
- **src/telemetry/** - Trade logging and latency monitoring

Entry points: `run_mm.py` (CLI), `run_tui.py` (dashboard)

### Data Flow

1. `MarketFeed` subscribes to WebSocket for real-time order book updates
2. `SmartMarketMaker` calculates quotes using volatility, inventory, and flow signals
3. `RiskManager` validates each quote against position limits and kill switches
4. Orders placed via `Client` (CLOB API) or simulated in DRY_RUN mode
5. Fills processed by `PartialFillHandler` with inventory updates
6. `TradeLogger` records all activity for analysis

## Key Patterns

- **Decimal type** for all monetary values (not float)
- **Dataclasses** for data transfer objects
- **Type hints** throughout
- **py-clob-client** for Polymarket CLOB API integration
- **WebSocket with REST fallback** for data resilience
- **DRY_RUN mode** uses `OrderSimulator` for paper trading without real orders

## Configuration

Configuration via ~80 environment variables in `.env`. Key ones:
- `DRY_RUN=true` - Paper trading (default)
- `MM_SPREAD=0.04` - Base spread (4 cents)
- `MM_SIZE=10` - Order size in contracts
- `RISK_MAX_POSITION=100` - Max position per token

See `.env.example` for complete reference.

## Safety Features

The bot includes production safety features:
- Startup cleanup of orphaned orders
- Cancel-on-disconnect when WebSocket drops
- Stale order cleanup (5 min timeout)
- Balance monitoring with alerts
- Kill switch on excessive errors/losses

## Additional Documentation

- [User Documentation](docs/user/) - For non-technical users (getting started, configuration, safety, FAQ)
- [Developer Documentation](docs/developer/) - For contributors (architecture, setup, contributing)
- [Roadmap](docs/roadmap.md) - Project plans and future features