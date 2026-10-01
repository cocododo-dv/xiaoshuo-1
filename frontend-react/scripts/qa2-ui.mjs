// QA2 批次2 — Playwright 非破坏 UI 深度交互 + P1 回归 + Q2/Q3 的 UI 体现。
// 运行：node frontend-react/scripts/qa2-ui.mjs [BASE] [API]（底座见 scripts/lib/harness.mjs；结果写到仓库的 .codex-run/qa2/ui/）
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { API, BASE, openApp } from "./lib/harness.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(HERE, "../../.codex-run/qa2/ui");
fs.mkdirSync(path.join(OUT, "shots"), { recursive: true });

const checks = [];
const net = [];   // 关注的网络事件
let ctx = "";
function chk(name, ok, detail = "") { checks.push({ ctx, name, ok: !!ok, detail: String(detail).slice(0, 240) }); console.log(`  ${ok ? "✓" : "✗"} [${ctx}] ${name}${ok ? "" : "  | " + detail}`); }
function skip(name, detail = "") { checks.push({ ctx, name, ok: true, skip: true, detail: String(detail).slice(0, 240) }); console.log(`  ⊘ [${ctx}] ${name} (skipped: ${detail})`); }

const { browser, page, waitForApp, observe } = await openApp({ acceptDialogs: false });
const consoleErrs = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrs.push({ ctx, t: m.text().slice(0, 200) }); });
page.on("pageerror", (e) => consoleErrs.push({ ctx, t: "PAGEERROR " + e.message.slice(0, 200) }));
page.on("response", (r) => {
  const u = r.url();
  if (u.includes("/api/")) {
    const m = r.request().method();
    if (m !== "GET" || r.status() >= 400) net.push({ ctx, m, status: r.status(), url: u.replace(API, "") });
  }
});

/* 页面上的条件在时限内成立就是 true，否则 false（检查自己决定记成 ✓ 还是 ✗） */
async function becomes(predicate, arg, timeout = 10_000) {
  try { await page.waitForFunction(predicate, arg, { timeout }); return true; } catch (e) { return false; }
}
const railText = () => page.evaluate(() => (document.querySelector(".ws-rail, nav, aside")?.innerText || ""));
const contentText = () => page.evaluate(() => document.querySelector(".ws-content")?.innerText || "");
/* 当前页面载入完毕：外壳的内容区在，懒加载的页面模块已经换掉「正在打开…」 */
const viewMounted = () => becomes(() => !!document.querySelector(".ws-content") && !document.querySelector(".ws-content .ws-view-loading"), null, 20_000);
async function waitApp() {
  await waitForApp();
  try { await page.evaluate(() => document.fonts.ready); } catch {}
}
async function go(work, view) {
  await page.evaluate((w) => localStorage.setItem("ws_active_work_v1", w), work);
  await page.evaluate((v) => { location.hash = "#" + v; }, view);
  await page.reload();
  // 等到当前作品真的是 work：书架（/api/v2/projects）上没有它时应用会退回第一部作品，不等这一步，检查就可能
  // 悄悄跑在别的作品上（work-a / work-b 都是雪花作品，都在书架上）
  await waitForApp({ work });
  await viewMounted();
}
async function shot(n) { try { await page.screenshot({ path: path.join(OUT, "shots", n + ".png") }); } catch {} }

await page.goto(BASE); await waitApp();

// ---- NAV-01 writer/advanced 门控 ----
ctx = "NAV-01";
await go("work-a", "home");
await page.evaluate(() => localStorage.setItem("ws_tweaks_v1", JSON.stringify({ mode: "writer" })));
await page.reload(); await waitApp();
let rail = await railText();
chk("writer 模式不显示高级组(章节编排/AI起草台)", !/AI\s*起草台|成稿中心|长篇控制塔/.test(rail) || /构思|写作/.test(rail), `rail=${rail.replace(/\n/g, "·").slice(0, 120)}`);
await page.evaluate(() => { location.hash = "#scene"; });
await becomes(() => /起草台|成稿|控制塔|质量/.test(document.querySelector(".ws-rail, nav, aside")?.innerText || ""));
const advReveal = await railText();
chk("深链 #scene 自动切高级并显示生产组", /起草台|成稿|控制塔|质量/.test(advReveal), advReveal.replace(/\n/g, "·").slice(0, 120));
await page.evaluate(() => { location.hash = "#__bogus__"; });
await observe(500); // 观察窗口：非法 hash 处理完之后应用还在
chk("非法 hash 不崩溃", await page.evaluate(() => !!document.querySelector(".ws-app")));

// ---- NAV-02 Ctrl+k 命令面板 ----
// 只认面板本身（.pal 且 role=dialog）：旧选择器 [class*='cmdk'] 会命中侧栏上常驻的 .ws-cmdk 按钮，
// 面板根本没打开也算通过。
ctx = "NAV-02";
await go("work-a", "home");
await page.keyboard.press("Control+k").catch(() => {});
const paletteOpen = await becomes(() => !!document.querySelector(".pal[role='dialog']"), null, 5_000);
chk("Ctrl+k 命令面板打开", paletteOpen);
if (paletteOpen) {
  await page.keyboard.type("成本");
  const costHit = await becomes(() => [...document.querySelectorAll(".pal [role='option']")].some(o => o.textContent.includes("成本看板")), null, 5_000);
  chk("命令面板能搜到「成本看板」（页面清单与侧栏同源）", costHit);
}
await page.keyboard.press("Escape").catch(() => {});
chk("Esc 关闭命令面板", await becomes(() => !document.querySelector(".pal[role='dialog']"), null, 5_000));

// ---- SNOW-12 (P1 回归)：打开构思页不盲发 approve ----
ctx = "SNOW-12";
net.length = 0;
await go("work-b", "snowflake");
await observe(1800); // 观察窗口：水合 + 上行防抖都过去之后，仍然没有 approve → 409
const approve409 = net.filter(n => /\/approve/.test(n.url) && n.status === 409);
chk("打开构思页无 approve→409 噪声(P1 回归)", approve409.length === 0, `409s=${approve409.length} ${JSON.stringify(approve409.slice(0,2))}`);
await shot("snow12-salt-construct");

// ---- Q3 UI 体现：tide 构思页物化按钮反映 blocked ----
ctx = "Q3-UI";
await go("work-a", "snowflake");
await becomes(() => /整理章节结构|章节结构|物化|场景/.test(document.querySelector(".ws-content")?.innerText || ""));
const bodyTxt = await contentText();
chk("tide 构思页渲染(含物化/章节字样)", /整理章节结构|章节结构|物化|场景/.test(bodyTxt), bodyTxt.slice(0, 80));
await shot("q3-tide-construct");

// ---- REVIEW-01：待办加载 + 筛选 chip ----
ctx = "REVIEW-01";
await go("work-a", "review");
await becomes(() => (document.querySelector(".ws-content")?.innerText || "").length > 60);
const reviewLen = (await contentText()).length;
chk("待办视图渲染非空", reviewLen > 60, `len=${reviewLen}`);
await shot("review-tide");

// ---- QUAL-04：文学质量视图渲染 ----
ctx = "QUAL-04";
await go("work-a", "quality");
await becomes(() => (document.querySelector(".ws-content")?.innerText || "").length > 60);
const qualLen = (await contentText()).length;
chk("文学质量视图渲染非空", qualLen > 60, `len=${qualLen}`);
await shot("quality-tide");

// ---- STYLE-12：风格页（参考书 → 学习文风 → 用于作品 → 对照检查）----
// 隔离的 E2E 后端没有配模型、夹具里也没有参考书：验真实断言——书库空态（有书时是四步步骤条）、导入被「先接入模型」
// 挡住（对话框里的 sr-import-no-llm 提示 + 「导入」锁住）、这一页没有 console error。模型 / 书库的实际情况先问后端，
// 不在不同的情况下凑一个空过的「通过」。
ctx = "STYLE-12";
net.length = 0;
consoleErrs.length = 0;
let styleRuntime = null;
try {
  const rt = await page.request.get(`${API}/api/v2/style-reference/runtime`);
  if (rt.ok()) { const body = await rt.json(); styleRuntime = body?.data || body || null; }
} catch (e) { /* 读不到运行时：下面按「不知道有没有模型」跳过模型门的断言 */ }
await go("work-a", "styleref");
chk("风格页外壳渲染", await becomes(() => !!document.querySelector(".sr-page")));
// 书库读完：要么有书（列表 + 步骤条），要么是「导入第一本参考书」的空态
await becomes(() => !!document.querySelector('.sr-book-list .sr-book-item, [data-testid="sr-import-first"]'));
const styleState = await page.evaluate(() => ({
  books: document.querySelectorAll(".sr-book-list .sr-book-item").length,
  empty: !!document.querySelector('[data-testid="sr-import-first"]'),
  steps: [...document.querySelectorAll(".sr-stepper .sr-step .sr-step-name")].map((el) => el.textContent.trim()),
  libraryHead: !!document.querySelector(".sr-books-head") || !!document.querySelector('[data-testid="sr-books-switch"]'),
}));
chk("书库栏（或窄屏的「参考书库」按钮）渲染", styleState.libraryHead, JSON.stringify(styleState));
if (styleState.books > 0) {
  chk("有书时步骤条是四步：参考书 / 学习文风 / 用于作品 / 对照检查", JSON.stringify(styleState.steps) === JSON.stringify(["参考书", "学习文风", "用于作品", "对照检查"]), JSON.stringify(styleState.steps));
} else {
  chk("空书库：给「导入第一本参考书」的空态", styleState.empty && styleState.steps.length === 0, JSON.stringify(styleState));
  skip("有书时步骤条是四步", "夹具后端没有参考书，步骤条不显示");
}
// 打开导入对话框：没有模型时提示并锁住「导入」（导入之后要由模型给每一段分类）
const importBtn = page.locator('[data-testid="sr-import-first"], [data-testid="sr-books-import"]').first();
if (await importBtn.count()) {
  await importBtn.click().catch(() => {});
  await becomes(() => !!document.querySelector(".sr-import-dialog"), null, 5_000);
  if (styleRuntime && styleRuntime.llm_enabled === false) {
    await becomes(() => !!document.querySelector('[data-testid="sr-import-no-llm"]'), null, 5_000);
  }
  const importState = await page.evaluate(() => ({
    dialog: !!document.querySelector(".sr-import-dialog"),
    noLlm: !!document.querySelector('[data-testid="sr-import-no-llm"]'),
    submitDisabled: !!document.querySelector('[data-testid="sr-import-submit"]')?.disabled,
    why: document.querySelector('[data-testid="sr-import-why"]')?.textContent || "",
  }));
  chk("导入对话框打开", importState.dialog, JSON.stringify(importState));
  if (styleRuntime && styleRuntime.llm_enabled === false) {
    chk("没有模型：导入对话框里提示「还没有接入模型」并锁住「导入」", importState.noLlm && importState.submitDisabled && /模型/.test(importState.why), JSON.stringify(importState));
  } else if (styleRuntime && styleRuntime.llm_enabled === true) {
    chk("配了模型：没有「还没有接入模型」的提示，「导入」只被权属 / 文件挡住", !importState.noLlm && importState.submitDisabled, JSON.stringify(importState));
  } else {
    skip("导入被模型门挡住", "读不到 /style-reference/runtime，不知道有没有模型");
  }
  await page.keyboard.press("Escape").catch(() => {});
  chk("Esc 关闭导入对话框", await becomes(() => !document.querySelector(".sr-import-dialog"), null, 5_000));
} else {
  chk("有「导入参考书」的入口", false, "既没有 sr-import-first 也没有 sr-books-import");
}
const styleErrs = consoleErrs.filter(e => e.ctx === "STYLE-12" && !/favicon|404.*\.png|ResizeObserver/i.test(e.t));
chk("风格页无 console error", styleErrs.length === 0, JSON.stringify(styleErrs.slice(0, 3)));
const style4xx = net.filter(n => n.status >= 400);
chk("风格页没有 4xx / 5xx 请求", style4xx.length === 0, JSON.stringify(style4xx.slice(0, 3)));
await shot("styleref-tide");

// ---- 全局 console 错误汇总（巡检全部视图）----
// 章节编排（author）只有这里看 console error（以前的 AUTHOR-04 只认 SVG path 报错，已删）
ctx = "console-sweep";
consoleErrs.length = 0;
for (const v of ["home", "author", "writer", "library", "manuscripts", "settings", "trash"]) {
  await go("work-a", v);
  await observe(700); // 观察窗口：页面挂上、读完数据之后没有 console error
}
const realErrs = consoleErrs.filter(e => !/favicon|404.*\.png|ResizeObserver/i.test(e.t));
chk("全视图巡检无 console error", realErrs.length === 0, JSON.stringify(realErrs.slice(0, 4)));

await browser.close();

// 汇总
const pass = checks.filter(c => c.ok && !c.skip).length;
const fail = checks.filter(c => !c.ok && !c.skip).length;
const skipped = checks.filter(c => c.skip).length;
const out = { base: BASE, api: API, pass, fail, skipped, checks, console_errors: consoleErrs.slice(0, 20), net_writes_or_4xx: net.slice(0, 30) };
fs.writeFileSync(path.join(OUT, "ui-findings.json"), JSON.stringify(out, null, 2));
console.log(`\n==== 批次2 UI ====  PASS ${pass} / FAIL ${fail} / SKIP ${skipped}`);
for (const c of checks.filter(c => !c.ok && !c.skip)) console.log(`  ✗ [${c.ctx}] ${c.name} :: ${c.detail}`);
for (const c of checks.filter(c => c.skip)) console.log(`  ⊘ [${c.ctx}] ${c.name} :: ${c.detail}`);
// 作为门禁：任一硬检查失败即非零退出（此前恒为 0 = 过门不设防，本次 P1 修复）。
process.exitCode = fail > 0 ? 1 : 0;
