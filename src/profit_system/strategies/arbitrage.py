from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .models import (
    ZERO,
    DecisionStatus,
    NormalizedOpportunity,
    PortfolioSnapshot,
    StrategyDecision,
    StrategyFeedback,
)


@dataclass(frozen=True)
class ArbitrageStrategyConfig:
    strategy_id: str = "track_a.complete_set.arbitrage.v0_2"
    strategy_version: str = "0.2.0"
    min_net_edge: Decimal = Decimal("0.01")
    min_capacity: Decimal = Decimal("10")
    min_confidence: Decimal = Decimal("0.70")
    max_unmatched_leg_seconds: int = 5
    entry_style: str = "FAK_BUNDLE"
    unwind_style: str = "FAK"


class ArbitrageStrategy:
    def __init__(self, config: ArbitrageStrategyConfig | None = None) -> None:
        self.config = config or ArbitrageStrategyConfig()
        self.strategy_id = self.config.strategy_id
        self.strategy_version = self.config.strategy_version

    def evaluate(
        self,
        opportunity: NormalizedOpportunity,
        portfolio: PortfolioSnapshot,
    ) -> StrategyDecision:
        reasons = list(opportunity.rejection_reasons)
        if opportunity.remaining_ttl_ms() <= 0:
            reasons.append("opportunity_expired")
        if len(opportunity.recommended_legs) < 2:
            reasons.append("incomplete_multi_leg_depth")
        if opportunity.estimated_capacity < self.config.min_capacity:
            reasons.append("estimated_capacity_below_floor")
        if opportunity.confidence < self.config.min_confidence:
            reasons.append("confidence_below_floor")
        if opportunity.tradable_edge <= self.config.min_net_edge:
            reasons.append("tradable_edge_below_floor")

        capital_required = sum(
            (leg.notional for leg in opportunity.recommended_legs if leg.side == "buy"),
            ZERO,
        )
        if capital_required > portfolio.available_capital:
            reasons.append("portfolio_capital_below_required")

        negative_risk_enabled = bool(opportunity.metadata.get("negative_risk", False))
        execution_plan = {
            "entry_style": opportunity.metadata.get("entry_style", self.config.entry_style),
            "submit_sequence": [leg.leg_id for leg in opportunity.recommended_legs],
            "single_leg_failure_actions": [
                "freeze_new_legs",
                "submit_unwind_order",
                "escalate_if_unwind_unconfirmed",
            ],
            "unwind_failure_plan": {
                "order_type": self.config.unwind_style,
                "max_unmatched_leg_seconds": self.config.max_unmatched_leg_seconds,
                "fallback_actions": [
                    "quote_out_remaining_inventory",
                    "cancel_residual_orders",
                    "raise_attention_required",
                ],
            },
        }
        negative_risk_metadata = {
            "enabled": negative_risk_enabled,
            "signature_path": opportunity.metadata.get(
                "negative_risk_signature_path", "standard-risk-signature"
            ),
            "settlement_notes": opportunity.metadata.get(
                "negative_risk_settlement_notes",
                "verify special settlement and signature semantics before mutation",
            ),
        }
        attribution = {
            "gross_edge": opportunity.expected_gross_edge,
            "fees": opportunity.expected_fees,
            "slippage": opportunity.expected_slippage,
            "adverse_selection": opportunity.expected_adverse_selection,
            "inventory_cost": opportunity.expected_inventory_cost,
            "confirmed_incentive": opportunity.expected_incentive,
            "unwind_cost": opportunity.expected_unwind_cost,
            "uncertainty_buffer": opportunity.uncertainty_buffer,
            "expected_net_edge": opportunity.expected_net_edge,
        }
        risk_tags = ["multi_leg"]
        if negative_risk_enabled:
            risk_tags.append("negative_risk")

        executable = not reasons
        if executable:
            status = DecisionStatus.EXECUTABLE
            rationale = "tradable multi-leg edge survives modeled fees, slippage, and unwind risk"
        elif "portfolio_capital_below_required" in reasons:
            status = DecisionStatus.REVIEW
            rationale = (
                "signal survives research checks but needs more capital than the runtime allows"
            )
        else:
            status = DecisionStatus.NO_GO
            rationale = (
                "research contract failed closed before the arbitrage plan became executable"
            )
        return StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            opportunity_id=opportunity.opportunity_id,
            status=status,
            executable=executable,
            rationale=rationale,
            expected_net_edge=opportunity.expected_net_edge,
            tradable_edge=opportunity.tradable_edge,
            max_loss=opportunity.max_loss,
            capital_required=capital_required,
            legs=opportunity.recommended_legs,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            attribution=attribution,
            failure_plan=execution_plan,
            metadata={
                "opportunity_kind": opportunity.opportunity_kind,
                "negative_risk": negative_risk_metadata,
                "estimated_capacity": str(opportunity.estimated_capacity),
            },
            risk_tags=tuple(risk_tags),
        )

    def observe(self, outcome: Mapping[str, Any]) -> StrategyFeedback:
        realized_pnl = Decimal(str(outcome.get("realized_pnl", "0")))
        realized_net_edge = Decimal(str(outcome.get("realized_net_edge", realized_pnl)))
        return StrategyFeedback(
            strategy_id=self.strategy_id,
            opportunity_id=str(outcome["opportunity_id"]),
            realized_net_edge=realized_net_edge,
            realized_pnl=realized_pnl,
            attribution={
                "fill_count": int(outcome.get("fill_count", 0)),
                "unwind_cost": str(outcome.get("realized_unwind_cost", "0")),
            },
            metadata={
                "unmatched_leg_exposure_seconds": int(
                    outcome.get("unmatched_leg_exposure_seconds", 0)
                ),
            },
        )
