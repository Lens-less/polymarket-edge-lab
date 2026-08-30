from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from secrets import token_urlsafe

from .models import (
    SCHEMA_VERSION,
    ConnectionState,
    CostItem,
    DepthItem,
    DeskConnectionStatus,
    DeskMode,
    DeskSnapshot,
    DeskStatusBar,
    ExecutionPlanSnapshot,
    ExpectedVsRealizedSnapshot,
    ExplanationSnapshot,
    FillSnapshot,
    LadderLevel,
    OpportunityState,
    OpportunitySummary,
    OrderSnapshot,
    OrderState,
    PlanLeg,
    PlanState,
    PnlWindow,
    PositionSnapshot,
    RecentTrade,
    ReconciliationDiff,
    ReconciliationSnapshot,
    RelatedMarket,
    ScenarioItem,
    StrategyPnlSnapshot,
    isoformat,
    utc_now,
)


def build_demo_snapshot(*, scenario: str, session: str, snapshot_version: int = 1) -> DeskSnapshot:
    now = utc_now()
    live_lock = scenario == "fake-live-lock"
    shadow_mode = scenario == "shadow"
    mode = DeskMode.LIVE_CANARY if live_lock else DeskMode.SHADOW if shadow_mode else DeskMode.PAPER
    data_state = ConnectionState.DISCONNECTED if live_lock else ConnectionState.LIVE
    order_state = ConnectionState.BLOCKED if live_lock or shadow_mode else ConnectionState.LIVE
    plan_state = PlanState.DRAFT
    opportunity_state = OpportunityState.REVIEW if live_lock else OpportunityState.EXECUTABLE
    if live_lock:
        blocked_reason = (
            "Live lock engaged until data feed, order stream, and reconciliation are healthy."
        )
        mode_banner = (
            "LIVE_CANARY locked: confirm disabled because feeds are stale and "
            "the user stream is blocked."
        )
    elif shadow_mode:
        blocked_reason = "shadow mode never submits venue orders"
        mode_banner = (
            "SHADOW observational only: review is recorded, but confirm and venue submission "
            "remain disabled."
        )
    else:
        blocked_reason = None
        mode_banner = "PAPER mode: deterministic fills and authoritative server-side PnL."
    primary_opportunity_id = "opp-btc-mean-revert"
    generated_trades = [
        RecentTrade(
            time=isoformat(now - timedelta(minutes=4)),
            side="BUY",
            price=Decimal("0.47"),
            size=Decimal("42.000000"),
        ),
        RecentTrade(
            time=isoformat(now - timedelta(minutes=2)),
            side="SELL",
            price=Decimal("0.49"),
            size=Decimal("37.000000"),
        ),
        RecentTrade(
            time=isoformat(now - timedelta(seconds=45)),
            side="BUY",
            price=Decimal("0.48"),
            size=Decimal("18.000000"),
        ),
    ]
    opportunities = [
        OpportunitySummary(
            id=primary_opportunity_id,
            rank=1,
            strategy="maker-basis",
            market="BTC > 110k by Friday",
            tradable_edge=Decimal("0.032500"),
            expected_net_profit=Decimal("14.250000"),
            max_loss=Decimal("38.000000"),
            expected_capacity=Decimal("120.000000"),
            confidence=Decimal("0.81"),
            ttl_seconds=95,
            state=opportunity_state,
            note="Tight spread plus reward uplift keeps adverse selection below fee budget.",
        ),
        OpportunitySummary(
            id="opp-eth-carry",
            rank=2,
            strategy="carry-arb",
            market="ETH monthly carry spread",
            tradable_edge=Decimal("0.019000"),
            expected_net_profit=Decimal("8.600000"),
            max_loss=Decimal("26.000000"),
            expected_capacity=Decimal("88.000000"),
            confidence=Decimal("0.72"),
            ttl_seconds=240,
            state=OpportunityState.WATCH,
            note="Waiting on depth at the inside ask before plan promotion.",
        ),
    ]
    explanation = ExplanationSnapshot(
        opportunity_id=primary_opportunity_id,
        market_title="BTC > 110k by Friday",
        settlement="Expires 2026-09-04T15:00:00Z",
        ladder=[
            LadderLevel(side="bid", price=Decimal("0.47"), size=Decimal("140.000000")),
            LadderLevel(side="bid", price=Decimal("0.46"), size=Decimal("180.000000")),
            LadderLevel(side="ask", price=Decimal("0.48"), size=Decimal("132.000000")),
            LadderLevel(side="ask", price=Decimal("0.49"), size=Decimal("174.000000")),
        ],
        recent_trades=generated_trades,
        related_markets=[
            RelatedMarket(label="BTC weekly range", relationship="hedge", mark=Decimal("0.41")),
            RelatedMarket(label="BTC momentum basket", relationship="signal", mark=Decimal("0.66")),
        ],
        reason_summary=[
            "Reward-adjusted maker edge remains positive after fees and two-tick slippage.",
            (
                "User stream drift is zero in paper mode, so fills stay "
                "attributable to the active plan."
            ),
            "Capacity comes from three visible levels and one modeled reserve queue release.",
        ],
        cost_breakdown=[
            CostItem(label="Trading fees", amount=Decimal("1.150000"), note="Venue fee schedule"),
            CostItem(label="Expected slippage", amount=Decimal("0.650000"), note="Two-tick stress"),
            CostItem(
                label="Adverse selection buffer", amount=Decimal("0.900000"), note="Markout haircut"
            ),
        ],
        scenarios=[
            ScenarioItem(
                name="Optimistic",
                net_profit=Decimal("18.400000"),
                worst_case_loss=Decimal("34.000000"),
                confidence=Decimal("0.29"),
            ),
            ScenarioItem(
                name="Baseline",
                net_profit=Decimal("14.250000"),
                worst_case_loss=Decimal("38.000000"),
                confidence=Decimal("0.52"),
            ),
            ScenarioItem(
                name="Pessimistic",
                net_profit=Decimal("6.900000"),
                worst_case_loss=Decimal("41.500000"),
                confidence=Decimal("0.19"),
            ),
        ],
        executable_depth=[
            DepthItem(
                level="Inside ask", price=Decimal("0.48"), executable_size=Decimal("44.000000")
            ),
            DepthItem(level="+1 tick", price=Decimal("0.49"), executable_size=Decimal("38.000000")),
            DepthItem(
                level="Reserve queue", price=Decimal("0.50"), executable_size=Decimal("28.000000")
            ),
        ],
    )
    execution_plan = ExecutionPlanSnapshot(
        id="plan-btc-maker-001",
        opportunity_id=primary_opportunity_id,
        state=plan_state,
        review_required=True,
        live_lock_reason=blocked_reason,
        estimated_fee=Decimal("1.150000"),
        estimated_slippage=Decimal("0.650000"),
        expected_net_profit=Decimal("14.250000"),
        worst_case_loss=Decimal("38.000000"),
        risk_budget_change=Decimal("0.040000"),
        execution_notes=[
            "Post first passive order, then monitor for taker sweep before replacement.",
            "If only one leg rests live beyond 8s, cancel and reconcile immediately.",
        ],
        failure_path=(
            "Single-leg exposure over 8s routes to cancel + reconcile and blocks new confirms."
        ),
        legs=[
            PlanLeg(
                id="leg-1",
                venue="paper-book",
                side="BUY",
                instrument="BTC weekly YES",
                price=Decimal("0.48"),
                size=Decimal("44.000000"),
                order_type="POST_ONLY_LIMIT",
                post_only=True,
            ),
            PlanLeg(
                id="leg-2",
                venue="paper-book",
                side="SELL",
                instrument="BTC weekly partial hedge",
                price=Decimal("0.56"),
                size=Decimal("18.000000"),
                order_type="GTD_LIMIT",
                post_only=False,
            ),
        ],
    )
    initial_orders = [
        OrderSnapshot(
            id="ord-prev-1",
            plan_id="plan-prev-1",
            market="ETH monthly carry spread",
            state=OrderState.FILLED,
            side="BUY",
            price=Decimal("0.43"),
            size=Decimal("16.000000"),
            filled_size=Decimal("16.000000"),
            venue_status="filled",
            updated_at=isoformat(now - timedelta(hours=2)),
        ),
        OrderSnapshot(
            id="ord-prev-live",
            plan_id="plan-prev-2",
            market="BTC weekly YES",
            state=OrderState.LIVE,
            side="SELL",
            price=Decimal("0.54"),
            size=Decimal("12.000000"),
            filled_size=Decimal("0.000000"),
            venue_status="resting",
            updated_at=isoformat(now - timedelta(minutes=11)),
        ),
    ]
    fills = [
        FillSnapshot(
            id="fill-prev-1",
            order_id="ord-prev-1",
            market="ETH monthly carry spread",
            side="BUY",
            price=Decimal("0.43"),
            size=Decimal("16.000000"),
            fee=Decimal("0.210000"),
            realized_net_pnl=Decimal("3.420000"),
            occurred_at=isoformat(now - timedelta(hours=2)),
        )
    ]
    positions = [
        PositionSnapshot(
            id="pos-btc-yes",
            market="BTC weekly YES",
            side="LONG",
            quantity=Decimal("16.000000"),
            avg_price=Decimal("0.44"),
            mark_price=Decimal("0.48"),
            realized_net_pnl=Decimal("4.350000"),
            unrealized_net_pnl=Decimal("0.640000"),
        ),
        PositionSnapshot(
            id="pos-eth-carry",
            market="ETH monthly carry spread",
            side="LONG",
            quantity=Decimal("8.000000"),
            avg_price=Decimal("0.39"),
            mark_price=Decimal("0.42"),
            realized_net_pnl=Decimal("2.100000"),
            unrealized_net_pnl=Decimal("0.240000"),
        ),
    ]
    strategy_pnl = [
        StrategyPnlSnapshot(
            strategy="maker-basis",
            realized_today=Decimal("12.450000"),
            realized_seven_day=Decimal("38.100000"),
            realized_thirty_day=Decimal("77.400000"),
            mark_to_market=Decimal("4.060000"),
        ),
        StrategyPnlSnapshot(
            strategy="carry-arb",
            realized_today=Decimal("5.200000"),
            realized_seven_day=Decimal("16.800000"),
            realized_thirty_day=Decimal("32.400000"),
            mark_to_market=Decimal("1.340000"),
        ),
    ]
    expected_vs_realized = [
        ExpectedVsRealizedSnapshot(
            strategy="maker-basis",
            expected_net_pnl=Decimal("14.250000"),
            realized_net_pnl=Decimal("12.450000"),
            variance=Decimal("-1.800000"),
        ),
        ExpectedVsRealizedSnapshot(
            strategy="carry-arb",
            expected_net_pnl=Decimal("9.100000"),
            realized_net_pnl=Decimal("8.300000"),
            variance=Decimal("-0.800000"),
        ),
    ]
    if shadow_mode:
        initial_orders = []
        fills = []
        positions = []
        strategy_pnl = [
            replace(
                item,
                realized_today=Decimal("0"),
                realized_seven_day=Decimal("0"),
                realized_thirty_day=Decimal("0"),
                mark_to_market=Decimal("0"),
            )
            for item in strategy_pnl
        ]
        expected_vs_realized = [
            replace(
                item,
                realized_net_pnl=Decimal("0"),
                variance=-item.expected_net_pnl,
            )
            for item in expected_vs_realized
        ]
    reconciliation = ReconciliationSnapshot(
        status="attention" if live_lock else "not_applicable" if shadow_mode else "clean",
        last_run_at=isoformat(now - timedelta(minutes=1 if live_lock else 6)),
        summary=(
            "Waiting for authenticated user stream recovery before live confirms."
            if live_lock
            else (
                "Shadow is observational only and creates no venue orders to reconcile."
                if shadow_mode
                else (
                    "Authoritative venue and local journal agree on balances, fills, and "
                    "open orders."
                )
            )
        ),
        differences=(
            [
                ReconciliationDiff(
                    scope="user-stream",
                    severity="high",
                    message="Gap exceeds configured maximum; confirm remains locked.",
                ),
                ReconciliationDiff(
                    scope="market-data",
                    severity="high",
                    message="Top-of-book is stale beyond max_market_data_age_ms.",
                ),
            ]
            if live_lock
            else []
        ),
    )
    return DeskSnapshot(
        schema_version=SCHEMA_VERSION,
        snapshot_version=snapshot_version,
        generated_at=now,
        scenario=scenario,
        session=session,
        status_bar=DeskStatusBar(
            mode=mode,
            available_cash=Decimal("1250.000000"),
            realized_net_pnl=PnlWindow(
                today=Decimal("0") if shadow_mode else Decimal("12.450000"),
                seven_day=Decimal("0") if shadow_mode else Decimal("38.100000"),
                thirty_day=Decimal("0") if shadow_mode else Decimal("77.400000"),
            ),
            current_drawdown=Decimal("0") if shadow_mode else Decimal("-3.250000"),
            risk_budget_used=Decimal("0") if shadow_mode else Decimal("0.240000"),
            connections=DeskConnectionStatus(
                data=data_state,
                orders=order_state,
                market_data_age_ms=8200 if live_lock else 320,
                blocked_reason=blocked_reason,
            ),
            is_live_locked=live_lock,
            kill_switch_engaged=False,
            mode_banner=mode_banner,
        ),
        selected_opportunity_id=primary_opportunity_id,
        opportunities=opportunities,
        explanation=explanation,
        execution_plan=execution_plan,
        orders=initial_orders,
        fills=fills,
        positions=positions,
        strategy_pnl=strategy_pnl,
        expected_vs_realized=expected_vs_realized,
        reconciliation=reconciliation,
        csrf_token=token_urlsafe(32),
        action_log=["snapshot-seeded"],
    )
