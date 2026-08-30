from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from src.profit_system.persistence import (
    DuplicateVenueIdentifierError,
    FillRecord,
    OrderRecord,
    PersistenceStore,
    VersionStamp,
)

VERSION = VersionStamp(
    strategy_id="maker.v0",
    strategy_config_version="cfg-001",
    code_version="test-suite",
)


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_sqlite_wal_schema_and_decimal_storage_contract(tmp_path: Path) -> None:
    store = PersistenceStore(tmp_path / "portfolio.db")
    try:
        journal_mode = store._connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(journal_mode).lower() == "wal"

        expected_tables = {
            "schema_migrations",
            "sequence_state",
            "idempotency_keys",
            "execution_journal",
            "market_snapshots",
            "opportunities",
            "strategy_decisions",
            "execution_plans",
            "orders",
            "fills",
            "positions",
            "pnl_attribution",
            "reconciliation_runs",
            "risk_events",
            "strategy_evaluation_windows",
            "system_state",
            "strategy_qualifications",
        }
        assert expected_tables.issubset(_tables(store._connection))

        order_columns = {
            row[1]: row[2] for row in store._connection.execute("PRAGMA table_info(orders)")
        }
        fill_columns = {
            row[1]: row[2] for row in store._connection.execute("PRAGMA table_info(fills)")
        }
        position_columns = {
            row[1]: row[2] for row in store._connection.execute("PRAGMA table_info(positions)")
        }
        assert order_columns["price"] == "TEXT"
        assert order_columns["quantity"] == "TEXT"
        assert fill_columns["price"] == "TEXT"
        assert fill_columns["fee_amount"] == "TEXT"
        assert position_columns["realized_trading_pnl"] == "TEXT"

        migration_count = store._connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        assert migration_count >= 1
    finally:
        store.close()


def test_idempotent_order_upsert_and_failed_duplicate_fill_are_atomic(
    tmp_path: Path,
) -> None:
    store = PersistenceStore(tmp_path / "portfolio.db")
    try:
        order = OrderRecord(
            order_id="order-1",
            venue_order_id="venue-order-1",
            lifecycle_state="LIVE",
            market_id="market-1",
            token_id="token-1",
            side="BUY",
            price=Decimal("0.42"),
            quantity=Decimal("5"),
            filled_quantity=Decimal("2"),
            order_role="maker",
            metadata={"market_price": "0.44"},
            version=VERSION,
        )
        first = store.upsert_order(order, idempotency_key="order:1")
        second = store.upsert_order(order, idempotency_key="order:1")

        assert first.sequence == second.sequence
        assert second.replayed is True
        assert store.journal_count() == 1

        fill = FillRecord(
            fill_id="fill-1",
            venue_fill_id="venue-fill-1",
            order_id="order-1",
            token_id="token-1",
            side="BUY",
            price=Decimal("0.42"),
            quantity=Decimal("2"),
            occurred_at="2026-08-30T10:00:00Z",
            fee_amount=Decimal("0.01"),
            liquidity_role="maker",
            version=VERSION,
        )
        store.record_fill(fill, idempotency_key="fill:1")
        journal_before = store.journal_count()

        with pytest.raises(DuplicateVenueIdentifierError):
            store.record_fill(
                FillRecord(
                    fill_id="fill-2",
                    venue_fill_id="venue-fill-1",
                    order_id="order-1",
                    token_id="token-1",
                    side="SELL",
                    price=Decimal("0.50"),
                    quantity=Decimal("1"),
                    occurred_at="2026-08-30T10:01:00Z",
                    version=VERSION,
                ),
                idempotency_key="fill:2",
            )

        assert store.journal_count() == journal_before
        fill_rows = store._connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        assert fill_rows == 1
    finally:
        store.close()


def test_backup_restore_and_restart_recovery_data_round_trip(tmp_path: Path) -> None:
    primary_path = tmp_path / "primary.db"
    backup_path = tmp_path / "backup.db"
    restored_path = tmp_path / "restored.db"
    store = PersistenceStore(primary_path)
    try:
        store.upsert_order(
            OrderRecord(
                order_id="order-open",
                venue_order_id="venue-open",
                lifecycle_state="AMBIGUOUS",
                market_id="market-1",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.40"),
                quantity=Decimal("3"),
                filled_quantity=Decimal("0"),
                order_role="maker",
                version=VERSION,
            ),
            idempotency_key="order:open",
        )
        store.save_cash_snapshot(
            {"balances": [{"currency": "USDC", "total": "100", "available": "97"}]},
            idempotency_key="cash:1",
        )
        store.save_kill_state(
            reason="persistent_kill",
            actor="test-suite",
            recovery={"action": "cancel_all_then_reconcile"},
            idempotency_key="kill:1",
        )
        store.save_restart_recovery_state(
            {
                "positions": [{"token_id": "token-1", "net_quantity": "0"}],
                "open_orders": [{"order_id": "order-open"}],
            },
            idempotency_key="recovery:1",
        )
        store.backup_to(backup_path)
    finally:
        store.close()

    restored = PersistenceStore(restored_path)
    try:
        restored.restore_from(backup_path)
        restart_context = restored.load_restart_context()
        assert restart_context["kill_state"]["kill_switch_engaged"] is True
        assert restart_context["restart_recovery"]["open_orders"] == [{"order_id": "order-open"}]
        assert restart_context["cash"]["balances"][0]["currency"] == "USDC"
        assert restart_context["open_orders"][0]["order_id"] == "order-open"
    finally:
        restored.close()
