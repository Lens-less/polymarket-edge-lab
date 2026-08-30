from decimal import Decimal

from src.profit_system.runtime import ModePolicy, StrategyRuntime
from src.profit_system.strategies.arbitrage import ArbitrageStrategy
from src.profit_system.strategies.models import (
    NormalizedOpportunity,
    PortfolioSnapshot,
    RuntimeMode,
)


def _runtime_opportunity() -> NormalizedOpportunity:
    return NormalizedOpportunity.from_candidate(
        {
            "opportunity_id": "rt-1",
            "strategy_id": "track_a.complete_set.arbitrage.v0_2",
            "opportunity_kind": "complete_set",
            "detected_at_ms": 1000,
            "expires_at_ms": 6000,
            "event_id": "event-rt-1",
            "recommended_legs": (
                {
                    "leg_id": "buy-a",
                    "market_id": "m1",
                    "token_id": "t1",
                    "event_id": "event-rt-1",
                    "side": "buy",
                    "price": "0.40",
                    "quantity": "10",
                },
                {
                    "leg_id": "buy-b",
                    "market_id": "m2",
                    "token_id": "t2",
                    "event_id": "event-rt-1",
                    "side": "buy",
                    "price": "0.45",
                    "quantity": "10",
                },
            ),
            "expected_gross_edge": "1.4",
            "expected_fees": "0.1",
            "expected_slippage": "0.05",
            "expected_adverse_selection": "0.02",
            "expected_inventory_cost": "0.01",
            "expected_incentive": "0",
            "expected_unwind_cost": "0.03",
            "uncertainty_buffer": "0.03",
            "expected_net_edge": "1.16",
            "tradable_edge": "1.0",
            "max_loss": "0.7",
            "estimated_capacity": "10",
            "confidence": "0.9",
        }
    )


def test_runtime_keeps_same_signal_across_replay_paper_shadow() -> None:
    runtime = StrategyRuntime(ArbitrageStrategy())
    opportunity = _runtime_opportunity()
    portfolio = PortfolioSnapshot(available_capital=Decimal("50"))

    replay = runtime.evaluate(opportunity, portfolio, RuntimeMode.REPLAY)
    paper = runtime.evaluate(opportunity, portfolio, RuntimeMode.PAPER)
    shadow = runtime.evaluate(opportunity, portfolio, RuntimeMode.SHADOW)

    assert replay.signal_signature() == paper.signal_signature() == shadow.signal_signature()
    assert replay.mutation_allowed is False
    assert paper.mutation_allowed is False
    assert shadow.mutation_allowed is False
    assert replay.capital_limit == paper.capital_limit == shadow.capital_limit == Decimal("50")


def test_runtime_mode_only_changes_mutation_and_capital() -> None:
    runtime = StrategyRuntime(
        ArbitrageStrategy(),
        mode_policies={
            RuntimeMode.LIVE_CANARY: ModePolicy(
                mutation_allowed=True,
                capital_fraction=Decimal("0.25"),
                absolute_capital=Decimal("2"),
                label="live_canary",
            )
        },
    )
    opportunity = _runtime_opportunity()
    portfolio = PortfolioSnapshot(available_capital=Decimal("50"))

    shadow = runtime.evaluate(opportunity, portfolio, RuntimeMode.SHADOW)
    canary = runtime.evaluate(opportunity, portfolio, RuntimeMode.LIVE_CANARY)

    assert shadow.signal_signature() == canary.signal_signature()
    assert shadow.executable is True
    assert canary.executable is False
    assert canary.mutation_allowed is True
    assert canary.capital_limit == Decimal("2")
    assert "mode_capital_limit_exceeded" in canary.rejection_reasons
