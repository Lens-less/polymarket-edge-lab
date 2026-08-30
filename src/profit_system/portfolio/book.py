from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.edge_lab.data_store import canonical_json_bytes
from src.profit_system.persistence import (
    ACTIVE_ORDER_STATES,
    FillRecord,
    OrderRecord,
    PersistenceStore,
    PnLAttributionRecord,
    PositionSnapshotRecord,
    ReconciliationRunRecord,
    VersionStamp,
)

ZERO = Decimal("0")


def _decimal(value: Any, *, name: str) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value))
        except Exception as exc:  # pragma: no cover - Decimal exceptions vary
            raise ValueError(f"{name} must be Decimal-compatible") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, name="value")


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _sum(values: Sequence[Decimal]) -> Decimal:
    return sum(values, ZERO)


@dataclass(frozen=True)
class CashBalance:
    currency: str
    total: Decimal
    available: Decimal
    reserved: Decimal = ZERO
    source: str = "derived"

    @classmethod
    def from_mapping(cls, currency: str, payload: Mapping[str, Any], *, source: str) -> CashBalance:
        return cls(
            currency=_text(currency, name="currency"),
            total=_decimal(payload.get("total", payload.get("available", ZERO)), name="total"),
            available=_decimal(
                payload.get("available", payload.get("total", ZERO)),
                name="available",
            ),
            reserved=_decimal(payload.get("reserved", ZERO), name="reserved"),
            source=source,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "total": self.total,
            "available": self.available,
            "reserved": self.reserved,
            "source": self.source,
        }


@dataclass(frozen=True)
class OpenOrderView:
    order_id: str
    venue_order_id: str | None
    market_id: str
    token_id: str
    side: str
    price: Decimal
    quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    lifecycle_state: str
    order_role: str
    source: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_order(cls, order: OrderRecord, *, source: str) -> OpenOrderView:
        return cls(
            order_id=order.order_id,
            venue_order_id=order.venue_order_id,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            price=order.price,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.remaining_quantity or ZERO,
            lifecycle_state=order.lifecycle_state,
            order_role=order.order_role,
            source=source,
            metadata=dict(order.metadata),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, source: str) -> OpenOrderView:
        return cls(
            order_id=_text(str(payload.get("order_id")), name="order_id"),
            venue_order_id=payload.get("venue_order_id"),
            market_id=_text(str(payload.get("market_id")), name="market_id"),
            token_id=_text(str(payload.get("token_id")), name="token_id"),
            side=_text(str(payload.get("side", "buy")), name="side").lower(),
            price=_decimal(payload.get("price", ZERO), name="price"),
            quantity=_decimal(payload.get("quantity", ZERO), name="quantity"),
            filled_quantity=_decimal(payload.get("filled_quantity", ZERO), name="filled_quantity"),
            remaining_quantity=_decimal(
                payload.get("remaining_quantity", payload.get("quantity", ZERO)),
                name="remaining_quantity",
            ),
            lifecycle_state=_text(
                str(payload.get("lifecycle_state", "live")), name="lifecycle_state"
            ).lower(),
            order_role=_text(str(payload.get("order_role", "unknown")), name="order_role").lower(),
            source=source,
            metadata=dict(payload.get("metadata", {})),
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "venue_order_id": self.venue_order_id,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "lifecycle_state": self.lifecycle_state,
            "order_role": self.order_role,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PositionView:
    token_id: str
    net_quantity: Decimal
    average_cost: Decimal | None
    market_price: Decimal | None
    open_buy_quantity: Decimal
    open_sell_quantity: Decimal
    realized_trading_pnl: Decimal
    fee_pnl: Decimal
    slippage_pnl: Decimal
    unwind_pnl: Decimal
    onchain_pnl: Decimal
    incentive_pnl_confirmed: Decimal
    incentive_pnl_pending: Decimal
    unrealized_pnl: Decimal
    source: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_document(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "net_quantity": self.net_quantity,
            "average_cost": self.average_cost,
            "market_price": self.market_price,
            "open_buy_quantity": self.open_buy_quantity,
            "open_sell_quantity": self.open_sell_quantity,
            "realized_trading_pnl": self.realized_trading_pnl,
            "fee_pnl": self.fee_pnl,
            "slippage_pnl": self.slippage_pnl,
            "unwind_pnl": self.unwind_pnl,
            "onchain_pnl": self.onchain_pnl,
            "incentive_pnl_confirmed": self.incentive_pnl_confirmed,
            "incentive_pnl_pending": self.incentive_pnl_pending,
            "unrealized_pnl": self.unrealized_pnl,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ReconciliationMismatch:
    scope: str
    key: str
    field: str
    local_value: Any
    authoritative_value: Any
    authoritative_source: str

    def to_document(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "key": self.key,
            "field": self.field,
            "local_value": self.local_value,
            "authoritative_value": self.authoritative_value,
            "authoritative_source": self.authoritative_source,
        }


@dataclass(frozen=True)
class PortfolioSnapshot:
    as_of: str
    cash_balances: tuple[CashBalance, ...]
    positions: tuple[PositionView, ...]
    open_orders: tuple[OpenOrderView, ...]
    realized_net_pnl: Decimal
    trading_net_pnl: Decimal
    incentive_pnl: Decimal
    incentive_pnl_pending: Decimal
    unrealized_pnl: Decimal
    unresolved_reconciliation_count: int
    mismatches: tuple[ReconciliationMismatch, ...]
    sources: Mapping[str, str]

    def to_document(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "cash_balances": [balance.to_document() for balance in self.cash_balances],
            "positions": [position.to_document() for position in self.positions],
            "open_orders": [order.to_document() for order in self.open_orders],
            "realized_net_pnl": self.realized_net_pnl,
            "trading_net_pnl": self.trading_net_pnl,
            "incentive_pnl": self.incentive_pnl,
            "incentive_pnl_pending": self.incentive_pnl_pending,
            "unrealized_pnl": self.unrealized_pnl,
            "unresolved_reconciliation_count": self.unresolved_reconciliation_count,
            "mismatches": [mismatch.to_document() for mismatch in self.mismatches],
            "sources": dict(self.sources),
        }


@dataclass(frozen=True)
class PerformanceReport:
    as_of: str
    metrics: Mapping[str, Decimal | int | None]

    def to_document(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "metrics": dict(self.metrics)}


@dataclass(frozen=True)
class ReconciliationReport:
    run_id: str
    status: str
    authoritative_source: str
    as_of: str
    mismatches: tuple[ReconciliationMismatch, ...]
    local_snapshot: PortfolioSnapshot
    authoritative_snapshot: Mapping[str, Any]

    @property
    def unresolved_count(self) -> int:
        return len(self.mismatches)

    def to_document(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "authoritative_source": self.authoritative_source,
            "as_of": self.as_of,
            "unresolved_count": self.unresolved_count,
            "mismatches": [mismatch.to_document() for mismatch in self.mismatches],
            "local_snapshot": self.local_snapshot.to_document(),
            "authoritative_snapshot": dict(self.authoritative_snapshot),
        }


@dataclass
class _Accumulator:
    quantity: Decimal = ZERO
    cost_basis: Decimal = ZERO
    average_cost: Decimal | None = None
    realized_trading_pnl: Decimal = ZERO
    fee_pnl: Decimal = ZERO
    slippage_pnl: Decimal = ZERO
    unwind_pnl: Decimal = ZERO
    onchain_pnl: Decimal = ZERO
    incentive_pnl_confirmed: Decimal = ZERO
    incentive_pnl_pending: Decimal = ZERO
    market_price: Decimal | None = None
    open_buy_quantity: Decimal = ZERO
    open_sell_quantity: Decimal = ZERO
    adverse_selection_bps_weighted: Decimal = ZERO
    adverse_selection_quantity: Decimal = ZERO


class PortfolioBook:
    """Authoritative portfolio read model with explicit reconciliation."""

    def __init__(self, store: PersistenceStore) -> None:
        self.store = store

    def snapshot(
        self,
        *,
        venue_snapshot: Mapping[str, Any] | None = None,
        user_stream_snapshot: Mapping[str, Any] | None = None,
    ) -> PortfolioSnapshot:
        local = self._build_local_snapshot()
        current_user = user_stream_snapshot or self.store.get_system_state("user_stream_snapshot")
        authoritative = venue_snapshot or self.store.get_system_state(
            "venue_authoritative_snapshot"
        )
        merged = local
        if current_user:
            merged = self._overlay_snapshot(
                merged,
                self._normalize_snapshot_input(current_user, source="user_stream"),
                authoritative_source="user_stream",
            )
        if authoritative:
            merged = self._overlay_snapshot(
                merged,
                self._normalize_snapshot_input(authoritative, source="venue"),
                authoritative_source="venue",
            )
        persisted_unresolved = self.store.get_system_state("reconciliation_unresolved_count", 0)
        unresolved_count = max(len(merged.mismatches), int(persisted_unresolved or 0))
        return PortfolioSnapshot(
            as_of=merged.as_of,
            cash_balances=merged.cash_balances,
            positions=merged.positions,
            open_orders=merged.open_orders,
            realized_net_pnl=merged.realized_net_pnl,
            trading_net_pnl=merged.trading_net_pnl,
            incentive_pnl=merged.incentive_pnl,
            incentive_pnl_pending=merged.incentive_pnl_pending,
            unrealized_pnl=merged.unrealized_pnl,
            unresolved_reconciliation_count=unresolved_count,
            mismatches=merged.mismatches,
            sources=merged.sources,
        )

    def performance(self, query: Mapping[str, Any] | None = None) -> PerformanceReport:
        snapshot = self.snapshot()
        query = {} if query is None else dict(query)
        allocated_capital = _optional_decimal(
            query.get(
                "allocated_capital",
                self.store.get_system_state("allocated_capital"),
            )
        )
        current_exposure = _sum(
            [
                abs(position.net_quantity)
                * (position.market_price or position.average_cost or ZERO)
                for position in snapshot.positions
            ]
        ) + _sum([cash.reserved for cash in snapshot.cash_balances])
        fill_metrics = self._fill_rate_metrics(self.store.list_orders(), self.store.list_fills())
        expected_edge = _sum(
            [
                plan.expected_net_edge or ZERO
                for plan in self.store.list_execution_plans()
                if plan.status in {"approved", "submitted", "filled", "live"}
            ]
        )
        realized_edge = snapshot.realized_net_pnl
        timeline = self._realized_pnl_timeline()
        profit_factor = None
        gross_profit = _sum([point for point in timeline if point > ZERO])
        gross_loss = abs(_sum([point for point in timeline if point < ZERO]))
        if gross_loss > ZERO:
            profit_factor = gross_profit / gross_loss
        max_drawdown = self._max_drawdown(timeline)
        metrics: dict[str, Decimal | int | None] = {
            "realized_net_pnl": snapshot.realized_net_pnl,
            "trading_net_pnl": snapshot.trading_net_pnl,
            "incentive_pnl": snapshot.incentive_pnl,
            "return_on_allocated_capital": (
                snapshot.realized_net_pnl / allocated_capital
                if allocated_capital and allocated_capital != ZERO
                else None
            ),
            "profit_factor": profit_factor,
            "max_drawdown": max_drawdown,
            "expected_net_edge": expected_edge if expected_edge != ZERO else None,
            "realized_net_edge": realized_edge,
            "edge_realization_ratio": (
                realized_edge / expected_edge if expected_edge != ZERO else None
            ),
            "fill_rate": fill_metrics["fill_rate"],
            "maker_fill_rate": fill_metrics["maker_fill_rate"],
            "adverse_selection_bps": self._adverse_selection_bps(snapshot.positions),
            "capital_utilization": (
                current_exposure / allocated_capital
                if allocated_capital and allocated_capital != ZERO
                else None
            ),
            "unmatched_leg_exposure_seconds": self._unmatched_leg_exposure_seconds(
                snapshot.open_orders
            ),
            "unresolved_reconciliation_count": snapshot.unresolved_reconciliation_count,
        }
        return PerformanceReport(as_of=snapshot.as_of, metrics=metrics)

    def reconcile(
        self, venue_snapshot: Mapping[str, Any], *, actor: str = "portfolio_book"
    ) -> ReconciliationReport:
        authoritative = self._normalize_snapshot_input(venue_snapshot, source="venue")
        local = self._build_local_snapshot()
        merged = self._overlay_snapshot(local, authoritative, authoritative_source="venue")
        report_document: dict[str, Any] = {
            "status": "matched" if not merged.mismatches else "mismatch",
            "actor": _text(actor, name="actor"),
            "mismatches": [mismatch.to_document() for mismatch in merged.mismatches],
        }
        run_id = hashlib.sha256(
            canonical_json_bytes(
                {
                    "authoritative": authoritative.to_document(),
                    "local": local.to_document(),
                    "report": report_document,
                }
            )
        ).hexdigest()
        report = ReconciliationReport(
            run_id=run_id,
            status=report_document["status"],
            authoritative_source="venue",
            as_of=merged.as_of,
            mismatches=merged.mismatches,
            local_snapshot=local,
            authoritative_snapshot=authoritative.to_document(),
        )
        version = VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="portfolio_book",
        )
        self.store.record_reconciliation_run(
            ReconciliationRunRecord(
                run_id=run_id,
                scope="portfolio",
                status=report.status,
                venue_snapshot=authoritative.to_document(),
                report=report.to_document(),
                unresolved_count=report.unresolved_count,
                created_at=report.as_of,
                version=version,
            ),
            idempotency_key=f"reconcile:{run_id}",
        )
        self.store.set_system_state(
            "venue_authoritative_snapshot",
            authoritative.to_document(),
            idempotency_key=f"system_state:venue:{run_id}",
        )
        self.store.set_system_state(
            "reconciliation_unresolved_count",
            report.unresolved_count,
            idempotency_key=f"system_state:unresolved:{run_id}",
        )
        if authoritative.cash_balances:
            self.store.save_cash_snapshot(
                {"balances": [balance.to_document() for balance in authoritative.cash_balances]},
                idempotency_key=f"cash:{run_id}",
            )
        self.store.save_restart_recovery_state(
            {
                "open_orders": [order.to_document() for order in authoritative.open_orders],
                "positions": [position.to_document() for position in authoritative.positions],
                "cash_balances": [balance.to_document() for balance in authoritative.cash_balances],
                "actor": actor,
            },
            idempotency_key=f"restart_recovery:{run_id}",
        )
        return report

    def rebuild_read_model(
        self,
        *,
        venue_snapshot: Mapping[str, Any] | None = None,
        user_stream_snapshot: Mapping[str, Any] | None = None,
    ) -> PortfolioSnapshot:
        snapshot = self.snapshot(
            venue_snapshot=venue_snapshot, user_stream_snapshot=user_stream_snapshot
        )
        fingerprint = hashlib.sha256(canonical_json_bytes(snapshot.to_document())).hexdigest()
        version = VersionStamp(
            strategy_id="system",
            strategy_config_version="system",
            code_version="portfolio_book",
        )
        for index, position in enumerate(snapshot.positions):
            record = PositionSnapshotRecord(
                snapshot_id=f"{fingerprint}:{index}:{position.token_id}",
                token_id=position.token_id,
                as_of=snapshot.as_of,
                net_quantity=position.net_quantity,
                average_cost=position.average_cost,
                market_price=position.market_price,
                open_buy_quantity=position.open_buy_quantity,
                open_sell_quantity=position.open_sell_quantity,
                realized_trading_pnl=position.realized_trading_pnl,
                fee_pnl=position.fee_pnl,
                slippage_pnl=position.slippage_pnl,
                unwind_pnl=position.unwind_pnl,
                onchain_pnl=position.onchain_pnl,
                incentive_pnl_confirmed=position.incentive_pnl_confirmed,
                incentive_pnl_pending=position.incentive_pnl_pending,
                unrealized_pnl=position.unrealized_pnl,
                metadata=dict(position.metadata),
                version=version,
            )
            self.store.record_position_snapshot(
                record,
                idempotency_key=f"position_snapshot:{record.snapshot_id}",
            )
        self.store.set_system_state(
            "portfolio_snapshot",
            snapshot.to_document(),
            idempotency_key=f"portfolio_snapshot:{fingerprint}",
        )
        return snapshot

    def _build_local_snapshot(self) -> PortfolioSnapshot:
        orders = self.store.list_orders()
        fills = self._dedupe_fills(self.store.list_fills())
        pnl_rows = self._resolve_authoritative_pnl_rows(self.store.list_pnl_attribution())
        as_of = self._latest_timestamp(orders, fills, pnl_rows)
        explicit_realization_cycles = self._explicit_realization_cycles(pnl_rows)
        accumulators = self._accumulate_positions(
            fills,
            suppressed_fill_realization_cycles=explicit_realization_cycles,
        )
        self._apply_order_open_quantities(accumulators, orders)
        self._apply_pnl_rows(accumulators, pnl_rows)
        market_prices = self._latest_market_prices()
        position_views = self._position_views(accumulators, market_prices)
        cash_balances = self._local_cash_balances()
        open_orders = tuple(
            OpenOrderView.from_order(order, source="journal")
            for order in orders
            if order.lifecycle_state in ACTIVE_ORDER_STATES
        )
        trading_net = _sum(
            [
                position.realized_trading_pnl
                + position.fee_pnl
                + position.slippage_pnl
                + position.unwind_pnl
                + position.onchain_pnl
                for position in position_views
            ]
        )
        incentive_confirmed = _sum(
            [position.incentive_pnl_confirmed for position in position_views]
        )
        incentive_pending = _sum([position.incentive_pnl_pending for position in position_views])
        unrealized = _sum([position.unrealized_pnl for position in position_views])
        return PortfolioSnapshot(
            as_of=as_of,
            cash_balances=cash_balances,
            positions=position_views,
            open_orders=open_orders,
            realized_net_pnl=trading_net + incentive_confirmed,
            trading_net_pnl=trading_net,
            incentive_pnl=incentive_confirmed,
            incentive_pnl_pending=incentive_pending,
            unrealized_pnl=unrealized,
            unresolved_reconciliation_count=0,
            mismatches=(),
            sources={"cash": "journal", "positions": "journal", "open_orders": "journal"},
        )

    def _normalize_snapshot_input(
        self, snapshot: Mapping[str, Any], *, source: str
    ) -> PortfolioSnapshot:
        balances = snapshot.get("cash_balances")
        if balances is None and "balances" in snapshot:
            balances = snapshot["balances"]
        if balances is None and "cash" in snapshot:
            balances = snapshot["cash"]
        cash_present = balances is not None
        cash_balances: list[CashBalance] = []
        if isinstance(balances, Mapping):
            for currency, payload in balances.items():
                cash_balances.append(CashBalance.from_mapping(currency, payload, source=source))
        elif isinstance(balances, Sequence):
            for item in balances:
                if not isinstance(item, Mapping):
                    continue
                currency = str(item.get("currency", "USD"))
                cash_balances.append(CashBalance.from_mapping(currency, item, source=source))

        positions_present = "positions" in snapshot
        positions_payload = snapshot.get("positions", ())
        positions: list[PositionView] = []
        if isinstance(positions_payload, Sequence):
            for item in positions_payload:
                if not isinstance(item, Mapping):
                    continue
                positions.append(
                    PositionView(
                        token_id=_text(str(item.get("token_id")), name="token_id"),
                        net_quantity=_decimal(
                            item.get("net_quantity", item.get("quantity", ZERO)),
                            name="net_quantity",
                        ),
                        average_cost=_optional_decimal(item.get("average_cost")),
                        market_price=_optional_decimal(item.get("market_price")),
                        open_buy_quantity=_decimal(
                            item.get("open_buy_quantity", ZERO), name="open_buy_quantity"
                        ),
                        open_sell_quantity=_decimal(
                            item.get("open_sell_quantity", ZERO), name="open_sell_quantity"
                        ),
                        realized_trading_pnl=_decimal(
                            item.get("realized_trading_pnl", ZERO),
                            name="realized_trading_pnl",
                        ),
                        fee_pnl=_decimal(item.get("fee_pnl", ZERO), name="fee_pnl"),
                        slippage_pnl=_decimal(item.get("slippage_pnl", ZERO), name="slippage_pnl"),
                        unwind_pnl=_decimal(item.get("unwind_pnl", ZERO), name="unwind_pnl"),
                        onchain_pnl=_decimal(item.get("onchain_pnl", ZERO), name="onchain_pnl"),
                        incentive_pnl_confirmed=_decimal(
                            item.get("incentive_pnl_confirmed", ZERO),
                            name="incentive_pnl_confirmed",
                        ),
                        incentive_pnl_pending=_decimal(
                            item.get("incentive_pnl_pending", ZERO),
                            name="incentive_pnl_pending",
                        ),
                        unrealized_pnl=_decimal(
                            item.get("unrealized_pnl", ZERO), name="unrealized_pnl"
                        ),
                        source=source,
                        metadata=dict(item.get("metadata", {})),
                    )
                )

        open_orders_present = "open_orders" in snapshot
        open_orders_payload = snapshot.get("open_orders", ())
        open_orders: list[OpenOrderView] = []
        if isinstance(open_orders_payload, Sequence):
            for item in open_orders_payload:
                if not isinstance(item, Mapping):
                    continue
                open_orders.append(OpenOrderView.from_mapping(item, source=source))

        trading_net = _sum(
            [
                position.realized_trading_pnl
                + position.fee_pnl
                + position.slippage_pnl
                + position.unwind_pnl
                + position.onchain_pnl
                for position in positions
            ]
        )
        incentive_confirmed = _sum([position.incentive_pnl_confirmed for position in positions])
        incentive_pending = _sum([position.incentive_pnl_pending for position in positions])
        unrealized = _sum([position.unrealized_pnl for position in positions])
        return PortfolioSnapshot(
            as_of=str(snapshot.get("as_of", self._latest_timestamp((), (), ()))),
            cash_balances=tuple(cash_balances),
            positions=tuple(positions),
            open_orders=tuple(open_orders),
            realized_net_pnl=trading_net + incentive_confirmed,
            trading_net_pnl=trading_net,
            incentive_pnl=incentive_confirmed,
            incentive_pnl_pending=incentive_pending,
            unrealized_pnl=unrealized,
            unresolved_reconciliation_count=int(snapshot.get("unresolved_reconciliation_count", 0)),
            mismatches=(),
            sources={
                "cash": source if cash_present else "absent",
                "positions": source if positions_present else "absent",
                "open_orders": source if open_orders_present else "absent",
            },
        )

    def _overlay_snapshot(
        self,
        local: PortfolioSnapshot,
        authoritative: PortfolioSnapshot,
        *,
        authoritative_source: str,
    ) -> PortfolioSnapshot:
        mismatches: list[ReconciliationMismatch] = list(local.mismatches)
        cash_by_currency = {balance.currency: balance for balance in local.cash_balances}
        for balance in authoritative.cash_balances:
            local_balance = cash_by_currency.get(balance.currency)
            if local_balance and (
                local_balance.total != balance.total
                or local_balance.available != balance.available
                or local_balance.reserved != balance.reserved
            ):
                mismatches.append(
                    ReconciliationMismatch(
                        scope="cash",
                        key=balance.currency,
                        field="balance",
                        local_value=local_balance.to_document(),
                        authoritative_value=balance.to_document(),
                        authoritative_source=authoritative_source,
                    )
                )
            cash_by_currency[balance.currency] = balance

        local_positions = {position.token_id: position for position in local.positions}
        for position in authoritative.positions:
            local_position = local_positions.get(position.token_id)
            if local_position and (
                local_position.net_quantity != position.net_quantity
                or local_position.average_cost != position.average_cost
                or local_position.market_price != position.market_price
            ):
                mismatches.append(
                    ReconciliationMismatch(
                        scope="position",
                        key=position.token_id,
                        field="position",
                        local_value=local_position.to_document(),
                        authoritative_value=position.to_document(),
                        authoritative_source=authoritative_source,
                    )
                )
            local_positions[position.token_id] = position

        local_orders = {order.order_id: order for order in local.open_orders}
        authoritative_order_ids = {order.order_id for order in authoritative.open_orders}
        for order in authoritative.open_orders:
            local_order = local_orders.get(order.order_id)
            if local_order and (
                local_order.remaining_quantity != order.remaining_quantity
                or local_order.lifecycle_state != order.lifecycle_state
            ):
                mismatches.append(
                    ReconciliationMismatch(
                        scope="open_order",
                        key=order.order_id,
                        field="open_order",
                        local_value=local_order.to_document(),
                        authoritative_value=order.to_document(),
                        authoritative_source=authoritative_source,
                    )
                )
            elif local_order is None:
                mismatches.append(
                    ReconciliationMismatch(
                        scope="open_order",
                        key=order.order_id,
                        field="missing_local_open_order",
                        local_value=None,
                        authoritative_value=order.to_document(),
                        authoritative_source=authoritative_source,
                    )
                )
            local_orders[order.order_id] = order
        authoritative_open_orders_known = authoritative.sources.get("open_orders") != "absent"
        for order_id, local_order in list(local_orders.items()):
            if authoritative_open_orders_known and order_id not in authoritative_order_ids:
                mismatches.append(
                    ReconciliationMismatch(
                        scope="open_order",
                        key=order_id,
                        field="unexpected_local_open_order",
                        local_value=local_order.to_document(),
                        authoritative_value=None,
                        authoritative_source=authoritative_source,
                    )
                )
                local_orders.pop(order_id, None)

        return PortfolioSnapshot(
            as_of=max(local.as_of, authoritative.as_of),
            cash_balances=tuple(sorted(cash_by_currency.values(), key=lambda item: item.currency)),
            positions=tuple(sorted(local_positions.values(), key=lambda item: item.token_id)),
            open_orders=tuple(sorted(local_orders.values(), key=lambda item: item.order_id)),
            realized_net_pnl=authoritative.realized_net_pnl
            if authoritative.positions
            else local.realized_net_pnl,
            trading_net_pnl=authoritative.trading_net_pnl
            if authoritative.positions
            else local.trading_net_pnl,
            incentive_pnl=authoritative.incentive_pnl
            if authoritative.positions
            else local.incentive_pnl,
            incentive_pnl_pending=authoritative.incentive_pnl_pending
            if authoritative.positions
            else local.incentive_pnl_pending,
            unrealized_pnl=authoritative.unrealized_pnl
            if authoritative.positions
            else local.unrealized_pnl,
            unresolved_reconciliation_count=len(mismatches),
            mismatches=tuple(mismatches),
            sources={
                "cash": (
                    authoritative_source
                    if authoritative.sources.get("cash") != "absent"
                    else local.sources["cash"]
                ),
                "positions": (
                    authoritative_source
                    if authoritative.sources.get("positions") != "absent"
                    else local.sources["positions"]
                ),
                "open_orders": (
                    authoritative_source
                    if authoritative.sources.get("open_orders") != "absent"
                    else local.sources["open_orders"]
                ),
            },
        )

    def _latest_market_prices(self) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {}
        for fill in self.store.list_fills():
            prices[fill.token_id] = fill.price
        market_snapshot = self.store.get_system_state("latest_market_prices", {})
        if isinstance(market_snapshot, Mapping):
            for token_id, price in market_snapshot.items():
                prices[str(token_id)] = _decimal(price, name="market_price")
        return prices

    def _local_cash_balances(self) -> tuple[CashBalance, ...]:
        snapshot = self.store.get_system_state("portfolio_cash", {})
        if not snapshot:
            return ()
        normalized = self._normalize_snapshot_input(snapshot, source="journal")
        return normalized.cash_balances

    def _dedupe_fills(self, fills: Sequence[FillRecord]) -> list[FillRecord]:
        unique: dict[str, FillRecord] = {}
        for fill in fills:
            unique.setdefault(fill.dedupe_key(), fill)
        return list(unique.values())

    def _accumulate_positions(
        self,
        fills: Sequence[FillRecord],
        *,
        suppressed_fill_realization_cycles: set[tuple[str, str]] | None = None,
    ) -> dict[str, _Accumulator]:
        positions: dict[str, _Accumulator] = {}
        for fill in fills:
            accumulator = positions.setdefault(fill.token_id, _Accumulator())
            quantity = fill.quantity
            realized_trading_pnl = ZERO
            cycle_key = self._fill_realization_cycle_key(fill)
            suppress_fill_realization = (
                cycle_key is not None
                and suppressed_fill_realization_cycles is not None
                and cycle_key in suppressed_fill_realization_cycles
            )
            if fill.side == "buy":
                if accumulator.quantity < ZERO:
                    close_quantity = min(abs(accumulator.quantity), quantity)
                    if accumulator.average_cost is not None:
                        realized_trading_pnl += (
                            accumulator.average_cost - fill.price
                        ) * close_quantity
                    accumulator.quantity += close_quantity
                    quantity -= close_quantity
                    if accumulator.quantity == ZERO:
                        accumulator.cost_basis = ZERO
                        accumulator.average_cost = None
                if quantity > ZERO:
                    accumulator.cost_basis += fill.price * quantity
                    accumulator.quantity += quantity
                    accumulator.average_cost = (
                        abs(accumulator.cost_basis) / abs(accumulator.quantity)
                        if accumulator.quantity != ZERO
                        else None
                    )
            else:
                if accumulator.quantity > ZERO:
                    close_quantity = min(accumulator.quantity, quantity)
                    if accumulator.average_cost is not None:
                        realized_trading_pnl += (
                            fill.price - accumulator.average_cost
                        ) * close_quantity
                    accumulator.quantity -= close_quantity
                    accumulator.cost_basis -= (accumulator.average_cost or ZERO) * close_quantity
                    quantity -= close_quantity
                    if accumulator.quantity == ZERO:
                        accumulator.cost_basis = ZERO
                        accumulator.average_cost = None
                if quantity > ZERO:
                    accumulator.cost_basis += fill.price * quantity
                    accumulator.quantity -= quantity
                    accumulator.average_cost = (
                        abs(accumulator.cost_basis) / abs(accumulator.quantity)
                        if accumulator.quantity != ZERO
                        else None
                    )

            if not suppress_fill_realization:
                accumulator.realized_trading_pnl += realized_trading_pnl
                accumulator.fee_pnl -= fill.fee_amount
                accumulator.slippage_pnl -= fill.slippage_amount
                accumulator.unwind_pnl -= fill.unwind_amount
                accumulator.onchain_pnl -= fill.onchain_amount
            adverse_bps = fill.metadata.get("adverse_selection_bps")
            if adverse_bps is not None:
                accumulator.adverse_selection_bps_weighted += (
                    _decimal(adverse_bps, name="adverse_selection_bps") * fill.quantity
                )
                accumulator.adverse_selection_quantity += fill.quantity
        return positions

    def _apply_order_open_quantities(
        self, accumulators: dict[str, _Accumulator], orders: Sequence[OrderRecord]
    ) -> None:
        for order in orders:
            if order.lifecycle_state not in ACTIVE_ORDER_STATES:
                continue
            accumulator = accumulators.setdefault(order.token_id, _Accumulator())
            if order.side == "buy":
                accumulator.open_buy_quantity += order.remaining_quantity or ZERO
            else:
                accumulator.open_sell_quantity += order.remaining_quantity or ZERO
            market_price = order.metadata.get("market_price")
            if market_price is not None:
                accumulator.market_price = _decimal(market_price, name="market_price")

    def _fill_realization_cycle_key(self, fill: FillRecord) -> tuple[str, str] | None:
        cycle_id = fill.metadata.get("cycle_id")
        if cycle_id is None:
            return None
        cycle_id_text = str(cycle_id).strip()
        if not cycle_id_text:
            return None
        return (fill.token_id, cycle_id_text)

    def _explicit_realization_cycle_key(self, row: PnLAttributionRecord) -> tuple[str, str] | None:
        metadata = row.metadata
        if not isinstance(metadata, Mapping) or "explicit_realized_net_pnl" not in metadata:
            return None
        cycle_id = metadata.get("cycle_id")
        if cycle_id is None:
            return None
        cycle_id_text = str(cycle_id).strip()
        if not cycle_id_text:
            return None
        return (row.token_id, cycle_id_text)

    def _explicit_realization_signature(
        self, row: PnLAttributionRecord
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, str]:
        metadata = row.metadata
        explicit_net = ""
        if isinstance(metadata, Mapping):
            raw_explicit_net = metadata.get("explicit_realized_net_pnl")
            if raw_explicit_net is not None:
                explicit_net = str(raw_explicit_net)
        return (
            row.realized_trading_pnl,
            row.fee_pnl,
            row.slippage_pnl,
            row.unwind_pnl,
            row.onchain_pnl,
            row.incentive_pnl_confirmed,
            row.incentive_pnl_pending,
            explicit_net,
        )

    def _resolve_authoritative_pnl_rows(
        self, rows: Sequence[PnLAttributionRecord]
    ) -> list[PnLAttributionRecord]:
        explicit_winners: dict[tuple[str, str], PnLAttributionRecord] = {}
        explicit_latest_at: dict[tuple[str, str], str] = {}
        explicit_latest_rows: dict[tuple[str, str], list[PnLAttributionRecord]] = {}
        for row in rows:
            cycle_key = self._explicit_realization_cycle_key(row)
            if cycle_key is not None:
                latest_at = explicit_latest_at.get(cycle_key)
                if latest_at is None or row.occurred_at > latest_at:
                    explicit_latest_at[cycle_key] = row.occurred_at
                    explicit_latest_rows[cycle_key] = [row]
                elif row.occurred_at == latest_at:
                    explicit_latest_rows.setdefault(cycle_key, []).append(row)
        for cycle_key, latest_rows in explicit_latest_rows.items():
            winner = latest_rows[0]
            winner_signature = self._explicit_realization_signature(winner)
            for candidate in latest_rows[1:]:
                if self._explicit_realization_signature(candidate) != winner_signature:
                    raise ValueError(
                        "conflicting explicit realized rows for "
                        f"{cycle_key[0]} cycle {cycle_key[1]} at {winner.occurred_at}"
                    )
            explicit_winners[cycle_key] = winner
        authoritative_rows: list[PnLAttributionRecord] = []
        for row in rows:
            cycle_key = self._explicit_realization_cycle_key(row)
            if cycle_key is None or explicit_winners.get(cycle_key) is row:
                authoritative_rows.append(row)
        return authoritative_rows

    def _explicit_realization_cycles(
        self, rows: Sequence[PnLAttributionRecord]
    ) -> set[tuple[str, str]]:
        return {
            cycle_key
            for row in rows
            if (cycle_key := self._explicit_realization_cycle_key(row)) is not None
        }

    def _apply_pnl_rows(
        self,
        accumulators: dict[str, _Accumulator],
        rows: Sequence[PnLAttributionRecord],
    ) -> None:
        for row in rows:
            accumulator = accumulators.setdefault(row.token_id, _Accumulator())
            accumulator.realized_trading_pnl += row.realized_trading_pnl
            accumulator.fee_pnl += row.fee_pnl
            accumulator.slippage_pnl += row.slippage_pnl
            accumulator.unwind_pnl += row.unwind_pnl
            accumulator.onchain_pnl += row.onchain_pnl
            accumulator.incentive_pnl_confirmed += row.incentive_pnl_confirmed
            accumulator.incentive_pnl_pending += row.incentive_pnl_pending

    def _position_views(
        self, accumulators: Mapping[str, _Accumulator], market_prices: Mapping[str, Decimal]
    ) -> tuple[PositionView, ...]:
        positions: list[PositionView] = []
        for token_id, accumulator in sorted(accumulators.items()):
            market_price = accumulator.market_price or market_prices.get(token_id)
            unrealized = ZERO
            if (
                market_price is not None
                and accumulator.average_cost is not None
                and accumulator.quantity != ZERO
            ):
                direction = Decimal(1) if accumulator.quantity > ZERO else Decimal(-1)
                unrealized = (
                    (market_price - accumulator.average_cost)
                    * abs(accumulator.quantity)
                    * direction
                )
            positions.append(
                PositionView(
                    token_id=token_id,
                    net_quantity=accumulator.quantity,
                    average_cost=accumulator.average_cost,
                    market_price=market_price,
                    open_buy_quantity=accumulator.open_buy_quantity,
                    open_sell_quantity=accumulator.open_sell_quantity,
                    realized_trading_pnl=accumulator.realized_trading_pnl,
                    fee_pnl=accumulator.fee_pnl,
                    slippage_pnl=accumulator.slippage_pnl,
                    unwind_pnl=accumulator.unwind_pnl,
                    onchain_pnl=accumulator.onchain_pnl,
                    incentive_pnl_confirmed=accumulator.incentive_pnl_confirmed,
                    incentive_pnl_pending=accumulator.incentive_pnl_pending,
                    unrealized_pnl=unrealized,
                    source="journal",
                    metadata={
                        "adverse_selection_bps": (
                            accumulator.adverse_selection_bps_weighted
                            / accumulator.adverse_selection_quantity
                            if accumulator.adverse_selection_quantity > ZERO
                            else None
                        )
                    },
                )
            )
        return tuple(positions)

    def _fill_rate_metrics(
        self, orders: Sequence[OrderRecord], fills: Sequence[FillRecord]
    ) -> dict[str, Decimal | None]:
        if not orders:
            return {"fill_rate": None, "maker_fill_rate": None}
        active_or_terminal = [
            order for order in orders if order.lifecycle_state not in {"draft", "reviewed"}
        ]
        if not active_or_terminal:
            return {"fill_rate": None, "maker_fill_rate": None}
        filled_order_ids = {fill.order_id for fill in fills}
        fill_rate = Decimal(len(filled_order_ids)) / Decimal(len(active_or_terminal))
        maker_orders = [order for order in active_or_terminal if order.order_role == "maker"]
        maker_fill_ids = {fill.order_id for fill in fills if fill.liquidity_role == "maker"}
        maker_fill_rate = (
            Decimal(len(maker_fill_ids)) / Decimal(len(maker_orders)) if maker_orders else None
        )
        return {"fill_rate": fill_rate, "maker_fill_rate": maker_fill_rate}

    def _adverse_selection_bps(self, positions: Sequence[PositionView]) -> Decimal | None:
        weighted_values: list[Decimal] = []
        total_weight = ZERO
        for position in positions:
            raw = position.metadata.get("adverse_selection_bps")
            if raw is None:
                continue
            weight = (
                abs(position.net_quantity)
                or position.open_buy_quantity
                or position.open_sell_quantity
            )
            if weight == ZERO:
                weight = Decimal(1)
            weighted_values.append(_decimal(raw, name="adverse_selection_bps") * weight)
            total_weight += weight
        if total_weight == ZERO:
            return None
        return _sum(weighted_values) / total_weight

    def _unmatched_leg_exposure_seconds(self, open_orders: Sequence[OpenOrderView]) -> Decimal:
        values = []
        for order in open_orders:
            raw = order.metadata.get("unmatched_leg_exposure_seconds")
            if raw is None:
                continue
            values.append(_decimal(raw, name="unmatched_leg_exposure_seconds"))
        return _sum(values)

    def _realized_pnl_timeline(self) -> list[Decimal]:
        timeline: list[tuple[str, Decimal]] = []
        for row in self._resolve_authoritative_pnl_rows(self.store.list_pnl_attribution()):
            net = (
                row.realized_trading_pnl
                + row.fee_pnl
                + row.slippage_pnl
                + row.unwind_pnl
                + row.onchain_pnl
                + row.incentive_pnl_confirmed
            )
            timeline.append((row.occurred_at, net))
        if not timeline:
            fills = self._dedupe_fills(self.store.list_fills())
            for fill in fills:
                total_cost = (
                    fill.fee_amount
                    + fill.slippage_amount
                    + fill.unwind_amount
                    + fill.onchain_amount
                )
                timeline.append((fill.occurred_at, -total_cost))
        return [value for _, value in sorted(timeline, key=lambda item: item[0])]

    def _max_drawdown(self, points: Sequence[Decimal]) -> Decimal | None:
        if not points:
            return None
        running = ZERO
        peak = ZERO
        max_drawdown = ZERO
        for point in points:
            running += point
            if running > peak:
                peak = running
            drawdown = peak - running
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        return max_drawdown

    def _latest_timestamp(
        self,
        orders: Sequence[OrderRecord],
        fills: Sequence[FillRecord],
        pnl_rows: Sequence[PnLAttributionRecord],
    ) -> str:
        candidates: list[str] = [order.updated_at for order in orders]
        candidates.extend(fill.occurred_at for fill in fills)
        candidates.extend(row.occurred_at for row in pnl_rows)
        if not candidates:
            state_snapshot = self.store.get_system_state("portfolio_snapshot")
            if isinstance(state_snapshot, Mapping) and "as_of" in state_snapshot:
                return str(state_snapshot["as_of"])
            return "1970-01-01T00:00:00Z"
        return max(candidates)
