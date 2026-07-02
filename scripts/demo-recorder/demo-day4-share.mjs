// Day 4 — Share Investigations with Your Team
//
// Records a ~35-second video showing:
// run an investigation -> click Share -> URL copied ->
// open the shared URL in a new tab -> same investigation visible read-only.
//
// Usage:
//   cd scripts/demo-recorder
//   node demo-day4-share.mjs
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
  BASE_URL,
} from "./demo-helpers.mjs";

const OUTPUT_DIR = getOutputDir(import.meta);

async function run() {
  console.log("Day 4 — Shareable Sessions Demo");
  const { browser, context, page } = await launchRecorder(OUTPUT_DIR);

  // ── Title card ────────────────────────────────────────────────────────────
  await openChat(page);
  await showTitleCard(
    page,
    "Share Investigations",
    "One URL = full incident timeline for your team",
    3500
  );

  // ── Scene 1: Run a quick investigation ────────────────────────────────────
  console.log("Scene 1: Running an investigation...");
  await typeAndSend(page, "analyze the health of the demo namespace");
  await waitForResponse(page, 20000);
  await scrollToBottom(page);
  await page.waitForTimeout(2000);

  // ── Scene 2: Click the Share button ───────────────────────────────────────
  console.log("Scene 2: Clicking Share...");
  let shareUrl = null;
  try {
    const shareBtn = page.locator('button:has-text("Share")').first();
    await shareBtn.waitFor({ state: "visible", timeout: 5000 });

    // Highlight the share button
    await shareBtn.evaluate((el) => {
      el.style.transition = "transform 0.2s, box-shadow 0.2s";
      el.style.boxShadow = "0 0 0 3px rgba(59, 130, 246, 0.5)";
      el.style.transform = "scale(1.05)";
    });
    await page.waitForTimeout(1000);

    await shareBtn.click();
    await page.waitForTimeout(1500);

    // Grab the share URL from the clipboard or the page
    shareUrl = await page.evaluate(() => {
      // The share handler copies the URL to clipboard and shows "Copied!"
      // Try to extract the session URL from the current page URL
      const url = window.location.href;
      if (url.includes("/chat/")) return url;
      // Fallback: construct from sessionId if available
      return null;
    });

    // Show the "Copied!" toast state
    await page.waitForTimeout(2000);
  } catch {
    console.log("  (share button not found)");
  }

  // ── Scene 3: Open the shared URL in a new tab ─────────────────────────────
  console.log("Scene 3: Opening shared URL in new tab...");
  if (shareUrl) {
    // Open a new page (simulates new browser tab)
    const sharedPage = await context.newPage();
    await sharedPage.goto(shareUrl, { waitUntil: "domcontentloaded" });
    await sharedPage.waitForTimeout(3000);

    // Scroll through the shared view to show it's read-only
    await sharedPage.evaluate(() => {
      const chatArea = document.querySelector(
        '[class*="chat"], [class*="messages"], main'
      );
      if (chatArea) {
        chatArea.scrollTo({ top: chatArea.scrollHeight, behavior: "smooth" });
      }
    });
    await sharedPage.waitForTimeout(3000);

    // Close the shared page and return to the original
    await sharedPage.close();
    await page.waitForTimeout(1000);
  } else {
    // Fallback: demonstrate by navigating in the same page
    console.log("  (share URL not captured, showing current session)");
    const currentUrl = page.url();
    if (currentUrl.includes("/chat/")) {
      // Navigate away and back to simulate opening the shared link
      await page.goto(BASE_URL.replace(/\/$/, "") + "/chat", {
        waitUntil: "domcontentloaded",
      });
      await page.waitForTimeout(1500);
      await page.goto(currentUrl, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(3000);
      await scrollToBottom(page);
    }
    await page.waitForTimeout(2000);
  }

  // ── Wrap up ───────────────────────────────────────────────────────────────
  await finishRecording(context, browser);
  console.log(`Done. Video saved to: ${OUTPUT_DIR}`);
}

run().catch((err) => {
  console.error("Day 4 demo failed:", err);
  process.exit(1);
});
