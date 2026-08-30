from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any, TypeAlias, cast

from ..execution.models import OrderLifecycleStatus

SCHEMA_VERSION = "profit-system.v0.2"


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_wire_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return isoformat(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return cast(
            dict[str, Any],
            {field.name: to_wire_value(getattr(value, field.name)) for field in fields(value)},
        )
    if isinstance(value, Mapping):
        return {str(key): to_wire_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_wire_value(item) for item in value]
    if isinstance(value, tuple):
        return [to_wire_value(item) for item in value]
    return value


class DeskMode(StrEnum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE_CANARY = "LIVE_CANARY"
    LIVE_LIMITED = "LIVE_LIMITED"
    KILLED = "KILLED"


class ConnectionState(StrEnum):
    LIVE = "live"
    STALE = "stale"
    DISCONNECTED = "disconnected"
    BLOCKED = "blocked"


class OpportunityState(StrEnum):
    REJECTED = "REJECTED"
    WATCH = "WATCH"
    REVIEW = "REVIEW"
    EXECUTABLE = "EXECUTABLE"


class PlanState(StrEnum):
    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    CANCELED = "CANCELED"
    KILLED = "KILLED"


OrderState: TypeAlias = OrderLifecycleStatus


@dataclass
class PnlWindow:
    today: Decimal
    seven_day: Decimal
    thirty_day: Decimal


@dataclass
class DeskConnectionStatus:
    data: ConnectionState
    orders: ConnectionState
    market_data_age_ms: int | None
    blocked_reason: str | None = None


@dataclass
class DeskStatusBar:
    mode: DeskMode
    available_cash: Decimal
    realized_net_pnl: PnlWindow
    current_drawdown: Decimal
    risk_budget_used: Decimal
    connections: DeskConnectionStatus
    is_live_locked: bool
    kill_switch_engaged: bool
    mode_banner: str


@dataclass
class OpportunitySummary:
    id: str
    rank: int
    strategy: str
    market: str
    tradable_edge: Decimal
    expected_net_profit: Decimal
    max_loss: Decimal
    expected_capacity: Decimal
    confidence: Decimal
    ttl_seconds: int
    state: OpportunityState
    note: str


@dataclass
class LadderLevel:
    side: str
    price: Decimal
    size: Decimal


@dataclass
class RecentTrade:
    time: str
    side: str
    price: Decimal
    size: Decimal


@dataclass
class RelatedMarket:
    label: str
    relationship: str
    mark: Decimal


@dataclass
class CostItem:
    label: str
    amount: Decimal
    note: str


@dataclass
class ScenarioItem:
    name: str
    net_profit: Decimal
    worst_case_loss: Decimal
    confidence: Decimal


@dataclass
class DepthItem:
    level: str
    price: Decimal
    executable_size: Decimal


@dataclass
class ExplanationSnapshot:
    opportunity_id: str
    market_title: str
    settlement: str
    ladder: list[LadderLevel]
    recent_trades: list[RecentTrade]
    related_markets: list[RelatedMarket]
    reason_summary: list[str]
    cost_breakdown: list[CostItem]
    scenarios: list[ScenarioItem]
    executable_depth: list[DepthItem]


@dataclass
class PlanLeg:
    id: str
    venue: str
    side: str
    instrument: str
    price: Decimal
    size: Decimal
    order_type: str
    post_only: bool


@dataclass
class ExecutionPlanSnapshot:
    id: str
    opportunity_id: str
    state: PlanState
    review_required: bool
    live_lock_reason: str | None
    estimated_fee: Decimal
    estimated_slippage: Decimal
    expected_net_profit: Decimal
    worst_case_loss: Decimal
    risk_budget_change: Decimal
    execution_notes: list[str]
    failure_path: str
    legs: list[PlanLeg]


@dataclass
class OrderSnapshot:
    id: str
    plan_id: str
    market: str
    state: OrderState
    side: str
    price: Decimal
    size: Decimal
    filled_size: Decimal
    venue_status: str
    updated_at: str


@dataclass
class FillSnapshot:
    id: str
    order_id: str
    market: str
    side: str
    price: Decimal
    size: Decimal
    fee: Decimal
    realized_net_pnl: Decimal
    occurred_at: str


@dataclass
class PositionSnapshot:
    id: str
    market: str
    side: str
    quantity: Decimal
    avg_price: Decimal
    mark_price: Decimal
    realized_net_pnl: Decimal
    unrealized_net_pnl: Decimal


@dataclass
class StrategyPnlSnapshot:
    strategy: str
    realized_today: Decimal
    realized_seven_day: Decimal
    realized_thirty_day: Decimal
    mark_to_market: Decimal


@dataclass
class ExpectedVsRealizedSnapshot:
    strategy: str
    expected_net_pnl: Decimal
    realized_net_pnl: Decimal
    variance: Decimal


@dataclass
class ReconciliationDiff:
    scope: str
    severity: str
    message: str


@dataclass
class ReconciliationSnapshot:
    status: str
    last_run_at: str
    summary: str
    differences: list[ReconciliationDiff]


@dataclass
class DeskSnapshot:
    schema_version: str
    snapshot_version: int
    generated_at: datetime
    scenario: str
    session: str
    status_bar: DeskStatusBar
    selected_opportunity_id: str
    opportunities: list[OpportunitySummary]
    explanation: ExplanationSnapshot
    execution_plan: ExecutionPlanSnapshot
    orders: list[OrderSnapshot]
    fills: list[FillSnapshot]
    positions: list[PositionSnapshot]
    strategy_pnl: list[StrategyPnlSnapshot]
    expected_vs_realized: list[ExpectedVsRealizedSnapshot]
    reconciliation: ReconciliationSnapshot
    csrf_token: str
    action_log: list[str] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return cast(dict[str, Any], to_wire_value(self))


@dataclass
class MutationRequest:
    actor: str
    idempotency_key: str
    opportunity_id: str | None = None
    order_id: str | None = None


@dataclass
class MutationResult:
    action: str
    accepted: bool
    message: str
    snapshot: DeskSnapshot

    def to_wire(self) -> dict[str, Any]:
        payload = {
            "action": self.action,
            "accepted": self.accepted,
            "message": self.message,
            "snapshot": self.snapshot.to_wire(),
        }
        return cast(dict[str, Any], payload)
