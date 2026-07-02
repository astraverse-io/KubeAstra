// Day 5 — Visual Resource Graph + Full Stack Overview
//
// Records a ~40-second video showing:
// ask for the resource graph -> interactive topology renders ->
// health glow on nodes -> click a crashing pod -> details panel ->
// zoom out to show MiniMap.
//
// Usage:
//   cd scripts/demo-recorder
//   node demo-day5-graph.mjs
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
  console.log("Day 5 — Visual Resource Graph Demo");
  const { browser, context, page } = await launchRecorder(OUTPUT_DIR);

  // ── Title card ────────────────────────────────────────────────────────────
  await openChat(page);
  await showTitleCard(
    page,
    "Visual Resource Graph",
    "Interactive namespace topology with health indicators",
    3500
  );

  // ── Scene 1: Request the resource graph ───────────────────────────────────
  console.log("Scene 1: Requesting resource graph...");
  await typeAndSend(page, "show me the resource graph for demo namespace");
  await waitForResponse(page, 18000);
  await scrollToBottom(page);
  await page.waitForTimeout(2000);

  // ── Scene 2: Let the graph render and settle ──────────────────────────────
  console.log("Scene 2: Graph rendering...");
  try {
    // Wait for the ReactFlow canvas to appear
    await page.waitForSelector('.react-flow, [class*="ResourceGraph"], [class*="resource-graph"]', {
      timeout: 8000,
    });
    await page.waitForTimeout(2000); // let the layout animation finish
  } catch {
    console.log("  (graph container not found by selector, continuing)");
  }

  // ── Scene 3: Highlight health indicators ──────────────────────────────────
  console.log("Scene 3: Showing health indicators...");
  // Pause to let viewers see the green/red health glow on nodes
  await page.waitForTimeout(2500);

  // ── Scene 4: Click a pod node to open the details panel ───────────────────
  console.log("Scene 4: Clicking a node...");
  try {
    // Find a graph node — ReactFlow renders nodes as divs with class "react-flow__node"
    const nodes = page.locator('.react-flow__node, [class*="ka-node"]');
    const nodeCount = await nodes.count();
    if (nodeCount > 0) {
      // Try to click a pod node (usually the last few nodes in the graph)
      // Click one near the end — more likely to be a pod
      const targetIndex = Math.min(nodeCount - 1, 3);
      const targetNode = nodes.nth(targetIndex);
      await targetNode.click();
      await page.waitForTimeout(2500); // let the detail panel appear
    }
  } catch {
    console.log("  (node click skipped)");
  }

  // ── Scene 5: Show the details panel ───────────────────────────────────────
  console.log("Scene 5: Details panel...");
  try {
    await page.waitForSelector('[class*="detail"], [class*="Detail"]', {
      timeout: 3000,
    });
    await page.waitForTimeout(2000);
  } catch {
    console.log("  (detail panel not found, continuing)");
  }

  // ── Scene 6: Zoom out to show MiniMap ─────────────────────────────────────
  console.log("Scene 6: Zooming out to show MiniMap...");
  try {
    // Use the ReactFlow zoom-out control or scroll to zoom out
    const zoomOut = page.locator(
      '.react-flow__controls button[title*="zoom out"], .react-flow__controls button:nth-child(2)'
    );
    if (await zoomOut.isVisible()) {
      // Click zoom out a few times
      for (let i = 0; i < 3; i++) {
        await zoomOut.click();
        await page.waitForTimeout(400);
      }
    }
    await page.waitForTimeout(2000);
  } catch {
    console.log("  (zoom controls not found, continuing)");
  }

  // ── Scene 7: Final pan to show the full graph ─────────────────────────────
  console.log("Scene 7: Final view...");
  // Try the fit-view control to show the entire graph
  try {
    const fitView = page.locator(
      '.react-flow__controls button[title*="fit"], .react-flow__controls button:nth-child(3)'
    );
    if (await fitView.isVisible()) {
      await fitView.click();
      await page.waitForTimeout(1500);
    }
  } catch {
    // no-op
  }
  await page.waitForTimeout(2500);

  // ── Wrap up ───────────────────────────────────────────────────────────────
  await finishRecording(context, browser);
  console.log(`Done. Video saved to: ${OUTPUT_DIR}`);
}

run().catch((err) => {
  console.error("Day 5 demo failed:", err);
  process.exit(1);
});
