"""
Fee and Rebate Model for Polymarket backtesting.

Models Polymarket's maker/taker fees and rebates.
"""

from dataclasses import dataclass
from decimal import Decimal
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
    taker_fee_rate: float = 0.02     # 2% taker fee (if applicable)
    maker_rebate_rate: float = 0.01  # 1% maker rebate (if applicable)
    min_rebate_threshold: float = 0.01  # Minimum trade size for rebate


class FeeModel:
    """
    Calculates fees and rebates for trades.

    Polymarket docs indicate:
    - Most markets are fee-free
    - Some markets have taker fees
    - Taker fees are redistributed to market makers as rebates
    """

    def __init__(self, config: Optional[FeeConfig] = None):
        self.config = config or FeeConfig()

    def calculate_taker_fee(self, trade_value: Decimal) -> Decimal:
        """Calculate taker fee for a trade."""
        if self.config.scenario == FeeScenario.FEE_FREE:
            return Decimal("0")

        rate = self.config.taker_fee_rate
        return trade_value * Decimal(str(rate))

    def calculate_maker_rebate(self, trade_value: Decimal) -> Decimal:
        """Calculate maker rebate for a trade."""
        if self.config.scenario not in (FeeScenario.TAKER_FEE, FeeScenario.WITH_REBATE):
            return Decimal("0")

        # Only give rebate above threshold
        if trade_value < Decimal(str(self.config.min_rebate_threshold)):
            return Decimal("0")

        rate = self.config.maker_rebate_rate
        return trade_value * Decimal(str(rate))

    def calculate_net_cost(self, trade_value: Decimal, is_maker: bool) -> Decimal:
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
            return self.calculate_taker_fee(trade_value)

    def get_spread_requirement(self, is_maker: bool = True) -> float:
        """
        Get minimum spread required to cover fees as a maker.

        Returns spread as a fraction (e.g., 0.001 = 0.1%)
        """
        if self.config.scenario == FeeScenario.FEE_FREE:
            return 0.0

        if is_maker:
            return self.config.maker_rebate_rate
        else:
            return self.config.taker_fee_rate