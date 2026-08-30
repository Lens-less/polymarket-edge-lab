from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from src.profit_system.persistence import (
    ExecutionPlanRecord,
    FillRecord,
    OrderRecord,
    PersistenceStore,
    PnLAttributionRecord,
    VersionStamp,
)
from src.profit_system.portfolio import PortfolioBook

VERSION = VersionStamp(
    strategy_id="maker.v0",
    strategy_config_version="cfg-001",
    code_version="test-suite",
)


def _store(tmp_path: Path) -> PersistenceStore:
    return PersistenceStore(tmp_path / "portfolio.db")


def test_portfolio_snapshot_separates_realized_unrealized_and_pending_rewards(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.save_cash_snapshot(
            {
                "balances": [
                    {"currency": "USDC", "total": "100", "available": "80", "reserved": "20"}
                ]
            },
            idempotency_key="cash:seed",
        )
        store.upsert_order(
            OrderRecord(
                order_id="buy-order",
                venue_order_id="venue-buy",
                lifecycle_state="FILLED",
                market_id="market-1",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.40"),
                quantity=Decimal("10"),
                filled_quantity=Decimal("10"),
                order_role="maker",
                updated_at="2026-08-30T10:00:00Z",
                version=VERSION,
            ),
            idempotency_key="order:buy",
        )
        store.upsert_order(
            OrderRecord(
                order_id="sell-order",
                venue_order_id="venue-sell",
                lifecycle_state="FILLED",
                market_id="market-1",
                token_id="token-1",
                side="SELL",
                price=Decimal("0.60"),
                quantity=Decimal("10"),
                filled_quantity=Decimal("10"),
                order_role="maker",
                updated_at="2026-08-30T10:05:00Z",
                version=VERSION,
            ),
            idempotency_key="order:sell",
        )
        store.record_fill(
            FillRecord(
                fill_id="fill-buy",
                venue_fill_id="venue-fill-buy",
                order_id="buy-order",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.40"),
                quantity=Decimal("10"),
                occurred_at="2026-08-30T10:00:01Z",
                fee_amount=Decimal("0.05"),
                slippage_amount=Decimal("0.01"),
                liquidity_role="maker",
                metadata={"adverse_selection_bps": "4"},
                version=VERSION,
            ),
            idempotency_key="fill:buy",
        )
        store.record_fill(
            FillRecord(
                fill_id="fill-sell",
                venue_fill_id="venue-fill-sell",
                order_id="sell-order",
                token_id="token-1",
                side="SELL",
                price=Decimal("0.60"),
                quantity=Decimal("10"),
                occurred_at="2026-08-30T10:05:01Z",
                fee_amount=Decimal("0.04"),
                slippage_amount=Decimal("0.02"),
                unwind_amount=Decimal("0.01"),
                onchain_amount=Decimal("0.03"),
                liquidity_role="maker",
                metadata={"adverse_selection_bps": "6"},
                version=VERSION,
            ),
            idempotency_key="fill:sell",
        )
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="reward-1",
                source_type="incentive",
                source_id="reward-source-1",
                token_id="token-1",
                strategy_id="maker.v0",
                occurred_at="2026-08-30T10:06:00Z",
                incentive_pnl_confirmed=Decimal("0.50"),
                incentive_pnl_pending=Decimal("1.25"),
                version=VERSION,
            ),
            idempotency_key="pnl:reward1",
        )
        store.record_execution_plan(
            ExecutionPlanRecord(
                plan_id="plan-1",
                decision_id=None,
                strategy_id="maker.v0",
                status="FILLED",
                expected_net_edge=Decimal("2.00"),
                plan_kind="maker",
                created_at="2026-08-30T09:59:00Z",
                metadata={},
                version=VERSION,
            ),
            idempotency_key="plan:1",
        )
        book = PortfolioBook(store)
        snapshot = book.snapshot()
        performance = book.performance({"allocated_capital": Decimal("100")})

        assert snapshot.trading_net_pnl == Decimal("1.84")
        assert snapshot.incentive_pnl == Decimal("0.50")
        assert snapshot.incentive_pnl_pending == Decimal("1.25")
        assert snapshot.realized_net_pnl == Decimal("2.34")
        assert snapshot.unrealized_pnl == Decimal("0")
        assert snapshot.positions[0].net_quantity == Decimal("0")
        assert performance.metrics["fill_rate"] == Decimal("1")
        assert performance.metrics["maker_fill_rate"] == Decimal("1")
        assert performance.metrics["expected_net_edge"] == Decimal("2.00")
        assert performance.metrics["edge_realization_ratio"] == Decimal("1.17")
        assert performance.metrics["return_on_allocated_capital"] == Decimal("0.0234")
        assert performance.metrics["adverse_selection_bps"] == Decimal("5")
    finally:
        store.close()


def test_reconciliation_is_explicit_and_rebuild_persists_authoritative_view(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.save_cash_snapshot(
            {"balances": [{"currency": "USDC", "total": "100", "available": "96"}]},
            idempotency_key="cash:local",
        )
        store.upsert_order(
            OrderRecord(
                order_id="open-order",
                venue_order_id="venue-open-order",
                lifecycle_state="LIVE",
                market_id="market-1",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.45"),
                quantity=Decimal("5"),
                filled_quantity=Decimal("0"),
                order_role="maker",
                updated_at="2026-08-30T11:00:00Z",
                metadata={"unmatched_leg_exposure_seconds": "12"},
                version=VERSION,
            ),
            idempotency_key="order:open",
        )
        store.record_fill(
            FillRecord(
                fill_id="fill-open",
                venue_fill_id="venue-fill-open",
                order_id="open-order",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.45"),
                quantity=Decimal("2"),
                occurred_at="2026-08-30T11:00:30Z",
                fee_amount=Decimal("0.01"),
                liquidity_role="maker",
                version=VERSION,
            ),
            idempotency_key="fill:open",
        )
        book = PortfolioBook(store)

        report = book.reconcile(
            {
                "as_of": "2026-08-30T11:01:00Z",
                "cash_balances": [
                    {"currency": "USDC", "total": "95", "available": "95", "reserved": "0"}
                ],
                "positions": [
                    {
                        "token_id": "token-1",
                        "net_quantity": "3",
                        "average_cost": "0.45",
                        "market_price": "0.47",
                    }
                ],
                "open_orders": [],
            }
        )
        assert report.status == "mismatch"
        assert report.unresolved_count == 3

        rebuilt = book.rebuild_read_model(
            venue_snapshot={
                "as_of": "2026-08-30T11:01:00Z",
                "cash_balances": [
                    {"currency": "USDC", "total": "95", "available": "95", "reserved": "0"}
                ],
                "positions": [
                    {
                        "token_id": "token-1",
                        "net_quantity": "3",
                        "average_cost": "0.45",
                        "market_price": "0.47",
                    }
                ],
                "open_orders": [],
            }
        )
        assert rebuilt.cash_balances[0].total == Decimal("95")
        assert rebuilt.positions[0].market_price == Decimal("0.47")

        reopened = PersistenceStore(tmp_path / "portfolio.db")
        try:
            snapshot = PortfolioBook(reopened).snapshot()
            assert snapshot.cash_balances[0].total == Decimal("95")
            assert snapshot.unresolved_reconciliation_count == 3
            persisted_positions = reopened.list_position_snapshots()
            assert persisted_positions[-1].market_price == Decimal("0.47")
        finally:
            reopened.close()
    finally:
        store.close()
