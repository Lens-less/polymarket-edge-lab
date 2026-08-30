from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, cast

from .strategies.models import (
    ZERO,
    DecisionStatus,
    NormalizedOpportunity,
    PortfolioSnapshot,
    RuntimeMode,
    Strategy,
    StrategyDecision,
    StrategyFeedback,
)


@dataclass(frozen=True)
class ModePolicy:
    mutation_allowed: bool
    capital_fraction: Decimal = Decimal("1")
    absolute_capital: Decimal | None = None
    label: str = ""

    def effective_capital(self, available_capital: Decimal) -> Decimal:
        cap = available_capital * self.capital_fraction
        if self.absolute_capital is not None:
            cap = min(cap, self.absolute_capital)
        return max(cap, ZERO)


_DEFAULT_POLICIES: dict[RuntimeMode, ModePolicy] = {
    RuntimeMode.RESEARCH: ModePolicy(
        mutation_allowed=False, capital_fraction=Decimal("1"), label="research"
    ),
    RuntimeMode.REPLAY: ModePolicy(
        mutation_allowed=False, capital_fraction=Decimal("1"), label="replay"
    ),
    RuntimeMode.PAPER: ModePolicy(
        mutation_allowed=False, capital_fraction=Decimal("1"), label="paper"
    ),
    RuntimeMode.SHADOW: ModePolicy(
        mutation_allowed=False, capital_fraction=Decimal("1"), label="shadow"
    ),
    RuntimeMode.LIVE_CANARY: ModePolicy(
        mutation_allowed=True,
        capital_fraction=Decimal("0.25"),
        label="live_canary",
    ),
    RuntimeMode.LIVE_LIMITED: ModePolicy(
        mutation_allowed=True,
        capital_fraction=Decimal("1"),
        label="live_limited",
    ),
    RuntimeMode.KILLED: ModePolicy(mutation_allowed=False, capital_fraction=ZERO, label="killed"),
}


class StrategyRuntime:
    def __init__(
        self,
        strategy: Strategy,
        *,
        mode_policies: dict[RuntimeMode, ModePolicy] | None = None,
    ) -> None:
        self.strategy = strategy
        self.mode_policies = dict(_DEFAULT_POLICIES)
        if mode_policies is not None:
            self.mode_policies.update(mode_policies)

    def evaluate(
        self,
        opportunity: NormalizedOpportunity | dict[str, Any],
        portfolio: PortfolioSnapshot,
        mode: RuntimeMode | str,
    ) -> StrategyDecision:
        runtime_mode = mode if isinstance(mode, RuntimeMode) else RuntimeMode(str(mode))
        normalized = (
            opportunity
            if isinstance(opportunity, NormalizedOpportunity)
            else NormalizedOpportunity.from_candidate(cast(Mapping[str, Any], opportunity))
        )
        base_decision = self.strategy.evaluate(normalized, portfolio)
        policy = self.mode_policies[runtime_mode]
        capital_limit = policy.effective_capital(portfolio.available_capital)
        runtime_reasons = list(base_decision.rejection_reasons)
        executable = base_decision.executable
        status = base_decision.status
        if runtime_mode is RuntimeMode.KILLED:
            runtime_reasons.append("runtime_killed")
            executable = False
            status = DecisionStatus.NO_GO
        elif base_decision.capital_required > capital_limit:
            runtime_reasons.append("mode_capital_limit_exceeded")
            executable = False
            if status is DecisionStatus.EXECUTABLE:
                status = DecisionStatus.REVIEW
        return replace(
            base_decision,
            status=status,
            executable=executable,
            rejection_reasons=tuple(dict.fromkeys(runtime_reasons)),
            mode=runtime_mode,
            mutation_allowed=policy.mutation_allowed,
            capital_limit=capital_limit,
            runtime_context={
                "policy": policy.label,
                "capital_fraction": str(policy.capital_fraction),
                "absolute_capital": None
                if policy.absolute_capital is None
                else str(policy.absolute_capital),
            },
        )

    def observe(self, outcome: dict[str, Any]) -> StrategyFeedback:
        return self.strategy.observe(outcome)
