import { expect, test } from "@playwright/test";
import { ensureDeskServer, stopDeskServer } from "./server";

const runId = `${Date.now()}`;
const paperSession = `e2e-paper-${runId}`;
const shadowSession = `e2e-shadow-${runId}`;
const liveSession = `e2e-live-lock-${runId}`;
const scanSession = `e2e-safety-scan-${runId}`;

test.beforeAll(async () => {
  await ensureDeskServer();
});

test.afterAll(async () => {
  await stopDeskServer();
});

test("paper flow renders opportunities, confirms, and survives refresh", async ({ page }) => {
  await page.goto(`/?scenario=paper&session=${paperSession}`);

  await expect(page.getByRole("heading", { name: "PAPER" })).toBeVisible();
  await expect(page.getByText("Ranked Opportunities")).toBeVisible();
  await expect(page.getByText("BTC > 110k by Friday").first()).toBeVisible();

  await page.getByRole("button", { name: "Review" }).click();
  await expect(page.getByText("REVIEWED")).toBeVisible();

  await page.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("APPROVED")).toBeVisible();

  await page.getByRole("button", { name: "Fills" }).click();
  const latestFillRow = page.locator(".row").filter({ hasText: "fill-3" });
  await expect(latestFillRow).toBeVisible();
  await expect(latestFillRow).toContainText("$14.250000");

  await page.reload();
  await expect(page.getByText("APPROVED")).toBeVisible();

  await page.getByRole("button", { name: "Cancel All" }).click();
  await page.getByRole("button", { name: "Reconciliation" }).click();
  await expect(page.getByText("Cancel-all requested")).toBeVisible();
});

test("shadow view records review but keeps venue submission blocked", async ({ page }) => {
  await page.goto(`/?scenario=shadow&session=${shadowSession}`);

  await expect(page.getByRole("heading", { name: "SHADOW" })).toBeVisible();
  await expect(page.getByText(/SHADOW observational only/)).toBeVisible();
  await expect(page.getByText("Orders blocked")).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm" })).toBeDisabled();

  await page.getByRole("button", { name: "Review" }).click();
  await expect(page.getByText("REVIEWED")).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm" })).toBeDisabled();

  await page.reload();
  await expect(page.getByText("REVIEWED")).toBeVisible();
});

test("fake live lock shows blocked, disconnected, stale, and kill states", async ({ page }) => {
  await page.goto(`/?scenario=fake-live-lock&session=${liveSession}`);

  await expect(page.getByRole("heading", { name: "LIVE_CANARY" })).toBeVisible();
  await expect(page.getByText("Data disconnected")).toBeVisible();
  await expect(page.getByText("Orders blocked")).toBeVisible();
  await expect(page.getByText("Age 8200ms")).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm" })).toBeDisabled();

  await page.getByRole("button", { name: "Review" }).click();
  await expect(page.getByText("REVIEWED")).toBeVisible();

  await page.getByRole("button", { name: "Kill" }).click();
  await expect(page.getByRole("heading", { name: "KILLED" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "KILLED" })).toBeVisible();
});

test("bundle, storage, and responses stay free of live secrets", async ({ page, request }) => {
  await page.goto(`/?scenario=paper&session=${scanSession}`);

  const html = await page.content();
  expect(html).not.toContain("PRIVATE_KEY");
  expect(html).not.toContain("API_KEY");
  expect(html).not.toContain("SECRET");

  const scripts = await page.locator("script[type='module']").evaluateAll((nodes) =>
    nodes.map((node) => (node as HTMLScriptElement).src)
  );
  for (const scriptUrl of scripts) {
    const response = await request.get(scriptUrl);
    const body = await response.text();
    expect(body).not.toContain("PRIVATE_KEY");
    expect(body).not.toContain("SECRET_KEY");
    expect(body).not.toContain("BEGIN PRIVATE KEY");
    expect(body).not.toContain("ALLOWANCE=");
  }

  const storage = await page.evaluate(() => ({
    local: Object.values(localStorage),
    session: Object.values(sessionStorage)
  }));
  expect(storage.local.join("|")).toBe("");
  expect(storage.session.join("|")).toBe("");

  const snapshotResponse = await request.get(`/api/v0.2/desk/snapshot?scenario=paper&session=${scanSession}`);
  const snapshotBody = await snapshotResponse.text();
  expect(snapshotBody).not.toContain("PRIVATE_KEY");
  expect(snapshotBody).not.toContain("BEGIN PRIVATE KEY");
  expect(snapshotBody).not.toContain("SECRET_KEY");
  expect(snapshotBody).not.toContain("api_key");
});
