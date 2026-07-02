// Day 3 — One-Click Fix Execution
//
// Records a ~50-second video showing the full flow:
// ask about a crashing pod -> investigation -> root cause ->
// Review & Execute Fix -> slide-to-confirm -> success ->
// verify the pod is now running.
//
// Usage:
//   cd scripts/demo-recorder
//   node demo-day3-fix.mjs
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
  console.log("Day 3 — One-Click Fix Execution Demo");
  const { browser, context, page } = await launchRecorder(OUTPUT_DIR);

  // ── Title card ────────────────────────────────────────────────────────────
  await openChat(page);
  await showTitleCard(
    page,
    "One-Click Fix Execution",
    "AI diagnoses the issue AND gives you the fix command",
    3500
  );

  // ── Scene 1: Trigger an investigation ─────────────────────────────────────
  console.log("Scene 1: Asking about a crashing pod...");
  await typeAndSend(page, "why is payment-service crashing?");
  await waitForResponse(page, 25000);
  await scrollToBottom(page);
  await page.waitForTimeout(2000);

  // ── Scene 2: Highlight the root cause card ────────────────────────────────
  console.log("Scene 2: Root cause card appeared...");
  await scrollToBottom(page);
  await page.waitForTimeout(1500);

  // ── Scene 3: Click Review & Execute Fix ───────────────────────────────────
  console.log("Scene 3: Clicking Review & Execute Fix...");
  try {
    const fixButton = page
      .locator('button:has-text("Review"), button:has-text("Execute Fix")')
      .first();
    await fixButton.waitFor({ state: "visible", timeout: 5000 });

    // Highlight the button briefly before clicking
    await fixButton.evaluate((el) => {
      el.style.transition = "transform 0.2s, box-shadow 0.2s";
      el.style.boxShadow = "0 0 0 3px rgba(251, 191, 36, 0.5)";
    });
    await page.waitForTimeout(1000);
    await fixButton.click();
    await page.waitForTimeout(2000);
  } catch {
    console.log("  (fix button not found, continuing)");
  }

  // ── Scene 4: Approval overlay — slide to confirm ──────────────────────────
  console.log("Scene 4: Approval overlay...");
  try {
    // Wait for the approval overlay to appear
    await page.waitForTimeout(1500);

    // Find and interact with the slide-to-confirm control
    // The approval overlay shows preflight checks, proposed changes,
    // and a slide-to-confirm button.
    const executeBtn = page
      .locator('button:has-text("Execute"), [class*="slide"], [class*="confirm"]')
      .first();
    await executeBtn.waitFor({ state: "visible", timeout: 5000 });

    // Simulate the slide-to-confirm gesture
    // Get the bounding box to perform a drag across ~80% of the element
    const box = await executeBtn.boundingBox();
    if (box) {
      const startX = box.x + 20;
      const startY = box.y + box.height / 2;
      const endX = box.x + box.width * 0.85;

      await page.mouse.move(startX, startY);
      await page.waitForTimeout(300);
      await page.mouse.down();
      // Slide slowly across for visual effect
      const steps = 20;
      for (let i = 1; i <= steps; i++) {
        const x = startX + ((endX - startX) * i) / steps;
        await page.mouse.move(x, startY);
        await page.waitForTimeout(40);
      }
      await page.mouse.up();
    }
    await page.waitForTimeout(3000); // wait for command execution
  } catch {
    console.log("  (approval overlay interaction skipped)");
  }

  // ── Scene 5: Success confirmation ─────────────────────────────────────────
  console.log("Scene 5: Command executed successfully...");
  await scrollToBottom(page);
  await page.waitForTimeout(2000);

  // ── Scene 6: Verify the fix worked ────────────────────────────────────────
  console.log("Scene 6: Verifying the fix...");
  await typeAndSend(page, "is payment-service running now?");
  await waitForResponse(page, 15000);
  await scrollToBottom(page);
  await page.waitForTimeout(2500);

  // ── Wrap up ───────────────────────────────────────────────────────────────
  await finishRecording(context, browser);
  console.log(`Done. Video saved to: ${OUTPUT_DIR}`);
}

run().catch((err) => {
  console.error("Day 3 demo failed:", err);
  process.exit(1);
});
