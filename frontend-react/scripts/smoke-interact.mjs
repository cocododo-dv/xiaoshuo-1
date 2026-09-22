// Phase 1 交互冒烟：主题切换 / ⌘K 面板 / 新建作品 / 作品切换 / 回收站（仍 localStorage）。
// 运行：cd frontend && node ../frontend-react/scripts/smoke-interact.mjs [BASE]
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5174/";
const errors = [];
let failed = 0;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.setDefaultTimeout(15_000);
page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });
page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));

async function check(label, fn) {
  try { await fn(); console.log("ok:", label); }
  catch (e) { failed++; console.log("FAIL:", label, "—", e.message.split("\n")[0]); }
}

await page.goto(BASE);
await page.waitForSelector(".ws-app");
await page.waitForTimeout(500);

await check("主题切换（昼→夜→昼）", async () => {
  const theme0 = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
  await page.click('.ws-foot-btn[title="切换昼夜"]');
  await page.waitForTimeout(200);
  const theme1 = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
  if (theme1 === theme0) throw new Error(`theme unchanged: ${theme1}`);
  await page.click('.ws-foot-btn[title="切换昼夜"]');
  await page.waitForTimeout(200);
});

await check("舒适度面板（本地事件开关）", async () => {
  await page.click('.ws-foot-btn[title="舒适度设置"]');
  await page.waitForSelector(".twk-panel", { timeout: 5000 });
  await page.click(".twk-x");
  await page.waitForSelector(".twk-panel", { state: "detached", timeout: 5000 });
});

await check("⌘K 面板开关", async () => {
  await page.keyboard.press("Control+k");
  await page.waitForSelector(".pal[role='dialog']", { timeout: 5000 });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
});

await check("新建作品并激活", async () => {
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  await page.click(".ws-wsw-new");
  await page.waitForSelector(".ws-nw");
  await page.fill(".ws-nw-input", "冒烟测试书");
  await page.click(".ws-nw-foot .btn-accent");
  await page.waitForTimeout(500);
  const title = await page.textContent(".ws-brand-title");
  if (!title.includes("冒烟测试书")) throw new Error(`active title: ${title}`);
});

await check("切回样例长卷", async () => {
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  await page.click('.ws-wsw-row:has-text("样例长卷")');
  await page.waitForTimeout(500);
  const title = await page.textContent(".ws-brand-title");
  if (!title.includes("样例长卷")) throw new Error(`active title: ${title}`);
});

await check("删除作品进回收站并恢复", async () => {
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  await page.click('.ws-wsw-item:has-text("冒烟测试书") .ws-wsw-del');
  // 删除作品的确认是应用内确认框（ws-notify.jsx），不是浏览器 confirm
  await page.click('[data-testid="ws-confirm-ok"]');
  await page.waitForTimeout(500);
  await page.keyboard.press("Escape");
  await page.evaluate(() => { location.hash = "#trash"; });
  await page.waitForTimeout(600);
  const body = await page.textContent("body");
  if (!body.includes("冒烟测试书")) throw new Error("trash does not list the deleted work");
  await page.click('button:has-text("恢复")');
  await page.waitForTimeout(500);
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  const list = await page.textContent(".ws-wsw-list");
  if (!list.includes("冒烟测试书")) throw new Error("restored work missing from switcher");
  await page.keyboard.press("Escape");
});

await browser.close();
const uniq = [...new Set(errors)];
if (uniq.length) { console.log(`\n${uniq.length} console/page errors:`); uniq.slice(0, 20).forEach(e => console.log(" -", e.slice(0, 300))); }
else console.log("\nno console/page errors");
process.exitCode = failed || uniq.length ? 1 : 0;
