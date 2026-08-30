import { useEffect, useState } from "react";

import { fetchSnapshot, getRuntimeConfig, sendDeskMutation } from "./api";
import type { DeskSnapshot, DeskTab } from "./types";
import { formatAmount, formatTimestamp, isConfirmLocked, modeClass, summaryPills } from "./utils";

const tabs: Array<{ id: DeskTab; label: string }> = [
  { id: "orders", label: "Orders" },
  { id: "fills", label: "Fills" },
  { id: "positions", label: "Positions" },
  { id: "strategy", label: "Strategy PnL" },
  { id: "expected", label: "Expected vs Realized" },
  { id: "reconciliation", label: "Reconciliation" }
];

export default function App() {
  const runtime = getRuntimeConfig(window.location.search);
  const [snapshot, setSnapshot] = useState<DeskSnapshot | null>(null);
  const [activeTab, setActiveTab] = useState<DeskTab>("orders");
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void fetchSnapshot(runtime)
      .then(setSnapshot)
      .catch((loadError: Error) => setError(loadError.message));
  }, [runtime.scenario, runtime.session]);

  async function runAction(action: "review" | "confirm" | "cancel" | "cancel-all" | "kill") {
    if (!snapshot) {
      return;
    }
    setPendingAction(action);
    setError(null);
    try {
      const updated = await sendDeskMutation(action, snapshot, runtime);
      setSnapshot(updated);
    } catch (mutationError) {
      setError((mutationError as Error).message);
    } finally {
      setPendingAction(null);
    }
  }

  if (!snapshot) {
    return <main className="desk-shell loading">Loading trade desk snapshot…</main>;
  }

  const selected =
    snapshot.opportunities.find((item) => item.id === snapshot.selected_opportunity_id) ??
    snapshot.opportunities[0];
  const confirmLocked = isConfirmLocked(snapshot);

  return (
    <main className={`desk-shell ${modeClass(snapshot.status_bar.mode)}`}>
      <section className="status-bar">
        <div>
          <p className="eyebrow">Mode</p>
          <h1>{snapshot.status_bar.mode}</h1>
          <p className="mode-banner">{snapshot.status_bar.mode_banner}</p>
        </div>
        <div className="status-grid">
          <article>
            <span>Cash</span>
            <strong>{formatAmount(snapshot.status_bar.available_cash)}</strong>
          </article>
          <article>
            <span>Today</span>
            <strong>{formatAmount(snapshot.status_bar.realized_net_pnl.today)}</strong>
          </article>
          <article>
            <span>7D</span>
            <strong>{formatAmount(snapshot.status_bar.realized_net_pnl.seven_day)}</strong>
          </article>
          <article>
            <span>30D</span>
            <strong>{formatAmount(snapshot.status_bar.realized_net_pnl.thirty_day)}</strong>
          </article>
          <article>
            <span>Drawdown</span>
            <strong>{formatAmount(snapshot.status_bar.current_drawdown)}</strong>
          </article>
          <article>
            <span>Risk Used</span>
            <strong>{snapshot.status_bar.risk_budget_used}</strong>
          </article>
        </div>
        <div className="status-actions">
          {summaryPills(snapshot).map((pill) => (
            <span className="pill" key={pill}>
              {pill}
            </span>
          ))}
          <button onClick={() => void runAction("cancel-all")} disabled={pendingAction !== null}>
            {pendingAction === "cancel-all" ? "Working…" : "Cancel All"}
          </button>
          <button className="danger" onClick={() => void runAction("kill")} disabled={pendingAction !== null}>
            {pendingAction === "kill" ? "Working…" : "Kill"}
          </button>
        </div>
      </section>

      {error ? <section className="error-banner">{error}</section> : null}

      <section className="workspace-grid">
        <aside className="panel opportunity-panel">
          <div className="panel-header">
            <h2>Ranked Opportunities</h2>
            <span>{snapshot.opportunities.length} live ideas</span>
          </div>
          <div className="opportunity-list">
            {snapshot.opportunities.map((opportunity) => (
              <article
                className={`opportunity-card ${opportunity.id === selected.id ? "selected" : ""}`}
                key={opportunity.id}
              >
                <div className="opportunity-head">
                  <span>#{opportunity.rank}</span>
                  <strong>{opportunity.strategy}</strong>
                  <span className="state-chip">{opportunity.state}</span>
                </div>
                <h3>{opportunity.market}</h3>
                <dl>
                  <div>
                    <dt>Edge</dt>
                    <dd>{opportunity.tradable_edge}</dd>
                  </div>
                  <div>
                    <dt>Expected</dt>
                    <dd>{formatAmount(opportunity.expected_net_profit)}</dd>
                  </div>
                  <div>
                    <dt>Max Loss</dt>
                    <dd>{formatAmount(opportunity.max_loss)}</dd>
                  </div>
                  <div>
                    <dt>TTL</dt>
                    <dd>{opportunity.ttl_seconds}s</dd>
                  </div>
                </dl>
                <p>{opportunity.note}</p>
              </article>
            ))}
          </div>
        </aside>

        <section className="panel center-panel">
          <div className="panel-header">
            <div>
              <h2>{snapshot.explanation.market_title}</h2>
              <span>{snapshot.explanation.settlement}</span>
            </div>
            <div className="selected-summary">
              <span>Selected</span>
              <strong>{selected.strategy}</strong>
            </div>
          </div>
          <div className="center-grid">
            <article className="card ladder-card">
              <h3>Ladder</h3>
              <div className="table-like">
                {snapshot.explanation.ladder.map((level) => (
                  <div className={`row ${level.side}`} key={`${level.side}-${level.price}`}>
                    <span>{level.side}</span>
                    <strong>{level.price}</strong>
                    <span>{level.size}</span>
                  </div>
                ))}
              </div>
            </article>
            <article className="card">
              <h3>Why It Exists</h3>
              {snapshot.explanation.reason_summary.map((item) => (
                <p key={item}>{item}</p>
              ))}
            </article>
            <article className="card">
              <h3>Cost Breakdown</h3>
              {snapshot.explanation.cost_breakdown.map((item) => (
                <div className="metric-row" key={item.label}>
                  <span>{item.label}</span>
                  <strong>{formatAmount(item.amount)}</strong>
                  <small>{item.note}</small>
                </div>
              ))}
            </article>
            <article className="card">
              <h3>Scenarios</h3>
              {snapshot.explanation.scenarios.map((scenario) => (
                <div className="scenario-row" key={scenario.name}>
                  <div>
                    <strong>{scenario.name}</strong>
                    <span>confidence {scenario.confidence}</span>
                  </div>
                  <div>
                    <strong>{formatAmount(scenario.net_profit)}</strong>
                    <span>Worst {formatAmount(scenario.worst_case_loss)}</span>
                  </div>
                </div>
              ))}
            </article>
          </div>
        </section>

        <aside className="panel plan-panel">
          <div className="panel-header">
            <h2>Execution Plan</h2>
            <span>{snapshot.execution_plan.state}</span>
          </div>
          <div className="plan-metrics">
            <div>
              <span>Fee</span>
              <strong>{formatAmount(snapshot.execution_plan.estimated_fee)}</strong>
            </div>
            <div>
              <span>Slippage</span>
              <strong>{formatAmount(snapshot.execution_plan.estimated_slippage)}</strong>
            </div>
            <div>
              <span>Expected</span>
              <strong>{formatAmount(snapshot.execution_plan.expected_net_profit)}</strong>
            </div>
            <div>
              <span>Worst Loss</span>
              <strong>{formatAmount(snapshot.execution_plan.worst_case_loss)}</strong>
            </div>
          </div>
          <div className="legs">
            {snapshot.execution_plan.legs.map((leg) => (
              <article className="leg-card" key={leg.id}>
                <strong>{leg.side}</strong>
                <span>{leg.instrument}</span>
                <span>
                  {leg.size} @ {leg.price}
                </span>
                <span>{leg.order_type}</span>
                <span>{leg.post_only ? "post-only" : "aggressive ok"}</span>
              </article>
            ))}
          </div>
          <p className="failure-note">{snapshot.execution_plan.failure_path}</p>
          <div className="action-row">
            <button onClick={() => void runAction("review")} disabled={pendingAction !== null}>
              {pendingAction === "review" ? "Working…" : "Review"}
            </button>
            <button
              className="primary"
              onClick={() => void runAction("confirm")}
              disabled={pendingAction !== null || confirmLocked}
            >
              {pendingAction === "confirm" ? "Working…" : "Confirm"}
            </button>
            <button onClick={() => void runAction("cancel")} disabled={pendingAction !== null}>
              {pendingAction === "cancel" ? "Working…" : "Cancel"}
            </button>
          </div>
          {snapshot.execution_plan.live_lock_reason ? (
            <p className="lock-note">{snapshot.execution_plan.live_lock_reason}</p>
          ) : null}
        </aside>
      </section>

      <section className="panel tab-panel">
        <div className="tab-strip">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              className={tab.id === activeTab ? "active" : ""}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        {activeTab === "orders" ? (
          <div className="table-like">
            {snapshot.orders.map((order) => (
              <div className="row" key={order.id}>
                <strong>{order.id}</strong>
                <span>{order.market}</span>
                <span>{order.state}</span>
                <span>
                  {order.size} @ {order.price}
                </span>
                <span>{formatTimestamp(order.updated_at)}</span>
              </div>
            ))}
          </div>
        ) : null}
        {activeTab === "fills" ? (
          <div className="table-like">
            {snapshot.fills.map((fill) => (
              <div className="row" key={fill.id}>
                <strong>{fill.id}</strong>
                <span>{fill.market}</span>
                <span>{fill.side}</span>
                <span>{formatAmount(fill.realized_net_pnl)}</span>
                <span>{formatTimestamp(fill.occurred_at)}</span>
              </div>
            ))}
          </div>
        ) : null}
        {activeTab === "positions" ? (
          <div className="table-like">
            {snapshot.positions.map((position) => (
              <div className="row" key={position.id}>
                <strong>{position.market}</strong>
                <span>{position.quantity}</span>
                <span>avg {position.avg_price}</span>
                <span>mark {position.mark_price}</span>
                <span>{formatAmount(position.unrealized_net_pnl)}</span>
              </div>
            ))}
          </div>
        ) : null}
        {activeTab === "strategy" ? (
          <div className="table-like">
            {snapshot.strategy_pnl.map((item) => (
              <div className="row" key={item.strategy}>
                <strong>{item.strategy}</strong>
                <span>{formatAmount(item.realized_today)}</span>
                <span>{formatAmount(item.realized_seven_day)}</span>
                <span>{formatAmount(item.realized_thirty_day)}</span>
                <span>{formatAmount(item.mark_to_market)}</span>
              </div>
            ))}
          </div>
        ) : null}
        {activeTab === "expected" ? (
          <div className="table-like">
            {snapshot.expected_vs_realized.map((item) => (
              <div className="row" key={item.strategy}>
                <strong>{item.strategy}</strong>
                <span>{formatAmount(item.expected_net_pnl)}</span>
                <span>{formatAmount(item.realized_net_pnl)}</span>
                <span>{formatAmount(item.variance)}</span>
              </div>
            ))}
          </div>
        ) : null}
        {activeTab === "reconciliation" ? (
          <div className="recon-panel">
            <div className="metric-row">
              <span>Status</span>
              <strong>{snapshot.reconciliation.status}</strong>
              <small>{formatTimestamp(snapshot.reconciliation.last_run_at)}</small>
            </div>
            <p>{snapshot.reconciliation.summary}</p>
            {snapshot.reconciliation.differences.map((difference) => (
              <article className="recon-issue" key={`${difference.scope}-${difference.message}`}>
                <strong>{difference.scope}</strong>
                <span>{difference.severity}</span>
                <p>{difference.message}</p>
              </article>
            ))}
          </div>
        ) : null}
      </section>
    </main>
  );
}
