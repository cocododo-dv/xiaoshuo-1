// 契约级 E2E 冒烟的共用底座：run-smokes.mjs 依次跑的各套用例（smoke-*.mjs、qa2-ui.mjs）都从这里起浏览器、
// 灌 API 地址、拿 store、记检查结果。单独跑一套：node frontend-react/scripts/<套名>.mjs [BASE] [API]
//   BASE 默认 http://127.0.0.1:5176/ —— scripts/verify_react_e2e.* 起的 React 开发服务器（`npm run dev`），从不是作者的 5174；
//   API  默认 http://127.0.0.1:8009  —— 已注入中性测试夹具的隔离后端（python tests/fixture_runtime.py），从不是作者的 8000。
//
// · openApp()：起 Chromium 和一个浏览器上下文——
//     – 作者开发栈的后端 127.0.0.1:8000（连着作者的真库）在上下文里一律掐断：哪怕前端的构建期默认地址还是 8000，
//       冒烟也只打隔离后端；API 自己指向 8000 时直接拒绝运行；
//     – 每次文档加载（含 reload）之前写好 API 地址的两个 localStorage 键：novel-system-api-base = API、
//       novel-system-api-base-default = 8000（lib/client.js 只在两个键都在、且不相等时才认本机的回环覆盖）；
//     – 记下 pageerror；按套件需要接受浏览器原生对话框（应用里的确认框是 ws-notify 的，另点「确定」）。
// · resetSession({ work })：清空本机存储（API 地址照旧）、可选地设好当前作品，重载并等应用装好（waitForApp）。
// · store：页面里经 window.__wsStores（src/ws-test-seam.js，只在开发服务器上有）拿——
//     page.evaluate(async () => { const { WsCatalog } = await window.__wsStores.load("WsCatalog"); … })
// · 等状态，不等时间：waitUntil(probe) 在 Node 侧轮询，直到 probe() 给出真值。异步条件不要交给 page.waitForFunction：
//   异步谓词返回的 Promise 本身就是真值，Playwright 只求值这一次、不再轮询，等这个 Promise 落定就带着落定的值返回
//   （哪怕是 false；条件不成立也不会接着等到超时，只有这一次求值本身超过时限才超时）——以前 phase3「编排台改章题」
//   等后端落库就栽在这上面：只等了一趟 fetch，就带着当时读到的章题往下走。各套件原来每轮约 79 s 的固定
//   waitForTimeout 也换成等页面元素 / store / 后端数据（phase6 资料库的间歇失败就是固定时长赶不上懒加载路由的
//   冷编译）。只有「这段时间里不该出现什么」的观察窗口还是定长的（observe）。
// · check(label, fn)：一条检查，失败只记数、打印首行，后面的照跑；finish()：关浏览器、列出页面错误、设退出码。
// · reseedFixtures()：重灌中性测试夹具（run-smokes.mjs 在每套之前调；单跑一套之前想要干净的夹具也可以调）。
import { spawnSync } from "node:child_process";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

export const BASE = process.argv[2] || "http://127.0.0.1:5176/";
export const API = (process.argv[3] || "http://127.0.0.1:8009").replace(/\/+$/, "");
/* 作者开发栈的后端：冒烟一律不碰 */
export const LIVE_API = "http://127.0.0.1:8000";
const LIVE_API_PATTERN = /^https?:\/\/(?:127\.0\.0\.1|localhost):8000(?:[/?#]|$)/;

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const BACKEND_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../backend");

/* 重灌中性测试夹具：backend/tests/fixture_runtime.py（解释器取 NOVEL_SYSTEM_PYTHON，否则 PATH 上的 python）。
   各套用例都会改动夹具数据（裁决 / 插场 / 改题），套与套之间重灌保独立性。失败抛错，带上脚本的输出。 */
export function reseedFixtures() {
  const python = process.env.NOVEL_SYSTEM_PYTHON || "python";
  const r = spawnSync(python, ["tests/fixture_runtime.py"], {
    cwd: BACKEND_DIR,
    env: { ...process.env, PYTHONPATH: "src" },
    stdio: "pipe",
  });
  if (r.error || r.status !== 0) {
    const detail = String(r.stderr || r.stdout || r.error || "unknown reseed failure").slice(0, 2000);
    throw new Error(`fixture reseed failed (exit=${r.status ?? "spawn-error"}): ${detail}`);
  }
}

/* Node 侧轮询，直到 probe() 给出真值并返回它；超时抛错，消息里带最后一次的值。 */
export async function waitUntil(probe, { timeout = 20_000, interval = 100, message = "等待超时" } = {}) {
  const startedAt = Date.now();
  let last;
  for (;;) {
    last = await probe();
    if (last) return last;
    if (Date.now() - startedAt > timeout) {
      const shown = last === undefined ? "" : `（最后一次：${JSON.stringify(last)}）`.slice(0, 300);
      throw new Error(`${message}：${timeout} ms 内没等到${shown}`);
    }
    await sleep(interval);
  }
}

/* 读隔离后端（GET），返回信封里的 data */
export async function api(path) {
  const response = await fetch(API + path);
  return (await response.json()).data;
}

/* 写隔离后端：带 X-Idempotency-Key（keyPrefix + 随机后缀）的 JSON 请求 → { status, body } */
export async function send(method, path, body, { keyPrefix = "smoke" } = {}) {
  const response = await fetch(API + path, {
    method,
    headers: { "Content-Type": "application/json", "X-Idempotency-Key": `${keyPrefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}` },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let parsed = null;
  try { parsed = await response.json(); } catch (e) { parsed = null; }
  return { status: response.status, body: parsed };
}

export async function openApp({ acceptDialogs = true, viewport = { width: 1600, height: 1000 } } = {}) {
  if (LIVE_API_PATTERN.test(`${API}/`)) {
    throw new Error(`API=${API} 是作者开发栈的后端：冒烟只跑隔离后端（默认 http://127.0.0.1:8009）`);
  }
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport });
  await context.route(LIVE_API_PATTERN, (route) => route.abort());
  await context.addInitScript(({ api: apiBase, live }) => {
    try {
      localStorage.setItem("novel-system-api-base", apiBase);
      localStorage.setItem("novel-system-api-base-default", live);
    } catch (e) { /* about:blank 之类没有存储的文档 */ }
  }, { api: API, live: LIVE_API });
  const page = await context.newPage();
  page.setDefaultTimeout(20_000);
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  if (acceptDialogs) page.on("dialog", (dialog) => dialog.accept());
  let failed = 0;

  /* 应用装好：外壳在、书架读到了（指定 work 时当前作品就是它）、当前作品的目录读到了 */
  async function waitForApp({ work = null, timeout = 30_000 } = {}) {
    await page.waitForSelector(".ws-app", { state: "attached", timeout });
    const seam = await page.evaluate(() => !!window.__wsStores);
    if (!seam) throw new Error("页面上没有 window.__wsStores：冒烟要跑在 `npm run dev` 的开发服务器上（src/ws-test-seam.js 只在开发态装）");
    await waitUntil(() => page.evaluate(async (expected) => {
      const { WsWorks, WsCatalog } = await window.__wsStores.load("WsWorks", "WsCatalog");
      if (WsWorks.status().projects.phase !== "ready") return false;
      const active = WsWorks.activeId();
      if (expected && active !== expected) return false;
      return !active || WsCatalog.ready();
    }, work), { timeout, message: "应用没有装好（书架 / 当前作品 / 目录）" });
  }

  /* 清空本机存储（API 地址的两个键由初始化脚本补回）、可选地设好当前作品，重载并等应用装好 */
  async function resetSession({ work = null } = {}) {
    if (!/^https?:/.test(page.url())) await page.goto(BASE);
    await page.evaluate(({ apiBase, live, workId }) => {
      localStorage.clear();
      localStorage.setItem("novel-system-api-base", apiBase);
      localStorage.setItem("novel-system-api-base-default", live);
      if (workId) localStorage.setItem("ws_active_work_v1", workId);
    }, { apiBase: API, live: LIVE_API, workId: work });
    await page.reload();
    await waitForApp({ work });
  }

  /* 换页：改 hash，等这一页的根元素出现 */
  async function goView(view, readySelector) {
    await page.evaluate((hash) => { location.hash = hash; }, `#${view}`);
    if (readySelector) await page.waitForSelector(readySelector);
  }

  /* 等页面正文里出现这些字（全部），超时报出缺哪几个 */
  async function waitForText(texts, { timeout = 20_000, root = "body" } = {}) {
    const wanted = Array.isArray(texts) ? texts : [texts];
    try {
      await page.waitForFunction(({ sel, items }) => {
        const el = document.querySelector(sel);
        const text = el ? el.innerText || el.textContent || "" : "";
        return items.every((item) => text.includes(item));
      }, { sel: root, items: wanted }, { timeout });
    } catch (e) {
      const text = await page.evaluate((sel) => { const el = document.querySelector(sel); return el ? el.innerText || el.textContent || "" : ""; }, root);
      const missing = wanted.filter((item) => !text.includes(item));
      throw new Error(`页面上没等到：${missing.join("、")}`);
    }
  }

  /* 观察窗口：检查「这段时间里不该出现什么」（错误、多余的请求）时才用的定长等待 */
  const observe = (ms) => page.waitForTimeout(ms);

  async function check(label, fn) {
    try { await fn(); console.log("ok:", label); }
    catch (e) { failed += 1; console.log("FAIL:", label, "—", String((e && e.message) || e).split("\n")[0]); }
  }

  async function finish() {
    await browser.close();
    const uniq = [...new Set(errors)];
    if (uniq.length) { console.log(`\n${uniq.length} page errors:`); uniq.slice(0, 10).forEach((e) => console.log(" -", e.slice(0, 300))); }
    process.exitCode = failed || uniq.length ? 1 : 0;
    console.log(failed ? `\n${failed} checks failed` : "\nall checks passed");
  }

  return { browser, context, page, errors, waitForApp, resetSession, goView, waitForText, observe, check, finish };
}
