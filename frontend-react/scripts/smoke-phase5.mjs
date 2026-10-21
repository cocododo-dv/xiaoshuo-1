// Phase 5 验收冒烟：「等你处理」——2026-10-21 取代待办收件箱。
// 两类提醒都由前端从目录现算（src/ws-attention.js）：被管线停下的场、写齐了等你批准的章；列在主页的「等你处理」卡里，
// 数字挂在 AI 起草台 / 成稿中心的侧栏项上（作家模式的侧栏里没有这两页，合计挂在主页）。
// 夹具里没有等你批准的章（导入把当前章之前的章都定成了已批准），这里自己做一章：一章一场，写一段、提升为权威正文——
// 这一章就写齐了。被管线停下的场要真跑过管线才有（隔离后端不接模型），那一类由 vitest 覆盖（ws-attention / ws-home /
// ws-app-rail）。
// 前置：React dev 5176 + 后端（默认 8009，已注入中性测试夹具）。
// 运行：node frontend-react/scripts/smoke-phase5.mjs [BASE] [API]（底座见 scripts/lib/harness.mjs）
import { API, api, openApp, waitUntil } from "./lib/harness.mjs";

const { page, check, resetSession, goView, waitForText, finish } = await openApp();

const CARD = '[data-testid="home-attention"]';
const TITLE = "等你批准样章";
const PROSE = "<p>雨城的旧信在案卷里压了十年。</p>"; // 去掉标签与空白 14 字

/* 侧栏某一项上的数字（没有徽标是 null） */
const badge = (label) => page.evaluate((text) => {
  const item = [...document.querySelectorAll(".ws-item")]
    .find((node) => (node.querySelector(".ws-item-label") || {}).textContent === text);
  const mark = item && item.querySelector(".ws-item-badge");
  return mark ? mark.textContent : null;
}, label);

await resetSession({ work: "work-a" });

await check("夹具里没有等你处理的事：「等你处理」说清它管什么，侧栏不挂数字", async () => {
  await goView("home", CARD);
  await waitForText("现在没有等你处理的事", { root: CARD });
  if ((await badge("主页")) !== null) throw new Error("没有等你处理的事时主页不该挂数字");
});

await check("一章唯一的一场提升为权威正文 → 这一章写齐了：目录记上终稿字数，「等你处理」列出等你批准、主页挂 1", async () => {
  await page.evaluate((title) => window.__wsStores.load("WsCatalog").then(({ WsCatalog }) => {
    WsCatalog.set([...WsCatalog.get(), { id: "tmp-attn", n: "11", title, state: "writing", scenes: [
      { title: "样场", kind: "主动", state: "writing", goal: "目标", obstacle: "阻碍", turn: "挫折" },
    ] }]);
  }), TITLE);
  const sid = await waitUntil(() => page.evaluate(async (title) => {
    const { WsCatalog } = await window.__wsStores.load("WsCatalog");
    const chapter = WsCatalog.get().find((c) => c.title === title);
    const scene = chapter && chapter.scenes[0];
    return scene && scene.backendId ? scene.sid : null;
  }, TITLE), { message: "新章没有拿到后端的场景 id" });
  // 写作台「提升为权威正文」走的同一个 store 方法
  await page.evaluate(async ({ s, html }) => {
    const { WrDocs } = await window.__wsStores.load("WrDocs");
    await WrDocs.save(s, html);
    await WrDocs.promote(s);
  }, { s: sid, html: PROSE });
  const chapter = await waitUntil(async () => {
    const found = (await api("/api/v2/projects/work-a/catalog")).chapters.find((c) => c.title === TITLE);
    return found && found.scenes[0].work.has_final ? found : null;
  }, { message: "提升之后目录里这一场没有终稿" });
  if (chapter.scenes[0].words !== 14 || chapter.words.cur !== 14) {
    throw new Error(`字数：场 ${chapter.scenes[0].words} / 章 ${chapter.words.cur}`);
  }
  await page.evaluate(() => window.__wsStores.load("WsCatalog").then(({ WsCatalog }) => WsCatalog.refresh()));
  await waitForText(["等你批准", TITLE], { root: CARD });
  await waitUntil(async () => (await badge("主页")) === "1", { message: "作家模式下主页没有挂 1" });
});

await check("旧深链 #review 回到主页的「等你处理」", async () => {
  await goView("review");
  await waitUntil(() => page.evaluate(() => location.hash === "#home"), { message: "#review 没有落到主页" });
  await page.waitForSelector(CARD);
  await waitForText(TITLE, { root: CARD });
});

await check("点「等你批准」那一行 → 成稿中心落在那一章；高级模式下数字挂在成稿中心", async () => {
  await page.click(`${CARD} .home-todo-open:has-text("${TITLE}")`);
  await waitUntil(() => page.evaluate(() => location.hash === "#manuscripts"), { message: "没有去成稿中心" });
  await page.waitForSelector(".ms-reader-title");
  await waitUntil(async () => (await page.textContent(".ms-reader-title")) === TITLE, {
    message: "成稿中心没有落在那一章（默认会落在当前章）",
  });
  // 去了高级页面就切到高级模式：数字挂在成稿中心，主页不再挂
  await waitUntil(async () => (await badge("成稿中心")) === "1", { message: "成稿中心没有挂 1" });
  if ((await badge("主页")) !== null) throw new Error("高级模式下主页不该再挂数字");
});

await check("待办收件箱的接口已经退役（404）", async () => {
  const response = await fetch(`${API}/api/v1/review-items?state=open&project_id=work-a`);
  if (response.status !== 404) throw new Error(`GET /api/v1/review-items → ${response.status}`);
});

await finish();
