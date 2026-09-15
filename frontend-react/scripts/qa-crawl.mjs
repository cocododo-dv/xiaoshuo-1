// QA Round 1 crawler — 全视图 × 多项目 健康巡检。
// 捕获：console error / pageerror / 4xx-5xx 网络失败 / 可见错误态 / 空白渲染 + 截图。
// 运行（Playwright 由 frontend-react 自己锁定）：
//   cd frontend && node ../frontend-react/scripts/qa-crawl.mjs [BASE] [API] [OUTDIR]
import path from "node:path";
import fs from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5174/";
const API = process.argv[3] || "http://127.0.0.1:8000";
const OUT = process.argv[4] || path.resolve("../.codex-run/qa-round1");
fs.mkdirSync(OUT, { recursive: true });
fs.mkdirSync(path.join(OUT, "shots"), { recursive: true });

// 权威视图清单（取自 ws-app.jsx WS_NAV_GROUPS；含 quality）
const VIEWS = [
  "home", "snowflake", "writer", "styleref", "review", "library",
  "author", "scene", "manuscripts", "longform", "quality",
  "index", "interop", "settings", "trash",
];

// 待巡检项目：两部 demo（数据丰富） + 一部真实 chapter_blocked 项目（边界态）
const WORKS = process.env.QA_WORKS ? process.env.QA_WORKS.split(",") : ["work-a", "work-b", "PRJ_1C88DEFF3D"];

const ERR_KEYWORDS = ["出错", "失败", "加载失败", "无法加载", "错误", "异常", "undefined", "NaN", "[object Object]", "Cannot read", "TypeError", "is not a function"];

const findings = []; // {work, view, kind, detail}
let ctx = { work: "", view: "" };

function record(kind, detail) {
  findings.push({ work: ctx.work, view: ctx.view, kind, detail: String(detail).slice(0, 500) });
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.setDefaultTimeout(30_000);

page.on("console", (msg) => {
  if (msg.type() === "error") record("console", msg.text());
  if (msg.type() === "warning" && /Warning:|deprecat|act\(/.test(msg.text())) record("console-warn", msg.text());
});
page.on("pageerror", (err) => record("pageerror", err.message + (err.stack ? " | " + err.stack.split("\n")[1] : "")));
page.on("requestfailed", (req) => record("requestfailed", `${req.method()} ${req.url()} :: ${req.failure()?.errorText}`));
page.on("response", (resp) => {
  const s = resp.status();
  if (s >= 400) {
    const u = resp.url();
    // 忽略与本应用无关的第三方/静态噪声
    if (u.includes("/api/") || u.startsWith(BASE) || u.includes(":8000")) {
      record(s >= 500 ? "http5xx" : "http4xx", `${s} ${resp.request().method()} ${u}`);
    }
  }
});

async function waitApp() {
  await page.waitForSelector(".ws-app", { state: "attached", timeout: 30000 });
  try { await page.evaluate(() => document.fonts.ready); } catch {}
  await page.waitForTimeout(500);
}

// 首跳前注入 api base，避免打到默认 8000 之外
await page.addInitScript((api) => {
  localStorage.setItem("novel-system-api-base", api);
  localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
}, API);

await page.goto(BASE);
await waitApp();

for (const work of WORKS) {
  ctx = { work, view: "(switch)" };
  await page.evaluate((wid) => localStorage.setItem("ws_active_work_v1", wid), work);
  await page.reload();
  await waitApp();

  for (const view of VIEWS) {
    ctx = { work, view };
    const before = findings.length;
    try {
      await page.evaluate((h) => { location.hash = "#" + h; }, view);
      await page.waitForTimeout(1100);
      try { await page.waitForLoadState("networkidle", { timeout: 4000 }); } catch {}
      await page.waitForTimeout(300);

      // 可见错误态 / 空白渲染检查
      const probe = await page.evaluate((keywords) => {
        const content = document.querySelector(".ws-content") || document.body;
        const text = (content.innerText || "").trim();
        const hits = keywords.filter(k => text.includes(k));
        // React 错误边界 / 崩溃常见标志
        const boundary = /Something went wrong|出错了|页面崩溃|render error/i.test(text);
        return { len: text.length, hits, boundary, sample: text.slice(0, 160) };
      }, ERR_KEYWORDS);

      if (probe.len < 40 && !["writer"].includes(view)) {
        record("blank-or-thin", `content text length=${probe.len} sample="${probe.sample}"`);
      }
      if (probe.hits.length) record("visible-error-text", `keywords=[${probe.hits.join(",")}] sample="${probe.sample}"`);
      if (probe.boundary) record("error-boundary", probe.sample);

      await page.screenshot({ path: path.join(OUT, "shots", `${work}__${view}.png`) });
    } catch (e) {
      record("crawl-exception", e.message.split("\n")[0]);
    }
    const added = findings.length - before;
    console.log(`[${work}] ${view.padEnd(11)} (+${added} findings)`);
  }
}

// ---- 只读交互探针（不污染数据）----
ctx = { work: WORKS[0], view: "interaction" };
try {
  await page.evaluate((wid) => localStorage.setItem("ws_active_work_v1", wid), WORKS[0]);
  await page.evaluate(() => { location.hash = "#home"; });
  await page.reload();
  await waitApp();

  // 1) 作品切换器能打开并列出作品
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw", { timeout: 8000 });
  const switcher = await page.textContent(".ws-wsw-list");
  if (!switcher || switcher.length < 4) record("interaction", "作品切换器列表为空");
  await page.keyboard.press("Escape").catch(() => {});

  // 2) 命令面板（Ctrl+K）能打开
  await page.keyboard.press("Control+KeyK").catch(() => {});
  await page.waitForTimeout(500);
  const paletteOpen = await page.evaluate(() => !!document.querySelector(".ws-palette, [class*='palette']"));
  if (!paletteOpen) record("interaction", "命令面板 Ctrl+K 未打开（可能未绑定）");
  await page.keyboard.press("Escape").catch(() => {});
} catch (e) {
  record("interaction", "交互探针异常: " + e.message.split("\n")[0]);
}

await browser.close();

// ---- 汇总 ----
const byKind = {};
for (const f of findings) byKind[f.kind] = (byKind[f.kind] || 0) + 1;

// 去重（同 work+view+kind+detail）
const seen = new Set();
const uniq = findings.filter(f => {
  const k = `${f.work}|${f.view}|${f.kind}|${f.detail}`;
  if (seen.has(k)) return false; seen.add(k); return true;
});

fs.writeFileSync(path.join(OUT, "findings.json"), JSON.stringify({ base: BASE, api: API, works: WORKS, total: findings.length, unique: uniq.length, byKind, findings: uniq }, null, 2));

// Markdown 摘要
let md = `# QA Round 1 — 浏览器巡检findings\n\n- BASE=${BASE} API=${API}\n- works=${WORKS.join(", ")}\n- 总计 ${findings.length}（去重 ${uniq.length}）\n\n## 按类型\n\n`;
for (const [k, v] of Object.entries(byKind).sort((a, b) => b[1] - a[1])) md += `- ${k}: ${v}\n`;
md += `\n## 明细（去重）\n\n| work | view | kind | detail |\n|---|---|---|---|\n`;
for (const f of uniq) md += `| ${f.work} | ${f.view} | ${f.kind} | ${f.detail.replace(/\|/g, "\\|").slice(0, 200)} |\n`;
fs.writeFileSync(path.join(OUT, "findings.md"), md);

console.log("\n==== 汇总 ====");
console.log(JSON.stringify(byKind, null, 2));
console.log(`unique findings: ${uniq.length} -> ${path.join(OUT, "findings.json")}`);
process.exitCode = 0;
