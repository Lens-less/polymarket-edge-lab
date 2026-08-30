from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .models import (
    ZERO,
    DecisionStatus,
    NormalizedOpportunity,
    OrderLeg,
    PortfolioSnapshot,
    StrategyDecision,
    StrategyFeedback,
)


def _bounded_probability(value: Decimal) -> Decimal:
    return min(max(value, ZERO), Decimal("1"))


@dataclass(frozen=True)
class MakerStrategyConfig:
    strategy_id: str = "track_b.maker.quote.v0_2"
    strategy_version: str = "0.2.0"
    min_net_edge: Decimal = Decimal("0.005")
    min_quote_size: Decimal = Decimal("5")
    inventory_target: Decimal = ZERO
    inventory_skew_per_contract: Decimal = Decimal("0.0025")
    quote_ttl_ms: int = 15_000
    gtd_buffer_ms: int = 5_000
    markout_window_ms: int = 5_000
    quote_stale_after_ms: int = 2_000


class MakerStrategy:
    def __init__(self, config: MakerStrategyConfig | None = None) -> None:
        self.config = config or MakerStrategyConfig()
        self.strategy_id = self.config.strategy_id
        self.strategy_version = self.config.strategy_version

    def evaluate(
        self,
        opportunity: NormalizedOpportunity,
        portfolio: PortfolioSnapshot,
    ) -> StrategyDecision:
        metadata = dict(opportunity.metadata)
        token_id = metadata.get("quote_token_id") or opportunity.token_ids[0]
        market_id = metadata.get("quote_market_id") or opportunity.market_ids[0]
        mid_price = Decimal(str(metadata.get("mid_price", "0.50")))
        half_spread = Decimal(str(metadata.get("half_spread", "0.02")))
        quote_size = Decimal(str(metadata.get("quote_size", opportunity.estimated_capacity)))
        quote_sides = tuple(
            str(side).lower() for side in metadata.get("quote_sides", ("buy", "sell"))
        )
        catalyst_at_ms = metadata.get("catalyst_at_ms")
        inventory_position = Decimal(
            str(metadata.get("inventory_position", portfolio.position_for(token_id)))
        )
        inventory_target = Decimal(
            str(metadata.get("inventory_target", self.config.inventory_target))
        )
        skew_shift = (
            inventory_position - inventory_target
        ) * self.config.inventory_skew_per_contract
        bid_price = _bounded_probability(mid_price - half_spread - skew_shift)
        ask_price = _bounded_probability(mid_price + half_spread - skew_shift)
        decision_expires_at = min(
            opportunity.expires_at_ms,
            (
                int(catalyst_at_ms) - self.config.gtd_buffer_ms
                if catalyst_at_ms is not None
                else opportunity.detected_at_ms + self.config.quote_ttl_ms
            ),
        )
        legs = []
        if "buy" in quote_sides:
            legs.append(
                OrderLeg(
                    leg_id="maker-bid",
                    market_id=market_id,
                    token_id=token_id,
                    event_id=opportunity.event_id,
                    side="buy",
                    price=bid_price,
                    quantity=quote_size,
                    post_only=True,
                    time_in_force="GTD",
                    expires_at_ms=decision_expires_at,
                    metadata={"quote_role": "bid"},
                )
            )
        if "sell" in quote_sides:
            legs.append(
                OrderLeg(
                    leg_id="maker-ask",
                    market_id=market_id,
                    token_id=token_id,
                    event_id=opportunity.event_id,
                    side="sell",
                    price=ask_price,
                    quantity=quote_size,
                    post_only=True,
                    time_in_force="GTD",
                    expires_at_ms=decision_expires_at,
                    metadata={"quote_role": "ask"},
                )
            )

        spread_capture = Decimal(
            str(metadata.get("expected_spread_capture", opportunity.expected_gross_edge))
        )
        confirmed_rebate = Decimal(str(metadata.get("expected_confirmed_rebate", "0")))
        confirmed_reward = Decimal(
            str(metadata.get("expected_confirmed_liquidity_reward", opportunity.expected_incentive))
        )
        cancel_replace_cost = Decimal(str(metadata.get("expected_cancel_replace_cost", "0")))
        expected_net_edge = (
            spread_capture
            + confirmed_rebate
            + confirmed_reward
            - opportunity.expected_fees
            - opportunity.expected_adverse_selection
            - opportunity.expected_inventory_cost
            - cancel_replace_cost
            - opportunity.uncertainty_buffer
        )
        tradable_edge = min(opportunity.tradable_edge, expected_net_edge)
        capital_required = sum((leg.notional for leg in legs if leg.side == "buy"), ZERO)

        reasons: list[str] = list(opportunity.rejection_reasons)
        if opportunity.remaining_ttl_ms() <= 0:
            reasons.append("opportunity_expired")
        if quote_size < self.config.min_quote_size:
            reasons.append("quote_size_below_floor")
        if tradable_edge <= self.config.min_net_edge:
            reasons.append("maker_edge_below_floor")
        if capital_required > portfolio.available_capital:
            reasons.append("portfolio_capital_below_required")

        executable = not reasons and bool(legs)
        if executable:
            status = DecisionStatus.EXECUTABLE
            rationale = (
                "maker quote survives spread, rebate, reward, adverse-selection, "
                "and inventory costs"
            )
        elif "portfolio_capital_below_required" in reasons:
            status = DecisionStatus.REVIEW
            rationale = (
                "quote logic is valid but the runtime capital cap blocks the buy-side notional"
            )
        else:
            status = DecisionStatus.NO_GO
            rationale = (
                "maker quote failed closed before it could qualify for paper or shadow execution"
            )
        return StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            opportunity_id=opportunity.opportunity_id,
            status=status,
            executable=executable,
            rationale=rationale,
            expected_net_edge=expected_net_edge,
            tradable_edge=tradable_edge,
            max_loss=opportunity.max_loss,
            capital_required=capital_required,
            legs=tuple(legs),
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            attribution={
                "spread_capture": spread_capture,
                "fees": opportunity.expected_fees,
                "maker_rebates": confirmed_rebate,
                "liquidity_rewards": confirmed_reward,
                "adverse_selection": opportunity.expected_adverse_selection,
                "inventory_mark_to_market": opportunity.expected_inventory_cost,
                "cancel_replace_cost": cancel_replace_cost,
                "uncertainty_buffer": opportunity.uncertainty_buffer,
                "expected_net_edge": expected_net_edge,
            },
            failure_plan={
                "cancel_replace_intent": {
                    "trigger": "book_move_or_ttl",
                    "ttl_ms": self.config.quote_ttl_ms,
                    "quote_stale_after_ms": self.config.quote_stale_after_ms,
                },
                "post_fill_markout_window_ms": self.config.markout_window_ms,
            },
            metadata={
                "post_only": True,
                "inventory_skew": {
                    "inventory_position": str(inventory_position),
                    "inventory_target": str(inventory_target),
                    "skew_shift": str(skew_shift),
                },
                "markout_model": {
                    "window_ms": self.config.markout_window_ms,
                    "expected_markout_bps": str(metadata.get("expected_markout_bps", "0")),
                },
                "adverse_selection_model": {
                    "expected_bps": str(metadata.get("expected_adverse_selection_bps", "0")),
                },
            },
            risk_tags=("maker", "inventory_sensitive"),
        )

    def observe(self, outcome: Mapping[str, Any]) -> StrategyFeedback:
        realized_pnl = Decimal(str(outcome.get("realized_pnl", "0")))
        realized_net_edge = Decimal(str(outcome.get("realized_net_edge", realized_pnl)))
        markout_bps = Decimal(str(outcome.get("markout_bps", "0")))
        adverse_selection_bps = Decimal(str(outcome.get("adverse_selection_bps", markout_bps)))
        return StrategyFeedback(
            strategy_id=self.strategy_id,
            opportunity_id=str(outcome["opportunity_id"]),
            realized_net_edge=realized_net_edge,
            realized_pnl=realized_pnl,
            markout_bps=markout_bps,
            adverse_selection_bps=adverse_selection_bps,
            attribution={
                "spread_capture": str(outcome.get("spread_capture", "0")),
                "fees": str(outcome.get("fees", "0")),
                "maker_rebates": str(outcome.get("maker_rebates", "0")),
                "liquidity_rewards": str(outcome.get("liquidity_rewards", "0")),
                "inventory_mark_to_market": str(outcome.get("inventory_mark_to_market", "0")),
            },
        )
