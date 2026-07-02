// Shared helpers for all demo recording scripts.
//
// Each demo-dayN.mjs imports from here so the recording setup,
// typing behavior, and cleanup are consistent across all videos.

import { chromium } from "playwright";
import { mkdirSync } from "fs";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

// ── Config ──────────────────────────────────────────────────────────────────

export const BASE_URL = process.env.KUBEASTRA_URL || "http://localhost:3300";
export const TYPING_DELAY_MS = 30;
export const VIEWPORT = { width: 1440, height: 900 };

// ── Output directory ────────────────────────────────────────────────────────

export function getOutputDir(importMeta) {
  const dir = dirname(fileURLToPath(importMeta.url));
  const out = resolve(dir, "output");
  mkdirSync(out, { recursive: true });
  return out;
}

// ── Browser setup ───────────────────────────────────────────────────────────

export async function launchRecorder(outputDir, opts = {}) {
  const viewport = opts.viewport || VIEWPORT;

  const browser = await chromium.launch({
    headless: false,
    slowMo: 0,
  });

  const context = await browser.newContext({
    viewport,
    recordVideo: {
      dir: outputDir,
      size: viewport,
    },
  });

  const page = await context.newPage();
  return { browser, context, page };
}

// ── Navigate to chat ────────────────────────────────────────────────────────

export async function openChat(page, waitMs = 2500) {
  const target = BASE_URL.replace(/\/$/, "") + "/chat";
  console.log(`Opening ${target}...`);
  await page.goto(target, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(waitMs);
}

// ── Type a message and press Enter ──────────────────────────────────────────

export async function typeAndSend(page, text, opts = {}) {
  const delay = opts.typingDelay || TYPING_DELAY_MS;

  const input = page
    .locator('input[placeholder*="Ask Astra"], input[placeholder*="cluster"], textarea')
    .first();
  await input.waitFor({ state: "visible", timeout: 10000 });
  await input.click();
  await input.fill("");
  await page.keyboard.type(text, { delay });
  await page.waitForTimeout(300);
  await page.keyboard.press("Enter");
}

// ── Wait for the AI response to finish ──────────────────────────────────────

export async function waitForResponse(page, timeoutMs = 60000) {
  // The UI shows "Investigating across your cluster…" while the ReAct loop
  // runs, then replaces it with the actual response (RootCauseCard or text).
  // Strategy: wait for the "Investigating" text to disappear, which means
  // the response has arrived. Fall back to waiting for any new assistant
  // message content if the investigating indicator isn't found.
  try {
    // Wait briefly for the "Investigating" message to appear
    await page.waitForTimeout(2000);

    // Wait for it to go away (response arrived) — poll until the text is gone
    await page.waitForFunction(
      () => {
        const body = document.body.innerText;
        return !body.includes("Investigating across your cluster");
      },
      { timeout: timeoutMs, polling: 1000 }
    );
  } catch {
    // If the indicator never appeared or didn't disappear, just wait
    await page.waitForTimeout(Math.min(timeoutMs, 30000));
  }
}

// ── Scroll to bottom of chat ────────────────────────────────────────────────

export async function scrollToBottom(page) {
  await page.evaluate(() => {
    const chatArea = document.querySelector('[class*="chat"], [class*="messages"], main');
    if (chatArea) chatArea.scrollTop = chatArea.scrollHeight;
  });
  await page.waitForTimeout(500);
}

// ── Close and flush video ───────────────────────────────────────────────────

export async function finishRecording(context, browser, pauseMs = 2000) {
  console.log("Final pause...");
  await new Promise((r) => setTimeout(r, pauseMs));
  await context.close(); // flushes the video file
  await browser.close();
}

// ── Title card (pause on empty screen with a title) ─────────────────────────

export async function showTitleCard(page, title, subtitle = "", durationMs = 3000) {
  await page.evaluate(
    ({ title, subtitle }) => {
      const overlay = document.createElement("div");
      overlay.id = "kubeastra-title-card";
      overlay.style.cssText = `
        position: fixed; inset: 0; z-index: 99999;
        display: flex; flex-direction: column;
        align-items: center; justify-content: center;
        background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
        color: white; font-family: system-ui, -apple-system, sans-serif;
      `;
      overlay.innerHTML = `
        <div style="font-size: 48px; font-weight: 700; letter-spacing: -1px;">
          ${title}
        </div>
        ${subtitle ? `<div style="font-size: 22px; margin-top: 16px; opacity: 0.7;">${subtitle}</div>` : ""}
        <div style="font-size: 16px; margin-top: 32px; opacity: 0.4;">kubeastra</div>
      `;
      document.body.appendChild(overlay);
    },
    { title, subtitle }
  );
  await page.waitForTimeout(durationMs);
  await page.evaluate(() => {
    const el = document.getElementById("kubeastra-title-card");
    if (el) el.remove();
  });
  await page.waitForTimeout(500);
}

// ── Highlight an element briefly (visual cue for viewers) ───────────────────

export async function flashHighlight(page, selector, durationMs = 1500) {
  await page.evaluate(
    ({ selector, durationMs }) => {
      const el = document.querySelector(selector);
      if (!el) return;
      const overlay = document.createElement("div");
      const rect = el.getBoundingClientRect();
      overlay.style.cssText = `
        position: fixed; z-index: 99998;
        top: ${rect.top - 4}px; left: ${rect.left - 4}px;
        width: ${rect.width + 8}px; height: ${rect.height + 8}px;
        border: 3px solid #3b82f6; border-radius: 8px;
        background: rgba(59, 130, 246, 0.08);
        pointer-events: none;
        animation: kubeastra-flash 0.6s ease-in-out;
      `;
      const style = document.createElement("style");
      style.textContent = `
        @keyframes kubeastra-flash {
          0% { opacity: 0; transform: scale(1.05); }
          50% { opacity: 1; transform: scale(1); }
          100% { opacity: 1; }
        }
      `;
      document.head.appendChild(style);
      document.body.appendChild(overlay);
      setTimeout(() => { overlay.remove(); style.remove(); }, durationMs);
    },
    { selector, durationMs }
  );
  await page.waitForTimeout(durationMs + 200);
}
