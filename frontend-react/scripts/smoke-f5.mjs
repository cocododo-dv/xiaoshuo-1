// FE-ALIGN F5 冒烟：参考书库接 style_reference v2。
// 覆盖：multipart 导入 → 书库渲染真实书 → LLM 关启动抽取得到明确引导 → 删除。
// 运行：cd frontend && node ../frontend-react/scripts/smoke-f5.mjs [BASE] [API]
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5174/";
const API = process.argv[3] || "http://127.0.0.1:8009";
let failed = 0;
const errors = [];
const dialogs = [];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.setDefaultTimeout(20_000);
page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
page.on("dialog", (d) => { dialogs.push(d.message()); d.accept(); });

async function check(label, fn) {
  try { await fn(); console.log("ok:", label); }
  catch (e) { failed++; console.log("FAIL:", label, "—", e.message.split("\n")[0]); }
}

const TITLE = "冒烟样书-" + Date.now().toString(36);
// 正文掺入唯一标题：book_id 由内容 checksum 决定，跨轮同文会撞「已存在」
const SAMPLE = `【${TITLE}】\n\n` + Array.from({ length: 40 }, (_, i) =>
  `第${i + 1}段。潮水在夜里退去，露出一行脚印。她数着脚印往前走，每一步都比上一步更接近那句没人认领的对不起。机器声很轻，轻到能听见显影液滴落的回响。`).join("\n\n");

await page.goto(BASE);
await page.evaluate((apiBase) => {
  localStorage.clear();
  localStorage.setItem("novel-system-api-base", apiBase);
  localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
}, API);
await page.reload();
await page.waitForSelector(".ws-app");
await page.waitForTimeout(1500);

let bookId = null;

await check("① 没有 LLM：导入直接 409（严格 LLM，2026-09-15：没有启发式兜底）", async () => {
  const outcome = await page.evaluate(async ({ apiBase, title, text }) => {
    const fd = new FormData();
    fd.append("file", new Blob([text], { type: "text/plain" }), "sample.txt");
    fd.append("title", title);
    fd.append("cloud_policy", "local_only");
    const res = await fetch(`${apiBase}/api/v2/style-reference/books/import-upload`, {
      method: "POST", headers: { "X-Idempotency-Key": "smoke-sr-" + Date.now() }, body: fd,
    });
    const body = await res.json();
    return { status: res.status, code: body.error && body.error.code, action: body.error && body.error.details && body.error.details.author_action };
  }, { apiBase: API, title: TITLE, text: SAMPLE });
  if (outcome.status !== 409 || outcome.code !== "STYLE_REFERENCE_LLM_REQUIRED") throw new Error(JSON.stringify(outcome));
  if (!outcome.action || outcome.action.view !== "systemConfig") throw new Error("author_action missing");
});

await check("② 书库视图：没有书时给空态，不再有演示书", async () => {
  await page.evaluate(async () => { await window.srSyncBooks(); });
  await page.evaluate(() => { location.hash = "#styleref"; });
  await page.waitForTimeout(1500);
  const text = await page.evaluate(() => document.body.innerText);
  if (text.includes("呐喊 · 短篇集")) throw new Error("demo books still shown");
  if (!text.includes("参考书库")) throw new Error("style reference view not rendered");
});

await browser.close();
const uniq = [...new Set(errors)];
if (uniq.length) { console.log(`\n${uniq.length} page errors:`); uniq.slice(0, 10).forEach(e => console.log(" -", e.slice(0, 300))); }
process.exitCode = failed || uniq.length ? 1 : 0;
console.log(failed ? `\n${failed} checks failed` : "\nall checks passed");
