from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

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


def test_portfolio_snapshot_dedupes_explicit_realized_outcome_rows_against_fill_pnl(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.save_cash_snapshot(
            {"balances": [{"currency": "USDC", "total": "100", "available": "100"}]},
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
                metadata={"cycle_id": "cycle-1"},
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
                metadata={"cycle_id": "cycle-1"},
                version=VERSION,
            ),
            idempotency_key="fill:sell",
        )
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="cycle-1:realized",
                source_type="replay",
                source_id="replay-realized-source",
                token_id="token-1",
                strategy_id="maker.v0",
                occurred_at="2026-08-30T10:06:00Z",
                realized_trading_pnl=Decimal("2.00"),
                fee_pnl=Decimal("-0.09"),
                slippage_pnl=Decimal("-0.03"),
                unwind_pnl=Decimal("-0.01"),
                onchain_pnl=Decimal("-0.03"),
                incentive_pnl_confirmed=Decimal("0.20"),
                incentive_pnl_pending=Decimal("0.40"),
                metadata={
                    "cycle_id": "cycle-1",
                    "explicit_realized_net_pnl": "2.04",
                },
                version=VERSION,
            ),
            idempotency_key="pnl:cycle-1",
        )

        book = PortfolioBook(store)
        snapshot = book.snapshot()
        performance = book.performance({"allocated_capital": Decimal("100")})

        assert snapshot.trading_net_pnl == Decimal("1.84")
        assert snapshot.incentive_pnl == Decimal("0.20")
        assert snapshot.incentive_pnl_pending == Decimal("0.40")
        assert snapshot.realized_net_pnl == Decimal("2.04")
        assert snapshot.positions[0].realized_trading_pnl == Decimal("2.00")
        assert snapshot.positions[0].fee_pnl == Decimal("-0.09")
        assert snapshot.positions[0].slippage_pnl == Decimal("-0.03")
        assert snapshot.positions[0].unwind_pnl == Decimal("-0.01")
        assert snapshot.positions[0].onchain_pnl == Decimal("-0.03")
        assert performance.metrics["realized_net_pnl"] == Decimal("2.04")
        assert performance.metrics["return_on_allocated_capital"] == Decimal("0.0204")
    finally:
        store.close()


def test_portfolio_snapshot_keeps_explicit_realized_row_without_matching_cycle_fill_realization(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.record_fill(
            FillRecord(
                fill_id="fill-seed",
                venue_fill_id="venue-fill-seed",
                order_id="seed-order",
                token_id="token-1",
                side="BUY",
                price=Decimal("0.40"),
                quantity=Decimal("1"),
                occurred_at="2026-08-30T09:55:00Z",
                liquidity_role="maker",
                metadata={"cycle_id": "cycle-0"},
                version=VERSION,
            ),
            idempotency_key="fill:seed",
        )
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="cycle-1:realized",
                source_type="replay",
                source_id="replay-realized-source",
                token_id="token-1",
                strategy_id="maker.v0",
                occurred_at="2026-08-30T10:06:00Z",
                realized_trading_pnl=Decimal("1.25"),
                fee_pnl=Decimal("-0.05"),
                slippage_pnl=Decimal("-0.02"),
                unwind_pnl=Decimal("-0.01"),
                onchain_pnl=Decimal("-0.03"),
                incentive_pnl_confirmed=Decimal("0.20"),
                incentive_pnl_pending=Decimal("0.40"),
                metadata={
                    "cycle_id": "cycle-1",
                    "explicit_realized_net_pnl": "1.34",
                },
                version=VERSION,
            ),
            idempotency_key="pnl:cycle-1",
        )

        snapshot = PortfolioBook(store).snapshot()

        assert snapshot.positions[0].net_quantity == Decimal("1")
        assert snapshot.trading_net_pnl == Decimal("1.14")
        assert snapshot.incentive_pnl == Decimal("0.20")
        assert snapshot.incentive_pnl_pending == Decimal("0.40")
        assert snapshot.realized_net_pnl == Decimal("1.34")
        assert snapshot.positions[0].realized_trading_pnl == Decimal("1.25")
        assert snapshot.positions[0].fee_pnl == Decimal("-0.05")
        assert snapshot.positions[0].slippage_pnl == Decimal("-0.02")
        assert snapshot.positions[0].unwind_pnl == Decimal("-0.01")
        assert snapshot.positions[0].onchain_pnl == Decimal("-0.03")
    finally:
        store.close()


def test_portfolio_snapshot_prefers_explicit_realized_row_over_mismatched_same_cycle_fills(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
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
                metadata={"cycle_id": "cycle-1"},
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
                fee_amount=Decimal("0.02"),
                slippage_amount=Decimal("0.01"),
                unwind_amount=Decimal("0.01"),
                onchain_amount=Decimal("0.02"),
                liquidity_role="maker",
                metadata={"cycle_id": "cycle-1"},
                version=VERSION,
            ),
            idempotency_key="fill:sell",
        )
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="cycle-1:realized",
                source_type="replay",
                source_id="replay-realized-source",
                token_id="token-1",
                strategy_id="maker.v0",
                occurred_at="2026-08-30T10:06:00Z",
                realized_trading_pnl=Decimal("2.00"),
                fee_pnl=Decimal("-0.09"),
                slippage_pnl=Decimal("-0.03"),
                unwind_pnl=Decimal("-0.01"),
                onchain_pnl=Decimal("-0.03"),
                incentive_pnl_confirmed=Decimal("0.20"),
                metadata={
                    "cycle_id": "cycle-1",
                    "explicit_realized_net_pnl": "2.04",
                },
                version=VERSION,
            ),
            idempotency_key="pnl:cycle-1",
        )

        snapshot = PortfolioBook(store).snapshot()

        assert snapshot.positions[0].net_quantity == Decimal("0")
        assert snapshot.trading_net_pnl == Decimal("1.84")
        assert snapshot.realized_net_pnl == Decimal("2.04")
        assert snapshot.positions[0].realized_trading_pnl == Decimal("2.00")
        assert snapshot.positions[0].fee_pnl == Decimal("-0.09")
        assert snapshot.positions[0].slippage_pnl == Decimal("-0.03")
        assert snapshot.positions[0].unwind_pnl == Decimal("-0.01")
        assert snapshot.positions[0].onchain_pnl == Decimal("-0.03")
    finally:
        store.close()


def test_portfolio_snapshot_uses_latest_explicit_row_for_same_cycle(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="cycle-1:realized:old",
                source_type="replay",
                source_id="replay-realized-source-old",
                token_id="token-1",
                strategy_id="maker.v0",
                occurred_at="2026-08-30T10:05:00Z",
                realized_trading_pnl=Decimal("0.80"),
                fee_pnl=Decimal("-0.02"),
                metadata={
                    "cycle_id": "cycle-1",
                    "explicit_realized_net_pnl": "0.78",
                },
                version=VERSION,
            ),
            idempotency_key="pnl:cycle-1:old",
        )
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="cycle-1:realized:new",
                source_type="replay",
                source_id="replay-realized-source-new",
                token_id="token-1",
                strategy_id="maker.v0",
                occurred_at="2026-08-30T10:06:00Z",
                realized_trading_pnl=Decimal("1.25"),
                fee_pnl=Decimal("-0.05"),
                slippage_pnl=Decimal("-0.02"),
                unwind_pnl=Decimal("-0.01"),
                onchain_pnl=Decimal("-0.03"),
                incentive_pnl_confirmed=Decimal("0.20"),
                metadata={
                    "cycle_id": "cycle-1",
                    "explicit_realized_net_pnl": "1.34",
                },
                version=VERSION,
            ),
            idempotency_key="pnl:cycle-1:new",
        )

        book = PortfolioBook(store)
        snapshot = book.snapshot()
        performance = book.performance({"allocated_capital": Decimal("100")})

        assert snapshot.trading_net_pnl == Decimal("1.14")
        assert snapshot.realized_net_pnl == Decimal("1.34")
        assert snapshot.incentive_pnl == Decimal("0.20")
        assert performance.metrics["realized_net_pnl"] == Decimal("1.34")
        assert performance.metrics["return_on_allocated_capital"] == Decimal("0.0134")
    finally:
        store.close()


def test_portfolio_snapshot_rejects_conflicting_latest_explicit_rows_for_same_cycle(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="cycle-1:realized:a",
                source_type="replay",
                source_id="replay-realized-source-a",
                token_id="token-1",
                strategy_id="maker.v0",
                occurred_at="2026-08-30T10:06:00Z",
                realized_trading_pnl=Decimal("1.25"),
                fee_pnl=Decimal("-0.05"),
                slippage_pnl=Decimal("-0.02"),
                unwind_pnl=Decimal("-0.01"),
                onchain_pnl=Decimal("-0.03"),
                incentive_pnl_confirmed=Decimal("0.20"),
                metadata={
                    "cycle_id": "cycle-1",
                    "explicit_realized_net_pnl": "1.34",
                },
                version=VERSION,
            ),
            idempotency_key="pnl:cycle-1:a",
        )
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="cycle-1:realized:b",
                source_type="replay",
                source_id="replay-realized-source-b",
                token_id="token-1",
                strategy_id="maker.v0",
                occurred_at="2026-08-30T10:06:00Z",
                realized_trading_pnl=Decimal("1.10"),
                fee_pnl=Decimal("-0.04"),
                slippage_pnl=Decimal("-0.02"),
                unwind_pnl=Decimal("-0.01"),
                onchain_pnl=Decimal("-0.03"),
                incentive_pnl_confirmed=Decimal("0.20"),
                metadata={
                    "cycle_id": "cycle-1",
                    "explicit_realized_net_pnl": "1.20",
                },
                version=VERSION,
            ),
            idempotency_key="pnl:cycle-1:b",
        )

        book = PortfolioBook(store)

        with pytest.raises(ValueError, match="conflicting explicit realized rows"):
            book.snapshot()
        with pytest.raises(ValueError, match="conflicting explicit realized rows"):
            book.performance({"allocated_capital": Decimal("100")})
    finally:
        store.close()


def test_portfolio_snapshot_collapses_exact_duplicate_latest_explicit_rows(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        shared_payload = dict(
            source_type="replay",
            token_id="token-1",
            strategy_id="maker.v0",
            occurred_at="2026-08-30T10:06:00Z",
            realized_trading_pnl=Decimal("1.25"),
            fee_pnl=Decimal("-0.05"),
            slippage_pnl=Decimal("-0.02"),
            unwind_pnl=Decimal("-0.01"),
            onchain_pnl=Decimal("-0.03"),
            incentive_pnl_confirmed=Decimal("0.20"),
            incentive_pnl_pending=Decimal("0.40"),
            metadata={
                "cycle_id": "cycle-1",
                "explicit_realized_net_pnl": "1.34",
            },
            version=VERSION,
        )
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="cycle-1:realized:a",
                source_id="replay-realized-source-a",
                **shared_payload,
            ),
            idempotency_key="pnl:cycle-1:a",
        )
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="cycle-1:realized:b",
                source_id="replay-realized-source-b",
                **shared_payload,
            ),
            idempotency_key="pnl:cycle-1:b",
        )

        book = PortfolioBook(store)
        snapshot = book.snapshot()
        performance = book.performance({"allocated_capital": Decimal("100")})

        assert snapshot.trading_net_pnl == Decimal("1.14")
        assert snapshot.realized_net_pnl == Decimal("1.34")
        assert snapshot.incentive_pnl == Decimal("0.20")
        assert snapshot.incentive_pnl_pending == Decimal("0.40")
        assert performance.metrics["realized_net_pnl"] == Decimal("1.34")
        assert performance.metrics["return_on_allocated_capital"] == Decimal("0.0134")
    finally:
        store.close()


def test_portfolio_snapshot_preserves_unmarked_standalone_pnl_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.record_pnl_attribution(
            PnLAttributionRecord(
                attribution_id="manual-adjustment-1",
                source_type="manual_adjustment",
                source_id="manual-adjustment-source-1",
                token_id="token-2",
                strategy_id="maker.v0",
                occurred_at="2026-08-30T12:00:00Z",
                realized_trading_pnl=Decimal("1.25"),
                fee_pnl=Decimal("-0.05"),
                slippage_pnl=Decimal("-0.02"),
                unwind_pnl=Decimal("-0.01"),
                onchain_pnl=Decimal("-0.03"),
                version=VERSION,
            ),
            idempotency_key="pnl:manual-adjustment-1",
        )

        snapshot = PortfolioBook(store).snapshot()

        assert snapshot.trading_net_pnl == Decimal("1.14")
        assert snapshot.realized_net_pnl == Decimal("1.14")
        assert snapshot.positions[0].realized_trading_pnl == Decimal("1.25")
        assert snapshot.positions[0].fee_pnl == Decimal("-0.05")
        assert snapshot.positions[0].slippage_pnl == Decimal("-0.02")
        assert snapshot.positions[0].unwind_pnl == Decimal("-0.01")
        assert snapshot.positions[0].onchain_pnl == Decimal("-0.03")
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
