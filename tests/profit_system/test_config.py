from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.profit_system.config import (
    ConfigValidationError,
    ProfitSystemConfig,
    RiskLimits,
    StrategyConfig,
    load_profit_system_config,
)
from src.profit_system.types import OperatingMode, StrategyQualification


def _risk_limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("25"),
        max_market_exposure=Decimal("50"),
        max_event_exposure=Decimal("75"),
        max_strategy_exposure=Decimal("80"),
        max_total_exposure=Decimal("100"),
        max_daily_loss=Decimal("20"),
        max_drawdown=Decimal("30"),
        max_open_orders=5,
        max_unmatched_leg_seconds=Decimal("10"),
        max_market_data_age_ms=1_000,
        max_user_stream_gap_seconds=Decimal("5"),
        max_order_rate=Decimal("2"),
    )


def test_load_profit_system_config_builds_stable_hashes() -> None:
    document = {
        "generated_at": "2026-08-30T12:00:00Z",
        "strategies": [
            {
                "strategy_id": "strategy.alpha",
                "strategy_version": "v0.2.0",
                "hypothesis_summary": "Shadow-only paired complete-set candidate.",
                "operating_mode": "SHADOW",
                "qualification": "SHADOW_ONLY",
                "allowed_modes": ["RESEARCH", "REPLAY", "PAPER", "SHADOW"],
                "min_net_edge": "0.25",
                "min_trade_size": "5",
                "risk_penalty_multiplier": "1.25",
                "max_candidate_ttl_seconds": "60",
                "risk_limits": {
                    "max_order_notional": "25",
                    "max_market_exposure": "50",
                    "max_event_exposure": "75",
                    "max_strategy_exposure": "80",
                    "max_total_exposure": "100",
                    "max_daily_loss": "20",
                    "max_drawdown": "30",
                    "max_open_orders": 5,
                    "max_unmatched_leg_seconds": "10",
                    "max_market_data_age_ms": 1000,
                    "max_user_stream_gap_seconds": "5",
                    "max_order_rate": "2",
                },
                "code_version_hash": "a" * 64,
            }
        ],
    }

    loaded_a = load_profit_system_config(document)
    loaded_b = load_profit_system_config(document)

    assert loaded_a.config_hash == loaded_b.config_hash
    assert loaded_a.strategies[0].config_hash == loaded_b.strategies[0].config_hash
    assert loaded_a.strategies[0].operating_mode is OperatingMode.SHADOW
    assert loaded_a.strategies[0].qualification is StrategyQualification.SHADOW_ONLY


def test_strategy_config_rejects_disallowed_live_mode_for_shadow_only() -> None:
    with pytest.raises(ConfigValidationError, match="disallowed modes"):
        StrategyConfig(
            strategy_id="strategy.alpha",
            strategy_version="v0.2.0",
            hypothesis_summary="Should not allow live.",
            operating_mode=OperatingMode.SHADOW,
            qualification=StrategyQualification.SHADOW_ONLY,
            allowed_modes=(
                OperatingMode.RESEARCH,
                OperatingMode.REPLAY,
                OperatingMode.PAPER,
                OperatingMode.SHADOW,
                OperatingMode.LIVE_CANARY,
            ),
            min_net_edge=Decimal("0.25"),
            min_trade_size=Decimal("5"),
            risk_penalty_multiplier=Decimal("1"),
            max_candidate_ttl_seconds=Decimal("60"),
            risk_limits=_risk_limits(),
            code_version_hash="a" * 64,
        )


def test_profit_system_config_rejects_duplicate_strategy_ids() -> None:
    strategy = StrategyConfig(
        strategy_id="strategy.alpha",
        strategy_version="v0.2.0",
        hypothesis_summary="Duplicate id test.",
        operating_mode=OperatingMode.SHADOW,
        qualification=StrategyQualification.SHADOW_ONLY,
        allowed_modes=(
            OperatingMode.RESEARCH,
            OperatingMode.REPLAY,
            OperatingMode.PAPER,
            OperatingMode.SHADOW,
        ),
        min_net_edge=Decimal("0.25"),
        min_trade_size=Decimal("5"),
        risk_penalty_multiplier=Decimal("1"),
        max_candidate_ttl_seconds=Decimal("60"),
        risk_limits=_risk_limits(),
        code_version_hash="b" * 64,
    )

    with pytest.raises(ConfigValidationError, match="duplicate strategy_id"):
        ProfitSystemConfig(
            strategies=(strategy, strategy),
            generated_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        )


def test_stopped_strategy_can_only_run_in_killed_mode() -> None:
    strategy = StrategyConfig(
        strategy_id="strategy.stopped",
        strategy_version="v0.2.0",
        hypothesis_summary="Stopped strategy stays stopped.",
        operating_mode=OperatingMode.KILLED,
        qualification=StrategyQualification.STOPPED,
        allowed_modes=(OperatingMode.KILLED,),
        min_net_edge=Decimal("0.25"),
        min_trade_size=Decimal("5"),
        risk_penalty_multiplier=Decimal("1"),
        max_candidate_ttl_seconds=Decimal("60"),
        risk_limits=_risk_limits(),
        code_version_hash="c" * 64,
    )

    assert strategy.allows_mode(OperatingMode.KILLED)
    assert not strategy.is_live_mode
