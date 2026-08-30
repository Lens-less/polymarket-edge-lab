"""Frozen configuration contracts for the V0.2 profit-system core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .serialization import canonical_sha256
from .types import (
    OperatingMode,
    ProfitSystemValidationError,
    StrategyQualification,
    parse_decimal,
    parse_utc_datetime,
    validate_identifier,
    validate_version_hash,
)


class ConfigValidationError(ProfitSystemValidationError):
    """Raised when a strategy or system config document is invalid."""


def _coerce_mode(value: OperatingMode | str, *, label: str) -> OperatingMode:
    try:
        return value if isinstance(value, OperatingMode) else OperatingMode(value)
    except ValueError as exc:
        raise ConfigValidationError(f"{label} must be a valid OperatingMode") from exc


def _coerce_qualification(
    value: StrategyQualification | str, *, label: str
) -> StrategyQualification:
    try:
        return value if isinstance(value, StrategyQualification) else StrategyQualification(value)
    except ValueError as exc:
        raise ConfigValidationError(f"{label} must be a valid StrategyQualification") from exc


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal
    max_market_exposure: Decimal
    max_event_exposure: Decimal
    max_strategy_exposure: Decimal
    max_total_exposure: Decimal
    max_daily_loss: Decimal
    max_drawdown: Decimal
    max_open_orders: int
    max_unmatched_leg_seconds: Decimal
    max_market_data_age_ms: int
    max_user_stream_gap_seconds: Decimal
    max_order_rate: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "max_order_notional",
            "max_market_exposure",
            "max_event_exposure",
            "max_strategy_exposure",
            "max_total_exposure",
            "max_daily_loss",
            "max_drawdown",
            "max_unmatched_leg_seconds",
            "max_user_stream_gap_seconds",
            "max_order_rate",
        ):
            object.__setattr__(
                self,
                field_name,
                parse_decimal(
                    getattr(self, field_name),
                    label=f"risk_limits.{field_name}",
                    minimum=Decimal("0"),
                ),
            )
        if isinstance(self.max_open_orders, bool) or self.max_open_orders < 0:
            raise ConfigValidationError("risk_limits.max_open_orders must be >= 0")
        if isinstance(self.max_market_data_age_ms, bool) or self.max_market_data_age_ms < 0:
            raise ConfigValidationError("risk_limits.max_market_data_age_ms must be >= 0")


_QUALIFICATION_ALLOWED_MODES: dict[StrategyQualification, frozenset[OperatingMode]] = {
    StrategyQualification.RESEARCH_ONLY: frozenset(
        {OperatingMode.RESEARCH, OperatingMode.REPLAY, OperatingMode.PAPER}
    ),
    StrategyQualification.SHADOW_ONLY: frozenset(
        {
            OperatingMode.RESEARCH,
            OperatingMode.REPLAY,
            OperatingMode.PAPER,
            OperatingMode.SHADOW,
        }
    ),
    StrategyQualification.PROBE_ALLOWED: frozenset(
        {
            OperatingMode.RESEARCH,
            OperatingMode.REPLAY,
            OperatingMode.PAPER,
            OperatingMode.SHADOW,
            OperatingMode.LIVE_CANARY,
        }
    ),
    StrategyQualification.LIVE_ALLOWED: frozenset(
        {
            OperatingMode.RESEARCH,
            OperatingMode.REPLAY,
            OperatingMode.PAPER,
            OperatingMode.SHADOW,
            OperatingMode.LIVE_CANARY,
            OperatingMode.LIVE_LIMITED,
        }
    ),
    StrategyQualification.STOPPED: frozenset({OperatingMode.KILLED}),
}


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    strategy_id: str
    strategy_version: str
    hypothesis_summary: str
    operating_mode: OperatingMode
    qualification: StrategyQualification
    allowed_modes: tuple[OperatingMode, ...]
    min_net_edge: Decimal
    min_trade_size: Decimal
    risk_penalty_multiplier: Decimal
    max_candidate_ttl_seconds: Decimal
    risk_limits: RiskLimits
    market_whitelist: tuple[str, ...] = ()
    effective_until: datetime | None = None
    code_version_hash: str = "0" * 64
    schema_version: str = "profit-system.strategy-config.v0.2"
    config_hash: str = field(init=False)

    def __post_init__(self) -> None:
        validate_identifier(self.strategy_id, label="strategy_config.strategy_id")
        if not isinstance(self.strategy_version, str) or not self.strategy_version.strip():
            raise ConfigValidationError("strategy_config.strategy_version cannot be blank")
        if not isinstance(self.hypothesis_summary, str) or not self.hypothesis_summary.strip():
            raise ConfigValidationError("strategy_config.hypothesis_summary cannot be blank")
        object.__setattr__(
            self,
            "operating_mode",
            _coerce_mode(self.operating_mode, label="strategy_config.operating_mode"),
        )
        object.__setattr__(
            self,
            "qualification",
            _coerce_qualification(self.qualification, label="strategy_config.qualification"),
        )
        normalized_modes = tuple(
            _coerce_mode(mode, label="strategy_config.allowed_modes") for mode in self.allowed_modes
        )
        if not normalized_modes:
            raise ConfigValidationError("strategy_config.allowed_modes cannot be empty")
        allowed_by_qualification = _QUALIFICATION_ALLOWED_MODES[self.qualification]
        invalid_modes = [
            mode.value for mode in normalized_modes if mode not in allowed_by_qualification
        ]
        if invalid_modes:
            raise ConfigValidationError(
                "strategy_config.allowed_modes contains disallowed modes for "
                f"{self.qualification.value}: {', '.join(invalid_modes)}"
            )
        if self.operating_mode not in normalized_modes:
            raise ConfigValidationError(
                "strategy_config.operating_mode must be included in allowed_modes"
            )
        object.__setattr__(self, "allowed_modes", normalized_modes)
        object.__setattr__(
            self,
            "min_net_edge",
            parse_decimal(
                self.min_net_edge,
                label="strategy_config.min_net_edge",
            ),
        )
        object.__setattr__(
            self,
            "min_trade_size",
            parse_decimal(
                self.min_trade_size,
                label="strategy_config.min_trade_size",
                minimum=Decimal("0"),
            ),
        )
        if self.min_trade_size <= Decimal("0"):
            raise ConfigValidationError("strategy_config.min_trade_size must be > 0")
        object.__setattr__(
            self,
            "risk_penalty_multiplier",
            parse_decimal(
                self.risk_penalty_multiplier,
                label="strategy_config.risk_penalty_multiplier",
                minimum=Decimal("0"),
            ),
        )
        object.__setattr__(
            self,
            "max_candidate_ttl_seconds",
            parse_decimal(
                self.max_candidate_ttl_seconds,
                label="strategy_config.max_candidate_ttl_seconds",
                minimum=Decimal("0"),
            ),
        )
        if self.max_candidate_ttl_seconds <= Decimal("0"):
            raise ConfigValidationError("strategy_config.max_candidate_ttl_seconds must be > 0")
        whitelist = tuple(self.market_whitelist)
        for index, market_id in enumerate(whitelist):
            validate_identifier(market_id, label=f"strategy_config.market_whitelist[{index}]")
        object.__setattr__(self, "market_whitelist", whitelist)
        if self.effective_until is not None:
            object.__setattr__(
                self,
                "effective_until",
                parse_utc_datetime(self.effective_until, label="strategy_config.effective_until"),
            )
        validate_version_hash(self.code_version_hash, label="strategy_config.code_version_hash")
        object.__setattr__(self, "config_hash", canonical_sha256(self._unsigned_document()))

    @property
    def is_live_mode(self) -> bool:
        return self.operating_mode in {
            OperatingMode.LIVE_CANARY,
            OperatingMode.LIVE_LIMITED,
        }

    def allows_mode(self, mode: OperatingMode) -> bool:
        return (
            mode in self.allowed_modes and mode in _QUALIFICATION_ALLOWED_MODES[self.qualification]
        )

    def _unsigned_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "hypothesis_summary": self.hypothesis_summary,
            "operating_mode": self.operating_mode,
            "qualification": self.qualification,
            "allowed_modes": self.allowed_modes,
            "min_net_edge": self.min_net_edge,
            "min_trade_size": self.min_trade_size,
            "risk_penalty_multiplier": self.risk_penalty_multiplier,
            "max_candidate_ttl_seconds": self.max_candidate_ttl_seconds,
            "risk_limits": self.risk_limits,
            "market_whitelist": self.market_whitelist,
            "effective_until": self.effective_until,
            "code_version_hash": self.code_version_hash,
        }


@dataclass(frozen=True, slots=True)
class ProfitSystemConfig:
    strategies: tuple[StrategyConfig, ...]
    generated_at: datetime
    schema_version: str = "profit-system.config.v0.2"
    config_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.strategies:
            raise ConfigValidationError("profit_system_config.strategies cannot be empty")
        object.__setattr__(
            self,
            "generated_at",
            parse_utc_datetime(self.generated_at, label="profit_system_config.generated_at"),
        )
        strategy_ids = [strategy.strategy_id for strategy in self.strategies]
        duplicates = sorted({value for value in strategy_ids if strategy_ids.count(value) > 1})
        if duplicates:
            raise ConfigValidationError(f"duplicate strategy_id values: {', '.join(duplicates)}")
        object.__setattr__(self, "config_hash", canonical_sha256(self._unsigned_document()))

    def strategy_by_id(self, strategy_id: str) -> StrategyConfig:
        for strategy in self.strategies:
            if strategy.strategy_id == strategy_id:
                return strategy
        raise KeyError(strategy_id)

    def _unsigned_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "strategies": self.strategies,
        }


def load_profit_system_config(document: Mapping[str, Any]) -> ProfitSystemConfig:
    """Parse a config mapping into the frozen root config dataclass."""

    if not isinstance(document, Mapping):
        raise ConfigValidationError("config document must be a mapping")
    strategies_raw = document.get("strategies")
    if not isinstance(strategies_raw, list):
        raise ConfigValidationError("config document strategies must be a list")
    strategies = tuple(
        StrategyConfig(
            strategy_id=item["strategy_id"],
            strategy_version=item["strategy_version"],
            hypothesis_summary=item["hypothesis_summary"],
            operating_mode=item["operating_mode"],
            qualification=item["qualification"],
            allowed_modes=tuple(item["allowed_modes"]),
            min_net_edge=item["min_net_edge"],
            min_trade_size=item["min_trade_size"],
            risk_penalty_multiplier=item.get("risk_penalty_multiplier", "1"),
            max_candidate_ttl_seconds=item["max_candidate_ttl_seconds"],
            risk_limits=RiskLimits(**item["risk_limits"]),
            market_whitelist=tuple(item.get("market_whitelist", ())),
            effective_until=item.get("effective_until"),
            code_version_hash=item["code_version_hash"],
            schema_version=item.get("schema_version", "profit-system.strategy-config.v0.2"),
        )
        for item in strategies_raw
    )
    return ProfitSystemConfig(
        strategies=strategies,
        generated_at=document["generated_at"],
        schema_version=document.get("schema_version", "profit-system.config.v0.2"),
    )
