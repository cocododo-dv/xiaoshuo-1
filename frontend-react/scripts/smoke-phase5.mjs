// Phase 5 验收冒烟：待办收件箱（卡片 + 后端 effect + 派生项 + badge）。
// 前置：React dev 5174 + 后端（默认 8009，已 seed demo）。
// 运行：cd frontend && node ../frontend-react/scripts/smoke-phase5.mjs
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5174/";
const API = process.argv[3] || "http://127.0.0.1:8009";
let failed = 0;
const errors = [];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.setDefaultTimeout(20_000);
page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
page.on("dialog", (d) => d.accept());

async function check(label, fn) {
  try { await fn(); console.log("ok:", label); }
  catch (e) { failed++; console.log("FAIL:", label, "—", e.message.split("\n")[0]); }
}

const api = async (p) => (await page.evaluate(async (u) => (await fetch(u)).json(), API + p)).data;

async function waitForCatalog(predicate, timeoutMs = 10_000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const catalog = await api("/api/v2/projects/work-a/catalog");
    if (predicate(catalog)) return catalog;
    await page.waitForTimeout(200);
  }
  return api("/api/v2/projects/work-a/catalog");
}

await page.goto(BASE);
await page.evaluate((apiBase) => {
  localStorage.clear();
  localStorage.setItem("novel-system-api-base", apiBase);
  localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
  localStorage.setItem("ws_active_work_v1", "work-a");
}, API);
await page.reload();
await page.waitForSelector(".ws-app");
await page.waitForTimeout(2200);

await check("收件箱来自后端（含 demo 卡 + 派生卡）", async () => {
  await page.evaluate(() => { location.hash = "#review"; });
  await page.waitForTimeout(1500);
  const body = await page.textContent("body");
  if (!body.includes("第 8 章标题在两个候选间未定")) throw new Error("demo decision card missing");
  if (!body.includes("已起草未确认")) throw new Error("derived snowflake card missing");
});

await check("QC 卡「采纳·插入反应场」→ 目录真的多一个反应场（后端事务）", async () => {
  const before = (await api("/api/v2/projects/work-a/catalog")).chapters[7].scenes.length;
  // 展开当前第 8 章 QC 卡并点采纳（历史前缀章已批准并锁定）。
  const card = page.locator('.rv-item:has-text("第 8 章节奏过快")');
  await card.click();
  await page.waitForTimeout(400);
  await card.locator('button:has-text("采纳 · 插入反应场")').click();
  const ch08 = (await waitForCatalog(catalog => catalog.chapters[7].scenes.length === before + 1)).chapters[7];
  if (ch08.scenes.length !== before + 1) throw new Error(`scenes ${before} -> ${ch08.scenes.length}`);
  const inserted = ch08.scenes.find(s => s.title === "样例反应场");
  if (!inserted) throw new Error("inserted scene missing");
  if (inserted.kind !== "reactive") throw new Error("kind wrong");
  if (inserted.seq !== 4) throw new Error(`at position: seq=${inserted.seq}`);
});

await check("决策卡选项 → rename effect 落库", async () => {
  const card = page.locator('.rv-item:has-text("第 8 章标题在两个候选间未定")');
  await card.click();
  await page.waitForTimeout(400);
  await card.locator('button:has-text("用「候选标题二」")').click();
  const tree = await waitForCatalog(catalog => catalog.chapters[7].title === "候选标题二");
  if (tree.chapters[7].title !== "候选标题二") throw new Error(`title: ${tree.chapters[7].title}`);
});

await check("badge = priority 1 的 open 数（处理后减少）", async () => {
  const badge = await api("/api/v1/review-items/badge?project_id=work-a");
  const fromStore = await page.evaluate(() => window.rvOpenItems().filter(i => i.priority === 1).length);
  if (badge.count !== fromStore) throw new Error(`badge ${badge.count} != store ${fromStore}`);
});

await check("派生卡：不可划掉 / snooze 按指纹 / 修好自动消失", async () => {
  // 制造空章 → 派生卡浮现
  const created = await page.evaluate(async (apiBase) => {
    const res = await fetch(`${apiBase}/api/v2/projects/work-a/catalog/chapters`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Idempotency-Key": "p5-empty-" + Date.now() },
      body: JSON.stringify({ title: "P5空章", current: false, with_scene: false }),
    }).then(r => r.json());
    return res.data.chapter.chapter_id;
  }, API);
  let items = (await api("/api/v1/review-items?state=open&project_id=work-a")).items;
  const empty = items.find(i => String(i.id).startsWith(`derived:catalog:empty:${created}`));
  if (!empty) throw new Error("empty-chapter derived card missing");
  // 不可 resolve
  const blocked = await page.evaluate(async (args) => {
    const res = await fetch(`${args.api}/api/v1/review-items/${encodeURIComponent(args.id)}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Idempotency-Key": "p5-noresolve-" + Date.now() },
      body: JSON.stringify({ project_id: "work-a" }),
    });
    return res.status;
  }, { api: API, id: empty.id });
  if (blocked !== 409) throw new Error(`resolve should be 409, got ${blocked}`);
  // 修好（删空章）→ 自动消失
  await page.evaluate(async (args) => {
    await fetch(`${args.api}/api/v1/chapters/trash`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Idempotency-Key": "p5-fix-" + Date.now() },
      body: JSON.stringify({ chapter_ids: [args.cid] }),
    });
  }, { api: API, cid: created });
  items = (await api("/api/v1/review-items?state=open&project_id=work-a")).items;
  if (items.some(i => String(i.id).startsWith(`derived:catalog:empty:${created}`))) throw new Error("derived card did not vanish");
});

await check("同一 dedupe_key 投两次只有一张卡", async () => {
  const mk = () => page.evaluate(async (apiBase) => {
    const res = await fetch(apiBase + "/api/v1/review-items", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Idempotency-Key": "p5-dedupe-" + Math.random() },
      body: JSON.stringify({ project_id: "work-a", kind: "note", title: "P5 dedupe 冒烟", dedupe_key: "p5:smoke:once" }),
    }).then(r => r.json());
    return res.data;
  }, API);
  await mk();
  const second = await mk();
  if (second.deduped !== true) throw new Error("second post not deduped");
  const items = (await api("/api/v1/review-items?state=open&project_id=work-a")).items;
  if (items.filter(i => i.title === "P5 dedupe 冒烟").length !== 1) throw new Error("duplicate cards");
});

await browser.close();
const uniq = [...new Set(errors)];
if (uniq.length) { console.log(`\n${uniq.length} page errors:`); uniq.slice(0, 10).forEach(e => console.log(" -", e.slice(0, 300))); }
process.exitCode = failed || uniq.length ? 1 : 0;
console.log(failed ? `\n${failed} checks failed` : "\nall checks passed");
