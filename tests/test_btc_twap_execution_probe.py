from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from src.edge_lab.btc_twap_execution_probe import (
    ExecutionProbeHarness,
    FileProbePersistence,
    InMemoryProbePersistence,
    ProbeCancelReconciliation,
    ProbeEvent,
    ProbeEventKind,
    ProbeExecutionQuote,
    ProbeFinalConfirmation,
    ProbeKillSwitchError,
    ProbeOrder,
    ProbePlan,
    ProbeRouteCandidate,
    ProbeSubmitAck,
    ProbeSubmitResult,
)

D = Decimal


class FakeVenue:
    def __init__(
        self,
        *,
        events: tuple[ProbeEvent, ...] = (),
        maker_ack: ProbeSubmitAck | None = None,
        fok_result: ProbeSubmitResult | None = None,
        fak_result: ProbeSubmitResult | None = None,
        hedge_quote: ProbeExecutionQuote | None = None,
        unwind_quote: ProbeExecutionQuote | None = None,
        reconciliation: ProbeCancelReconciliation | None = None,
        cancel_ok: bool = True,
    ) -> None:
        self.events = list(events)
        self.maker_ack = maker_ack or ProbeSubmitAck.accepted("maker-order-1")
        self.fok_result = fok_result
        self.fak_result = fak_result
        self.hedge_quote = hedge_quote
        self.unwind_quote = unwind_quote
        self.reconciliation = reconciliation
        self.cancel_ok = cancel_ok
        self.auth_reads = 0
        self.stream_reads = 0
        self.maker_orders: list[ProbeOrder] = []
        self.fok_orders: list[ProbeOrder] = []
        self.fak_orders: list[ProbeOrder] = []
        self.cancel_reasons: list[str] = []

    def assert_authenticated_readiness(self) -> None:
        self.auth_reads += 1

    def open_user_event_stream(self, *, plan_id: str) -> tuple[ProbeEvent, ...]:
        self.stream_reads += 1
        return tuple(self.events)

    def submit_post_only_maker(self, order: ProbeOrder) -> ProbeSubmitAck:
        self.maker_orders.append(order)
        return self.maker_ack

    def submit_fok_hedge(self, order: ProbeOrder) -> ProbeSubmitResult:
        self.fok_orders.append(order)
        return self.fok_result or ProbeSubmitResult.filled(
            "fok-order-1",
            filled_quantity=order.quantity,
            executed_notional=order.notional,
            fee=D("0"),
        )

    def submit_fak_unwind(self, order: ProbeOrder) -> ProbeSubmitResult:
        self.fak_orders.append(order)
        return self.fak_result or ProbeSubmitResult.filled(
            "fak-order-1",
            filled_quantity=order.quantity,
            executed_notional=order.notional,
            fee=D("0"),
        )

    def quote_fok_hedge(self, order: ProbeOrder) -> ProbeExecutionQuote:
        if self.hedge_quote is not None:
            return self.hedge_quote.with_quantity(order.quantity)
        return ProbeExecutionQuote(
            order=order,
            full_depth_available=True,
            quoted_notional=order.notional,
            quoted_fee=D("0"),
            source_timestamp_ms=1_000,
            receipt_timestamp_ms=1_010,
            book_sha256="a" * 64,
            fee_rule_sha256="b" * 64,
        )

    def quote_fak_unwind(self, order: ProbeOrder) -> ProbeExecutionQuote:
        if self.unwind_quote is not None:
            return self.unwind_quote.with_quantity(order.quantity)
        return ProbeExecutionQuote(
            order=order,
            full_depth_available=True,
            quoted_notional=order.notional,
            quoted_fee=D("0"),
            source_timestamp_ms=1_000,
            receipt_timestamp_ms=1_010,
            book_sha256="c" * 64,
            fee_rule_sha256="d" * 64,
        )

    def reconcile_after_cancel(
        self, *, plan_id: str, maker_order_id: str
    ) -> ProbeCancelReconciliation:
        if self.reconciliation is not None:
            return self.reconciliation
        fills = tuple(
            event
            for event in self.events
            if event.kind is ProbeEventKind.FILL and event.order_id == maker_order_id
        )
        return ProbeCancelReconciliation(
            maker_order_id=maker_order_id,
            open_order_ids=(),
            fills=fills,
            user_stream_verified=True,
            captured_at_ms=1_020,
            state_sha256="e" * 64,
        )

    def cancel_all(self, *, reason: str) -> bool:
        self.cancel_reasons.append(reason)
        return self.cancel_ok


def order(
    leg_id: str,
    *,
    horizon: str,
    market_id: str,
    token_id: str,
    price: str,
    quantity: str = "2",
) -> ProbeOrder:
    return ProbeOrder(
        leg_id=leg_id,
        horizon=horizon,
        market_id=market_id,
        token_id=token_id,
        side="buy",
        price=D(price),
        quantity=D(quantity),
    )


def plan(*, strategy_kind: str = "structure_floor", balance: str = "12") -> ProbePlan:
    slow = order(
        "leg-15m-up",
        horizon="15m",
        market_id="market-15m-up",
        token_id="token-15m-up",
        price="0.48",
    )
    fast = order(
        "leg-5m-down",
        horizon="5m",
        market_id="market-5m-down",
        token_id="token-5m-down",
        price="0.47",
    )
    return ProbePlan(
        plan_id="probe-plan-1",
        strategy_kind=strategy_kind,
        strike_ordering="five_above_fifteen",
        floor_action="long_15_up_long_5_down",
        co_terminal_validator_sha256="f" * 64,
        common_expiry_id="expiry-1780000000",
        isolated_balance=D(balance),
        route_candidates=(
            ProbeRouteCandidate(
                candidate_id="maker-on-15m",
                common_expiry_id="expiry-1780000000",
                maker_order=slow,
                hedge_order=fast,
                total_cost=D("0.95"),
                hedge_depth_available=True,
            ),
            ProbeRouteCandidate(
                candidate_id="maker-on-5m",
                common_expiry_id="expiry-1780000000",
                maker_order=fast,
                hedge_order=slow,
                total_cost=D("0.98"),
                hedge_depth_available=True,
            ),
        ),
    )


def bind(harness: ExecutionProbeHarness, candidate_plan: ProbePlan) -> None:
    harness.bind_risk_acceptance(
        candidate_plan,
        accepted_plan_hash=candidate_plan.plan_hash,
        accepted_max_loss=D("12"),
    )


def confirmation(
    candidate_plan: ProbePlan, *, now_ms: int = 10_000
) -> ProbeFinalConfirmation:
    return ProbeFinalConfirmation(
        plan_hash=candidate_plan.plan_hash,
        accepted_max_loss=D("12"),
        nonce="fresh-user-confirmation-nonce",
        issued_at_ms=now_ms - 1,
        expires_at_ms=now_ms + 60_000,
        acknowledgement="I ACCEPT ACCOUNT MAX LOSS 12 USDC",
    )


def fill(
    *, fill_id: str, quantity: str, price: str = "0.48", fee: str = "0"
) -> ProbeEvent:
    return ProbeEvent.fill(
        order_id="maker-order-1",
        fill_id=fill_id,
        quantity=quantity,
        price=price,
        fee=fee,
    )


def harness(
    venue: FakeVenue, persistence: object | None = None
) -> ExecutionProbeHarness:
    return ExecutionProbeHarness(
        venue=venue,
        persistence=persistence or InMemoryProbePersistence(),
        now_ms=lambda: 10_000,
    )


def test_zero_mutation_gate_requires_bound_plan_hash_and_final_confirmation() -> None:
    venue = FakeVenue()
    probe = harness(venue)
    candidate_plan = plan()

    with pytest.raises(ValueError, match="risk acceptance"):
        probe.execute(candidate_plan, final_confirmation=confirmation(candidate_plan))
    assert venue.maker_orders == []
    assert venue.fok_orders == []
    assert venue.fak_orders == []
    assert venue.cancel_reasons == []

    bind(probe, candidate_plan)
    assert venue.maker_orders == []
    assert venue.fok_orders == []

    with pytest.raises(ValueError, match="final confirmation"):
        probe.execute(
            candidate_plan,
            final_confirmation=ProbeFinalConfirmation(
                plan_hash=candidate_plan.plan_hash,
                accepted_max_loss=D("12"),
                nonce="fresh-user-confirmation-nonce",
                issued_at_ms=10_001,
                expires_at_ms=20_000,
                acknowledgement="I ACCEPT ACCOUNT MAX LOSS 12 USDC",
            ),
        )
    assert venue.maker_orders == []
    assert venue.cancel_reasons == []


def test_compares_both_maker_directions_without_hardcoding_5m() -> None:
    venue = FakeVenue()
    probe = harness(venue)
    candidate_plan = plan()

    bind(probe, candidate_plan)
    result = probe.execute(
        candidate_plan, final_confirmation=confirmation(candidate_plan)
    )

    assert result.selected_candidate_id == "maker-on-15m"
    assert venue.maker_orders[0].leg_id == "leg-15m-up"
    assert venue.maker_orders[0].horizon == "15m"
    assert venue.fok_orders == []
    assert result.completed_flat is True


def test_partial_and_late_fills_trigger_exactly_one_fok_after_reconcile() -> None:
    venue = FakeVenue(
        events=(
            fill(fill_id="fill-1", quantity="1", price="0.48", fee="0.001"),
            fill(fill_id="fill-1", quantity="1", price="0.48", fee="0.001"),
            fill(fill_id="fill-2", quantity="1", price="0.47", fee="0.001"),
            ProbeEvent.cancelled(order_id="maker-order-1"),
        )
    )
    probe = harness(venue)
    candidate_plan = plan()

    bind(probe, candidate_plan)
    result = probe.execute(
        candidate_plan, final_confirmation=confirmation(candidate_plan)
    )

    assert result.maker_filled_quantity == D("2")
    assert len(venue.fok_orders) == 1
    assert venue.fok_orders[0].quantity == D("2")
    assert venue.cancel_reasons == ["maker_fill_detected"]
    assert result.hedge_submitted is True
    assert result.completed_matched is True
    assert result.completed_flat is False


def test_fok_depth_failure_uses_single_emergency_unwind() -> None:
    venue = FakeVenue(
        events=(
            fill(fill_id="fill-1", quantity="2"),
            ProbeEvent.cancelled(order_id="maker-order-1"),
        ),
        fok_result=ProbeSubmitResult.rejected(
            order_id="fok-order-1",
            reason="depth_shortfall",
        ),
        fak_result=ProbeSubmitResult.filled(
            "fak-order-1",
            filled_quantity=D("2"),
            executed_notional=D("0.96"),
            fee=D("0"),
        ),
    )
    probe = harness(venue)
    candidate_plan = plan()

    bind(probe, candidate_plan)
    result = probe.execute(
        candidate_plan, final_confirmation=confirmation(candidate_plan)
    )

    assert len(venue.fok_orders) == 1
    assert len(venue.fak_orders) == 1
    assert venue.fak_orders[0].leg_id == venue.maker_orders[0].leg_id
    assert venue.fak_orders[0].side == "sell"
    assert venue.fak_orders[0].quantity == D("2")
    assert result.unwind_submitted is True
    assert result.completed_flat is True


def test_cancel_all_failure_leaves_persistent_kill_switch_engaged() -> None:
    persistence = InMemoryProbePersistence()
    venue = FakeVenue(
        events=(fill(fill_id="fill-1", quantity="2"),),
        cancel_ok=False,
    )
    probe = harness(venue, persistence)
    candidate_plan = plan()

    bind(probe, candidate_plan)
    with pytest.raises(ProbeKillSwitchError, match="cancel_all_failed"):
        probe.execute(candidate_plan, final_confirmation=confirmation(candidate_plan))
    assert probe.kill_switch_engaged is True

    restarted = harness(FakeVenue(), persistence)
    assert restarted.kill_switch_engaged is True
    with pytest.raises(ProbeKillSwitchError, match="kill switch"):
        restarted.bind_risk_acceptance(
            candidate_plan,
            accepted_plan_hash=candidate_plan.plan_hash,
            accepted_max_loss=D("12"),
        )


def test_limits_and_second_submit_are_rejected_without_extra_mutation() -> None:
    venue = FakeVenue()
    too_large = plan(balance="13")
    probe = harness(venue)

    with pytest.raises(ValueError, match="isolated balance"):
        probe.bind_risk_acceptance(
            too_large,
            accepted_plan_hash=too_large.plan_hash,
            accepted_max_loss=D("12"),
        )
    assert venue.maker_orders == []

    candidate_plan = plan()
    bind(probe, candidate_plan)
    probe.execute(candidate_plan, final_confirmation=confirmation(candidate_plan))
    with pytest.raises(ValueError, match="already submitted"):
        probe.execute(candidate_plan, final_confirmation=confirmation(candidate_plan))
    assert len(venue.maker_orders) == 1


def test_disconnect_kills_and_restart_stays_killed() -> None:
    persistence = InMemoryProbePersistence()
    venue = FakeVenue(
        events=(ProbeEvent(kind=ProbeEventKind.DISCONNECT, reason="ws_drop"),)
    )
    candidate_plan = plan()
    probe = harness(venue, persistence)
    bind(probe, candidate_plan)

    with pytest.raises(ProbeKillSwitchError, match="disconnect"):
        probe.execute(candidate_plan, final_confirmation=confirmation(candidate_plan))

    restarted = harness(FakeVenue(), persistence)
    with pytest.raises(ProbeKillSwitchError, match="kill switch"):
        restarted.execute(
            candidate_plan, final_confirmation=confirmation(candidate_plan)
        )


def test_predictive_routes_are_rejected_before_any_venue_mutation() -> None:
    venue = FakeVenue()
    probe = harness(venue)
    predictive_plan = plan(strategy_kind="split_probability")

    with pytest.raises(ValueError, match="structure_floor"):
        probe.bind_risk_acceptance(
            predictive_plan,
            accepted_plan_hash=predictive_plan.plan_hash,
            accepted_max_loss=D("12"),
        )
    assert venue.maker_orders == []
    assert venue.cancel_reasons == []


def test_actual_fill_and_current_full_depth_must_remain_below_point_99() -> None:
    candidate_plan = plan()
    hedge_template = candidate_plan.route_candidates[0].hedge_order
    expensive_hedge = ProbeExecutionQuote(
        order=ProbeOrder(
            **{
                **hedge_template.__dict__,
                "price": D("0.52"),
                "quantity": D("2"),
            }
        ),
        full_depth_available=True,
        quoted_notional=D("1.04"),
        quoted_fee=D("0.01"),
        source_timestamp_ms=1_000,
        receipt_timestamp_ms=1_010,
        book_sha256="1" * 64,
        fee_rule_sha256="2" * 64,
    )
    venue = FakeVenue(
        events=(fill(fill_id="fill-1", quantity="2", price="0.48", fee="0.01"),),
        hedge_quote=expensive_hedge,
    )
    probe = harness(venue)
    bind(probe, candidate_plan)

    result = probe.execute(
        candidate_plan, final_confirmation=confirmation(candidate_plan)
    )

    assert venue.fok_orders == []
    assert len(venue.fak_orders) == 1
    assert venue.fak_orders[0].side == "sell"
    assert result.unwind_submitted is True


def test_overfill_or_foreign_user_event_kills_before_hedge() -> None:
    candidate_plan = plan()
    overfill_venue = FakeVenue(events=(fill(fill_id="overfill", quantity="3"),))
    overfill_probe = harness(overfill_venue)
    bind(overfill_probe, candidate_plan)

    with pytest.raises(ProbeKillSwitchError, match="maker_overfill"):
        overfill_probe.execute(
            candidate_plan, final_confirmation=confirmation(candidate_plan)
        )
    assert overfill_venue.fok_orders == []

    foreign_venue = FakeVenue(
        events=(
            ProbeEvent.fill(
                order_id="unexpected-order",
                fill_id="foreign-fill",
                quantity="1",
                price="0.4",
                fee="0",
            ),
        )
    )
    foreign_probe = harness(foreign_venue)
    bind(foreign_probe, candidate_plan)
    with pytest.raises(ProbeKillSwitchError, match="unexpected_foreign_order_event"):
        foreign_probe.execute(
            candidate_plan, final_confirmation=confirmation(candidate_plan)
        )


def test_reconciliation_is_authoritative_and_open_order_fails_closed() -> None:
    candidate_plan = plan()
    reconciliation = ProbeCancelReconciliation(
        maker_order_id="maker-order-1",
        open_order_ids=("maker-order-1",),
        fills=(),
        user_stream_verified=True,
        captured_at_ms=1_020,
        state_sha256="3" * 64,
    )
    venue = FakeVenue(reconciliation=reconciliation)
    probe = harness(venue)
    bind(probe, candidate_plan)

    with pytest.raises(ProbeKillSwitchError, match="maker_order_still_open"):
        probe.execute(candidate_plan, final_confirmation=confirmation(candidate_plan))


def test_file_persistence_detects_snapshot_tampering_and_confirmation_is_redacted(
    tmp_path: Path,
) -> None:
    persistence = FileProbePersistence(tmp_path / "probe-state.json")
    venue = FakeVenue()
    candidate_plan = plan()
    probe = harness(venue, persistence)
    bind(probe, candidate_plan)
    probe.execute(candidate_plan, final_confirmation=confirmation(candidate_plan))

    raw = (tmp_path / "probe-state.json").read_text(encoding="utf-8")
    assert "fresh-user-confirmation-nonce" not in raw
    assert "I ACCEPT ACCOUNT MAX LOSS" not in raw

    document = __import__("json").loads(raw)
    document["snapshot"]["kill_switch_engaged"] = True
    (tmp_path / "probe-state.json").write_text(
        __import__("json").dumps(document), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="snapshot hash"):
        FileProbePersistence(tmp_path / "probe-state.json").load()


def test_stale_or_replayed_final_confirmation_is_rejected_before_mutation() -> None:
    venue = FakeVenue()
    candidate_plan = plan()
    probe = harness(venue)
    bind(probe, candidate_plan)
    stale = ProbeFinalConfirmation(
        plan_hash=candidate_plan.plan_hash,
        accepted_max_loss=D("12"),
        nonce="fresh-user-confirmation-nonce",
        issued_at_ms=1,
        expires_at_ms=2,
        acknowledgement="I ACCEPT ACCOUNT MAX LOSS 12 USDC",
    )

    with pytest.raises(ValueError, match="final confirmation"):
        probe.execute(candidate_plan, final_confirmation=stale)
    assert venue.maker_orders == []


def test_restart_with_unreconciled_submission_automatically_kills_and_cancels() -> None:
    class CrashingVenue(FakeVenue):
        def assert_authenticated_readiness(self) -> None:
            raise KeyboardInterrupt("simulated process death")

    persistence = InMemoryProbePersistence()
    candidate_plan = plan()
    probe = harness(CrashingVenue(), persistence)
    bind(probe, candidate_plan)

    with pytest.raises(KeyboardInterrupt, match="process death"):
        probe.execute(candidate_plan, final_confirmation=confirmation(candidate_plan))

    recovery_venue = FakeVenue()
    restarted = harness(recovery_venue, persistence)
    assert restarted.kill_switch_engaged is True
    assert recovery_venue.cancel_reasons == [
        "kill:restart_with_unreconciled_submission"
    ]
    with pytest.raises(ProbeKillSwitchError, match="restart_with_unreconciled"):
        restarted.bind_risk_acceptance(
            candidate_plan,
            accepted_plan_hash=candidate_plan.plan_hash,
            accepted_max_loss=D("12"),
        )
