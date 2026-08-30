import type { DeskSnapshot } from "./types";

export function formatAmount(amount: string, unit = "$"): string {
  return `${unit}${amount}`;
}

export function formatTimestamp(value: string): string {
  return new Date(value).toLocaleString("en-US", {
    hour12: false,
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

export function modeClass(mode: string): string {
  return `mode-${mode.toLowerCase().replace(/_/g, "-")}`;
}

export function isConfirmLocked(snapshot: DeskSnapshot): boolean {
  const { status_bar, execution_plan } = snapshot;
  return (
    status_bar.kill_switch_engaged ||
    status_bar.is_live_locked ||
    status_bar.connections.data !== "live" ||
    status_bar.connections.orders !== "live" ||
    execution_plan.state === "DRAFT" ||
    execution_plan.state === "CANCELED" ||
    execution_plan.state === "KILLED"
  );
}

export function summaryPills(snapshot: DeskSnapshot): string[] {
  const marketAge = snapshot.status_bar.connections.market_data_age_ms;
  return [
    `Data ${snapshot.status_bar.connections.data}`,
    `Orders ${snapshot.status_bar.connections.orders}`,
    marketAge === null ? "Age unavailable" : `Age ${marketAge}ms`,
    `Risk used ${snapshot.status_bar.risk_budget_used}`
  ];
}
