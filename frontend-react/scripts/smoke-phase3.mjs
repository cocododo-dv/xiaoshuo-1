// Phase 3 验收冒烟：目录统一（catalog API 唯一真相源）。
// 前置：React dev 5174 + 后端（参数2，默认 8009，已 seed demo）。
// 运行：cd frontend && node ../frontend-react/scripts/smoke-phase3.mjs
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

await check("目录来自后端（tide 10 章，含戏剧卡）", async () => {
  const res = await page.evaluate(() => {
    const chs = window.WsCatalog.get();
    const ch1 = chs[0];
    const first = ch1 && ch1.scenes[0];
    return {
      n: chs.length, title: ch1 && ch1.title, spine: ch1 && ch1.drama && ch1.drama.spine,
      sid: first && first.sid, backendId: first && first.backendId, legacySid: first && first.legacySid,
    };
  });
  if (res.n !== 10) throw new Error(`chapters: ${res.n}`);
  if (res.title !== "样章01") throw new Error(`title: ${res.title}`);
  if (!res.spine) throw new Error("drama.spine missing");
  // 阶段 X：场景 sid = 稳定的 scene_id（身份跟着行走）；位置式旧 slug 只留作 legacySid
  if (!res.sid || res.sid !== res.backendId) throw new Error(`sid: ${res.sid} / ${res.backendId}`);
  if (res.legacySid !== "ch01s1") throw new Error(`legacySid: ${res.legacySid}`);
});

await check("主页（dashboard 兜底 + 目录同源）渲染", async () => {
  const title = await page.textContent(".hm-title");
  if (!title.includes("样例长卷")) throw new Error(title);
  const body = await page.textContent(".hm-top");
  if (!body.includes("章")) throw new Error("progress missing");
});

await check("编排台改章题 → 后端落库 + 主页一致", async () => {
  // 第 1 章已批准并锁定；通过编排台编辑当前进行中的第 8 章，覆盖真实 UI 保存路径。
  await page.evaluate(() => { location.hash = "#author"; });
  await page.waitForSelector('.arr-card:has-text("样章08")');
  await page.click('.arr-card:has-text("样章08")');
  const titleInput = page.locator('input[aria-label="章节标题"]');
  await titleInput.fill("P3改名·样章08");
  await titleInput.press("Enter");
  await page.waitForFunction(async (apiBase) => {
    const body = await fetch(`${apiBase}/api/v2/projects/work-a/catalog`).then(r => r.json());
    return body.data.chapters[7].title === "P3改名·样章08";
  }, API);
  const tree = await api("/api/v2/projects/work-a/catalog");
  if (tree.chapters[7].title !== "P3改名·样章08") throw new Error(`backend title: ${tree.chapters[7].title}`);
  await page.evaluate(() => { location.hash = "#home"; });
  await page.waitForSelector(".hm-chaps");
  if (!(await page.textContent(".hm-chaps")).includes("P3改名·样章08")) throw new Error("home title not refreshed");
  // 仍经编排台还原，避免后续检查继承临时标题。
  await page.evaluate(() => { location.hash = "#author"; });
  await page.waitForSelector('input[aria-label="章节标题"]');
  await page.fill('input[aria-label="章节标题"]', "样章08");
  await page.press('input[aria-label="章节标题"]', "Enter");
  await page.waitForFunction(async (apiBase) => {
    const body = await fetch(`${apiBase}/api/v2/projects/work-a/catalog`).then(r => r.json());
    return body.data.chapters[7].title === "样章08";
  }, API);
});

await check("写作器加场景 → 后端可见", async () => {
  const before = (await api("/api/v2/projects/work-a/catalog")).chapters[7].scenes.length;
  await page.evaluate(() => {
    const cur = window.WsCatalog.get()[7];
    window.WsCatalog.addScene(cur.id, "P3冒烟新场景");
  });
  await page.waitForTimeout(1500);
  const after = (await api("/api/v2/projects/work-a/catalog")).chapters[7];
  if (after.scenes.length !== before + 1) throw new Error(`scenes: ${before} -> ${after.scenes.length}`);
  if (!after.scenes.some(s => s.title === "P3冒烟新场景")) throw new Error("new scene title missing");
});

await check("删场景 → v1 trash 生效（后端目录消失）", async () => {
  await page.evaluate(() => {
    const cur = window.WsCatalog.get()[7];
    const victim = cur.scenes.find(s => s.title === "P3冒烟新场景");
    window.WsCatalog.removeScene(cur.id, victim.sid);
  });
  await page.waitForTimeout(1500);
  const after = (await api("/api/v2/projects/work-a/catalog")).chapters[7];
  if (after.scenes.some(s => s.title === "P3冒烟新场景")) throw new Error("scene still present");
});

await check("写作器正文保存 → words rollup 全链路后端", async () => {
  const statsBefore = await api("/api/v2/projects/work-a/writing-stats");
  await page.evaluate(() => { location.hash = "#writer"; });
  await page.waitForTimeout(1800);
  // 直接经 WrDocs 保存（等价于编辑器自动保存路径）
  await page.evaluate(async () => {
    const w = window.WsCatalog.writingScene();
    await window.WrDocs.save(w.scene.sid, `<p>样例正文第一段：占位句。</p><p>样例正文第二段：占位句。</p><p>这是 P3 冒烟新增的第三段，用来验证字数增量上报。本轮标记：${Date.now().toString(36)}</p>`);
  });
  await page.waitForTimeout(1500);
  const statsAfter = await api("/api/v2/projects/work-a/writing-stats");
  if (statsAfter.words_total === statsBefore.words_total) throw new Error("words_total unchanged");
  const tree = await api("/api/v2/projects/work-a/catalog");
  const sc = tree.chapters[7].scenes.find(s => s.state === "writing");
  if (!sc || sc.words <= 0) throw new Error("scene words_current not updated");
});

await check("跨会话正文水合（清缓存重载后编辑器有服务端正文）", async () => {
  await page.evaluate((apiBase) => {
    localStorage.clear();
    localStorage.setItem("novel-system-api-base", apiBase);
    localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
    localStorage.setItem("ws_active_work_v1", "work-a");
  }, API);
  await page.reload();
  await page.waitForSelector(".ws-app");
  await page.waitForTimeout(2000);
  await page.evaluate(() => { location.hash = "#writer"; });
  await page.waitForTimeout(2500);
  const text = await page.evaluate(() => {
    const el = document.querySelector(".wr-editor, [contenteditable=true]");
    return el ? el.innerText : "";
  });
  if (!text.includes("P3 冒烟新增的第三段")) throw new Error(`editor text: ${text.slice(0, 80)}…`);
});

await browser.close();
const uniq = [...new Set(errors)];
if (uniq.length) { console.log(`\n${uniq.length} page errors:`); uniq.slice(0, 10).forEach(e => console.log(" -", e.slice(0, 300))); }
process.exitCode = failed || uniq.length ? 1 : 0;
console.log(failed ? `\n${failed} checks failed` : "\nall checks passed");
