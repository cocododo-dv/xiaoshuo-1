// Phase 4 验收冒烟：回收站（三级软删 + 整体恢复）。
// 前置：React dev 5174 + 后端（参数2，默认 8009，已 seed demo）。
// 运行：cd frontend && node ../frontend-react/scripts/smoke-phase4.mjs
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

await page.goto(BASE);
await page.evaluate((apiBase) => {
  localStorage.clear();
  localStorage.setItem("novel-system-api-base", apiBase);
  localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
  localStorage.setItem("ws_active_work_v1", "work-a");
}, API);
await page.reload();
await page.waitForSelector(".ws-app");
await page.waitForTimeout(2000);

await check("删整部书（样例短卷）→ 切换器消失", async () => {
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  await page.click('.ws-wsw-item:has-text("样例短卷") .ws-wsw-del');
  // 删除作品的确认是应用内确认框（ws-notify.jsx），不是浏览器 confirm
  await page.click('[data-testid="ws-confirm-ok"]');
  await page.waitForTimeout(1500);
  await page.keyboard.press("Escape");
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  const list = await page.textContent(".ws-wsw-list");
  await page.keyboard.press("Escape");
  if (list.includes("样例短卷")) throw new Error("still in switcher");
  const projects = await api("/api/v2/projects");
  if (projects.items.some(i => i.project_id === "work-b")) throw new Error("still in backend list");
});

await check("回收站可见整部条目 → 恢复 → 数据无损", async () => {
  await page.evaluate(() => { location.hash = "#trash"; });
  await page.waitForTimeout(1500);
  const body = await page.textContent("body");
  if (!body.includes("样例短卷")) throw new Error("trash view missing the work");
  await page.click('tr:has-text("样例短卷") button:has-text("恢复")');
  await page.waitForTimeout(1800);
  const projects = await api("/api/v2/projects");
  if (!projects.items.some(i => i.project_id === "work-b")) throw new Error("not restored in backend");
  const tree = await api("/api/v2/projects/work-b/catalog");
  if (tree.chapters.length !== 3) throw new Error(`salt chapters: ${tree.chapters.length}`);
  const stats = await api("/api/v2/projects/work-b/writing-stats");
  if (stats.words_total <= 0) throw new Error("stats lost");
});

await check("删场景 → 回收站条目 → 恢复（正文保留）", async () => {
  await page.evaluate(() => { location.hash = "#home"; });
  await page.waitForTimeout(800);
  const sceneId = await page.evaluate(async () => {
    const chs = window.WsCatalog.get();
    const ch = chs[7]; // tide ch08
    const victim = ch.scenes[ch.scenes.length - 1];
    window.WsCatalog.removeScene(ch.id, victim.sid);
    await new Promise(r => setTimeout(r, 1500));
    return victim.backendId;
  });
  const trash = await api("/api/v2/trash?project_id=work-a");
  const entry = trash.items.find(i => i.id === `scene:${sceneId}`);
  if (!entry) throw new Error("scene entry missing in trash");
  await page.evaluate(() => { location.hash = "#trash"; });
  await page.waitForTimeout(1200);
  await page.click('tbody tr:has-text("场景") button:has-text("恢复")');
  await page.waitForTimeout(1500);
  const tree = await api("/api/v2/projects/work-a/catalog");
  const restored = tree.chapters[7].scenes.some(s => s.scene_id === sceneId);
  if (!restored) throw new Error("scene not restored");
});

await check("永久清除后无残影", async () => {
  // 建一部临时书 → 删 → 永久清除
  const pid = await page.evaluate(async (apiBase) => {
    const res = await fetch(apiBase + "/api/v2/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Idempotency-Key": "p4-smoke-" + Date.now() },
      body: JSON.stringify({ title: "P4临时书", outline_text: "临时" }),
    }).then(r => r.json());
    return res.data.project.project_id;
  }, API);
  await page.evaluate(async (args) => {
    await fetch(`${args.api}/api/v2/projects/${args.pid}`, { method: "DELETE", headers: { "X-Idempotency-Key": "p4-del-" + Date.now() } });
    await fetch(`${args.api}/api/v2/trash/work:${args.pid}`, { method: "DELETE", headers: { "X-Idempotency-Key": "p4-purge-" + Date.now() } });
  }, { api: API, pid });
  const projects = await api("/api/v2/projects");
  if (projects.items.some(i => i.project_id === pid)) throw new Error("project survived purge");
  const trash = await api("/api/v2/trash");
  if (trash.items.some(i => i.id === `work:${pid}`)) throw new Error("trash entry survived purge");
});

await browser.close();
const uniq = [...new Set(errors)];
if (uniq.length) { console.log(`\n${uniq.length} page errors:`); uniq.slice(0, 10).forEach(e => console.log(" -", e.slice(0, 300))); }
process.exitCode = failed || uniq.length ? 1 : 0;
console.log(failed ? `\n${failed} checks failed` : "\nall checks passed");
