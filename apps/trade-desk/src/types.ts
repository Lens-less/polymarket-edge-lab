export type AmountString = string;

export interface DeskSnapshot {
  schema_version: string;
  snapshot_version: number;
  generated_at: string;
  scenario: string;
  session: string;
  csrf_token: string;
  selected_opportunity_id: string;
  action_log: string[];
  status_bar: {
    mode: string;
    available_cash: AmountString;
    current_drawdown: AmountString;
    risk_budget_used: AmountString;
    is_live_locked: boolean;
    kill_switch_engaged: boolean;
    mode_banner: string;
    realized_net_pnl: {
      today: AmountString;
      seven_day: AmountString;
      thirty_day: AmountString;
    };
    connections: {
      data: string;
      orders: string;
      market_data_age_ms: number | null;
      blocked_reason: string | null;
    };
  };
  opportunities: Array<{
    id: string;
    rank: number;
    strategy: string;
    market: string;
    tradable_edge: AmountString;
    expected_net_profit: AmountString;
    max_loss: AmountString;
    expected_capacity: AmountString;
    confidence: AmountString;
    ttl_seconds: number;
    state: string;
    note: string;
  }>;
  explanation: {
    opportunity_id: string;
    market_title: string;
    settlement: string;
    reason_summary: string[];
    ladder: Array<{ side: string; price: AmountString; size: AmountString }>;
    recent_trades: Array<{ time: string; side: string; price: AmountString; size: AmountString }>;
    related_markets: Array<{ label: string; relationship: string; mark: AmountString }>;
    cost_breakdown: Array<{ label: string; amount: AmountString; note: string }>;
    scenarios: Array<{ name: string; net_profit: AmountString; worst_case_loss: AmountString; confidence: AmountString }>;
    executable_depth: Array<{ level: string; price: AmountString; executable_size: AmountString }>;
  };
  execution_plan: {
    id: string;
    opportunity_id: string;
    state: string;
    review_required: boolean;
    live_lock_reason: string | null;
    estimated_fee: AmountString;
    estimated_slippage: AmountString;
    expected_net_profit: AmountString;
    worst_case_loss: AmountString;
    risk_budget_change: AmountString;
    execution_notes: string[];
    failure_path: string;
    legs: Array<{
      id: string;
      venue: string;
      side: string;
      instrument: string;
      price: AmountString;
      size: AmountString;
      order_type: string;
      post_only: boolean;
    }>;
  };
  orders: Array<{
    id: string;
    plan_id: string;
    market: string;
    state: string;
    side: string;
    price: AmountString;
    size: AmountString;
    filled_size: AmountString;
    venue_status: string;
    updated_at: string;
  }>;
  fills: Array<{
    id: string;
    order_id: string;
    market: string;
    side: string;
    price: AmountString;
    size: AmountString;
    fee: AmountString;
    realized_net_pnl: AmountString;
    occurred_at: string;
  }>;
  positions: Array<{
    id: string;
    market: string;
    side: string;
    quantity: AmountString;
    avg_price: AmountString;
    mark_price: AmountString;
    realized_net_pnl: AmountString;
    unrealized_net_pnl: AmountString;
  }>;
  strategy_pnl: Array<{
    strategy: string;
    realized_today: AmountString;
    realized_seven_day: AmountString;
    realized_thirty_day: AmountString;
    mark_to_market: AmountString;
  }>;
  expected_vs_realized: Array<{
    strategy: string;
    expected_net_pnl: AmountString;
    realized_net_pnl: AmountString;
    variance: AmountString;
  }>;
  reconciliation: {
    status: string;
    last_run_at: string;
    summary: string;
    differences: Array<{ scope: string; severity: string; message: string }>;
  };
}

export type DeskTab =
  | "orders"
  | "fills"
  | "positions"
  | "strategy"
  | "expected"
  | "reconciliation";
