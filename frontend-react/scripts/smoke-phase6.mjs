// Phase 6 验收冒烟：资料库（实体/关系/时间线/编辑落库/idea 卡入库）。
// 运行：cd frontend && node ../frontend-react/scripts/smoke-phase6.mjs
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5176/";
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

await check("资料库来自后端（人物/世界/大事记齐全）", async () => {
  await page.evaluate(() => { location.hash = "#library"; });
  // 资料库是懒加载的路由：冷启动时模块加载 + 读后端要 2.0–2.3 s，以前固定等 1.8 s 偶尔落空。等资料库的 store 真的装好
  // （编辑入口挂上、条目读到）再看；下一项「人物改名」也要用 LIB_persist。
  await page.waitForFunction(
    () => typeof window.LIB_persist === "function" && (window.LIB_ENTRIES || []).length > 0,
    null,
    { timeout: 20_000 },
  );
  const counts = await page.evaluate(() => {
    const all = window.LIB_ENTRIES || [];
    return {
      people: all.filter(e => e.cat === "people").length,
      world: all.filter(e => e.cat === "world").length,
      events: all.filter(e => e.cat === "events").length,
      linCen: all.find(e => e.name === "角色甲"),
    };
  });
  if (counts.people < 5) throw new Error(`people: ${counts.people}`);
  if (counts.world < 5) throw new Error(`world: ${counts.world}`);
  if (counts.events < 2) throw new Error(`events: ${counts.events}`);
  if (!counts.linCen || !counts.linCen.facts.length) throw new Error("角色甲 facts missing");
  if (!counts.linCen.links.length) throw new Error("角色甲 links missing");
  const body = await page.textContent("body");
  if (!body.includes("角色甲")) throw new Error("library view missing 角色甲");
});

await check("人物改名落库（character_id 不变）", async () => {
  await page.evaluate(() => {
    window.LIB_persist({ "lin-cen": { name: "角色甲·改" } });
  });
  await page.waitForTimeout(1500);
  const overview = await api("/api/v2/projects/work-a/library");
  const c = overview.characters.find(x => x.character_id === "lin-cen");
  if (!c || c.name !== "角色甲·改") throw new Error(`name: ${c && c.name}`);
  // 还原
  await page.evaluate(() => { window.LIB_persist({ "lin-cen": { name: "角色甲" } }); });
  await page.waitForTimeout(1200);
});

await check("建关系 → 关系图投影即时可见", async () => {
  const graphBefore = await api("/api/v2/projects/work-a/library/graph");
  await page.evaluate(async (apiBase) => {
    await fetch(`${apiBase}/api/v2/projects/work-a/library/relations`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Idempotency-Key": "p6-rel-" + Date.now() },
      body: JSON.stringify({ from_ref: "character:a-ke", to_ref: "entity:old-archive", kind: "works_at", note: "P6 冒烟新边" }),
    });
  }, API);
  const graphAfter = await api("/api/v2/projects/work-a/library/graph");
  if (graphAfter.edges.length !== graphBefore.edges.length + 1) throw new Error("edge not added");
  if (!graphAfter.edges.some(e => e.note === "P6 冒烟新边")) throw new Error("edge note missing");
});

await check("时间线按章排序数据完整", async () => {
  const timeline = await api("/api/v2/projects/work-a/library/timeline");
  if (timeline.items.length < 2) throw new Error(`events: ${timeline.items.length}`);
  if (!timeline.items.every(e => e.label)) throw new Error("labels missing");
});

await check("idea 卡「确认入库」→ 实体进库进图（D5 半自动）", async () => {
  const card = await page.evaluate(async (apiBase) => {
    const res = await fetch(apiBase + "/api/v1/review-items", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Idempotency-Key": "p6-idea-" + Date.now() },
      body: JSON.stringify({
        project_id: "work-a", kind: "idea", priority: 2,
        title: "发现新地点：盐雾灯塔", source: "资料派生", where: "成稿归档 · 模拟",
        dedupe_key: "derive:p6smoke:盐雾灯塔",
        actions: [
          { label: "确认入库", intent: "primary", op: "resolve", effect: { type: "create_entity", name: "盐雾灯塔", kind: "location", summary: "P6 冒烟生成" } },
          { label: "忽略", intent: "quiet", op: "resolve" },
        ],
      }),
    }).then(r => r.json());
    return res.data.card;
  }, API);
  // 在收件箱里点确认
  await page.evaluate(() => { location.hash = "#review"; });
  await page.waitForTimeout(1500);
  await page.click('.rv-item:has-text("盐雾灯塔")');
  await page.waitForTimeout(400);
  await page.click('button:has-text("确认入库")');
  await page.waitForTimeout(2000);
  const overview = await api("/api/v2/projects/work-a/library");
  if (!overview.entities.some(e => e.name === "盐雾灯塔")) throw new Error("entity not created");
  const graph = await api("/api/v2/projects/work-a/library/graph");
  if (!graph.nodes.some(n => n.name === "盐雾灯塔")) throw new Error("node missing in graph");
});

await browser.close();
const uniq = [...new Set(errors)];
if (uniq.length) { console.log(`\n${uniq.length} page errors:`); uniq.slice(0, 10).forEach(e => console.log(" -", e.slice(0, 300))); }
process.exitCode = failed || uniq.length ? 1 : 0;
console.log(failed ? `\n${failed} checks failed` : "\nall checks passed");
