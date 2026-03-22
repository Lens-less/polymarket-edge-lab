# Documentation System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a comprehensive Markdown-based documentation system for terminal users, including 10 documents covering getting started, configuration, trading modes, safety, FAQ, and roadmap.

**Language:** Chinese (Simplified) with English technical terms explained in Chinese.

**Architecture:** Simple Markdown file structure under docs/ directory, organized by user/developer categories. Extends existing README.md and PROJECT_CONTEXT.md rather than replacing them.

**Tech Stack:** Markdown, Git

---

## File Structure

```
docs/
├── index.md                  # Documentation entry point
├── user/
│   ├── getting-started.md   # Step-by-step guide
│   ├── configuration.md      # Configuration by scenario
│   ├── trading-modes.md     # DRY_RUN vs LIVE
│   ├── safety.md            # Safety warnings
│   └── faq.md               # Frequently asked questions
├── developer/
│   ├── architecture.md      # Architecture overview
│   ├── setup.md             # Dev environment setup
│   └── contributing.md     # Contribution guide
└── roadmap.md               # Project roadmap
```

---

## Implementation Tasks

### Task 1: Create docs directory structure and index.md

**Files:**
- Create: `docs/index.md`
- Create: `docs/user/getting-started.md`
- Create: `docs/user/configuration.md`
- Create: `docs/user/trading-modes.md`
- Create: `docs/user/safety.md`
- Create: `docs/user/faq.md`
- Create: `docs/developer/architecture.md`
- Create: `docs/developer/setup.md`
- Create: `docs/developer/contributing.md`
- Create: `docs/roadmap.md`

- [ ] **Step 1: Create docs/index.md**

```markdown
# Polymarket MM Bot Documentation

Welcome to the Polymarket Market-Making Bot documentation.

## Quick Links

- [Getting Started](user/getting-started.md) - Start here if you're new
- [Configuration Guide](user/configuration.md) - Configure for your account
- [Trading Modes](user/trading-modes.md) - DRY_RUN vs LIVE explained
- [Safety](user/safety.md) - Important safety warnings
- [FAQ](user/faq.md) - Common questions answered

## Additional Resources

- [GitHub Repository](https://github.com/your-repo)
- [Polymarket](https://polymarket.com)
- [Polymarket API Docs](https://docs.polymarket.com/)

---

*Version: 1.0.0 | Last Updated: 2026-03-23*
```

- [ ] **Step 2: Commit**

```bash
git add docs/index.md
git commit -m "docs: Add docs directory structure and index.md"
```

---

### Task 2: Create user/getting-started.md

- [ ] **Step 1: Write docs/user/getting-started.md**

```markdown
# Getting Started with Polymarket MM Bot

This guide walks you through setting up and running the bot for the first time.

## Prerequisites

Before you begin, you'll need:

1. **Python 3.11 or higher**
   - Download from python.org
   - Verify installation: `python3 --version`

2. **Polymarket Account** (for live trading)
   - Create at polymarket.com
   - API credentials required for LIVE mode

## Installation

### 1. Clone the Repository

```bash
git clone <repository-url>
cd polymarket-mm-bot
```

### 2. Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate  # macOS/Linux
# venv\Scripts\activate   # Windows
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

```bash
cp .env.example .env
```

Open `.env` in your text editor. The default settings work for initial testing.

## Running in DRY_RUN Mode (Paper Trading)

Start with DRY_RUN mode to test the bot without risking real money:

```bash
python run_mm.py
```

You should see output like:
```
INFO: Starting SmartMarketMaker in DRY_RUN mode
INFO: Connected to market feed
INFO: Placing quotes on [token_id]...
```

**Congratulations!** Your bot is now running in paper trading mode.

## Verifying the Bot is Running

- Check console output for trading activity
- Look for order placement messages
- Monitor for fill notifications

## Next Steps

1. **Read Configuration Guide** - Customize settings for your account
2. **Understand Trading Modes** - Learn when to use DRY_RUN vs LIVE
3. **Review Safety Guidelines** - Important risk warnings before live trading

## Troubleshooting

### "Module not found" error
- Solution: Run `pip install -r requirements.txt` again

### "No markets found"
- Solution: Check internet connection, wait for market data

### Bot not placing orders
- Solution: Verify DRY_RUN=true in .env

## Moving to Live Trading

Once you're comfortable with DRY_RUN mode:

1. Read [Trading Modes](trading-modes.md)
2. Review [Safety Guidelines](safety.md)
3. Configure API credentials in .env
4. Start with small position sizes

---

*See also: [Configuration Guide](configuration.md) | [FAQ](faq.md)*
```

- [ ] **Step 2: Commit**

```bash
git add docs/user/getting-started.md
git commit -m "docs: Add getting started guide for new users"
```

---

### Task 3: Create user/configuration.md

- [ ] **Step 1: Write docs/user/configuration.md**

```markdown
# Configuration Guide

This guide explains how to configure the bot for different account sizes and risk tolerances.

## Understanding Configuration

The bot uses environment variables in the `.env` file. Each setting controls a specific behavior.

## Quick Configuration by Account Size

### Small Account ($100 - $500)

Best for testing and learning. Conservative settings:

```bash
# Trading
DRY_RUN=true
MM_SIZE=5
MM_SPREAD=0.05
MM_POSITION_LIMIT=20
RISK_MAX_POSITION=20
RISK_MAX_TOTAL_EXPOSURE=100
RISK_MAX_DAILY_LOSS=10

# Market filters
MARKET_MIN_VOLUME=5000
MARKET_MIN_HOURS_TO_RESOLUTION=24
```

**Rationale:** Smaller positions limit potential losses while you learn.

### Medium Account ($500 - $5000)

Balanced risk and reward:

```bash
# Trading
DRY_RUN=true
MM_SIZE=10
MM_SPREAD=0.04
MM_POSITION_LIMIT=50
RISK_MAX_POSITION=50
RISK_MAX_TOTAL_EXPOSURE=500
RISK_MAX_DAILY_LOSS=25

# Market filters
MARKET_MIN_VOLUME=10000
MARKET_MIN_HOURS_TO_RESOLUTION=12
```

### Large Account ($5000+)

Higher capacity, but requires careful monitoring:

```bash
# Trading
DRY_RUN=true
MM_SIZE=25
MM_SPREAD=0.03
MM_POSITION_LIMIT=100
RISK_MAX_POSITION=100
RISK_MAX_TOTAL_EXPOSURE=2000
RISK_MAX_DAILY_LOSS=100

# Market filters
MARKET_MIN_VOLUME=25000
MARKET_MIN_HOURS_TO_RESOLUTION=6
```

## Key Settings Explained

### Trading Parameters

| Setting | What It Does | Recommended Range |
|---------|--------------|-------------------|
| MM_SPREAD | Bid-ask spread (in dollars) | 0.03 - 0.10 |
| MM_SIZE | Order size in contracts | 5 - 50 |
| MM_POSITION_LIMIT | Max position per token before pausing | 20 - 100 |

### Risk Management

| Setting | What It Does | Recommended Range |
|---------|--------------|-------------------|
| RISK_MAX_POSITION | Hard limit per token | 20 - 100 |
| RISK_MAX_TOTAL_EXPOSURE | Total portfolio limit | 100 - 5000 |
| RISK_MAX_DAILY_LOSS | Pause trading after daily loss | 10 - 100 |

### Market Selection

| Setting | What It Does | Recommended Range |
|---------|--------------|-------------------|
| MARKET_MIN_VOLUME | Minimum 24h volume | 5000 - 25000 |
| MARKET_MIN_SPREAD | Avoid too competitive markets | 0.02 - 0.05 |
| MARKET_MAX_SPREAD | Avoid too illiquid markets | 0.10 - 0.20 |

## Common Configurations

### Aggressive (Higher Returns, Higher Risk)

```bash
MM_SPREAD=0.03
MM_SIZE=20
KELLY_FRACTION=0.35
RISK_MAX_POSITION=80
```

### Conservative (Lower Returns, Lower Risk)

```bash
MM_SPREAD=0.06
MM_SIZE=5
KELLY_FRACTION=0.15
RISK_MAX_POSITION=15
```

## Testing Configuration Changes

1. Make changes in `.env`
2. Restart the bot
3. Monitor in DRY_RUN mode for 24+ hours
4. Check P&L and position sizes

## Advanced Settings

These require understanding of market making:

- `KELLY_FRACTION` - Kelly criterion for position sizing
- `VOL_MULT_MAX` - Maximum volatility multiplier
- `ADVERSE_TOXIC_THRESHOLD` - Adverse selection detection

---

*See also: [Getting Started](getting-started.md) | [Trading Modes](trading-modes.md)*
```

- [ ] **Step 2: Commit**

```bash
git add docs/user/configuration.md
git commit -m "docs: Add configuration guide by account size"
```

---

### Task 4: Create user/trading-modes.md

- [ ] **Step 1: Write docs/user/trading-modes.md**

```markdown
# Trading Modes: DRY_RUN vs LIVE

Understanding when and how to use each trading mode is critical for safe operation.

## DRY_RUN Mode (Paper Trading)

DRY_RUN mode simulates trading without placing real orders. Use this for:

- Initial testing and learning
- Strategy validation
- Configuration tuning
- Collecting market data

### How It Works

1. Bot fetches real market data (prices, order book)
2. Simulates order placement and fills locally
3. Tracks virtual P&L and positions
4. No real money changes hands

### Pros
- Zero financial risk
- Test at any time
- Experiment freely
- Learn the system

### Cons
- Simulation may not reflect actual fill rates
- No real slippage testing
- No liquidity rewards

### When to Use
- First 1-2 weeks of operation
- After any configuration change
- When testing new markets

## LIVE Mode (Real Trading)

LIVE mode places actual orders on Polymarket. Use this for:

- Production trading
- Earning real profits
- Liquidity rewards

### Prerequisites

Before enabling LIVE mode:

1. **API Credentials Required:**
   ```bash
   POLY_PRIVATE_KEY=0x...
   POLY_API_KEY=...
   POLY_API_SECRET=...
   POLY_PASSPHRASE=...
   ```

2. **Dedicated Trading Wallet**
   - Create a new wallet for trading
   - Never use your main wallet
   - Transfer only what you can afford to lose

3. **Sufficient Balance**
   - USDC or ETH for gas
   - Enough to cover desired position sizes

### How to Switch to LIVE

**Option 1: Environment Variable**
```bash
DRY_RUN=false python run_mm.py
```

**Option 2: Edit .env**
```bash
DRY_RUN=false
```

**Then restart the bot.**

### Pros
- Real profits
- Liquidity rewards
- Actual market experience

### Cons
- Real financial risk
- Requires careful monitoring
- Must manage API keys securely

### When to Use
- After 1+ week of successful DRY_RUN
- When you understand the risks
- When you can afford potential losses

## Switching Between Modes

### From DRY_RUN to LIVE

1. Complete thorough DRY_RUN testing (1+ week)
2. Review safety guidelines
3. Configure API credentials
4. Start with small position sizes
5. Monitor actively for first 24 hours

### From LIVE to DRY_RUN

1. Cancel all open orders first
2. Wait for pending fills
3. Set DRY_RUN=true
4. Restart bot

## Monitoring Your Bot

### DRY_RUN Monitoring

- Check console output
- Review simulated P&L
- Verify order placement
- Monitor for errors

### LIVE Monitoring (Critical)

- **Active monitoring required** - Check every 1-2 hours minimum
- Monitor open orders
- Watch positions and P&L
- Set up alerts for:
  - Large position changes
  - Daily loss threshold
  - Disconnection events

### TUI Dashboard

Use the terminal UI for live monitoring:

```bash
python run_tui.py
```

Shows:
- Current positions
- Open orders
- P&L
- Market data
- System status

## Best Practices

### Start in DRY_RUN
- Don't skip this step
- Test for at least 1 week
- Test across different market conditions

### Gradual Transition
- Start with minimal capital
- Small position sizes
- Low risk tolerance settings

### Active Monitoring (LIVE)
- Check every 1-2 hours minimum
- More frequently during volatile markets
- Have emergency procedures ready

### Emergency Procedures

If something goes wrong:

1. **Press Ctrl+C** to stop the bot
2. Orders will remain open
3. Manually cancel via Polymarket interface
4. Review logs for issues

---

*See also: [Getting Started](getting-started.md) | [Safety](safety.md)*
```

- [ ] **Step 2: Commit**

```bash
git add docs/user/trading-modes.md
git commit -m "docs: Add trading modes documentation"
```

---

### Task 5: Create user/safety.md

- [ ] **Step 1: Write docs/user/safety.md**

```markdown
# Safety Guidelines

**⚠️ WARNING: Trading prediction markets involves substantial risk of loss.**

This software is for educational and research purposes. Never trade more than you can afford to lose.

---

## Critical Safety Rules

### 1. Never Use Personal Wallet Keys

**This is the most important rule.**

- Create a dedicated trading wallet
- Never use your main wallet with personal funds
- Transfer only what you're willing to lose
- Keep your trading wallet separate

```bash
# WRONG - Never do this
POLY_PRIVATE_KEY=0xYourMainWalletPrivateKey

# CORRECT - Use a dedicated trading wallet
POLY_PRIVATE_KEY=0xDedicatedTradingWalletPrivateKey
```

### 2. Start Small

- Begin with minimal position sizes
- Increase gradually after proving the system
- Use conservative settings initially

**Suggested Starting Values:**
```bash
MM_SIZE=5
RISK_MAX_POSITION=20
RISK_MAX_DAILY_LOSS=10
```

### 3. Monitor Actively at First

When starting live trading:

- Check every 30 minutes for the first few hours
- Check every 1-2 hours thereafter
- More frequently during volatile markets

### 4. Use DRY_RUN First

- Test thoroughly in DRY_RUN mode
- Wait at least 1 week before going live
- Validate across different market conditions

---

## Automatic Safety Features

The bot includes several automatic protections:

### Kill Switch

Automatically halts trading when:
- Daily loss exceeds `RISK_MAX_DAILY_LOSS`
- Too many consecutive errors occur
- Position limit breached

### Balance Monitoring

- Checks balance before placing orders
- Alerts if balance drops 20%+ unexpectedly
- Helps detect unauthorized access or errors

### Cancel-on-Disconnect

- Cancels all orders when WebSocket disconnects
- Prevents stale orders in volatile markets
- Requires manual restart to resume

### Stale Order Cleanup

- Removes orders older than 5 minutes
- Prevents zombie orders
- Helps maintain accurate positions

---

## Emergency Procedures

### Bot Not Responding

1. Press `Ctrl+C` to stop the bot
2. Manually cancel orders at polymarket.com
3. Review logs for errors
4. Fix issues and restart

### Unexpected Losses

1. Stop the bot immediately
2. Cancel all orders
3. Check `RISK_MAX_DAILY_LOSS` - bot should have paused
4. Review recent activity
5. Adjust configuration before restarting

### WebSocket Disconnection

- Bot automatically cancels orders
- Wait for reconnection (usually 10-30 seconds)
- If prolonged, restart manually

### Suspected Unauthorized Access

1. Revoke API keys immediately
2. Transfer funds from trading wallet
3. Check transaction history
4. Create new API keys if needed

---

## Risk Settings Reference

### Conservative (Recommended for Beginners)

```bash
RISK_MAX_DAILY_LOSS=10
RISK_MAX_POSITION=20
RISK_MAX_TOTAL_EXPOSURE=100
KELLY_FRACTION=0.15
```

### Moderate

```bash
RISK_MAX_DAILY_LOSS=25
RISK_MAX_POSITION=50
RISK_MAX_TOTAL_EXPOSURE=500
KELLY_FRACTION=0.25
```

### Aggressive (Experienced Only)

```bash
RISK_MAX_DAILY_LOSS=50
RISK_MAX_POSITION=100
RISK_MAX_TOTAL_EXPOSURE=1000
KELLY_FRACTION=0.35
```

---

## What to Do If Things Go Wrong

1. **Stay calm** - Don't make hasty decisions
2. **Stop the bot** - Cancel all orders first
3. **Assess the situation** - Check positions, P&L, logs
4. **Limit losses** - Close positions if necessary
5. **Learn and adjust** - Review what went wrong
6. **Start again small** - Don't revenge trade

---

## Disclaimer

THIS SOFTWARE IS PROVIDED FOR EDUCATIONAL AND RESEARCH PURPOSES.

Trading prediction markets involves substantial risk of loss. Past performance does not guarantee future results.

Always:
- Start with DRY_RUN mode
- Use small position sizes
- Monitor actively during live trading
- Never invest more than you can afford to lose
- Understand the risks before trading live

---

*See also: [Getting Started](getting-started.md) | [Trading Modes](trading-modes.md) | [FAQ](faq.md)*
```

- [ ] **Step 2: Commit**

```bash
git add docs/user/safety.md
git commit -m "docs: Add safety guidelines"
```

---

### Task 6: Create user/faq.md

- [ ] **Step 1: Write docs/user/faq.md**

```markdown
# Frequently Asked Questions

Common questions and answers about the Polymarket MM Bot.

---

## General Questions

### What does this bot do?

The bot provides liquidity to Polymarket prediction markets by placing both buy and sell orders. It earns money from the spread (difference between buy and sell prices) and liquidity rewards.

### Is this legal?

Yes, market making on prediction markets is legal. This software is for educational and research purposes.

### How much money can I make?

There's no guarantee. Results depend on:
- Market conditions
- Configuration settings
- Position sizes
- Market volatility

Past performance does not guarantee future results.

---

## Getting Started

### What do I need to start?

1. Python 3.11+
2. A computer with internet access
3. For LIVE trading: Polymarket account + API credentials

### How long does setup take?

About 10-15 minutes for initial setup. Plan to spend 1-2 weeks testing in DRY_RUN before live trading.

### Can I run this on a server?

Yes. The bot runs on any computer with Python 3.11+. Many users run it on cloud servers (AWS, DigitalOcean, etc.).

### Does the bot need to run 24/7?

Ideally yes, but it's not required. The bot:
- Checks markets every few seconds
- Can be stopped and started safely
- Will cancel orders on shutdown

---

## Configuration

### What settings should I start with?

See our [Configuration Guide](configuration.md). Start with small account settings and conservative values.

### What's the difference between MM_SIZE and RISK_MAX_POSITION?

- `MM_SIZE` - Size of each new order
- `RISK_MAX_POSITION` - Maximum total position in one direction

Example: If MM_SIZE=10 and RISK_MAX_POSITION=50, you could have up to 5 orders before hitting the limit.

### How do I know if my settings are good?

Test in DRY_RUN mode for at least 1 week. Monitor:
- P&L (should be positive)
- Fill rates (should be reasonable)
- Position sizes (shouldn't hit limits frequently)

---

## Trading

### Why aren't my orders filling?

Possible reasons:
1. Spread too wide - Other makers have better prices
2. Market too competitive - Consider different markets
3. Size too small - Increase MM_SIZE
4. Price at extremes - Orders near 0 or 1 fill less

### Why is the bot quoting on only one side?

The bot skips quoting on one side when you have a large position in that direction. This is intentional - it prevents overexposure.

### What markets should I trade?

Look for:
- High volume ($10,000+ daily)
- Moderate spread (3-10 cents)
- At least 12 hours to resolution

Avoid:
- Very tight spreads (too competitive)
- Very wide spreads (no liquidity)
- Markets about to resolve

### How do I switch between DRY_RUN and LIVE?

Edit your `.env` file:
```bash
DRY_RUN=true   # Paper trading
DRY_RUN=false  # Real trading
```

Then restart the bot.

---

## Troubleshooting

### "Connection refused" error

- Check internet connection
- Verify Polymarket API is operational
- Try again in a few minutes

### "Insufficient balance" error

- Check your USDC/ETH balance
- For DRY_RUN: reset the simulator
- For LIVE: add funds to trading wallet

### Bot not placing orders

Check:
1. Is the market feed connected?
2. Are there active markets?
3. Is DRY_RUN set correctly?
4. Check logs for errors

### High losses in DRY_RUN

This can happen. Options:
- Wait for market to move back
- Cancel orders and try different markets
- Adjust spread settings

### High losses in LIVE

1. Stop the bot immediately
2. Cancel all orders
3. Review logs
4. Check if kill switch triggered

---

## Security

### Is it safe to enter my API keys?

Yes, but:
- Use a dedicated trading wallet
- Never share your private key
- Keep your .env file secure
- Don't commit .env to git

### What if someone gets my API keys?

1. Revoke keys immediately at Polymarket
2. Transfer funds from trading wallet
3. Create new keys
4. Review recent transactions

---

## Support

### Where can I get help?

1. Check this FAQ
2. Review the documentation
3. Check logs for error messages
4. Search existing issues

### How do I report bugs?

- Document the issue clearly
- Include logs
- Include your configuration
- Describe steps to reproduce

---

## Advanced

### Can I run multiple instances?

Yes, but:
- Use different accounts
- Different position limits
- Different markets to avoid competition

### Can I add my own strategies?

Yes. The code is in `src/strategy/`. Modify carefully and test thoroughly.

### What's the Kelly criterion?

A mathematical formula for optimal bet sizing. Higher KELLY_FRACTION = larger positions but more risk. We recommend 0.15-0.25 for most users.

---

*See also: [Getting Started](getting-started.md) | [Configuration](configuration.md) | [Safety](safety.md)*
```

- [ ] **Step 2: Commit**

```bash
git add docs/user/faq.md
git commit -m "docs: Add FAQ documentation"
```

---

### Task 7: Create developer documentation

- [ ] **Step 1: Write docs/developer/architecture.md**

```markdown
# Architecture Overview

For developers who want to understand the bot's technical design.

> **Note:** This is a high-level overview. For detailed technical documentation, see `PROJECT_CONTEXT.md` in the project root.

## System Overview

```
                          ┌─────────────────────────────────────────────────┐
                          │                 SmartMarketMaker                 │
                          │  - Quote calculation (spread, skew, size)        │
                          │  - Order lifecycle management                   │
                          └───────────────────────┬─────────────────────────┘
                                                    │
          ┌─────────────────────────────────────────┼─────────────────────────────────────────┐
          │                                         │                                         │
          ▼                                         ▼                                         ▼
┌─────────────────────┐              ┌─────────────────────┐              ┌─────────────────────┐
│     MarketFeed     │              │     RiskManager     │              │   Alpha Signals    │
│  - WebSocket data  │              │  - Position limits   │              │  - Arbitrage       │
│  - REST fallback   │              │  - Daily P&L        │              │  - Flow analysis   │
│  - Health tracking │              │  - Kill switch      │              │  - Competitors     │
└─────────────────────┘              └─────────────────────┘              └─────────────────────┘
```

## Core Components

| Component | Location | Purpose |
|-----------|----------|---------|
| SmartMarketMaker | `src/strategy/market_maker.py` | Main strategy, quote calculation |
| MarketFeed | `src/feed/feed.py` | Real-time market data |
| RiskManager | `src/risk/manager.py` | Risk controls |
| Alpha Signals | `src/alpha/` | Market intelligence |

## Key Files

- `run_mm.py` - Main entry point
- `run_tui.py` - Terminal UI entry point
- `src/config.py` - All configuration
- `src/strategy/market_maker.py` - Core strategy
- `src/risk/manager.py` - Risk management

## Data Flow

1. MarketFeed subscribes to WebSocket for order book updates
2. SmartMarketMaker calculates quotes using volatility, inventory, flow signals
3. RiskManager validates each quote against position limits
4. Orders placed via Client or simulated in DRY_RUN
5. Fills processed with inventory updates
6. TradeLogger records all activity

## Quick Reference

### Running Tests
```bash
pytest tests/ -v
```

### Adding a New Alpha Signal
1. Create signal class in `src/alpha/`
2. Add config to `src/config.py`
3. Integrate in SmartMarketMaker._calculate_quotes()
4. Add tests

### Modifying Quote Logic
All quote calculation in `SmartMarketMaker._calculate_quotes()`

---

*Full technical documentation: See PROJECT_CONTEXT.md in project root*
```

- [ ] **Step 2: Write docs/developer/setup.md**

```markdown
# Development Setup

Guide for developers who want to contribute to the project.

## Prerequisites

- Python 3.11+
- Git
- Text editor or IDE (VS Code recommended)

## Initial Setup

```bash
# Clone repository
git clone <repo-url>
cd polymarket-mm-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt  # If available
```

## Running Tests

```bash
# All tests
pytest tests/ -v

# Specific module
pytest tests/test_smart_mm.py -v
pytest tests/test_safety.py -v

# With coverage
pytest --cov=src tests/
```

## Code Style

- Use type hints throughout
- Use Decimal for monetary values
- Use dataclasses for data transfer
- Run linting: `flake8 src/`

## Debugging Tips

### Enable debug logging
```bash
export LOG_LEVEL=DEBUG
python run_mm.py
```

### Use TUI for live debugging
```bash
python run_tui.py
```

### Test with mock data
```python
from src.feed.mock import MockFeed

feed = MockFeed()
feed.set_midpoint("token", Decimal("0.50"))
```

## Common Development Tasks

### Adding a new config variable

1. Add to `src/config.py`
2. Add to `.env.example`
3. Document in README

### Adding a new risk check

1. Add to `RiskManager._run_checks()`
2. Add config threshold to `src/config.py`
3. Add tests

---

*Contributing guidelines: See CONTRIBUTING.md or docs/developer/contributing.md*
```

- [ ] **Step 3: Write docs/developer/contributing.md**

```markdown
# Contributing Guide

Thank you for your interest in contributing!

## Ways to Contribute

- Report bugs
- Suggest features
- Improve documentation
- Submit pull requests

## Pull Request Process

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make changes with tests
4. Ensure all tests pass
5. Commit with clear messages
6. Push to your fork
7. Submit a pull request

## Code Standards

- Use type hints
- Write tests for new features
- Follow existing code style
- Update documentation

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test
pytest tests/test_specific.py::test_name -v
```

## Getting Help

- Open an issue for bugs or features
- Check existing issues before creating new ones

---

*We appreciate all contributions, big or small!*
```

- [ ] **Step 4: Commit**

```bash
git add docs/developer/architecture.md docs/developer/setup.md docs/developer/contributing.md
git commit -m "docs: Add developer documentation"
```

---

### Task 8: Create roadmap.md

- [ ] **Step 1: Write docs/roadmap.md**

```markdown
# Project Roadmap

Current status and future plans for the Polymarket MM Bot.

---

## Current Version: v1.0

### Features Implemented

- **Core Market Making**
  - Two-sided quote placement
  - Adaptive spread based on volatility
  - Inventory skew management

- **Risk Management**
  - Position limits
  - Daily loss limits
  - Kill switch
  - Kelly criterion sizing

- **Alpha Generation**
  - Arbitrage detection
  - Order flow analysis
  - Competitor detection
  - Regime detection

- **Data & Infrastructure**
  - WebSocket + REST fallback
  - Real-time order book
  - Trade logging

- **Monitoring**
  - TUI Dashboard
  - Latency monitoring

- **Safety**
  - Cancel-on-disconnect
  - Stale order cleanup
  - Balance monitoring

---

## Planned Features

### v2.0 - Multi-Market Optimization

- [ ] Multi-market pool management
- [ ] Dynamic capital allocation across markets
- [ ] Cross-market correlation tracking
- [ ] Portfolio-level P&L

### v2.1 - Enhanced Alpha

- [ ] Machine learning-based regime detection
- [ ] Better competitor detection
- [ ] Time-series prediction signals
- [ ] News event detection

### v2.2 - Advanced Risk

- [ ] Real-timeVaR calculation
- [ ] Stress testing scenarios
- [ ] Dynamic margin management
- [ ] Hedging strategies

---

## Known Limitations

1. **Single token focus** - Currently trades one market at a time
2. **No hedging** - No automatic hedge positions
3. **Basic backtesting** - Limited historical replay
4. **No cloud deployment** - No Docker or Kubernetes configs

---

## Feature Requests

Have a suggestion? Open an issue on GitHub with:
- Clear description
- Use case explanation
- Potential implementation approach

---

## Community Feedback

- Star the repo if you find it useful
- Share your experiences
- Contribute improvements

---

## Changelog

### v1.0 (Current)
- Initial release
- Core market making functionality
- Risk management system
- Alpha generation signals
- TUI dashboard
- Backtesting framework

---

*Last Updated: 2026-03-23*
```

- [ ] **Step 2: Commit**

```bash
git add docs/roadmap.md
git commit -m "docs: Add project roadmap"
```

---

### Task 9: Update existing documentation links

According to spec (Section 6), we need to update README.md and CLAUDE.md to reference the new docs/ directory.

- [ ] **Step 1: Add link to README.md**

Add a documentation section at the end of README.md or update the Quick Start section to include a link:

```markdown
## Documentation

For detailed guides, see the [docs/](docs/) directory:

- [User Documentation](docs/user/) - Getting started, configuration, safety
- [Developer Documentation](docs/developer/) - Architecture, setup, contributing
- [Roadmap](docs/roadmap.md) - Project plans and future features
```

- [ ] **Step 2: Update CLAUDE.md**

Add reference to docs/ in the CLAUDE.md file. Find the relevant section and add:

```markdown
## Additional Documentation

- [User Documentation](docs/user/) - For non-technical users
- [Developer Documentation](docs/developer/) - For contributors
- [Roadmap](docs/roadmap.md) - Project plans
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: Add link to documentation from README and CLAUDE"
```

---

## Summary

Total tasks: 9
Total steps: ~25
Estimated time: 1-2 hours (writing documentation)

---

## Acceptance Criteria Verification

After implementation, verify:
- [ ] All 10 documents created with meaningful content
- [ ] Non-technical user can go from zero to running bot (getting-started.md)
- [ ] Safety warnings are prominent and clear (safety.md)
- [ ] Configuration guide helps users make informed choices (configuration.md)
- [ ] FAQ addresses at least 10 common questions
- [ ] Existing README remains functional (not modified)
- [ ] Navigation is intuitive (index.md)