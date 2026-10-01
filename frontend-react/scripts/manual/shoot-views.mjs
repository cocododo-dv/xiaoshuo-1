// 手动工具（不在任何自动化 lane 里）：对 React 工作台逐视图截图 + 捕获 console/page 错误。
// 运行：node frontend-react/scripts/manual/shoot-views.mjs [BASE] [OUTDIR] [API] [WORK]
// （Playwright 由 frontend-react 自己锁定，可从任意工作目录启动）
// 作者的前端占着 5174、后端占着 8000：做探针时另起端口的前端，API 指向库副本 / 夹具的后端。默认与契约 E2E 相同
// （React 5176、后端 8009），浏览器上下文里一律掐断 127.0.0.1:8000——不会碰到作者的开发栈。
import path from "node:path";
import fs from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5176/";
const OUT = process.argv[3] || path.resolve("react-shots");
const API = process.argv[4] || "http://127.0.0.1:8009"; // 写进 novel-system-api-base
const WORK = process.argv[5] || null; // 可选：先切到这部作品（ws_active_work_v1）
const LIVE_API_PATTERN = /^https?:\/\/(?:127\.0\.0\.1|localhost):8000(?:[/?#]|$)/;
if (LIVE_API_PATTERN.test(`${API}/`)) throw new Error(`API=${API} 是作者开发栈的后端：截图探针要另起后端`);
fs.mkdirSync(OUT, { recursive: true });

// 与 src/ws-nav.js 的页面一致；高级页（章节编排起）打开时会自动切到高级模式
const VIEWS = [
  "home", "snowflake", "writer", "styleref", "review", "library",
  "author", "scene", "manuscripts", "quality", "cost", "settings", "trash",
];

const errors = [];
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
await context.route(LIVE_API_PATTERN, (route) => route.abort());
const page = await context.newPage();
page.setDefaultTimeout(60_000);
page.on("console", (msg) => { if (msg.type() === "error") errors.push(`[console] ${msg.text()}`); });
page.on("pageerror", (err) => errors.push(`[pageerror] ${err.message}`));

async function waitApp() {
  await page.waitForSelector(".ws-app", { state: "attached" });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(600);
}

// 首个导航前注入 api base，避免首跳打到默认 8000（lib/client.js 两个键都在、且不相等时才认回环覆盖）
await page.addInitScript((api) => {
  if (!localStorage.getItem("novel-system-api-base") || localStorage.getItem("novel-system-api-base") !== api) {
    localStorage.setItem("novel-system-api-base", api);
    localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
  }
}, API);
await page.goto(BASE);
await waitApp();

if (WORK) {
  await page.evaluate((wid) => localStorage.setItem("ws_active_work_v1", wid), WORK);
  await page.reload();
  await waitApp();
}

for (let i = 0; i < VIEWS.length; i++) {
  const v = VIEWS[i];
  await page.evaluate((hash) => { location.hash = hash; }, "#" + v);
  await page.waitForTimeout(900);
  await page.screenshot({ path: path.join(OUT, `${String(i + 1).padStart(2, "0")}-${v}.png`) });
  console.log("shot", v, `(errors so far: ${errors.length})`);
}

await browser.close();
if (errors.length) {
  console.log(`\n==== ${errors.length} errors ====`);
  const uniq = [...new Set(errors)];
  for (const e of uniq.slice(0, 40)) console.log(" -", e.slice(0, 400));
  process.exitCode = 1;
} else {
  console.log("\nno console/page errors");
}
