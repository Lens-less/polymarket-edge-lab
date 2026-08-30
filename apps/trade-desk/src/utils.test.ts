import { buildDeskUrl, getRuntimeConfig } from "./api";
import { formatAmount, isConfirmLocked, modeClass, summaryPills } from "./utils";
import type { DeskSnapshot } from "./types";

function snapshot(overrides: Partial<DeskSnapshot["status_bar"]> = {}, planState = "REVIEWED"): DeskSnapshot {
  return {
    schema_version: "profit-system.v0.2",
    snapshot_version: 1,
    generated_at: "2026-08-30T00:00:00Z",
    scenario: "paper",
    session: "default",
    csrf_token: "token",
    selected_opportunity_id: "opp",
    action_log: [],
    status_bar: {
      mode: "PAPER",
      available_cash: "10.000000",
      current_drawdown: "-1.000000",
      risk_budget_used: "0.240000",
      is_live_locked: false,
      kill_switch_engaged: false,
      mode_banner: "paper",
      realized_net_pnl: {
        today: "1.0",
        seven_day: "2.0",
        thirty_day: "3.0"
      },
      connections: {
        data: "live",
        orders: "live",
        market_data_age_ms: 100,
        blocked_reason: null
      },
      ...overrides
    },
    opportunities: [],
    explanation: {
      opportunity_id: "opp",
      market_title: "m",
      settlement: "s",
      reason_summary: [],
      ladder: [],
      recent_trades: [],
      related_markets: [],
      cost_breakdown: [],
      scenarios: [],
      executable_depth: []
    },
    execution_plan: {
      id: "plan",
      opportunity_id: "opp",
      state: planState,
      review_required: true,
      live_lock_reason: null,
      estimated_fee: "1",
      estimated_slippage: "1",
      expected_net_profit: "1",
      worst_case_loss: "1",
      risk_budget_change: "1",
      execution_notes: [],
      failure_path: "fp",
      legs: []
    },
    orders: [],
    fills: [],
    positions: [],
    strategy_pnl: [],
    expected_vs_realized: [],
    reconciliation: {
      status: "clean",
      last_run_at: "2026-08-30T00:00:00Z",
      summary: "",
      differences: []
    }
  };
}

describe("trade desk helpers", () => {
  it("keeps amounts server-authored", () => {
    expect(formatAmount("12.450000")).toBe("$12.450000");
  });

  it("builds same-session runtime urls", () => {
    const runtime = getRuntimeConfig("?scenario=fake-live-lock&session=test-a");
    window.history.replaceState({}, "", "/");
    expect(buildDeskUrl("/api/v0.2/desk/snapshot", runtime)).toBe(
      "http://localhost:3000/api/v0.2/desk/snapshot?scenario=fake-live-lock&session=test-a"
    );
  });

  it("flags confirm lock on live health regressions", () => {
    expect(isConfirmLocked(snapshot())).toBe(false);
    expect(isConfirmLocked(snapshot({ is_live_locked: true }))).toBe(true);
    expect(isConfirmLocked(snapshot({ connections: { data: "stale", orders: "live", market_data_age_ms: 9000, blocked_reason: "stale" } }))).toBe(true);
    expect(isConfirmLocked(snapshot({}, "DRAFT"))).toBe(true);
  });

  it("maps modes to visual classes", () => {
    expect(modeClass("LIVE_CANARY")).toBe("mode-live-canary");
  });

  it("does not fabricate market-data freshness when no live feed is attached", () => {
    const locked = snapshot({
      connections: {
        data: "disconnected",
        orders: "blocked",
        market_data_age_ms: null,
        blocked_reason: "not attached"
      }
    });
    expect(summaryPills(locked)).toContain("Age unavailable");
  });
});
