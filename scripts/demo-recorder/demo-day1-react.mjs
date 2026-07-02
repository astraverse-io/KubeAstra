// Day 1 — The Hero Demo: ReAct Investigation
//
// Records a ~45-second video showing multi-step AI investigation
// that autonomously diagnoses a crashing pod.
//
// Usage:
//   cd scripts/demo-recorder
//   node demo-day1-react.mjs
//
// Pre-requisites:
//   - kind cluster + broken workloads running (make demo)
//   - Backend on http://localhost:8800, Frontend on http://localhost:3300

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
} from "./demo-helpers.mjs";

const OUTPUT_DIR = getOutputDir(import.meta);

async function run() {
  console.log("Day 1 — ReAct Investigation Demo");
  const { browser, context, page } = await launchRecorder(OUTPUT_DIR);

  // ── Title card ────────────────────────────────────────────────────────────
  await openChat(page);
  await showTitleCard(
    page,
    "AI-Powered K8s Debugging",
    "Multi-step investigation with ReAct reasoning",
    3500
  );

  // ── Scene 1: Ask why a pod is crashing ────────────────────────────────────
  console.log("Scene 1: Typing the investigation query...");
  await typeAndSend(page, "why is payment-service crashing?");

  // ── Scene 2: Watch the investigation unfold ───────────────────────────────
  console.log("Scene 2: Waiting for investigation steps...");
  // ReAct loop runs: find_workload → get_pod_logs → describe_pod → AI analysis
  // This can take 30-60s depending on Gemini response time
  await waitForResponse(page, 90000);
  await scrollToBottom(page);
  await page.waitForTimeout(2000);

  // ── Scene 3: Highlight the root cause card ────────────────────────────────
  console.log("Scene 3: Highlighting root cause...");
  try {
    // The RootCauseCard title is the classificationMode (e.g. "CrashLoopBackOff")
    // with a severity badge ("CRITICAL"). Look for the CRITICAL/WARNING badge.
    const badge = page.locator('text=/CRITICAL|WARNING|HIGH/i').first();
    await badge.waitFor({ state: "visible", timeout: 10000 });
    // Highlight the outermost card container
    await badge.evaluate((el) => {
      // Walk up to the card root (the div with boxShadow and borderRadius)
      let card = el;
      for (let i = 0; i < 8 && card.parentElement; i++) {
        card = card.parentElement;
        if (card.style && card.style.borderRadius === "12px") break;
      }
      card.style.transition = "box-shadow 0.3s";
      card.style.boxShadow = "0 0 0 3px rgba(59, 130, 246, 0.5)";
      setTimeout(() => { card.style.boxShadow = ""; }, 2500);
    });
    console.log("  Root cause card highlighted!");
  } catch {
    console.log("  (root cause card not found, continuing)");
  }
  await scrollToBottom(page);
  await page.waitForTimeout(2500);

  // ── Scene 4: Highlight manual steps or fix button ─────────────────────────
  console.log("Scene 4: Highlighting fix steps...");
  try {
    // When recovery ops are disabled, the card shows "Manual Steps Required"
    // When enabled, it shows "Review & Execute Fix" button
    const fixArea = page.locator('text=/Manual Steps Required|Review.*Execute Fix/i').first();
    await fixArea.waitFor({ state: "visible", timeout: 10000 });
    await fixArea.evaluate((el) => {
      const container = el.closest("div[style]") || el.parentElement;
      if (container) {
        container.style.transition = "box-shadow 0.3s";
        container.style.boxShadow = "0 0 0 3px rgba(251, 191, 36, 0.5)";
        setTimeout(() => { container.style.boxShadow = ""; }, 2500);
      }
    });
    console.log("  Fix steps highlighted!");
  } catch {
    console.log("  (fix steps not found, continuing)");
  }

  // ── Linger on the final state ─────────────────────────────────────────────
  await scrollToBottom(page);
  await page.waitForTimeout(2000);

  // ── Wrap up ───────────────────────────────────────────────────────────────
  await finishRecording(context, browser);
  console.log(`Done. Video saved to: ${OUTPUT_DIR}`);
}

run().catch((err) => {
  console.error("Day 1 demo failed:", err);
  process.exit(1);
});
