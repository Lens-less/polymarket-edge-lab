"""
Configuration management.

Loads settings from environment variables.
"""

import os
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()


# === Network ===
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL")

# === API Endpoints ===
CLOB_API_URL = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
GAMMA_API_URL = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com")

# === Authentication ===
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY")
POLY_API_KEY = os.getenv("POLY_API_KEY")
POLY_API_SECRET = os.getenv("POLY_API_SECRET")
POLY_PASSPHRASE = os.getenv("POLY_PASSPHRASE")
POLY_SIGNATURE_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
POLY_FUNDER = os.getenv("POLY_FUNDER")

# === WebSocket ===
WS_MARKET_URL = os.getenv(
    "WS_MARKET_URL",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market"
)
WS_RECONNECT_ATTEMPTS = int(os.getenv("WS_RECONNECT_ATTEMPTS", "10"))
WS_RECONNECT_BASE_DELAY = float(os.getenv("WS_RECONNECT_BASE_DELAY", "1.0"))
WS_RECONNECT_MAX_DELAY = float(os.getenv("WS_RECONNECT_MAX_DELAY", "60.0"))
WS_HEARTBEAT_INTERVAL = float(os.getenv("WS_HEARTBEAT_INTERVAL", "30.0"))
WS_STALE_DATA_THRESHOLD = float(os.getenv("WS_STALE_DATA_THRESHOLD", "60.0"))

# === Contract Addresses (Polygon) ===
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# === Trading ===
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
MAX_POSITION_PER_MARKET = Decimal(os.getenv("MAX_POSITION_PER_MARKET", "100"))
MAX_ORDER_SIZE = Decimal(os.getenv("MAX_ORDER_SIZE", "50"))
MIN_ORDER_SIZE = Decimal(os.getenv("MIN_ORDER_SIZE", "5"))

# === Market Making ===
MM_SPREAD = Decimal(os.getenv("MM_SPREAD", "0.04"))        # 4 cents each side
MM_SIZE = Decimal(os.getenv("MM_SIZE", "10"))              # Order size
MM_REQUOTE_THRESHOLD = Decimal(os.getenv("MM_REQUOTE_THRESHOLD", "0.03"))  # Requote if mid moves 3c
MM_POSITION_LIMIT = Decimal(os.getenv("MM_POSITION_LIMIT", "50"))  # Max position before skipping side
MM_LOOP_INTERVAL = float(os.getenv("MM_LOOP_INTERVAL", "1.0"))     # Seconds between loops

# === Smart Market Making ===
# Market Selection
MARKET_MIN_VOLUME = float(os.getenv("MARKET_MIN_VOLUME", "10000"))    # Min 24h volume ($)
MARKET_MIN_SPREAD = float(os.getenv("MARKET_MIN_SPREAD", "0.02"))     # Min spread (too tight = too competitive)
MARKET_MAX_SPREAD = float(os.getenv("MARKET_MAX_SPREAD", "0.15"))     # Max spread (too wide = illiquid)
MARKET_MIN_HOURS_TO_RESOLUTION = float(os.getenv("MARKET_MIN_HOURS_TO_RESOLUTION", "12"))
MARKET_MIN_PRICE = float(os.getenv("MARKET_MIN_PRICE", "0.05"))       # Avoid extreme prices
MARKET_MAX_PRICE = float(os.getenv("MARKET_MAX_PRICE", "0.95"))

# Market Scoring Weights
MARKET_SCORE_WEIGHT_VOLUME = float(os.getenv("MARKET_SCORE_WEIGHT_VOLUME", "0.30"))
MARKET_SCORE_WEIGHT_SPREAD = float(os.getenv("MARKET_SCORE_WEIGHT_SPREAD", "0.35"))
MARKET_SCORE_WEIGHT_DEPTH = float(os.getenv("MARKET_SCORE_WEIGHT_DEPTH", "0.15"))
MARKET_SCORE_WEIGHT_TIMING = float(os.getenv("MARKET_SCORE_WEIGHT_TIMING", "0.10"))
MARKET_SCORE_WEIGHT_PRICE = float(os.getenv("MARKET_SCORE_WEIGHT_PRICE", "0.10"))

# Dynamic Spread
SPREAD_MIN = Decimal(os.getenv("SPREAD_MIN", "0.02"))                 # Minimum spread (2 cents)
SPREAD_MAX = Decimal(os.getenv("SPREAD_MAX", "0.10"))                 # Maximum spread (10 cents)
SPREAD_BASE = Decimal(os.getenv("SPREAD_BASE", "0.05"))               # Base spread before adjustments

# Volatility Tracking
VOL_SAMPLE_INTERVAL = float(os.getenv("VOL_SAMPLE_INTERVAL", "5.0"))      # Seconds between samples
VOL_WINDOW_SECONDS = float(os.getenv("VOL_WINDOW_SECONDS", "1800"))       # 30-minute rolling window
VOL_MIN_SAMPLES = int(os.getenv("VOL_MIN_SAMPLES", "10"))                 # Min samples before calculating
VOL_MULT_MIN = float(os.getenv("VOL_MULT_MIN", "0.5"))                    # Spread multiplier in calm markets
VOL_MULT_MAX = float(os.getenv("VOL_MULT_MAX", "3.0"))                    # Spread multiplier in volatile markets

# Inventory Skewing
INVENTORY_SKEW_MAX = Decimal(os.getenv("INVENTORY_SKEW_MAX", "0.02"))     # Max price skew (2 cents)
INVENTORY_SIZE_REDUCTION_START = Decimal(os.getenv("INVENTORY_SIZE_REDUCTION_START", "0.5"))  # Start reducing at 50%

# Book Analysis
BOOK_IMBALANCE_THRESHOLD = float(os.getenv("BOOK_IMBALANCE_THRESHOLD", "0.10"))  # 10% from balanced
BOOK_DEPTH_CENTS = float(os.getenv("BOOK_DEPTH_CENTS", "5.0"))                    # Analyze depth within 5c
BOOK_TICK_IMPROVE = float(os.getenv("BOOK_TICK_IMPROVE", "0.01"))                 # Improve by 1 tick

# === Alpha Generation ===
# Arbitrage
ARB_MIN_PROFIT_BPS = int(os.getenv("ARB_MIN_PROFIT_BPS", "20"))
ARB_SCAN_INTERVAL = float(os.getenv("ARB_SCAN_INTERVAL", "5.0"))

# Order Flow
FLOW_WINDOW_SECONDS = float(os.getenv("FLOW_WINDOW_SECONDS", "60"))
FLOW_AGGRESSIVE_WEIGHT = float(os.getenv("FLOW_AGGRESSIVE_WEIGHT", "2.0"))
FLOW_IMBALANCE_THRESHOLD = float(os.getenv("FLOW_IMBALANCE_THRESHOLD", "0.15"))

# Events
EVENT_RESOLUTION_WARNING_HOURS = int(os.getenv("EVENT_RESOLUTION_WARNING_HOURS", "24"))

# === Market Intelligence ===
# Competitor Detection
COMPETITOR_WINDOW_SIZE = int(os.getenv("COMPETITOR_WINDOW_SIZE", "1000"))
COMPETITOR_BACK_OFF_THRESHOLD = Decimal(os.getenv("COMPETITOR_BACK_OFF_THRESHOLD", "5000"))
COMPETITOR_SIZE_TOLERANCE = Decimal(os.getenv("COMPETITOR_SIZE_TOLERANCE", "0.1"))
COMPETITOR_OFFSET_TOLERANCE = Decimal(os.getenv("COMPETITOR_OFFSET_TOLERANCE", "0.005"))

# Regime Detection
REGIME_WINDOW_SIZE = int(os.getenv("REGIME_WINDOW_SIZE", "50"))
REGIME_HIGH_THRESHOLD = float(os.getenv("REGIME_HIGH_THRESHOLD", "0.7"))
REGIME_LOW_THRESHOLD = float(os.getenv("REGIME_LOW_THRESHOLD", "0.3"))
REGIME_CRISIS_THRESHOLD = float(os.getenv("REGIME_CRISIS_THRESHOLD", "0.1"))
REGIME_SPREAD_WEIGHT = float(os.getenv("REGIME_SPREAD_WEIGHT", "0.3"))
REGIME_DEPTH_WEIGHT = float(os.getenv("REGIME_DEPTH_WEIGHT", "0.4"))
REGIME_VOLUME_WEIGHT = float(os.getenv("REGIME_VOLUME_WEIGHT", "0.3"))

# Time Patterns
TIME_PATTERN_HISTORY = int(os.getenv("TIME_PATTERN_HISTORY", "100"))

# === Risk Management ===
RISK_MAX_DAILY_LOSS = Decimal(os.getenv("RISK_MAX_DAILY_LOSS", "50"))  # Stop if lose $50
RISK_MAX_POSITION = Decimal(os.getenv("RISK_MAX_POSITION", "100"))     # Max position per token
RISK_MAX_TOTAL_EXPOSURE = Decimal(os.getenv("RISK_MAX_TOTAL_EXPOSURE", "500"))  # Total across all
RISK_ERROR_COOLDOWN = int(os.getenv("RISK_ERROR_COOLDOWN", "60"))      # Seconds to pause after errors
RISK_MAX_ERRORS_PER_MINUTE = int(os.getenv("RISK_MAX_ERRORS_PER_MINUTE", "5"))  # Error rate limit

# Enforce risk limits? Default: OFF in dry-run (gather data), ON in live (protect money)
_default_enforce = "false" if DRY_RUN else "true"
RISK_ENFORCE = os.getenv("RISK_ENFORCE", _default_enforce).lower() == "true"

# === Risk-Adjusted Returns ===
# Dynamic Limits
DYNAMIC_LIMIT_FLOOR = Decimal(os.getenv("DYNAMIC_LIMIT_FLOOR", "0.2"))
DYNAMIC_LIMIT_CEILING = Decimal(os.getenv("DYNAMIC_LIMIT_CEILING", "2.0"))

# Adverse Selection
ADVERSE_LOOKBACK_SECONDS = float(os.getenv("ADVERSE_LOOKBACK_SECONDS", "300"))
ADVERSE_TOXIC_THRESHOLD = float(os.getenv("ADVERSE_TOXIC_THRESHOLD", "0.4"))
ADVERSE_HIGHLY_TOXIC_THRESHOLD = float(os.getenv("ADVERSE_HIGHLY_TOXIC_THRESHOLD", "0.6"))
ADVERSE_PRICE_THRESHOLD = Decimal(os.getenv("ADVERSE_PRICE_THRESHOLD", "0.005"))
ADVERSE_OBSERVATION_WINDOW = float(os.getenv("ADVERSE_OBSERVATION_WINDOW", "10"))

# Kelly Criterion
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
KELLY_MAX_POSITION = float(os.getenv("KELLY_MAX_POSITION", "0.10"))
KELLY_MIN_TRADES = int(os.getenv("KELLY_MIN_TRADES", "20"))

# Correlation Risk
CORRELATION_THRESHOLD = float(os.getenv("CORRELATION_THRESHOLD", "0.5"))
MAX_CORRELATED_EXPOSURE = Decimal(os.getenv("MAX_CORRELATED_EXPOSURE", "500"))

# === Simulation ===
SIMULATED_FEE_RATE = Decimal(os.getenv("SIMULATED_FEE_RATE", "0.001"))  # 0.1% per trade

# === Rate Limiting ===
RATE_LIMIT_ORDERS_PER_SECOND = float(os.getenv("RATE_LIMIT_ORDERS_PER_SECOND", "5"))
RATE_LIMIT_DATA_PER_SECOND = float(os.getenv("RATE_LIMIT_DATA_PER_SECOND", "10"))

# === Execution Optimization ===
# Adaptive Timing
TIMING_BASE_INTERVAL = float(os.getenv("TIMING_BASE_INTERVAL", "2.0"))
TIMING_FAST_INTERVAL = float(os.getenv("TIMING_FAST_INTERVAL", "0.1"))
TIMING_SLEEP_INTERVAL = float(os.getenv("TIMING_SLEEP_INTERVAL", "5.0"))
TIMING_VOL_THRESHOLD = float(os.getenv("TIMING_VOL_THRESHOLD", "0.01"))
TIMING_INACTIVITY_THRESHOLD = float(os.getenv("TIMING_INACTIVITY_THRESHOLD", "60.0"))
TIMING_FAST_MODE_DURATION = float(os.getenv("TIMING_FAST_MODE_DURATION", "10.0"))

# Queue Optimization
QUEUE_IMPROVE_THRESHOLD = float(os.getenv("QUEUE_IMPROVE_THRESHOLD", "100"))


def has_credentials() -> bool:
    """Check if enough auth material exists to create an authenticated client."""
    if not POLY_PRIVATE_KEY:
        return False

    # Polymarket clients can derive/create API creds from the signing key.
    if POLY_SIGNATURE_TYPE == 1 and not POLY_FUNDER:
        return False

    return True


def get_mode_string() -> str:
    """Get human-readable mode string."""
    return "DRY RUN (paper trading)" if DRY_RUN else "LIVE"


def validate_config():
    """Validate configuration. Raises ValueError if invalid."""
    errors = []

    if CHAIN_ID not in (137, 80001):  # Polygon mainnet or Mumbai testnet
        errors.append(f"Invalid CHAIN_ID: {CHAIN_ID}")

    if not CLOB_API_URL:
        errors.append("CLOB_API_URL is required")

    if errors:
        raise ValueError("Configuration errors: " + "; ".join(errors))
