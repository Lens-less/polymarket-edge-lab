import type { DeskSnapshot } from "./types";

export interface DeskRuntimeConfig {
  scenario: string;
  session: string;
}

export function getRuntimeConfig(search: string): DeskRuntimeConfig {
  const params = new URLSearchParams(search);
  return {
    scenario: params.get("scenario") ?? "paper",
    session: params.get("session") ?? "default"
  };
}

export function buildDeskUrl(path: string, runtime: DeskRuntimeConfig): string {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("scenario", runtime.scenario);
  url.searchParams.set("session", runtime.session);
  return url.toString();
}

export async function fetchSnapshot(runtime: DeskRuntimeConfig): Promise<DeskSnapshot> {
  const response = await fetch(buildDeskUrl("/api/v0.2/desk/snapshot", runtime));
  if (!response.ok) {
    throw new Error(`Desk snapshot failed: ${response.status}`);
  }
  return (await response.json()) as DeskSnapshot;
}

export async function sendDeskMutation(
  action: "review" | "confirm" | "cancel" | "cancel-all" | "kill",
  snapshot: DeskSnapshot,
  runtime: DeskRuntimeConfig
): Promise<DeskSnapshot> {
  const response = await fetch(buildDeskUrl(`/api/v0.2/desk/${action}`, runtime), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Desk-CSRF": snapshot.csrf_token
    },
    body: JSON.stringify({
      actor: "desk-operator-ui",
      idempotency_key: `${action}-${snapshot.session}-${snapshot.snapshot_version}`
    })
  });
  if (!response.ok) {
    const payload = (await response.json()) as { detail?: string };
    throw new Error(payload.detail ?? `Desk mutation failed: ${response.status}`);
  }
  const payload = (await response.json()) as { snapshot: DeskSnapshot };
  return payload.snapshot;
}
