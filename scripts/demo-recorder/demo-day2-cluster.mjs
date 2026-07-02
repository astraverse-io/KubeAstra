// Day 2 — Connect Any Cluster in Seconds
//
// Records a ~30-second video showing the cluster connection flow:
// auto-detect contexts, select one, connect, then run a query.
//
// Usage:
//   cd scripts/demo-recorder
//   node demo-day2-cluster.mjs
//
// Pre-requisites:
//   - kind cluster running (make demo)
//   - Backend on http://localhost:8800, Frontend on http://localhost:3300
//   - Ideally start WITHOUT a pre-connected cluster so the connect
//     screen is visible. Clear any stored session first.

import {
  launchRecorder,
  openChat,
  typeAndSend,
  waitForResponse,
  scrollToBottom,
  finishRecording,
  showTitleCard,
  flashHighlight,
  getOutputDir,
  BASE_URL,
} from "./demo-helpers.mjs";

const OUTPUT_DIR = getOutputDir(import.meta);

async function run() {
  console.log("Day 2 — Cluster Connection Demo");
  const { browser, context, page } = await launchRecorder(OUTPUT_DIR);

  // ── Title card ────────────────────────────────────────────────────────────
  const target = BASE_URL.replace(/\/$/, "") + "/chat";
  await page.goto(target, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);
  await showTitleCard(
    page,
    "Connect Any Cluster",
    "Auto-detect, kubeconfig paste, or SSH — your choice",
    3500
  );

  // ── Scene 1: Show the cluster connect panel ───────────────────────────────
  console.log("Scene 1: Cluster connect panel...");
  // The connect screen should be visible if no cluster is connected.
  // If already connected, click "Switch" to show the panel.
  try {
    const switchBtn = page.locator('button:has-text("Switch")').first();
    await switchBtn.waitFor({ state: "visible", timeout: 3000 });
    await switchBtn.click();
    await page.waitForTimeout(1000);
  } catch {
    console.log("  (no Switch button — connect screen likely already visible)");
  }
  await page.waitForTimeout(2000);

  // ── Scene 2: Show auto-detect tab finding contexts ────────────────────────
  console.log("Scene 2: Auto-detect tab...");
  try {
    // Click the Auto-Detect tab if not already active
    const autoTab = page.locator('button:has-text("Auto-Detect"), button:has-text("Auto")').first();
    await autoTab.waitFor({ state: "visible", timeout: 3000 });
    await autoTab.click();
    await page.waitForTimeout(1500);

    // The select dropdown should populate with available contexts
    await flashHighlight(page, 'select, [class*="select"]', 1500);
  } catch {
    console.log("  (auto-detect tab interaction skipped)");
  }

  // ── Scene 3: Select a context from the dropdown ───────────────────────────
  console.log("Scene 3: Selecting context...");
  try {
    const select = page.locator("select").first();
    await select.waitFor({ state: "visible", timeout: 3000 });
    // Pick the kind cluster context
    const options = await select.locator("option").allTextContents();
    const kindOption = options.find((o) => o.includes("kind") || o.includes("kubeastra"));
    if (kindOption) {
      await select.selectOption({ label: kindOption });
      await page.waitForTimeout(1000);
    }
  } catch {
    console.log("  (context selection skipped)");
  }

  // ── Scene 4: Click Connect ────────────────────────────────────────────────
  console.log("Scene 4: Connecting...");
  try {
    const connectBtn = page
      .locator('button:has-text("Connect")')
      .filter({ hasNot: page.locator(':has-text("SSH")') })
      .first();
    await connectBtn.waitFor({ state: "visible", timeout: 3000 });
    await connectBtn.click();
    await page.waitForTimeout(3000); // wait for connectivity check
  } catch {
    console.log("  (connect button not found)");
  }

  // ── Scene 5: Connected — run a quick query to prove it works ──────────────
  console.log("Scene 5: Running a query on the connected cluster...");
  await page.waitForTimeout(2000);

  // Check if we're now on the chat screen
  try {
    await typeAndSend(page, "what pods are running in demo namespace?");
    await waitForResponse(page, 15000);
    await scrollToBottom(page);
    await page.waitForTimeout(2000);
  } catch {
    console.log("  (query step skipped — may not have reached chat screen)");
  }

  // ── Wrap up ───────────────────────────────────────────────────────────────
  await finishRecording(context, browser);
  console.log(`Done. Video saved to: ${OUTPUT_DIR}`);
}

run().catch((err) => {
  console.error("Day 2 demo failed:", err);
  process.exit(1);
});
