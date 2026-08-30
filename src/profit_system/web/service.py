from __future__ import annotations

import json
import secrets
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, TypedDict

from .demo import build_demo_snapshot
from .models import (
    ConnectionState,
    DeskMode,
    DeskSnapshot,
    FillSnapshot,
    MutationRequest,
    MutationResult,
    OrderSnapshot,
    OrderState,
    PlanState,
    PositionSnapshot,
    ReconciliationDiff,
    isoformat,
    utc_now,
)


class DeskService(Protocol):
    def get_snapshot(
        self, *, scenario: str = "paper", session: str = "default"
    ) -> DeskSnapshot: ...
    def review(
        self, mutation: MutationRequest, *, scenario: str, session: str
    ) -> MutationResult: ...
    def confirm(
        self, mutation: MutationRequest, *, scenario: str, session: str
    ) -> MutationResult: ...
    def cancel(
        self, mutation: MutationRequest, *, scenario: str, session: str
    ) -> MutationResult: ...
    def cancel_all(
        self, mutation: MutationRequest, *, scenario: str, session: str
    ) -> MutationResult: ...
    def kill(self, mutation: MutationRequest, *, scenario: str, session: str) -> MutationResult: ...


class DeskConflictError(RuntimeError):
    pass


class MutationReceipt(TypedDict):
    accepted: bool
    message: str


class InMemoryDeskService:
    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir or (Path.cwd() / ".desk-state")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def get_snapshot(self, *, scenario: str = "paper", session: str = "default") -> DeskSnapshot:
        with self._lock:
            return self._load_or_seed(scenario=scenario, session=session)

    def review(self, mutation: MutationRequest, *, scenario: str, session: str) -> MutationResult:
        return self._mutate("review", mutation, scenario=scenario, session=session)

    def confirm(self, mutation: MutationRequest, *, scenario: str, session: str) -> MutationResult:
        return self._mutate("confirm", mutation, scenario=scenario, session=session)

    def cancel(self, mutation: MutationRequest, *, scenario: str, session: str) -> MutationResult:
        return self._mutate("cancel", mutation, scenario=scenario, session=session)

    def cancel_all(
        self, mutation: MutationRequest, *, scenario: str, session: str
    ) -> MutationResult:
        return self._mutate("cancel-all", mutation, scenario=scenario, session=session)

    def kill(self, mutation: MutationRequest, *, scenario: str, session: str) -> MutationResult:
        return self._mutate("kill", mutation, scenario=scenario, session=session)

    def _mutate(
        self, action: str, mutation: MutationRequest, *, scenario: str, session: str
    ) -> MutationResult:
        if not mutation.actor.strip():
            raise ValueError("actor must be non-empty")
        if not mutation.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        mutation_cache_key = (scenario, session, action, mutation.idempotency_key)
        with self._lock:
            receipts = self._load_receipts(scenario=scenario, session=session)
            cached = receipts.get(self._receipt_key(mutation_cache_key))
            if cached is not None:
                return MutationResult(
                    action=action,
                    accepted=bool(cached["accepted"]),
                    message=str(cached["message"]),
                    snapshot=self._load_or_seed(scenario=scenario, session=session),
                )
            snapshot = self._load_or_seed(scenario=scenario, session=session)
            if snapshot.status_bar.mode is DeskMode.SHADOW and action in {
                "confirm",
                "cancel",
                "cancel-all",
            }:
                raise DeskConflictError(
                    "Shadow mode is observational only and has no venue order mutation path."
                )
            if action == "review":
                updated = self._review_snapshot(snapshot, mutation)
                accepted = True
                message = f"Plan reviewed by {mutation.actor}."
            elif action == "confirm":
                updated = self._confirm_snapshot(snapshot, mutation)
                accepted = True
                message = f"Plan confirmed by {mutation.actor}."
            elif action == "cancel":
                updated = self._cancel_snapshot(snapshot, mutation)
                accepted = True
                message = f"Plan canceled by {mutation.actor}."
            elif action == "cancel-all":
                updated = self._cancel_all_snapshot(snapshot, mutation)
                accepted = True
                message = f"Canceled all open orders for {mutation.actor}."
            else:
                updated = self._kill_snapshot(snapshot, mutation)
                accepted = True
                message = f"Kill switch engaged by {mutation.actor}."
            self._save_snapshot(updated)
            receipts[self._receipt_key(mutation_cache_key)] = {
                "accepted": accepted,
                "message": message,
            }
            self._save_receipts(scenario=scenario, session=session, receipts=receipts)
            return MutationResult(
                action=action, accepted=accepted, message=message, snapshot=updated
            )

    def _review_snapshot(self, snapshot: DeskSnapshot, mutation: MutationRequest) -> DeskSnapshot:
        self._ensure_not_killed(snapshot)
        if snapshot.execution_plan.state is PlanState.APPROVED:
            return self._touch_snapshot(snapshot, f"review replayed by {mutation.actor}")
        plan = replace(snapshot.execution_plan, state=PlanState.REVIEWED)
        return self._replace_snapshot(
            snapshot,
            execution_plan=plan,
            action_log=snapshot.action_log + [f"review:{mutation.actor}"],
        )

    def _confirm_snapshot(self, snapshot: DeskSnapshot, mutation: MutationRequest) -> DeskSnapshot:
        self._ensure_not_killed(snapshot)
        if snapshot.status_bar.is_live_locked:
            raise DeskConflictError(snapshot.status_bar.mode_banner)
        if snapshot.status_bar.connections.data in {
            ConnectionState.DISCONNECTED,
            ConnectionState.STALE,
            ConnectionState.BLOCKED,
        }:
            raise DeskConflictError("Market data is not healthy enough to confirm.")
        if snapshot.status_bar.connections.orders in {
            ConnectionState.DISCONNECTED,
            ConnectionState.STALE,
            ConnectionState.BLOCKED,
        }:
            raise DeskConflictError("Order channel is not healthy enough to confirm.")
        if snapshot.execution_plan.state not in {PlanState.REVIEWED, PlanState.APPROVED}:
            raise DeskConflictError("Execution plan must be reviewed before confirm.")
        now = utc_now()
        updated_order = OrderSnapshot(
            id=f"ord-{snapshot.snapshot_version + 1}",
            plan_id=snapshot.execution_plan.id,
            market=snapshot.explanation.market_title,
            state=OrderState.FILLED,
            side=snapshot.execution_plan.legs[0].side,
            price=snapshot.execution_plan.legs[0].price,
            size=snapshot.execution_plan.legs[0].size,
            filled_size=snapshot.execution_plan.legs[0].size,
            venue_status="paper-filled",
            updated_at=isoformat(now),
        )
        fill = FillSnapshot(
            id=f"fill-{snapshot.snapshot_version + 1}",
            order_id=updated_order.id,
            market=snapshot.explanation.market_title,
            side=updated_order.side,
            price=updated_order.price,
            size=updated_order.size,
            fee=snapshot.execution_plan.estimated_fee,
            realized_net_pnl=snapshot.execution_plan.expected_net_profit,
            occurred_at=isoformat(now),
        )
        previous_position = snapshot.positions[0]
        updated_position = replace(
            previous_position,
            quantity=previous_position.quantity + updated_order.size,
            avg_price=updated_order.price,
            realized_net_pnl=previous_position.realized_net_pnl + fill.realized_net_pnl,
            unrealized_net_pnl=previous_position.unrealized_net_pnl + Decimal("0.900000"),
        )
        previous_strategy = snapshot.strategy_pnl[0]
        updated_strategy = replace(
            previous_strategy,
            realized_today=previous_strategy.realized_today + fill.realized_net_pnl,
            realized_seven_day=previous_strategy.realized_seven_day + fill.realized_net_pnl,
            realized_thirty_day=previous_strategy.realized_thirty_day + fill.realized_net_pnl,
            mark_to_market=previous_strategy.mark_to_market + Decimal("0.900000"),
        )
        previous_expected = snapshot.expected_vs_realized[0]
        updated_expected = replace(
            previous_expected,
            realized_net_pnl=previous_expected.realized_net_pnl + fill.realized_net_pnl,
            variance=(
                previous_expected.realized_net_pnl
                + fill.realized_net_pnl
                - previous_expected.expected_net_pnl
            ),
        )
        updated_status_bar = replace(
            snapshot.status_bar,
            available_cash=snapshot.status_bar.available_cash - Decimal("21.120000"),
            realized_net_pnl=replace(
                snapshot.status_bar.realized_net_pnl,
                today=snapshot.status_bar.realized_net_pnl.today + fill.realized_net_pnl,
                seven_day=snapshot.status_bar.realized_net_pnl.seven_day + fill.realized_net_pnl,
                thirty_day=snapshot.status_bar.realized_net_pnl.thirty_day + fill.realized_net_pnl,
            ),
            risk_budget_used=snapshot.status_bar.risk_budget_used
            + snapshot.execution_plan.risk_budget_change,
        )
        updated_reconciliation = replace(
            snapshot.reconciliation,
            status="clean",
            last_run_at=isoformat(now),
            summary="Venue snapshot, user stream, and journal agree after confirm.",
            differences=[],
        )
        return self._replace_snapshot(
            snapshot,
            status_bar=updated_status_bar,
            execution_plan=replace(snapshot.execution_plan, state=PlanState.APPROVED),
            orders=[updated_order, *snapshot.orders],
            fills=[fill, *snapshot.fills],
            positions=[updated_position, *snapshot.positions[1:]],
            strategy_pnl=[updated_strategy, *snapshot.strategy_pnl[1:]],
            expected_vs_realized=[updated_expected, *snapshot.expected_vs_realized[1:]],
            reconciliation=updated_reconciliation,
            action_log=snapshot.action_log + [f"confirm:{mutation.actor}"],
        )

    def _cancel_snapshot(self, snapshot: DeskSnapshot, mutation: MutationRequest) -> DeskSnapshot:
        self._ensure_not_killed(snapshot)
        canceled_orders = [
            replace(
                order,
                state=OrderState.CANCELED,
                venue_status="canceled",
                updated_at=isoformat(utc_now()),
            )
            if order.state is OrderState.LIVE
            else order
            for order in snapshot.orders
        ]
        return self._replace_snapshot(
            snapshot,
            execution_plan=replace(snapshot.execution_plan, state=PlanState.CANCELED),
            orders=canceled_orders,
            action_log=snapshot.action_log + [f"cancel:{mutation.actor}"],
        )

    def _cancel_all_snapshot(
        self, snapshot: DeskSnapshot, mutation: MutationRequest
    ) -> DeskSnapshot:
        self._ensure_not_killed(snapshot)
        now = isoformat(utc_now())
        updated_orders = [
            replace(order, state=OrderState.CANCELED, venue_status="cancel-all", updated_at=now)
            if order.state in {OrderState.DRAFT, OrderState.LIVE}
            else order
            for order in snapshot.orders
        ]
        updated_reconciliation = replace(
            snapshot.reconciliation,
            status="attention",
            last_run_at=now,
            summary="Cancel-all requested; verify any remaining venue orders before re-entry.",
            differences=[
                *snapshot.reconciliation.differences,
                ReconciliationDiff(
                    scope="orders",
                    severity="medium",
                    message="Cancel-all issued; awaiting final venue confirmation sweep.",
                ),
            ],
        )
        return self._replace_snapshot(
            snapshot,
            orders=updated_orders,
            execution_plan=replace(snapshot.execution_plan, state=PlanState.CANCELED),
            reconciliation=updated_reconciliation,
            action_log=snapshot.action_log + [f"cancel-all:{mutation.actor}"],
        )

    def _kill_snapshot(self, snapshot: DeskSnapshot, mutation: MutationRequest) -> DeskSnapshot:
        now = isoformat(utc_now())
        updated_orders = [
            replace(order, state=OrderState.KILLED, venue_status="killed", updated_at=now)
            if order.state not in {OrderState.CANCELED, OrderState.FILLED, OrderState.KILLED}
            else order
            for order in snapshot.orders
        ]
        updated_status_bar = replace(
            snapshot.status_bar,
            mode=DeskMode.KILLED,
            is_live_locked=True,
            kill_switch_engaged=True,
            mode_banner=(
                "KILLED: new orders blocked until a manual operator reset completes reconciliation."
            ),
            connections=replace(
                snapshot.status_bar.connections,
                orders=ConnectionState.BLOCKED,
                blocked_reason="kill switch engaged",
            ),
        )
        updated_reconciliation = replace(
            snapshot.reconciliation,
            status="attention",
            last_run_at=now,
            summary="Kill switch persists across restart until manual intervention clears it.",
            differences=[
                ReconciliationDiff(
                    scope="system",
                    severity="high",
                    message="Kill switch is durable; confirm remains disabled after restart.",
                )
            ],
        )
        return self._replace_snapshot(
            snapshot,
            status_bar=updated_status_bar,
            execution_plan=replace(
                snapshot.execution_plan,
                state=PlanState.KILLED,
                live_lock_reason="kill switch engaged",
            ),
            orders=updated_orders,
            reconciliation=updated_reconciliation,
            action_log=snapshot.action_log + [f"kill:{mutation.actor}"],
        )

    def _replace_snapshot(self, snapshot: DeskSnapshot, **changes: Any) -> DeskSnapshot:
        changes.setdefault("csrf_token", snapshot.csrf_token)
        updated = replace(snapshot, **changes)
        return self._touch_snapshot(updated)

    def _touch_snapshot(
        self, snapshot: DeskSnapshot, action_log_entry: str | None = None
    ) -> DeskSnapshot:
        new_log = (
            snapshot.action_log
            if action_log_entry is None
            else snapshot.action_log + [action_log_entry]
        )
        return replace(
            snapshot,
            snapshot_version=snapshot.snapshot_version + 1,
            generated_at=utc_now(),
            action_log=new_log,
        )

    def _ensure_not_killed(self, snapshot: DeskSnapshot) -> None:
        if snapshot.status_bar.kill_switch_engaged or snapshot.status_bar.mode is DeskMode.KILLED:
            raise DeskConflictError(
                "Kill switch engaged; manual reset required before more mutations."
            )

    def _path_for(self, *, scenario: str, session: str) -> Path:
        safe_scenario = "".join(
            char if char.isalnum() or char in {"-", "_"} else "-" for char in scenario
        )
        safe_session = "".join(
            char if char.isalnum() or char in {"-", "_"} else "-" for char in session
        )
        return self._state_dir / f"{safe_scenario}__{safe_session}.json"

    def _receipt_path_for(self, *, scenario: str, session: str) -> Path:
        safe_scenario = "".join(
            char if char.isalnum() or char in {"-", "_"} else "-" for char in scenario
        )
        safe_session = "".join(
            char if char.isalnum() or char in {"-", "_"} else "-" for char in session
        )
        return self._state_dir / f"{safe_scenario}__{safe_session}.receipts.json"

    def _receipt_key(self, mutation_key: tuple[str, str, str, str]) -> str:
        return "::".join(mutation_key)

    @staticmethod
    def _mapping(value: object, *, label: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise TypeError(f"{label} must be a JSON object")
        return value

    @staticmethod
    def _items(value: object, *, label: str) -> list[dict[str, object]]:
        if not isinstance(value, list):
            raise TypeError(f"{label} must be a JSON array")
        items: list[dict[str, object]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise TypeError(f"{label}[{index}] must be a JSON object")
            items.append(item)
        return items

    def _load_or_seed(self, *, scenario: str, session: str) -> DeskSnapshot:
        path = self._path_for(scenario=scenario, session=session)
        if not path.exists():
            snapshot = self._ensure_random_csrf_token(
                build_demo_snapshot(scenario=scenario, session=session)
            )
            self._save_snapshot(snapshot)
            return snapshot
        payload = json.loads(path.read_text(encoding="utf-8"))
        snapshot = self._ensure_random_csrf_token(self._from_wire(payload))
        self._save_snapshot(snapshot)
        return snapshot

    def _load_receipts(self, *, scenario: str, session: str) -> dict[str, MutationReceipt]:
        path = self._receipt_path_for(scenario=scenario, session=session)
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("receipt store must be a JSON object")
        receipts: dict[str, MutationReceipt] = {}
        for key, value in payload.items():
            item = self._mapping(value, label=f"receipts[{key}]")
            receipts[str(key)] = {
                "accepted": self._boolean(item, "accepted"),
                "message": self._text(item, "message"),
            }
        return receipts

    def _save_receipts(
        self,
        *,
        scenario: str,
        session: str,
        receipts: dict[str, MutationReceipt],
    ) -> None:
        path = self._receipt_path_for(scenario=scenario, session=session)
        path.write_text(json.dumps(receipts, indent=2, sort_keys=True), encoding="utf-8")

    def _save_snapshot(self, snapshot: DeskSnapshot) -> None:
        path = self._path_for(scenario=snapshot.scenario, session=snapshot.session)
        path.write_text(json.dumps(snapshot.to_wire(), indent=2, sort_keys=True), encoding="utf-8")

    def _from_wire(self, payload: dict[str, object]) -> DeskSnapshot:
        from .demo import build_demo_snapshot

        snapshot = build_demo_snapshot(
            scenario=self._text(payload, "scenario"),
            session=self._text(payload, "session"),
            snapshot_version=self._integer(payload, "snapshot_version"),
        )
        status_bar = self._mapping(payload["status_bar"], label="status_bar")
        connections = self._mapping(status_bar["connections"], label="status_bar.connections")
        realized_net_pnl = self._mapping(
            status_bar["realized_net_pnl"], label="status_bar.realized_net_pnl"
        )
        execution_plan = self._mapping(payload["execution_plan"], label="execution_plan")
        reconciliation = self._mapping(payload["reconciliation"], label="reconciliation")
        strategy_pnl = self._items(payload["strategy_pnl"], label="strategy_pnl")
        expected_vs_realized = self._items(
            payload["expected_vs_realized"], label="expected_vs_realized"
        )
        orders = self._items(payload["orders"], label="orders")
        fills = self._items(payload["fills"], label="fills")
        positions = self._items(payload["positions"], label="positions")
        snapshot = replace(
            snapshot,
            generated_at=utc_now(),
            csrf_token=self._text(payload, "csrf_token"),
            selected_opportunity_id=self._text(payload, "selected_opportunity_id"),
            action_log=self._string_items(payload.get("action_log", []), label="action_log"),
            status_bar=replace(
                snapshot.status_bar,
                mode=DeskMode(self._text(status_bar, "mode")),
                available_cash=self._decimal(status_bar, "available_cash"),
                current_drawdown=self._decimal(status_bar, "current_drawdown"),
                risk_budget_used=self._decimal(status_bar, "risk_budget_used"),
                is_live_locked=self._boolean(status_bar, "is_live_locked"),
                kill_switch_engaged=self._boolean(status_bar, "kill_switch_engaged"),
                mode_banner=self._text(status_bar, "mode_banner"),
                connections=replace(
                    snapshot.status_bar.connections,
                    data=ConnectionState(self._text(connections, "data")),
                    orders=ConnectionState(self._text(connections, "orders")),
                    market_data_age_ms=self._integer(connections, "market_data_age_ms"),
                    blocked_reason=self._optional_text(connections.get("blocked_reason")),
                ),
                realized_net_pnl=replace(
                    snapshot.status_bar.realized_net_pnl,
                    today=self._decimal(realized_net_pnl, "today"),
                    seven_day=self._decimal(realized_net_pnl, "seven_day"),
                    thirty_day=self._decimal(realized_net_pnl, "thirty_day"),
                ),
            ),
            execution_plan=replace(
                snapshot.execution_plan,
                state=PlanState(self._text(execution_plan, "state")),
                live_lock_reason=self._optional_text(execution_plan.get("live_lock_reason")),
                estimated_fee=self._decimal(execution_plan, "estimated_fee"),
                estimated_slippage=self._decimal(execution_plan, "estimated_slippage"),
                expected_net_profit=self._decimal(execution_plan, "expected_net_profit"),
                worst_case_loss=self._decimal(execution_plan, "worst_case_loss"),
                risk_budget_change=self._decimal(execution_plan, "risk_budget_change"),
            ),
            strategy_pnl=[
                replace(
                    snapshot.strategy_pnl[index],
                    realized_today=self._decimal(item, "realized_today"),
                    realized_seven_day=self._decimal(item, "realized_seven_day"),
                    realized_thirty_day=self._decimal(item, "realized_thirty_day"),
                    mark_to_market=self._decimal(item, "mark_to_market"),
                )
                for index, item in enumerate(strategy_pnl)
            ],
            expected_vs_realized=[
                replace(
                    snapshot.expected_vs_realized[index],
                    expected_net_pnl=self._decimal(item, "expected_net_pnl"),
                    realized_net_pnl=self._decimal(item, "realized_net_pnl"),
                    variance=self._decimal(item, "variance"),
                )
                for index, item in enumerate(expected_vs_realized)
            ],
            reconciliation=replace(
                snapshot.reconciliation,
                status=self._text(reconciliation, "status"),
                last_run_at=self._text(reconciliation, "last_run_at"),
                summary=self._text(reconciliation, "summary"),
                differences=[
                    ReconciliationDiff(
                        scope=self._text(diff, "scope"),
                        severity=self._text(diff, "severity"),
                        message=self._text(diff, "message"),
                    )
                    for diff in self._items(
                        reconciliation["differences"], label="reconciliation.differences"
                    )
                ],
            ),
            orders=[
                OrderSnapshot(
                    id=self._text(item, "id"),
                    plan_id=self._text(item, "plan_id"),
                    market=self._text(item, "market"),
                    state=OrderState(self._text(item, "state")),
                    side=self._text(item, "side"),
                    price=self._decimal(item, "price"),
                    size=self._decimal(item, "size"),
                    filled_size=self._decimal(item, "filled_size"),
                    venue_status=self._text(item, "venue_status"),
                    updated_at=self._text(item, "updated_at"),
                )
                for item in orders
            ],
            fills=[
                FillSnapshot(
                    id=self._text(item, "id"),
                    order_id=self._text(item, "order_id"),
                    market=self._text(item, "market"),
                    side=self._text(item, "side"),
                    price=self._decimal(item, "price"),
                    size=self._decimal(item, "size"),
                    fee=self._decimal(item, "fee"),
                    realized_net_pnl=self._decimal(item, "realized_net_pnl"),
                    occurred_at=self._text(item, "occurred_at"),
                )
                for item in fills
            ],
            positions=[
                PositionSnapshot(
                    id=self._text(item, "id"),
                    market=self._text(item, "market"),
                    side=self._text(item, "side"),
                    quantity=self._decimal(item, "quantity"),
                    avg_price=self._decimal(item, "avg_price"),
                    mark_price=self._decimal(item, "mark_price"),
                    realized_net_pnl=self._decimal(item, "realized_net_pnl"),
                    unrealized_net_pnl=self._decimal(item, "unrealized_net_pnl"),
                )
                for item in positions
            ],
        )
        return snapshot

    @staticmethod
    def _ensure_random_csrf_token(snapshot: DeskSnapshot) -> DeskSnapshot:
        if snapshot.csrf_token.startswith("desk-csrf-") or len(snapshot.csrf_token) < 32:
            return replace(snapshot, csrf_token=secrets.token_urlsafe(32))
        return snapshot

    @staticmethod
    def _text(value: dict[str, object], key: str) -> str:
        item = value[key]
        if not isinstance(item, str):
            raise TypeError(f"{key} must be a string")
        return item

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("optional text must be a string")
        return value

    @staticmethod
    def _integer(value: dict[str, object], key: str) -> int:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int):
            raise TypeError(f"{key} must be an integer")
        return item

    @staticmethod
    def _boolean(value: dict[str, object], key: str) -> bool:
        item = value[key]
        if not isinstance(item, bool):
            raise TypeError(f"{key} must be a boolean")
        return item

    @staticmethod
    def _decimal(value: dict[str, object], key: str) -> Decimal:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise TypeError(f"{key} must be decimal-compatible")
        return Decimal(str(item))

    @staticmethod
    def _string_items(value: object, *, label: str) -> list[str]:
        if not isinstance(value, list):
            raise TypeError(f"{label} must be a JSON array")
        items: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise TypeError(f"{label}[{index}] must be a string")
            items.append(item)
        return items
