// Phase 1 验收辅助：对 React 工程逐视图截图 + 捕获 console/page 错误。
// 运行：cd frontend && node ../frontend-react/scripts/shoot-views.mjs <BASE> <OUTDIR>
// （Playwright 由 frontend-react 自己锁定，可从任意工作目录启动）
import path from "node:path";
import fs from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5174/";
const OUT = process.argv[3] || path.resolve("react-shots");
const API = process.argv[4] || null; // 可选：覆盖 novel-system-api-base（默认 8000）
fs.mkdirSync(OUT, { recursive: true });

const VIEWS = [
  "home", "snowflake", "writer", "styleref", "review", "library",
  "author", "scene", "manuscripts", "longform", "index", "interop", "settings", "trash",
];

const errors = [];
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.setDefaultTimeout(60_000);
page.on("console", (msg) => { if (msg.type() === "error") errors.push(`[console] ${msg.text()}`); });
page.on("pageerror", (err) => errors.push(`[pageerror] ${err.message}`));

async function waitApp() {
  await page.waitForSelector(".ws-app", { state: "attached" });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(600);
}

if (API) {
  // 首个导航前注入 api base，避免首跳打到默认 8000
  await page.addInitScript((api) => {
    if (!localStorage.getItem("novel-system-api-base") || localStorage.getItem("novel-system-api-base") !== api) {
      localStorage.setItem("novel-system-api-base", api);
      localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
    }
  }, API);
}
await page.goto(BASE);
await waitApp();

for (const id of ["salt", "tide"]) {
  await page.evaluate((wid) => localStorage.setItem("ws_active_work_v1", wid), id);
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
