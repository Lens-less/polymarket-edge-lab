from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from src.profit_system.persistence import (
    DuplicateVenueIdentifierError,
    ExecutionPlanRecord,
    FillRecord,
    OpportunityRecord,
    OrderRecord,
    PersistenceStore,
    PositionSnapshotRecord,
    ReconciliationRunRecord,
    RiskEventRecord,
    StrategyDecisionRecord,
    VersionStamp,
)

VERSION = VersionStamp(
    strategy_id="maker.v0",
    strategy_config_version="cfg-001",
    code_version="test-suite",
)


def _store(path: Path) -> PersistenceStore:
    return PersistenceStore(path)


def test_reopens_all_persisted_recovery_records_from_disk(tmp_path: Path) -> None:
    db_path = tmp_path / "recovery.db"
    store = _store(db_path)
    try:
        store.record_opportunity(
            OpportunityRecord(
                opportunity_id="opp-1",
                market_id="market-1",
                token_id="token-1",
                expected_net_edge=Decimal("1.25"),
                status="QUALIFIED",
                observed_at="2026-08-30T12:00:00Z",
                metadata={"lane": "track_a"},
                version=VERSION,
            ),
            idempotency_key="opportunity:1",
        )
        store.record_strategy_decision(
            StrategyDecisionRecord(
                decision_id="decision-1",
                opportunity_id="opp-1",
                strategy_id="maker.v0",
                decision="APPROVE",
                expected_net_edge=Decimal("1.25"),
                decided_at="2026-08-30T12:00:05Z",
                metadata={"reason": "ranked-first"},
                version=VERSION,
            ),
            idempotency_key="decision:1",
        )
        store.record_execution_plan(
            ExecutionPlanRecord(
                plan_id="plan-1",
                decision_id="decision-1",
                strategy_id="maker.v0",
                status="READY",
                expected_net_edge=Decimal("1.25"),
                plan_kind="MAKER",
                created_at="2026-08-30T12:00:10Z",
                metadata={"phase": "paper"},
                version=VERSION,
            ),
            idempotency_key="plan:1",
        )
        store.upsert_order(
            OrderRecord(
                order_id="order-1",
                venue_order_id="venue-order-1",
                lifecycle_state="LIVE",
                market_id="market-1",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.41"),
                quantity=Decimal("5"),
                filled_quantity=Decimal("2"),
                order_role="maker",
                updated_at="2026-08-30T12:00:20Z",
                metadata={"phase": "shadow"},
                version=VERSION,
            ),
            idempotency_key="order:1",
        )
        store.record_fill(
            FillRecord(
                fill_id="fill-1",
                venue_fill_id="venue-fill-1",
                order_id="order-1",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.41"),
                quantity=Decimal("2"),
                occurred_at="2026-08-30T12:00:25Z",
                fee_amount=Decimal("0.01"),
                liquidity_role="maker",
                version=VERSION,
            ),
            idempotency_key="fill:1",
        )
        store.record_position_snapshot(
            PositionSnapshotRecord(
                snapshot_id="position-1",
                token_id="token-1",
                as_of="2026-08-30T12:00:30Z",
                net_quantity=Decimal("2"),
                average_cost=Decimal("0.41"),
                market_price=Decimal("0.43"),
                open_buy_quantity=Decimal("3"),
                realized_trading_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0.04"),
                metadata={"source": "reconcile"},
                version=VERSION,
            ),
            idempotency_key="position:1",
        )
        store.record_reconciliation_run(
            ReconciliationRunRecord(
                run_id="recon-1",
                scope="portfolio",
                status="matched",
                venue_snapshot={"cash": "97", "open_orders": ["order-1"]},
                report={"summary": "clean"},
                unresolved_count=0,
                created_at="2026-08-30T12:00:35Z",
                version=VERSION,
            ),
            idempotency_key="recon:1",
        )
        store.record_risk_event(
            RiskEventRecord(
                event_id="risk-1",
                event_type="kill_switch",
                severity="critical",
                occurred_at="2026-08-30T12:00:40Z",
                kill_switch_engaged=True,
                details={"reason": "operator_drill"},
                version=VERSION,
            ),
            idempotency_key="risk:1",
        )
        store.save_cash_snapshot(
            {"balances": [{"currency": "USDC", "total": "100", "available": "97"}]},
            idempotency_key="cash:1",
        )
        store.save_kill_state(
            reason="operator_drill",
            actor="test-suite",
            recovery={"action": "cancel_all_then_reconcile"},
            idempotency_key="kill:1",
        )
        store.save_restart_recovery_state(
            {"open_orders": [{"order_id": "order-1"}], "last_plan_id": "plan-1"},
            idempotency_key="recovery:1",
        )
    finally:
        store.close()

    reopened = _store(db_path)
    try:
        context = reopened.load_restart_context()
        plans = reopened.list_execution_plans()
        reconciliations = reopened.list_reconciliation_runs()
        position_snapshots = reopened.list_position_snapshots()

        opportunity_row = reopened._connection.execute(
            (
                "SELECT opportunity_id, expected_net_edge, status "
                "FROM opportunities WHERE opportunity_id = ?"
            ),
            ("opp-1",),
        ).fetchone()
        decision_row = reopened._connection.execute(
            (
                "SELECT decision_id, opportunity_id, decision "
                "FROM strategy_decisions WHERE decision_id = ?"
            ),
            ("decision-1",),
        ).fetchone()

        assert opportunity_row is not None
        assert opportunity_row["opportunity_id"] == "opp-1"
        assert opportunity_row["expected_net_edge"] == "1.25"
        assert opportunity_row["status"] == "qualified"
        assert decision_row is not None
        assert decision_row["decision_id"] == "decision-1"
        assert decision_row["opportunity_id"] == "opp-1"
        assert decision_row["decision"] == "approve"
        assert [plan.plan_id for plan in plans] == ["plan-1"]
        assert reconciliations[-1].run_id == "recon-1"
        assert reconciliations[-1].status == "matched"
        assert position_snapshots[-1].snapshot_id == "position-1"
        assert position_snapshots[-1].market_price == Decimal("0.43")
        assert context["kill_state"]["kill_switch_engaged"] is True
        assert context["restart_recovery"]["last_plan_id"] == "plan-1"
        assert context["cash"]["balances"][0]["available"] == "97"
        assert context["open_orders"][0]["order_id"] == "order-1"
        assert context["recent_fills"][0]["fill_id"] == "fill-1"
    finally:
        reopened.close()


def test_sequences_remain_monotonic_across_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "sequence.db"
    store = _store(db_path)
    try:
        first = store.record_opportunity(
            OpportunityRecord(
                opportunity_id="opp-1",
                market_id="market-1",
                token_id="token-1",
                expected_net_edge=Decimal("0.50"),
                status="seen",
                observed_at="2026-08-30T12:10:00Z",
                version=VERSION,
            ),
            idempotency_key="opportunity:1",
        )
        second = store.record_execution_plan(
            ExecutionPlanRecord(
                plan_id="plan-1",
                decision_id=None,
                strategy_id="maker.v0",
                status="ready",
                expected_net_edge=Decimal("0.50"),
                plan_kind="maker",
                created_at="2026-08-30T12:10:05Z",
                version=VERSION,
            ),
            idempotency_key="plan:1",
        )
        third = store.upsert_order(
            OrderRecord(
                order_id="order-1",
                venue_order_id="venue-order-1",
                lifecycle_state="submitting",
                market_id="market-1",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.33"),
                quantity=Decimal("1"),
                order_role="maker",
                updated_at="2026-08-30T12:10:10Z",
                version=VERSION,
            ),
            idempotency_key="order:1",
        )
        assert [first.sequence, second.sequence, third.sequence] == [1, 2, 3]
    finally:
        store.close()

    reopened = _store(db_path)
    try:
        fourth = reopened.record_reconciliation_run(
            ReconciliationRunRecord(
                run_id="recon-1",
                scope="portfolio",
                status="matched",
                venue_snapshot={"cash": "100"},
                report={"summary": "ok"},
                unresolved_count=0,
                created_at="2026-08-30T12:10:15Z",
                version=VERSION,
            ),
            idempotency_key="recon:1",
        )
        sequences = [event.sequence for event in reopened.iter_execution_journal()]

        assert fourth.sequence == 4
        assert sequences == [1, 2, 3, 4]
        assert (
            reopened._connection.execute(
                "SELECT next_value FROM sequence_state WHERE name = 'global'"
            ).fetchone()["next_value"]
            == 5
        )
    finally:
        reopened.close()


def test_replays_same_order_idempotently_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "idempotent.db"
    store = _store(db_path)
    order = OrderRecord(
        order_id="order-1",
        venue_order_id="venue-order-1",
        lifecycle_state="LIVE",
        market_id="market-1",
        token_id="token-1",
        side="BUY",
        price=Decimal("0.44"),
        quantity=Decimal("5"),
        filled_quantity=Decimal("1"),
        order_role="maker",
        updated_at="2026-08-30T12:20:00Z",
        version=VERSION,
    )
    try:
        original = store.upsert_order(order, idempotency_key="order:1")
        assert original.replayed is False
    finally:
        store.close()

    reopened = _store(db_path)
    try:
        replay = reopened.upsert_order(order, idempotency_key="order:1")

        assert replay.replayed is True
        assert replay.sequence == original.sequence
        assert reopened.journal_count() == 1
        assert reopened.list_orders()[0].filled_quantity == Decimal("1")
    finally:
        reopened.close()


def test_rejects_duplicate_venue_order_id_without_partial_commit_after_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "duplicate-order.db"
    store = _store(db_path)
    try:
        store.upsert_order(
            OrderRecord(
                order_id="order-1",
                venue_order_id="venue-order-1",
                lifecycle_state="LIVE",
                market_id="market-1",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.25"),
                quantity=Decimal("2"),
                order_role="maker",
                updated_at="2026-08-30T12:30:00Z",
                version=VERSION,
            ),
            idempotency_key="order:1",
        )
    finally:
        store.close()

    reopened = _store(db_path)
    try:
        journal_before = reopened.journal_count()
        with pytest.raises(DuplicateVenueIdentifierError):
            reopened.upsert_order(
                OrderRecord(
                    order_id="order-2",
                    venue_order_id="venue-order-1",
                    lifecycle_state="LIVE",
                    market_id="market-2",
                    token_id="token-2",
                    side="SELL",
                    price=Decimal("0.75"),
                    quantity=Decimal("3"),
                    order_role="taker",
                    updated_at="2026-08-30T12:30:05Z",
                    version=VERSION,
                ),
                idempotency_key="order:2",
            )

        assert reopened.journal_count() == journal_before
        assert [order.order_id for order in reopened.list_orders()] == ["order-1"]
        assert (
            reopened._connection.execute(
                "SELECT next_value FROM sequence_state WHERE name = 'global'"
            ).fetchone()["next_value"]
            == 2
        )
    finally:
        reopened.close()


def test_backup_restore_continues_sequence_and_state(tmp_path: Path) -> None:
    primary_path = tmp_path / "primary.db"
    backup_path = tmp_path / "backup.db"
    restored_path = tmp_path / "restored.db"
    primary = _store(primary_path)
    try:
        primary.record_execution_plan(
            ExecutionPlanRecord(
                plan_id="plan-1",
                decision_id=None,
                strategy_id="maker.v0",
                status="ready",
                expected_net_edge=Decimal("0.80"),
                plan_kind="maker",
                created_at="2026-08-30T12:40:00Z",
                metadata={"phase": "replay"},
                version=VERSION,
            ),
            idempotency_key="plan:1",
        )
        primary.save_kill_state(
            reason="drill",
            actor="test-suite",
            recovery={"action": "reconcile"},
            idempotency_key="kill:1",
        )
        primary.backup_to(backup_path)
    finally:
        primary.close()

    restored = _store(restored_path)
    try:
        restored.restore_from(backup_path)
        next_result = restored.save_restart_recovery_state(
            {"resume": "shadow"},
            idempotency_key="recovery:1",
        )

        assert next_result.sequence == 3
        assert restored.get_system_state("kill_state")["reason"] == "drill"
        assert restored.get_system_state("restart_recovery")["resume"] == "shadow"
        assert [event.sequence for event in restored.iter_execution_journal()] == [1, 2, 3]
    finally:
        restored.close()


def test_rolls_back_projection_and_journal_when_mutation_fails_mid_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path / "rollback.db")
    calls: list[int] = []
    original_persist = store._persist_idempotency

    def fail_after_journal(
        connection: sqlite3.Connection,
        *,
        idempotency_key: str,
        mutation_type: str,
        request_hash: str,
        sequence: int,
        payload: object,
    ) -> None:
        calls.append(sequence)
        raise sqlite3.OperationalError("simulated idempotency write failure")

    monkeypatch.setattr(store, "_persist_idempotency", fail_after_journal)
    try:
        with pytest.raises(sqlite3.OperationalError, match="simulated idempotency write failure"):
            store.upsert_order(
                OrderRecord(
                    order_id="order-1",
                    venue_order_id="venue-order-1",
                    lifecycle_state="LIVE",
                    market_id="market-1",
                    token_id="token-1",
                    side="BUY",
                    price=Decimal("0.51"),
                    quantity=Decimal("4"),
                    order_role="maker",
                    updated_at="2026-08-30T12:50:00Z",
                    version=VERSION,
                ),
                idempotency_key="order:1",
            )

        assert calls == [1]
        assert store.journal_count() == 0
        assert store.list_orders() == []
        assert (
            store._connection.execute(
                "SELECT next_value FROM sequence_state WHERE name = 'global'"
            ).fetchone()["next_value"]
            == 1
        )
        assert (
            store._connection.execute("SELECT COUNT(*) AS count FROM idempotency_keys").fetchone()[
                "count"
            ]
            == 0
        )
    finally:
        monkeypatch.setattr(store, "_persist_idempotency", original_persist)
        store.close()
