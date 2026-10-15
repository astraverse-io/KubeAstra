// Render the captured timeline into PNG frames, one every STEP ms of the
// animation (deterministic: the page is told what time it is, nothing runs in
// real time). Run from scripts/demo-recorder so `playwright` resolves:
//
//   node upgrade-pilot/frames.mjs <timeline.json> <frames-dir>
import { chromium } from 'playwright';
import { readFileSync, writeFileSync, mkdirSync, rmSync } from 'fs';
import { fileURLToPath } from 'url';

const [, , timelinePath, framesDir] = process.argv;
if (!timelinePath || !framesDir) {
  console.error('usage: node upgrade-pilot/frames.mjs <timeline.json> <frames-dir>');
  process.exit(1);
}
const steps = JSON.parse(readFileSync(timelinePath, 'utf8')).steps.map(({ cmd, out }) => ({ cmd, out }));
const player = fileURLToPath(new URL('./player.html', import.meta.url));
rmSync(framesDir, { recursive: true, force: true });
mkdirSync(framesDir, { recursive: true });

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1100, height: 700 }, deviceScaleFactor: 1 });
await page.goto(`file://${player}`, { waitUntil: 'networkidle' });
// Fonts load only once text uses them, and no command text exists at t=0.
await page.evaluate(() => Promise.all([
  '400 15px "IBM Plex Mono"', '500 15px "IBM Plex Mono"', '600 15px "IBM Plex Mono"',
  '500 21px "IBM Plex Sans"', '600 21px "IBM Plex Sans"', '700 40px "IBM Plex Sans"',
].map((f) => document.fonts.load(f))));
const ok = await page.evaluate(() =>
  document.fonts.check('500 15px "IBM Plex Mono"') && document.fonts.check('700 40px "IBM Plex Sans"'));
if (!ok) { console.error('frames: IBM Plex did not load; refusing to render with a fallback font'); process.exit(2); }

const total = await page.evaluate((t) => window.init(t), steps);
const cardAt = await page.evaluate(() => window.PLAN.cardAt);
const STEP = 80;
let n = 0;
for (let t = 0; t <= total; t += STEP) {
  await page.evaluate((x) => window.render(x), t);
  await page.screenshot({ path: `${framesDir}/${String(n++).padStart(4, '0')}.png` });
}
await browser.close();
// The frame just before the closing card shows the full terminal: the busiest
// frame, so the assembler builds its palette from it.
const meta = { step_ms: STEP, frames: n, total_ms: total, full_terminal_frame: Math.floor(cardAt / STEP) - 1 };
writeFileSync(`${framesDir}/meta.json`, JSON.stringify(meta));
console.log(`frames: ${JSON.stringify(meta)}`);
