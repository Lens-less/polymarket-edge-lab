"""
Fee and Rebate Model for Polymarket backtesting.

Models current price-dependent Polymarket taker fees.

Liquidity rewards and maker rebates are competitive daily pools, not a fixed
percentage of each maker trade.  They must be supplied from realized account
data and are intentionally not invented by this model.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from enum import Enum


class FeeScenario(Enum):
    """Switchable fee scenarios for backtesting."""
    FEE_FREE = "fee_free"           # Most Polymarket markets
    TAKER_FEE = "taker_fee"         # Some markets have taker fees
    WITH_REBATE = "with_rebate"     # Maker rebates enabled


@dataclass
class FeeConfig:
    """Fee configuration for backtesting."""
    scenario: FeeScenario = FeeScenario.FEE_FREE
    taker_fee_rate: float = 0.05
    taker_fee_exponent: float = 1.0
    # Deprecated compatibility field.  It is never applied automatically.
    maker_rebate_rate: float = 0.0
    min_rebate_threshold: float = 1.0


class FeeModel:
    """
    Calculates fees and rebates for trades.

    Current formula:
        fee = shares * fee_rate * (price * (1-price)) ** exponent

    Fee parameters are market-specific and should be read from market metadata.
    """

    def __init__(self, config: Optional[FeeConfig] = None):
        self.config = config or FeeConfig()

    def calculate_taker_fee(
        self,
        trade_value: Optional[Decimal] = None,
        *,
        shares: Optional[Decimal] = None,
        price: Optional[Decimal] = None,
    ) -> Decimal:
        """Calculate the five-decimal taker fee for a trade.

        ``trade_value`` remains in the signature for old callers, but it is not
        enough to evaluate the current nonlinear curve.
        """
        if self.config.scenario == FeeScenario.FEE_FREE:
            return Decimal("0")

        if shares is None or price is None:
            raise ValueError(
                "current taker fees require both shares and execution price"
            )
        if shares <= 0 or price <= 0 or price >= 1:
            return Decimal("0")
        rate = Decimal(str(self.config.taker_fee_rate))
        exponent = Decimal(str(self.config.taker_fee_exponent))
        raw = shares * rate * ((price * (Decimal("1") - price)) ** exponent)
        quantum = Decimal("0.00001")
        if raw < quantum:
            return Decimal("0")
        return raw.quantize(quantum, rounding=ROUND_HALF_UP)

    def calculate_maker_rebate(self, trade_value: Decimal) -> Decimal:
        """Calculate maker rebate for a trade."""
        # A configured market-wide pool and all makers' fee-equivalent volume
        # are required.  Returning zero is the only defensible public-data
        # assumption.
        return Decimal("0")

    def calculate_net_cost(
        self,
        trade_value: Decimal,
        is_maker: bool,
        *,
        shares: Optional[Decimal] = None,
        price: Optional[Decimal] = None,
    ) -> Decimal:
        """
        Calculate net cost/revenue for a trade.

        Returns:
            - Positive value: cost to trader
            - Negative value: rebate received (revenue)
        """
        if is_maker:
            # Makers receive rebates
            return -self.calculate_maker_rebate(trade_value)
        else:
            # Takers pay fees
            return self.calculate_taker_fee(
                trade_value,
                shares=shares,
                price=price,
            )

    def get_spread_requirement(self, is_maker: bool = True) -> float:
        """
        Get minimum spread required to cover fees as a maker.

        Returns spread as a fraction (e.g., 0.001 = 0.1%)
        """
        if self.config.scenario == FeeScenario.FEE_FREE:
            return 0.0

        # There is no single fractional spread that represents the nonlinear
        # fee curve or a competitive maker rebate pool.
        return 0.0
