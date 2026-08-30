from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from src.edge_lab.data_store import canonical_json_bytes

ZERO = Decimal("0")
ACTIVE_ORDER_STATES = frozenset(
    {
        "approved",
        "submitting",
        "live",
        "delayed",
        "matched",
        "partially_filled",
        "cancel_requested",
        "ambiguous",
        "reconciling",
        "attention_required",
    }
)


class PersistenceError(RuntimeError):
    """Base error raised for persistence contract violations."""


class IdempotencyConflictError(PersistenceError):
    """Raised when an idempotency key is re-used for a different request."""


class DuplicateVenueIdentifierError(PersistenceError):
    """Raised when a venue order or fill identifier is reused."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ensure_text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _ensure_optional_text(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    return _ensure_text(value, name=name)


def _decimal_text(value: Decimal | str | int, *, name: str) -> str:
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Decimal exceptions vary
        raise ValueError(f"{name} must be a finite Decimal-compatible value") from exc
    if not decimal_value.is_finite():
        raise ValueError(f"{name} must be finite")
    return str(decimal_value)


def _optional_decimal_text(value: Decimal | str | int | None, *, name: str) -> str | None:
    if value is None:
        return None
    return _decimal_text(value, name=name)


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value)


def _json_text(value: Any) -> str:
    payload_bytes = bytes(canonical_json_bytes(value))
    return payload_bytes.decode("utf-8")


def _json_value(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _bool_as_int(value: bool) -> int:
    return 1 if value else 0


@dataclass(frozen=True)
class VersionStamp:
    strategy_id: str
    strategy_config_version: str
    code_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_id", _ensure_text(self.strategy_id, name="strategy_id"))
        object.__setattr__(
            self,
            "strategy_config_version",
            _ensure_text(self.strategy_config_version, name="strategy_config_version"),
        )
        object.__setattr__(
            self, "code_version", _ensure_text(self.code_version, name="code_version")
        )

    def to_document(self) -> dict[str, str]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_config_version": self.strategy_config_version,
            "code_version": self.code_version,
        }


@dataclass(frozen=True)
class MarketSnapshotRecord:
    snapshot_id: str
    market_id: str
    token_id: str
    captured_at: str
    book: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: VersionStamp = field(
        default_factory=lambda: VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="system",
        )
    )


@dataclass(frozen=True)
class OpportunityRecord:
    opportunity_id: str
    market_id: str
    token_id: str
    expected_net_edge: Decimal
    status: str
    observed_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: VersionStamp = field(
        default_factory=lambda: VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="system",
        )
    )


@dataclass(frozen=True)
class StrategyDecisionRecord:
    decision_id: str
    opportunity_id: str
    strategy_id: str
    decision: str
    expected_net_edge: Decimal | None
    decided_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: VersionStamp = field(
        default_factory=lambda: VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="system",
        )
    )


@dataclass(frozen=True)
class ExecutionPlanRecord:
    plan_id: str
    decision_id: str | None
    strategy_id: str
    status: str
    expected_net_edge: Decimal | None
    plan_kind: str
    created_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: VersionStamp = field(
        default_factory=lambda: VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="system",
        )
    )


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    venue_order_id: str | None
    lifecycle_state: str
    market_id: str
    token_id: str
    side: str
    price: Decimal
    quantity: Decimal
    filled_quantity: Decimal = ZERO
    remaining_quantity: Decimal | None = None
    order_role: str = "unknown"
    parent_order_id: str | None = None
    updated_at: str = field(default_factory=_utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: VersionStamp = field(
        default_factory=lambda: VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="system",
        )
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", _ensure_text(self.order_id, name="order_id"))
        object.__setattr__(
            self,
            "venue_order_id",
            _ensure_optional_text(self.venue_order_id, name="venue_order_id"),
        )
        object.__setattr__(
            self,
            "lifecycle_state",
            _ensure_text(self.lifecycle_state, name="lifecycle_state").lower(),
        )
        object.__setattr__(self, "market_id", _ensure_text(self.market_id, name="market_id"))
        object.__setattr__(self, "token_id", _ensure_text(self.token_id, name="token_id"))
        side = _ensure_text(self.side, name="side").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "price", Decimal(_decimal_text(self.price, name="price")))
        object.__setattr__(self, "quantity", Decimal(_decimal_text(self.quantity, name="quantity")))
        object.__setattr__(
            self,
            "filled_quantity",
            Decimal(_decimal_text(self.filled_quantity, name="filled_quantity")),
        )
        remaining = (
            self.quantity - self.filled_quantity
            if self.remaining_quantity is None
            else Decimal(_decimal_text(self.remaining_quantity, name="remaining_quantity"))
        )
        object.__setattr__(self, "remaining_quantity", remaining)
        object.__setattr__(
            self,
            "order_role",
            _ensure_text(self.order_role, name="order_role").lower(),
        )
        object.__setattr__(
            self,
            "parent_order_id",
            _ensure_optional_text(self.parent_order_id, name="parent_order_id"),
        )
        object.__setattr__(self, "updated_at", _ensure_text(self.updated_at, name="updated_at"))

    def to_document(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "venue_order_id": self.venue_order_id,
            "lifecycle_state": self.lifecycle_state,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "order_role": self.order_role,
            "parent_order_id": self.parent_order_id,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
            **self.version.to_document(),
        }


@dataclass(frozen=True)
class FillRecord:
    fill_id: str
    venue_fill_id: str | None
    order_id: str
    token_id: str
    side: str
    price: Decimal
    quantity: Decimal
    occurred_at: str
    fee_amount: Decimal = ZERO
    slippage_amount: Decimal = ZERO
    unwind_amount: Decimal = ZERO
    onchain_amount: Decimal = ZERO
    liquidity_role: str = "unknown"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: VersionStamp = field(
        default_factory=lambda: VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="system",
        )
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", _ensure_text(self.fill_id, name="fill_id"))
        object.__setattr__(
            self,
            "venue_fill_id",
            _ensure_optional_text(self.venue_fill_id, name="venue_fill_id"),
        )
        object.__setattr__(self, "order_id", _ensure_text(self.order_id, name="order_id"))
        object.__setattr__(self, "token_id", _ensure_text(self.token_id, name="token_id"))
        side = _ensure_text(self.side, name="side").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        object.__setattr__(self, "side", side)
        for field_name in (
            "price",
            "quantity",
            "fee_amount",
            "slippage_amount",
            "unwind_amount",
            "onchain_amount",
        ):
            object.__setattr__(
                self,
                field_name,
                Decimal(_decimal_text(getattr(self, field_name), name=field_name)),
            )
        object.__setattr__(
            self,
            "liquidity_role",
            _ensure_text(self.liquidity_role, name="liquidity_role").lower(),
        )
        object.__setattr__(self, "occurred_at", _ensure_text(self.occurred_at, name="occurred_at"))

    def dedupe_key(self) -> str:
        return self.venue_fill_id or self.fill_id

    def to_document(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "venue_fill_id": self.venue_fill_id,
            "order_id": self.order_id,
            "token_id": self.token_id,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "occurred_at": self.occurred_at,
            "fee_amount": self.fee_amount,
            "slippage_amount": self.slippage_amount,
            "unwind_amount": self.unwind_amount,
            "onchain_amount": self.onchain_amount,
            "liquidity_role": self.liquidity_role,
            "metadata": dict(self.metadata),
            **self.version.to_document(),
        }


@dataclass(frozen=True)
class PositionSnapshotRecord:
    snapshot_id: str
    token_id: str
    as_of: str
    net_quantity: Decimal
    average_cost: Decimal | None
    market_price: Decimal | None
    open_buy_quantity: Decimal = ZERO
    open_sell_quantity: Decimal = ZERO
    realized_trading_pnl: Decimal = ZERO
    fee_pnl: Decimal = ZERO
    slippage_pnl: Decimal = ZERO
    unwind_pnl: Decimal = ZERO
    onchain_pnl: Decimal = ZERO
    incentive_pnl_confirmed: Decimal = ZERO
    incentive_pnl_pending: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: VersionStamp = field(
        default_factory=lambda: VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="system",
        )
    )


@dataclass(frozen=True)
class PnLAttributionRecord:
    attribution_id: str
    source_type: str
    source_id: str
    token_id: str
    strategy_id: str
    occurred_at: str
    realized_trading_pnl: Decimal = ZERO
    fee_pnl: Decimal = ZERO
    slippage_pnl: Decimal = ZERO
    unwind_pnl: Decimal = ZERO
    onchain_pnl: Decimal = ZERO
    incentive_pnl_confirmed: Decimal = ZERO
    incentive_pnl_pending: Decimal = ZERO
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: VersionStamp = field(
        default_factory=lambda: VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="system",
        )
    )


@dataclass(frozen=True)
class ReconciliationRunRecord:
    run_id: str
    scope: str
    status: str
    venue_snapshot: Mapping[str, Any]
    report: Mapping[str, Any]
    unresolved_count: int
    created_at: str
    version: VersionStamp = field(
        default_factory=lambda: VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="system",
        )
    )


@dataclass(frozen=True)
class RiskEventRecord:
    event_id: str
    event_type: str
    severity: str
    occurred_at: str
    kill_switch_engaged: bool
    details: Mapping[str, Any] = field(default_factory=dict)
    version: VersionStamp = field(
        default_factory=lambda: VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="system",
        )
    )


@dataclass(frozen=True)
class StrategyEvaluationWindowRecord:
    window_id: str
    strategy_id: str
    started_at: str
    ended_at: str
    metrics: Mapping[str, Any]
    version: VersionStamp


@dataclass(frozen=True)
class StrategyQualificationRecord:
    qualification_id: str
    strategy_id: str
    qualified: bool
    status: str
    observed_at: str
    details: Mapping[str, Any]
    version: VersionStamp


@dataclass(frozen=True)
class JournalEvent:
    sequence: int
    event_type: str
    aggregate_type: str
    aggregate_id: str
    idempotency_key: str
    payload: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True)
class IdempotentResult:
    sequence: int
    payload: Mapping[str, Any]
    replayed: bool


def _row_to_order(row: sqlite3.Row) -> OrderRecord:
    return OrderRecord(
        order_id=row["order_id"],
        venue_order_id=row["venue_order_id"],
        lifecycle_state=row["lifecycle_state"],
        market_id=row["market_id"],
        token_id=row["token_id"],
        side=row["side"],
        price=Decimal(row["price"]),
        quantity=Decimal(row["quantity"]),
        filled_quantity=Decimal(row["filled_quantity"]),
        remaining_quantity=Decimal(row["remaining_quantity"]),
        order_role=row["order_role"],
        parent_order_id=row["parent_order_id"],
        updated_at=row["updated_at"],
        metadata=_json_value(row["payload_json"]).get("metadata", {}),
        version=VersionStamp(
            strategy_id=row["strategy_id"],
            strategy_config_version=row["strategy_config_version"],
            code_version=row["code_version"],
        ),
    )


def _row_to_fill(row: sqlite3.Row) -> FillRecord:
    return FillRecord(
        fill_id=row["fill_id"],
        venue_fill_id=row["venue_fill_id"],
        order_id=row["order_id"],
        token_id=row["token_id"],
        side=row["side"],
        price=Decimal(row["price"]),
        quantity=Decimal(row["quantity"]),
        occurred_at=row["occurred_at"],
        fee_amount=Decimal(row["fee_amount"]),
        slippage_amount=Decimal(row["slippage_amount"]),
        unwind_amount=Decimal(row["unwind_amount"]),
        onchain_amount=Decimal(row["onchain_amount"]),
        liquidity_role=row["liquidity_role"],
        metadata=_json_value(row["payload_json"]).get("metadata", {}),
        version=VersionStamp(
            strategy_id=row["strategy_id"],
            strategy_config_version=row["strategy_config_version"],
            code_version=row["code_version"],
        ),
    )


def _row_to_pnl(row: sqlite3.Row) -> PnLAttributionRecord:
    payload = _json_value(row["payload_json"]) or {}
    return PnLAttributionRecord(
        attribution_id=row["attribution_id"],
        source_type=row["source_type"],
        source_id=row["source_id"],
        token_id=row["token_id"],
        strategy_id=row["strategy_id"],
        occurred_at=row["occurred_at"],
        realized_trading_pnl=Decimal(row["realized_trading_pnl"]),
        fee_pnl=Decimal(row["fee_pnl"]),
        slippage_pnl=Decimal(row["slippage_pnl"]),
        unwind_pnl=Decimal(row["unwind_pnl"]),
        onchain_pnl=Decimal(row["onchain_pnl"]),
        incentive_pnl_confirmed=Decimal(row["incentive_pnl_confirmed"]),
        incentive_pnl_pending=Decimal(row["incentive_pnl_pending"]),
        metadata=payload.get("metadata", {}),
        version=VersionStamp(
            strategy_id=row["strategy_id"],
            strategy_config_version=row["strategy_config_version"],
            code_version=row["code_version"],
        ),
    )


def _row_to_reconciliation(row: sqlite3.Row) -> ReconciliationRunRecord:
    return ReconciliationRunRecord(
        run_id=row["run_id"],
        scope=row["scope"],
        status=row["status"],
        venue_snapshot=_json_value(row["venue_snapshot_json"]) or {},
        report=_json_value(row["report_json"]) or {},
        unresolved_count=row["unresolved_count"],
        created_at=row["created_at"],
        version=VersionStamp(
            strategy_id=row["strategy_id"],
            strategy_config_version=row["strategy_config_version"],
            code_version=row["code_version"],
        ),
    )


def _row_to_position(row: sqlite3.Row) -> PositionSnapshotRecord:
    payload = _json_value(row["payload_json"]) or {}
    return PositionSnapshotRecord(
        snapshot_id=row["snapshot_id"],
        token_id=row["token_id"],
        as_of=row["as_of"],
        net_quantity=Decimal(row["net_quantity"]),
        average_cost=_parse_decimal(row["average_cost"]),
        market_price=_parse_decimal(row["market_price"]),
        open_buy_quantity=Decimal(row["open_buy_quantity"]),
        open_sell_quantity=Decimal(row["open_sell_quantity"]),
        realized_trading_pnl=Decimal(row["realized_trading_pnl"]),
        fee_pnl=Decimal(row["fee_pnl"]),
        slippage_pnl=Decimal(row["slippage_pnl"]),
        unwind_pnl=Decimal(row["unwind_pnl"]),
        onchain_pnl=Decimal(row["onchain_pnl"]),
        incentive_pnl_confirmed=Decimal(row["incentive_pnl_confirmed"]),
        incentive_pnl_pending=Decimal(row["incentive_pnl_pending"]),
        unrealized_pnl=Decimal(row["unrealized_pnl"]),
        metadata=payload.get("metadata", {}),
        version=VersionStamp(
            strategy_id=row["strategy_id"],
            strategy_config_version=row["strategy_config_version"],
            code_version=row["code_version"],
        ),
    )


class PersistenceStore:
    """SQLite WAL store for the V0.2 profit-system persistence contract."""

    _MIGRATIONS: tuple[tuple[int, str], ...] = (
        (
            1,
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sequence_state (
                name TEXT PRIMARY KEY,
                next_value INTEGER NOT NULL
            );

            INSERT OR IGNORE INTO sequence_state(name, next_value)
            VALUES ('global', 1);

            CREATE TABLE IF NOT EXISTS idempotency_keys (
                idempotency_key TEXT PRIMARY KEY,
                mutation_type TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_journal (
                sequence INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS market_snapshots (
                sequence INTEGER PRIMARY KEY,
                snapshot_id TEXT NOT NULL UNIQUE,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                book_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                sequence INTEGER PRIMARY KEY,
                opportunity_id TEXT NOT NULL UNIQUE,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                expected_net_edge TEXT NOT NULL,
                status TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy_decisions (
                sequence INTEGER PRIMARY KEY,
                decision_id TEXT NOT NULL UNIQUE,
                opportunity_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                expected_net_edge TEXT,
                decided_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_plans (
                sequence INTEGER PRIMARY KEY,
                plan_id TEXT NOT NULL UNIQUE,
                decision_id TEXT,
                strategy_id TEXT NOT NULL,
                status TEXT NOT NULL,
                plan_kind TEXT NOT NULL,
                expected_net_edge TEXT,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                sequence INTEGER NOT NULL,
                order_id TEXT NOT NULL PRIMARY KEY,
                venue_order_id TEXT UNIQUE,
                lifecycle_state TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                filled_quantity TEXT NOT NULL,
                remaining_quantity TEXT NOT NULL,
                order_role TEXT NOT NULL,
                parent_order_id TEXT,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fills (
                sequence INTEGER PRIMARY KEY,
                fill_id TEXT NOT NULL UNIQUE,
                venue_fill_id TEXT UNIQUE,
                order_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                fee_amount TEXT NOT NULL,
                slippage_amount TEXT NOT NULL,
                unwind_amount TEXT NOT NULL,
                onchain_amount TEXT NOT NULL,
                liquidity_role TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                sequence INTEGER PRIMARY KEY,
                snapshot_id TEXT NOT NULL UNIQUE,
                token_id TEXT NOT NULL,
                as_of TEXT NOT NULL,
                net_quantity TEXT NOT NULL,
                average_cost TEXT,
                market_price TEXT,
                open_buy_quantity TEXT NOT NULL,
                open_sell_quantity TEXT NOT NULL,
                realized_trading_pnl TEXT NOT NULL,
                fee_pnl TEXT NOT NULL,
                slippage_pnl TEXT NOT NULL,
                unwind_pnl TEXT NOT NULL,
                onchain_pnl TEXT NOT NULL,
                incentive_pnl_confirmed TEXT NOT NULL,
                incentive_pnl_pending TEXT NOT NULL,
                unrealized_pnl TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pnl_attribution (
                sequence INTEGER PRIMARY KEY,
                attribution_id TEXT NOT NULL UNIQUE,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL UNIQUE,
                token_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                realized_trading_pnl TEXT NOT NULL,
                fee_pnl TEXT NOT NULL,
                slippage_pnl TEXT NOT NULL,
                unwind_pnl TEXT NOT NULL,
                onchain_pnl TEXT NOT NULL,
                incentive_pnl_confirmed TEXT NOT NULL,
                incentive_pnl_pending TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reconciliation_runs (
                sequence INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                venue_snapshot_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                unresolved_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS risk_events (
                sequence INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                kill_switch_engaged INTEGER NOT NULL,
                details_json TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy_evaluation_windows (
                sequence INTEGER PRIMARY KEY,
                window_id TEXT NOT NULL UNIQUE,
                strategy_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS system_state (
                state_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_sequence INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy_qualifications (
                sequence INTEGER PRIMARY KEY,
                qualification_id TEXT NOT NULL UNIQUE,
                strategy_id TEXT NOT NULL,
                qualified INTEGER NOT NULL,
                status TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                details_json TEXT NOT NULL,
                strategy_config_version TEXT NOT NULL,
                code_version TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_orders_token_state
                ON orders(token_id, lifecycle_state);
            CREATE INDEX IF NOT EXISTS idx_fills_order
                ON fills(order_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_positions_token_asof
                ON positions(token_id, as_of);
            CREATE INDEX IF NOT EXISTS idx_pnl_token_time
                ON pnl_attribution(token_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_reconciliation_created
                ON reconciliation_runs(created_at);
            """,
        ),
    )

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = self._open_connection()
        self._apply_migrations()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL;")
        connection.execute("PRAGMA synchronous=FULL;")
        connection.execute("PRAGMA foreign_keys=ON;")
        connection.execute("PRAGMA busy_timeout=5000;")
        return connection

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _apply_migrations(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            }
            for version, sql in self._MIGRATIONS:
                if version in applied:
                    continue
                connection.executescript(sql)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (?, ?)
                    """,
                    (version, _utc_now()),
                )

    def _reserve_sequence(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT next_value FROM sequence_state WHERE name = 'global'"
        ).fetchone()
        if row is None:
            raise PersistenceError("global sequence is missing")
        sequence = int(row["next_value"])
        connection.execute(
            "UPDATE sequence_state SET next_value = ? WHERE name = 'global'",
            (sequence + 1,),
        )
        return sequence

    def _claim_idempotency(
        self,
        connection: sqlite3.Connection,
        *,
        idempotency_key: str,
        mutation_type: str,
        request_hash: str,
    ) -> IdempotentResult | None:
        existing = connection.execute(
            """
            SELECT mutation_type, request_hash, response_json, sequence
            FROM idempotency_keys
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if existing is None:
            return None
        if existing["mutation_type"] != mutation_type or existing["request_hash"] != request_hash:
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} was already used for a different request"
            )
        return IdempotentResult(
            sequence=int(existing["sequence"]),
            payload=_json_value(existing["response_json"]) or {},
            replayed=True,
        )

    def _persist_idempotency(
        self,
        connection: sqlite3.Connection,
        *,
        idempotency_key: str,
        mutation_type: str,
        request_hash: str,
        sequence: int,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO idempotency_keys(
                idempotency_key,
                mutation_type,
                request_hash,
                response_json,
                sequence,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                mutation_type,
                request_hash,
                _json_text(payload),
                sequence,
                _utc_now(),
            ),
        )

    def _append_journal(
        self,
        connection: sqlite3.Connection,
        *,
        sequence: int,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO execution_journal(
                sequence,
                event_type,
                aggregate_type,
                aggregate_id,
                idempotency_key,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence,
                event_type,
                aggregate_type,
                aggregate_id,
                idempotency_key,
                _json_text(payload),
                _utc_now(),
            ),
        )

    def _commit_mutation(
        self,
        *,
        idempotency_key: str,
        mutation_type: str,
        aggregate_type: str,
        aggregate_id: str,
        request_document: Mapping[str, Any],
        apply: Callable[[sqlite3.Connection, int], Mapping[str, Any]],
    ) -> IdempotentResult:
        request_hash = canonical_json_bytes(request_document).hex()
        with self._transaction() as connection:
            replay = self._claim_idempotency(
                connection,
                idempotency_key=idempotency_key,
                mutation_type=mutation_type,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay
            sequence = self._reserve_sequence(connection)
            try:
                response = apply(connection, sequence)
            except sqlite3.IntegrityError as exc:
                message = str(exc).lower()
                if "venue_order_id" in message or "venue_fill_id" in message:
                    raise DuplicateVenueIdentifierError(str(exc)) from exc
                raise
            self._append_journal(
                connection,
                sequence=sequence,
                event_type=mutation_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                idempotency_key=idempotency_key,
                payload=request_document,
            )
            self._persist_idempotency(
                connection,
                idempotency_key=idempotency_key,
                mutation_type=mutation_type,
                request_hash=request_hash,
                sequence=sequence,
                payload=response,
            )
            return IdempotentResult(sequence=sequence, payload=response, replayed=False)

    def record_market_snapshot(
        self, record: MarketSnapshotRecord, *, idempotency_key: str
    ) -> IdempotentResult:
        document: dict[str, Any] = {
            "snapshot_id": _ensure_text(record.snapshot_id, name="snapshot_id"),
            "market_id": _ensure_text(record.market_id, name="market_id"),
            "token_id": _ensure_text(record.token_id, name="token_id"),
            "captured_at": _ensure_text(record.captured_at, name="captured_at"),
            "book": dict(record.book),
            "metadata": dict(record.metadata),
            **record.version.to_document(),
        }

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO market_snapshots(
                    sequence, snapshot_id, market_id, token_id, captured_at,
                    book_json, metadata_json, strategy_id, strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    document["snapshot_id"],
                    document["market_id"],
                    document["token_id"],
                    document["captured_at"],
                    _json_text(document["book"]),
                    _json_text(document["metadata"]),
                    document["strategy_id"],
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {"snapshot_id": document["snapshot_id"], "sequence": sequence}

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="market_snapshot.recorded",
            aggregate_type="market_snapshot",
            aggregate_id=document["snapshot_id"],
            request_document=document,
            apply=apply,
        )

    def record_opportunity(
        self, record: OpportunityRecord, *, idempotency_key: str
    ) -> IdempotentResult:
        document: dict[str, Any] = {
            "opportunity_id": _ensure_text(record.opportunity_id, name="opportunity_id"),
            "market_id": _ensure_text(record.market_id, name="market_id"),
            "token_id": _ensure_text(record.token_id, name="token_id"),
            "expected_net_edge": record.expected_net_edge,
            "status": _ensure_text(record.status, name="status").lower(),
            "observed_at": _ensure_text(record.observed_at, name="observed_at"),
            "metadata": dict(record.metadata),
            **record.version.to_document(),
        }

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO opportunities(
                    sequence, opportunity_id, market_id, token_id, expected_net_edge,
                    status, observed_at, payload_json, strategy_id,
                    strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    document["opportunity_id"],
                    document["market_id"],
                    document["token_id"],
                    _decimal_text(document["expected_net_edge"], name="expected_net_edge"),
                    document["status"],
                    document["observed_at"],
                    _json_text(document),
                    document["strategy_id"],
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {"opportunity_id": document["opportunity_id"], "sequence": sequence}

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="opportunity.recorded",
            aggregate_type="opportunity",
            aggregate_id=document["opportunity_id"],
            request_document=document,
            apply=apply,
        )

    def record_strategy_decision(
        self, record: StrategyDecisionRecord, *, idempotency_key: str
    ) -> IdempotentResult:
        document: dict[str, Any] = {
            "decision_id": _ensure_text(record.decision_id, name="decision_id"),
            "opportunity_id": _ensure_text(record.opportunity_id, name="opportunity_id"),
            "strategy_id": _ensure_text(record.strategy_id, name="strategy_id"),
            "decision": _ensure_text(record.decision, name="decision").lower(),
            "expected_net_edge": record.expected_net_edge,
            "decided_at": _ensure_text(record.decided_at, name="decided_at"),
            "metadata": dict(record.metadata),
            **record.version.to_document(),
        }

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO strategy_decisions(
                    sequence, decision_id, opportunity_id, strategy_id, decision,
                    expected_net_edge, decided_at, payload_json,
                    strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    document["decision_id"],
                    document["opportunity_id"],
                    document["strategy_id"],
                    document["decision"],
                    _optional_decimal_text(
                        cast(Decimal | str | int | None, document["expected_net_edge"]),
                        name="expected_net_edge",
                    ),
                    document["decided_at"],
                    _json_text(document),
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {"decision_id": document["decision_id"], "sequence": sequence}

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="strategy_decision.recorded",
            aggregate_type="strategy_decision",
            aggregate_id=document["decision_id"],
            request_document=document,
            apply=apply,
        )

    def record_execution_plan(
        self, record: ExecutionPlanRecord, *, idempotency_key: str
    ) -> IdempotentResult:
        plan_id = _ensure_text(record.plan_id, name="plan_id")
        decision_id = _ensure_optional_text(record.decision_id, name="decision_id")
        strategy_id = _ensure_text(record.strategy_id, name="strategy_id")
        status = _ensure_text(record.status, name="status").lower()
        expected_net_edge = record.expected_net_edge
        plan_kind = _ensure_text(record.plan_kind, name="plan_kind").lower()
        created_at = _ensure_text(record.created_at, name="created_at")
        document: dict[str, Any] = {
            "plan_id": plan_id,
            "decision_id": decision_id,
            "strategy_id": strategy_id,
            "status": status,
            "expected_net_edge": expected_net_edge,
            "plan_kind": plan_kind,
            "created_at": created_at,
            "metadata": dict(record.metadata),
            **record.version.to_document(),
        }

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO execution_plans(
                    sequence, plan_id, decision_id, strategy_id, status, plan_kind,
                    expected_net_edge, created_at, payload_json,
                    strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    plan_id,
                    decision_id,
                    strategy_id,
                    status,
                    plan_kind,
                    _optional_decimal_text(expected_net_edge, name="expected_net_edge"),
                    created_at,
                    _json_text(document),
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {"plan_id": plan_id, "sequence": sequence}

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="execution_plan.recorded",
            aggregate_type="execution_plan",
            aggregate_id=plan_id,
            request_document=document,
            apply=apply,
        )

    def upsert_order(self, record: OrderRecord, *, idempotency_key: str) -> IdempotentResult:
        document: dict[str, Any] = record.to_document()

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO orders(
                    sequence, order_id, venue_order_id, lifecycle_state, market_id, token_id,
                    side, price, quantity, filled_quantity, remaining_quantity, order_role,
                    parent_order_id, updated_at, payload_json, strategy_id,
                    strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    sequence = excluded.sequence,
                    venue_order_id = excluded.venue_order_id,
                    lifecycle_state = excluded.lifecycle_state,
                    market_id = excluded.market_id,
                    token_id = excluded.token_id,
                    side = excluded.side,
                    price = excluded.price,
                    quantity = excluded.quantity,
                    filled_quantity = excluded.filled_quantity,
                    remaining_quantity = excluded.remaining_quantity,
                    order_role = excluded.order_role,
                    parent_order_id = excluded.parent_order_id,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json,
                    strategy_id = excluded.strategy_id,
                    strategy_config_version = excluded.strategy_config_version,
                    code_version = excluded.code_version
                """,
                (
                    sequence,
                    document["order_id"],
                    document["venue_order_id"],
                    document["lifecycle_state"],
                    document["market_id"],
                    document["token_id"],
                    document["side"],
                    _decimal_text(document["price"], name="price"),
                    _decimal_text(document["quantity"], name="quantity"),
                    _decimal_text(document["filled_quantity"], name="filled_quantity"),
                    _decimal_text(document["remaining_quantity"], name="remaining_quantity"),
                    document["order_role"],
                    document["parent_order_id"],
                    document["updated_at"],
                    _json_text(document),
                    document["strategy_id"],
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {
                "order_id": document["order_id"],
                "lifecycle_state": document["lifecycle_state"],
                "sequence": sequence,
            }

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="order.upserted",
            aggregate_type="order",
            aggregate_id=document["order_id"],
            request_document=document,
            apply=apply,
        )

    def record_fill(self, record: FillRecord, *, idempotency_key: str) -> IdempotentResult:
        document: dict[str, Any] = record.to_document()

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO fills(
                    sequence, fill_id, venue_fill_id, order_id, token_id, side, price,
                    quantity, occurred_at, fee_amount, slippage_amount, unwind_amount,
                    onchain_amount, liquidity_role, payload_json, strategy_id,
                    strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    document["fill_id"],
                    document["venue_fill_id"],
                    document["order_id"],
                    document["token_id"],
                    document["side"],
                    _decimal_text(document["price"], name="price"),
                    _decimal_text(document["quantity"], name="quantity"),
                    document["occurred_at"],
                    _decimal_text(document["fee_amount"], name="fee_amount"),
                    _decimal_text(document["slippage_amount"], name="slippage_amount"),
                    _decimal_text(document["unwind_amount"], name="unwind_amount"),
                    _decimal_text(document["onchain_amount"], name="onchain_amount"),
                    document["liquidity_role"],
                    _json_text(document),
                    document["strategy_id"],
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {"fill_id": document["fill_id"], "sequence": sequence}

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="fill.recorded",
            aggregate_type="fill",
            aggregate_id=document["fill_id"],
            request_document=document,
            apply=apply,
        )

    def record_position_snapshot(
        self, record: PositionSnapshotRecord, *, idempotency_key: str
    ) -> IdempotentResult:
        document: dict[str, Any] = {
            "snapshot_id": _ensure_text(record.snapshot_id, name="snapshot_id"),
            "token_id": _ensure_text(record.token_id, name="token_id"),
            "as_of": _ensure_text(record.as_of, name="as_of"),
            "net_quantity": record.net_quantity,
            "average_cost": record.average_cost,
            "market_price": record.market_price,
            "open_buy_quantity": record.open_buy_quantity,
            "open_sell_quantity": record.open_sell_quantity,
            "realized_trading_pnl": record.realized_trading_pnl,
            "fee_pnl": record.fee_pnl,
            "slippage_pnl": record.slippage_pnl,
            "unwind_pnl": record.unwind_pnl,
            "onchain_pnl": record.onchain_pnl,
            "incentive_pnl_confirmed": record.incentive_pnl_confirmed,
            "incentive_pnl_pending": record.incentive_pnl_pending,
            "unrealized_pnl": record.unrealized_pnl,
            "metadata": dict(record.metadata),
            **record.version.to_document(),
        }

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO positions(
                    sequence, snapshot_id, token_id, as_of, net_quantity, average_cost,
                    market_price, open_buy_quantity, open_sell_quantity,
                    realized_trading_pnl, fee_pnl, slippage_pnl, unwind_pnl,
                    onchain_pnl, incentive_pnl_confirmed, incentive_pnl_pending,
                    unrealized_pnl, payload_json, strategy_id,
                    strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    document["snapshot_id"],
                    document["token_id"],
                    document["as_of"],
                    _decimal_text(document["net_quantity"], name="net_quantity"),
                    _optional_decimal_text(document["average_cost"], name="average_cost"),
                    _optional_decimal_text(document["market_price"], name="market_price"),
                    _decimal_text(document["open_buy_quantity"], name="open_buy_quantity"),
                    _decimal_text(document["open_sell_quantity"], name="open_sell_quantity"),
                    _decimal_text(
                        document["realized_trading_pnl"],
                        name="realized_trading_pnl",
                    ),
                    _decimal_text(document["fee_pnl"], name="fee_pnl"),
                    _decimal_text(document["slippage_pnl"], name="slippage_pnl"),
                    _decimal_text(document["unwind_pnl"], name="unwind_pnl"),
                    _decimal_text(document["onchain_pnl"], name="onchain_pnl"),
                    _decimal_text(
                        document["incentive_pnl_confirmed"],
                        name="incentive_pnl_confirmed",
                    ),
                    _decimal_text(
                        document["incentive_pnl_pending"],
                        name="incentive_pnl_pending",
                    ),
                    _decimal_text(document["unrealized_pnl"], name="unrealized_pnl"),
                    _json_text(document),
                    document["strategy_id"],
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {"snapshot_id": document["snapshot_id"], "sequence": sequence}

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="position_snapshot.recorded",
            aggregate_type="position_snapshot",
            aggregate_id=document["snapshot_id"],
            request_document=document,
            apply=apply,
        )

    def record_pnl_attribution(
        self, record: PnLAttributionRecord, *, idempotency_key: str
    ) -> IdempotentResult:
        document: dict[str, Any] = {
            "attribution_id": _ensure_text(record.attribution_id, name="attribution_id"),
            "source_type": _ensure_text(record.source_type, name="source_type").lower(),
            "source_id": _ensure_text(record.source_id, name="source_id"),
            "token_id": _ensure_text(record.token_id, name="token_id"),
            "strategy_id": _ensure_text(record.strategy_id, name="strategy_id"),
            "occurred_at": _ensure_text(record.occurred_at, name="occurred_at"),
            "realized_trading_pnl": record.realized_trading_pnl,
            "fee_pnl": record.fee_pnl,
            "slippage_pnl": record.slippage_pnl,
            "unwind_pnl": record.unwind_pnl,
            "onchain_pnl": record.onchain_pnl,
            "incentive_pnl_confirmed": record.incentive_pnl_confirmed,
            "incentive_pnl_pending": record.incentive_pnl_pending,
            "metadata": dict(record.metadata),
            **record.version.to_document(),
        }

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO pnl_attribution(
                    sequence, attribution_id, source_type, source_id, token_id, strategy_id,
                    occurred_at, realized_trading_pnl, fee_pnl, slippage_pnl, unwind_pnl,
                    onchain_pnl, incentive_pnl_confirmed, incentive_pnl_pending,
                    payload_json, strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    document["attribution_id"],
                    document["source_type"],
                    document["source_id"],
                    document["token_id"],
                    document["strategy_id"],
                    document["occurred_at"],
                    _decimal_text(document["realized_trading_pnl"], name="realized_trading_pnl"),
                    _decimal_text(document["fee_pnl"], name="fee_pnl"),
                    _decimal_text(document["slippage_pnl"], name="slippage_pnl"),
                    _decimal_text(document["unwind_pnl"], name="unwind_pnl"),
                    _decimal_text(document["onchain_pnl"], name="onchain_pnl"),
                    _decimal_text(
                        document["incentive_pnl_confirmed"],
                        name="incentive_pnl_confirmed",
                    ),
                    _decimal_text(
                        document["incentive_pnl_pending"],
                        name="incentive_pnl_pending",
                    ),
                    _json_text(document),
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {
                "attribution_id": document["attribution_id"],
                "sequence": sequence,
            }

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="pnl_attribution.recorded",
            aggregate_type="pnl_attribution",
            aggregate_id=document["attribution_id"],
            request_document=document,
            apply=apply,
        )

    def record_reconciliation_run(
        self, record: ReconciliationRunRecord, *, idempotency_key: str
    ) -> IdempotentResult:
        document: dict[str, Any] = {
            "run_id": _ensure_text(record.run_id, name="run_id"),
            "scope": _ensure_text(record.scope, name="scope").lower(),
            "status": _ensure_text(record.status, name="status").lower(),
            "venue_snapshot": dict(record.venue_snapshot),
            "report": dict(record.report),
            "unresolved_count": int(record.unresolved_count),
            "created_at": _ensure_text(record.created_at, name="created_at"),
            **record.version.to_document(),
        }

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO reconciliation_runs(
                    sequence, run_id, scope, status, venue_snapshot_json, report_json,
                    unresolved_count, created_at, strategy_id,
                    strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    document["run_id"],
                    document["scope"],
                    document["status"],
                    _json_text(document["venue_snapshot"]),
                    _json_text(document["report"]),
                    document["unresolved_count"],
                    document["created_at"],
                    document["strategy_id"],
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {"run_id": document["run_id"], "sequence": sequence}

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="reconciliation.recorded",
            aggregate_type="reconciliation",
            aggregate_id=document["run_id"],
            request_document=document,
            apply=apply,
        )

    def record_risk_event(
        self, record: RiskEventRecord, *, idempotency_key: str
    ) -> IdempotentResult:
        document: dict[str, Any] = {
            "event_id": _ensure_text(record.event_id, name="event_id"),
            "event_type": _ensure_text(record.event_type, name="event_type").lower(),
            "severity": _ensure_text(record.severity, name="severity").lower(),
            "occurred_at": _ensure_text(record.occurred_at, name="occurred_at"),
            "kill_switch_engaged": bool(record.kill_switch_engaged),
            "details": dict(record.details),
            **record.version.to_document(),
        }

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO risk_events(
                    sequence, event_id, event_type, severity, occurred_at,
                    kill_switch_engaged, details_json, strategy_id,
                    strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    document["event_id"],
                    document["event_type"],
                    document["severity"],
                    document["occurred_at"],
                    _bool_as_int(document["kill_switch_engaged"]),
                    _json_text(document["details"]),
                    document["strategy_id"],
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {"event_id": document["event_id"], "sequence": sequence}

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="risk_event.recorded",
            aggregate_type="risk_event",
            aggregate_id=document["event_id"],
            request_document=document,
            apply=apply,
        )

    def record_strategy_evaluation_window(
        self, record: StrategyEvaluationWindowRecord, *, idempotency_key: str
    ) -> IdempotentResult:
        document: dict[str, Any] = {
            "window_id": _ensure_text(record.window_id, name="window_id"),
            "strategy_id": _ensure_text(record.strategy_id, name="strategy_id"),
            "started_at": _ensure_text(record.started_at, name="started_at"),
            "ended_at": _ensure_text(record.ended_at, name="ended_at"),
            "metrics": dict(record.metrics),
            **record.version.to_document(),
        }

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO strategy_evaluation_windows(
                    sequence, window_id, strategy_id, started_at, ended_at, metrics_json,
                    strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    document["window_id"],
                    document["strategy_id"],
                    document["started_at"],
                    document["ended_at"],
                    _json_text(document["metrics"]),
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {"window_id": document["window_id"], "sequence": sequence}

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="strategy_evaluation.recorded",
            aggregate_type="strategy_evaluation_window",
            aggregate_id=document["window_id"],
            request_document=document,
            apply=apply,
        )

    def record_strategy_qualification(
        self, record: StrategyQualificationRecord, *, idempotency_key: str
    ) -> IdempotentResult:
        qualification_id = _ensure_text(record.qualification_id, name="qualification_id")
        strategy_id = _ensure_text(record.strategy_id, name="strategy_id")
        qualified = bool(record.qualified)
        status = _ensure_text(record.status, name="status").lower()
        observed_at = _ensure_text(record.observed_at, name="observed_at")
        document: dict[str, Any] = {
            "qualification_id": qualification_id,
            "strategy_id": strategy_id,
            "qualified": qualified,
            "status": status,
            "observed_at": observed_at,
            "details": dict(record.details),
            **record.version.to_document(),
        }

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO strategy_qualifications(
                    sequence, qualification_id, strategy_id, qualified, status,
                    observed_at, details_json, strategy_config_version, code_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    qualification_id,
                    strategy_id,
                    _bool_as_int(qualified),
                    status,
                    observed_at,
                    _json_text(document["details"]),
                    document["strategy_config_version"],
                    document["code_version"],
                ),
            )
            return {
                "qualification_id": qualification_id,
                "sequence": sequence,
            }

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="strategy_qualification.recorded",
            aggregate_type="strategy_qualification",
            aggregate_id=qualification_id,
            request_document=document,
            apply=apply,
        )

    def set_system_state(self, key: str, value: Any, *, idempotency_key: str) -> IdempotentResult:
        state_key = _ensure_text(key, name="key")
        document: dict[str, Any] = {"state_key": state_key, "value": value}

        def apply(connection: sqlite3.Connection, sequence: int) -> Mapping[str, Any]:
            connection.execute(
                """
                INSERT INTO system_state(state_key, value_json, updated_sequence, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_sequence = excluded.updated_sequence,
                    updated_at = excluded.updated_at
                """,
                (state_key, _json_text(value), sequence, _utc_now()),
            )
            return {"state_key": state_key, "sequence": sequence}

        return self._commit_mutation(
            idempotency_key=idempotency_key,
            mutation_type="system_state.set",
            aggregate_type="system_state",
            aggregate_id=state_key,
            request_document=document,
            apply=apply,
        )

    def save_cash_snapshot(
        self, snapshot: Mapping[str, Any], *, idempotency_key: str
    ) -> IdempotentResult:
        return self.set_system_state(
            "portfolio_cash", dict(snapshot), idempotency_key=idempotency_key
        )

    def save_user_stream_snapshot(
        self, snapshot: Mapping[str, Any], *, idempotency_key: str
    ) -> IdempotentResult:
        return self.set_system_state(
            "user_stream_snapshot", dict(snapshot), idempotency_key=idempotency_key
        )

    def save_kill_state(
        self,
        *,
        reason: str,
        actor: str,
        recovery: Mapping[str, Any],
        idempotency_key: str,
    ) -> IdempotentResult:
        document: dict[str, Any] = {
            "kill_switch_engaged": True,
            "reason": _ensure_text(reason, name="reason"),
            "actor": _ensure_text(actor, name="actor"),
            "engaged_at": _utc_now(),
            "recovery": dict(recovery),
        }
        return self.set_system_state("kill_state", document, idempotency_key=idempotency_key)

    def save_restart_recovery_state(
        self, recovery: Mapping[str, Any], *, idempotency_key: str
    ) -> IdempotentResult:
        return self.set_system_state(
            "restart_recovery", dict(recovery), idempotency_key=idempotency_key
        )

    def get_system_state(self, key: str, default: Any = None) -> Any:
        row = self._connection.execute(
            "SELECT value_json FROM system_state WHERE state_key = ?",
            (_ensure_text(key, name="key"),),
        ).fetchone()
        if row is None:
            return default
        return _json_value(row["value_json"])

    def list_system_state(self) -> dict[str, Any]:
        rows = self._connection.execute(
            "SELECT state_key, value_json FROM system_state ORDER BY state_key"
        ).fetchall()
        return {row["state_key"]: _json_value(row["value_json"]) for row in rows}

    def list_orders(self) -> list[OrderRecord]:
        rows = self._connection.execute(
            "SELECT * FROM orders ORDER BY sequence, order_id"
        ).fetchall()
        return [_row_to_order(row) for row in rows]

    def list_open_orders(self) -> list[OrderRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM orders
            WHERE lifecycle_state IN ({placeholders})
            ORDER BY updated_at, order_id
            """.format(placeholders=",".join("?" for _ in ACTIVE_ORDER_STATES)),
            tuple(sorted(ACTIVE_ORDER_STATES)),
        ).fetchall()
        return [_row_to_order(row) for row in rows]

    def list_fills(self) -> list[FillRecord]:
        rows = self._connection.execute(
            "SELECT * FROM fills ORDER BY occurred_at, fill_id"
        ).fetchall()
        return [_row_to_fill(row) for row in rows]

    def list_position_snapshots(self) -> list[PositionSnapshotRecord]:
        rows = self._connection.execute(
            "SELECT * FROM positions ORDER BY as_of, token_id"
        ).fetchall()
        return [_row_to_position(row) for row in rows]

    def list_pnl_attribution(self) -> list[PnLAttributionRecord]:
        rows = self._connection.execute(
            "SELECT * FROM pnl_attribution ORDER BY occurred_at, attribution_id"
        ).fetchall()
        return [_row_to_pnl(row) for row in rows]

    def list_reconciliation_runs(self) -> list[ReconciliationRunRecord]:
        rows = self._connection.execute(
            "SELECT * FROM reconciliation_runs ORDER BY created_at, run_id"
        ).fetchall()
        return [_row_to_reconciliation(row) for row in rows]

    def list_execution_plans(self) -> list[ExecutionPlanRecord]:
        rows = self._connection.execute(
            "SELECT * FROM execution_plans ORDER BY created_at, plan_id"
        ).fetchall()
        result: list[ExecutionPlanRecord] = []
        for row in rows:
            payload = _json_value(row["payload_json"]) or {}
            result.append(
                ExecutionPlanRecord(
                    plan_id=row["plan_id"],
                    decision_id=row["decision_id"],
                    strategy_id=row["strategy_id"],
                    status=row["status"],
                    expected_net_edge=_parse_decimal(row["expected_net_edge"]),
                    plan_kind=row["plan_kind"],
                    created_at=row["created_at"],
                    metadata=payload.get("metadata", {}),
                    version=VersionStamp(
                        strategy_id=row["strategy_id"],
                        strategy_config_version=row["strategy_config_version"],
                        code_version=row["code_version"],
                    ),
                )
            )
        return result

    def iter_execution_journal(self) -> Iterator[JournalEvent]:
        rows = self._connection.execute(
            """
            SELECT sequence, event_type, aggregate_type, aggregate_id, idempotency_key,
                   payload_json, created_at
            FROM execution_journal
            ORDER BY sequence
            """
        ).fetchall()
        for row in rows:
            yield JournalEvent(
                sequence=int(row["sequence"]),
                event_type=row["event_type"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                idempotency_key=row["idempotency_key"],
                payload=_json_value(row["payload_json"]) or {},
                created_at=row["created_at"],
            )

    def journal_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS count FROM execution_journal").fetchone()
        return int(row["count"])

    def load_restart_context(self) -> dict[str, Any]:
        kill_state = self.get_system_state("kill_state")
        restart_recovery = self.get_system_state("restart_recovery")
        cash = self.get_system_state("portfolio_cash")
        return {
            "kill_state": kill_state,
            "restart_recovery": restart_recovery,
            "cash": cash,
            "open_orders": [order.to_document() for order in self.list_open_orders()],
            "recent_fills": [fill.to_document() for fill in self.list_fills()[-20:]],
            "positions": [snapshot for snapshot in self.list_position_snapshots()[-20:]],
        }

    def backup_to(self, destination: str | Path) -> Path:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(destination_path)) as backup_connection:
            self._connection.backup(backup_connection)
            backup_connection.commit()
        return destination_path

    def restore_from(self, source: str | Path) -> None:
        source_path = Path(source)
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        self.close()
        with sqlite3.connect(str(source_path)) as source_connection:
            with sqlite3.connect(str(self.db_path)) as target_connection:
                source_connection.backup(target_connection)
                target_connection.commit()
        self._connection = self._open_connection()
        self._apply_migrations()
