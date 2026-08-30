from decimal import Decimal

from src.profit_system.strategies.arbitrage import ArbitrageStrategy
from src.profit_system.strategies.maker import MakerStrategy
from src.profit_system.strategies.models import (
    DecisionStatus,
    NormalizedOpportunity,
    PortfolioSnapshot,
)


def test_arbitrage_strategy_emits_failure_plan_and_negative_risk_metadata() -> None:
    opportunity = NormalizedOpportunity.from_candidate(
        {
            "opportunity_id": "arb-1",
            "strategy_id": "track_a.complete_set.arbitrage.v0_2",
            "opportunity_kind": "complete_set",
            "detected_at_ms": 1000,
            "expires_at_ms": 5000,
            "recommended_legs": (
                {
                    "leg_id": "buy-1",
                    "market_id": "m1",
                    "token_id": "t1",
                    "side": "buy",
                    "price": "0.42",
                    "quantity": "20",
                    "time_in_force": "FAK",
                },
                {
                    "leg_id": "buy-2",
                    "market_id": "m2",
                    "token_id": "t2",
                    "side": "buy",
                    "price": "0.43",
                    "quantity": "20",
                    "time_in_force": "FAK",
                },
            ),
            "expected_gross_edge": "2.0",
            "expected_fees": "0.2",
            "expected_slippage": "0.1",
            "expected_adverse_selection": "0.05",
            "expected_inventory_cost": "0.02",
            "expected_incentive": "0",
            "expected_unwind_cost": "0.08",
            "uncertainty_buffer": "0.05",
            "expected_net_edge": "1.5",
            "tradable_edge": "1.4",
            "max_loss": "0.8",
            "estimated_capacity": "20",
            "confidence": "0.95",
            "metadata": {"negative_risk": True, "negative_risk_signature_path": "nr-signature"},
        }
    )
    portfolio = PortfolioSnapshot(available_capital=Decimal("50"))
    decision = ArbitrageStrategy().evaluate(opportunity, portfolio)

    assert decision.status is DecisionStatus.EXECUTABLE
    assert decision.executable is True
    assert decision.failure_plan["entry_style"] == "FAK_BUNDLE"
    assert decision.failure_plan["unwind_failure_plan"]["max_unmatched_leg_seconds"] == 5
    assert decision.metadata["negative_risk"]["enabled"] is True
    assert decision.metadata["negative_risk"]["signature_path"] == "nr-signature"
    assert "negative_risk" in decision.risk_tags


def test_maker_strategy_emits_post_only_quote_policy_and_separate_attribution() -> None:
    opportunity = NormalizedOpportunity.from_candidate(
        {
            "opportunity_id": "maker-1",
            "strategy_id": "track_b.maker.quote.v0_2",
            "opportunity_kind": "maker",
            "detected_at_ms": 1000,
            "expires_at_ms": 15000,
            "market_ids": ("market-maker",),
            "token_ids": ("token-maker",),
            "recommended_legs": (
                {
                    "leg_id": "anchor",
                    "market_id": "market-maker",
                    "token_id": "token-maker",
                    "side": "buy",
                    "price": "0.49",
                    "quantity": "12",
                },
            ),
            "expected_gross_edge": "0.35",
            "expected_fees": "0.05",
            "expected_slippage": "0",
            "expected_adverse_selection": "0.08",
            "expected_inventory_cost": "0.03",
            "expected_incentive": "0.07",
            "expected_unwind_cost": "0",
            "uncertainty_buffer": "0.02",
            "expected_net_edge": "0.24",
            "tradable_edge": "0.20",
            "max_loss": "1.0",
            "estimated_capacity": "12",
            "confidence": "0.9",
            "metadata": {
                "quote_market_id": "market-maker",
                "quote_token_id": "token-maker",
                "mid_price": "0.5",
                "half_spread": "0.02",
                "quote_size": "12",
                "quote_sides": ("buy", "sell"),
                "inventory_position": "5",
                "inventory_target": "1",
                "expected_spread_capture": "0.20",
                "expected_confirmed_rebate": "0.04",
                "expected_confirmed_liquidity_reward": "0.07",
                "expected_cancel_replace_cost": "0.01",
                "expected_markout_bps": "-5",
                "expected_adverse_selection_bps": "12",
                "catalyst_at_ms": 14000,
            },
        }
    )
    decision = MakerStrategy().evaluate(
        opportunity, PortfolioSnapshot(available_capital=Decimal("30"))
    )

    assert decision.status is DecisionStatus.EXECUTABLE
    assert all(leg.post_only for leg in decision.legs)
    assert decision.failure_plan["cancel_replace_intent"]["trigger"] == "book_move_or_ttl"
    assert set(decision.attribution) == {
        "spread_capture",
        "fees",
        "maker_rebates",
        "liquidity_rewards",
        "adverse_selection",
        "inventory_mark_to_market",
        "cancel_replace_cost",
        "uncertainty_buffer",
        "expected_net_edge",
    }
    assert decision.metadata["inventory_skew"]["inventory_position"] == "5"
    assert decision.metadata["markout_model"]["window_ms"] == 5000
