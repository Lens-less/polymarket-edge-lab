from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.profit_system.acceptance import (
    build_fixed_paper_venue,
    build_fixed_replay_venue,
    build_track_a_fixture,
    build_track_b_fixture,
)
from src.profit_system.execution import ExecutionMode, VenueEventKind, VenueUserEvent
from src.profit_system.opportunities import OpportunityEngine
from src.profit_system.orchestrator import ProfitPipelineOrchestrator
from src.profit_system.persistence import PersistenceStore
from src.profit_system.runtime import StrategyRuntime

pytestmark = pytest.mark.asyncio


async def test_execution_stage_persists_journal_and_realized_net_after_explicit_costs(
    tmp_path: Path,
) -> None:
    fixture = build_track_a_fixture()
    assert fixture.replay_outcome is not None
    store = PersistenceStore(tmp_path / "orchestrator-track-a.db")
    orchestrator = ProfitPipelineOrchestrator(
        config=fixture.config,
        strategy=fixture.strategy,
        store=store,
        opportunity_engine=OpportunityEngine(),
        runtime=StrategyRuntime(fixture.strategy),
        now=lambda: fixture.snapshot.captured_at + timedelta(seconds=10),
    )
    try:
        result = await orchestrator.run_execution_stage(
            snapshot=fixture.snapshot,
            portfolio=fixture.portfolio,
            mode=ExecutionMode.REPLAY,
            venue=build_fixed_replay_venue(fixture),
            phase="replay",
            actor="pytest",
            realized_outcome=fixture.replay_outcome,
        )

        assert result.decision.executable is True
        assert result.ticket is not None
        assert result.reconciliation is not None
        assert result.ledger_snapshot is not None
        assert result.performance is not None
        assert result.ledger_snapshot.realized_net_pnl == Decimal("1.43")
        assert result.ledger_snapshot.realized_net_pnl == fixture.replay_outcome.realized_net_pnl
        assert result.ledger_snapshot.realized_net_pnl != result.decision.expected_net_edge
        assert result.feedback is not None
        assert result.feedback.realized_pnl == Decimal("1.43")
        assert len(store.list_execution_plans()) == 1
        assert len(store.list_orders()) == 2
        assert len(store.list_fills()) == 2
        assert len(store.list_pnl_attribution()) == 1
        assert len(store.list_reconciliation_runs()) == 1
        assert store.journal_count() >= 8
    finally:
        store.close()


async def test_execution_stage_persists_submitting_orders_before_adapter_submit(
    tmp_path: Path,
) -> None:
    fixture = build_track_a_fixture()
    store = PersistenceStore(tmp_path / "orchestrator-submit-journal.db")
    orchestrator = ProfitPipelineOrchestrator(
        config=fixture.config,
        strategy=fixture.strategy,
        store=store,
        opportunity_engine=OpportunityEngine(),
        runtime=StrategyRuntime(fixture.strategy),
        now=lambda: fixture.snapshot.captured_at + timedelta(seconds=10),
    )

    class JournalAwareVenue:
        def __init__(self) -> None:
            self.base = build_fixed_replay_venue(fixture)
            self.submit_calls = 0
            self.states_seen_at_submit: tuple[str, ...] = ()

        async def preflight(self, request: object) -> object:
            return await self.base.preflight(request)

        async def submit(
            self,
            *,
            orders: Sequence[object],
            mode: object,
            idempotency_key: str,
        ) -> object:
            del mode, idempotency_key
            self.submit_calls += 1
            persisted_orders = store.list_orders()
            self.states_seen_at_submit = tuple(order.lifecycle_state for order in persisted_orders)
            assert len(persisted_orders) == len(orders)
            assert self.states_seen_at_submit
            assert set(self.states_seen_at_submit) == {"submitting"}
            return await self.base.submit(
                orders=orders,
                mode=ExecutionMode.REPLAY,
                idempotency_key="journal-aware-submit",
            )

        async def cancel(
            self,
            *,
            order_ids: Sequence[str],
            idempotency_key: str,
            reason: str | None = None,
        ) -> object:
            return await self.base.cancel(
                order_ids=order_ids,
                idempotency_key=idempotency_key,
                reason=reason,
            )

        async def reconcile(
            self,
            *,
            open_order_ids: Sequence[str] | None = None,
            since_trade_id: str | None = None,
        ) -> object:
            return await self.base.reconcile(
                open_order_ids=open_order_ids,
                since_trade_id=since_trade_id,
            )

        def stream_user_events(
            self,
            *,
            markets: Sequence[str] | None = None,
            since_cursor: str | None = None,
        ) -> AsyncIterator[VenueUserEvent]:
            return self.base.stream_user_events(markets=markets, since_cursor=since_cursor)

    venue = JournalAwareVenue()
    try:
        result = await orchestrator.run_execution_stage(
            snapshot=fixture.snapshot,
            portfolio=fixture.portfolio,
            mode=ExecutionMode.REPLAY,
            venue=venue,
            phase="replay",
            actor="pytest",
            realized_outcome=fixture.replay_outcome,
        )

        assert result.ticket is not None
        assert venue.submit_calls == 1
        assert venue.states_seen_at_submit
        assert set(venue.states_seen_at_submit) == {"submitting"}
        assert {order.lifecycle_state for order in store.list_orders()} == {"filled"}
    finally:
        store.close()


async def test_shadow_uses_identical_strategy_logic_without_submitting_orders(
    tmp_path: Path,
) -> None:
    fixture = build_track_a_fixture()
    store = PersistenceStore(tmp_path / "orchestrator-shadow.db")
    orchestrator = ProfitPipelineOrchestrator(
        config=fixture.config,
        strategy=fixture.strategy,
        store=store,
        opportunity_engine=OpportunityEngine(),
        runtime=StrategyRuntime(fixture.strategy),
        now=lambda: fixture.snapshot.captured_at + timedelta(seconds=10),
    )

    class SubmitProbe:
        def __init__(self) -> None:
            self.submit_calls = 0

        async def preflight(self, request: object) -> object:
            del request
            raise AssertionError("shadow should not preflight")

        async def submit(
            self,
            *,
            orders: Sequence[object],
            mode: object,
            idempotency_key: str,
        ) -> object:
            del orders, mode, idempotency_key
            self.submit_calls += 1
            raise AssertionError("shadow should never submit")

        async def cancel(
            self,
            *,
            order_ids: Sequence[str],
            idempotency_key: str,
            reason: str | None = None,
        ) -> object:
            del order_ids, idempotency_key, reason
            raise AssertionError("shadow should never cancel")

        async def reconcile(
            self,
            *,
            open_order_ids: Sequence[str] | None = None,
            since_trade_id: str | None = None,
        ) -> object:
            del open_order_ids, since_trade_id
            raise AssertionError("shadow should never reconcile")

        async def stream_user_events(
            self,
            *,
            markets: Sequence[str] | None = None,
            since_cursor: str | None = None,
        ) -> AsyncIterator[VenueUserEvent]:
            del markets
            if since_cursor == "__shadow_fixture__":
                yield VenueUserEvent(
                    kind=VenueEventKind.STATUS,
                    occurred_at=fixture.snapshot.captured_at,
                    cursor="shadow-fixture-sentinel",
                    detail="unused sentinel",
                )

    probe = SubmitProbe()
    try:
        replay = await orchestrator.run_execution_stage(
            snapshot=fixture.snapshot,
            portfolio=fixture.portfolio,
            mode=ExecutionMode.REPLAY,
            venue=build_fixed_replay_venue(fixture),
            phase="replay",
            actor="pytest",
            realized_outcome=fixture.replay_outcome,
        )
        paper = await orchestrator.run_execution_stage(
            snapshot=fixture.snapshot,
            portfolio=fixture.portfolio,
            mode=ExecutionMode.PAPER,
            venue=build_fixed_paper_venue(),
            phase="paper",
            actor="pytest",
            realized_outcome=fixture.paper_outcome,
        )
        shadow = await orchestrator.run_shadow_stage(
            snapshot=fixture.snapshot,
            portfolio=fixture.portfolio,
            phase="shadow",
            submit_probe=probe,
        )

        assert replay.signal_signature == paper.signal_signature == shadow.signal_signature
        assert shadow.execution_mode is None
        assert probe.submit_calls == 0
        assert len(store.list_execution_plans()) == 2
        assert len(store.list_orders()) >= 2
    finally:
        store.close()


async def test_monitor_cycle_consumes_user_events_and_rebuilds_authoritative_view(
    tmp_path: Path,
) -> None:
    fixture = build_track_b_fixture()
    store = PersistenceStore(tmp_path / "orchestrator-monitor.db")
    orchestrator = ProfitPipelineOrchestrator(
        config=fixture.config,
        strategy=fixture.strategy,
        store=store,
        opportunity_engine=OpportunityEngine(),
        runtime=StrategyRuntime(fixture.strategy),
        now=lambda: fixture.snapshot.captured_at + timedelta(seconds=12),
    )
    venue = build_fixed_paper_venue()
    try:
        session = await orchestrator.run_execution_stage(
            snapshot=fixture.snapshot,
            portfolio=fixture.portfolio,
            mode=ExecutionMode.PAPER,
            venue=venue,
            phase="paper",
            actor="pytest",
            realized_outcome=None,
        )
        monitored = await orchestrator.run_monitor_cycle(
            session=session,
            venue=venue,
            phase="paper",
            actor="pytest-monitor",
            event_limit=6,
        )

        snapshot_state = store.get_system_state("user_stream_snapshot")
        assert monitored.execution_snapshot is not None
        assert monitored.reconciliation is not None
        assert monitored.ledger_snapshot is not None
        assert snapshot_state["consumed_events"]
        assert monitored.execution_snapshot.last_user_event_at is not None
        assert len(store.list_reconciliation_runs()) >= 2
        assert len(store.list_orders()) == 2
        assert monitored.ledger_snapshot.open_orders
    finally:
        store.close()
