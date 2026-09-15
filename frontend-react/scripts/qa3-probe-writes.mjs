// QA3 写请求探针 — 判定 ensure POST / 雪花 step PATCH 是 per-mount 还是 per-nav 的 spurious write。
// 用纯 hash 导航（不 reload），逐段统计非 GET 写请求。
// 运行：cd frontend && node ../frontend-react/scripts/qa3-probe-writes.mjs
import path from "node:path";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const BASE = "http://127.0.0.1:5174/", API = "http://127.0.0.1:8000";

const writes = [];
let seg = "init";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1680, height: 1050 } });
page.on("response", (r) => {
  const u = r.url(); if (!u.includes("/api/")) return;
  const m = r.request().method();
  if (m !== "GET") writes.push({ seg, m, s: r.status(), url: u.replace(API, "") });
});
await page.addInitScript((api) => {
  localStorage.setItem("novel-system-api-base", api);
  localStorage.setItem("ws_tweaks_v1", JSON.stringify({ mode: "advanced" }));
  localStorage.setItem("ws_active_work_v1", "work-a");
}, API);
async function waitApp() { await page.waitForSelector(".ws-app", { state: "attached" }); await page.waitForTimeout(600); }

// 段1: 冷加载 home（reload 一次）
seg = "A:cold-load-home";
await page.goto(BASE + "#home"); await waitApp(); await page.waitForTimeout(1500);

// 段2-7: 纯 hash 导航，绝不 reload
for (const v of ["writer", "review", "library", "settings", "trash", "home"]) {
  seg = `B:nav→${v}`;
  await page.evaluate((view) => { location.hash = "#" + view; }, v);
  await page.waitForTimeout(1300);
}

// 段8: 进入 snowflake（hash 导航，不 reload），纯加载不点击
seg = "C:nav→snowflake(load-only)";
await page.evaluate(() => { location.hash = "#snowflake"; });
await page.waitForTimeout(2500);

// 段9: 在 snowflake 点 总览/逐步/步骤
seg = "D:snowflake-click-tabs";
for (const t of ["总览", "逐步"]) {
  const loc = page.locator(`text=${t}`).first();
  if (await loc.count()) { await loc.click().catch(() => {}); await page.waitForTimeout(800); }
}
seg = "E:snowflake-click-steps";
for (let i = 1; i <= 10; i++) {
  const num = String(i).padStart(2, "0");
  const loc = page.locator(`.sf-rail, .ct-rail, nav, aside`).locator(`text=${num}`).first();
  if (await loc.count()) { await loc.click().catch(() => {}); await page.waitForTimeout(300); }
}

// 段10: 离开再回 home，再回 snowflake（看是否每次重入都重发）
seg = "F:nav→home";
await page.evaluate(() => { location.hash = "#home"; }); await page.waitForTimeout(1000);
seg = "G:re-enter-snowflake";
await page.evaluate(() => { location.hash = "#snowflake"; }); await page.waitForTimeout(2200);

await browser.close();

// 汇总：按段统计
const bySeg = {};
for (const w of writes) {
  bySeg[w.seg] = bySeg[w.seg] || {};
  const key = `${w.m} ${w.url.replace(/tide_CH_[a-f0-9]+(_SC\d+)?/g, "{scene}").replace(/steps\/\w+/, "steps/{step}")}`;
  bySeg[w.seg][key] = (bySeg[w.seg][key] || 0) + 1;
}
console.log("==== 写请求按段统计（纯 hash 导航，无 reload）====\n");
for (const [s, m] of Object.entries(bySeg)) {
  console.log(`[${s}]`);
  for (const [k, c] of Object.entries(m)) console.log(`    ${c}×  ${k}`);
}
console.log(`\n总写请求数: ${writes.length}`);
