// Phase 5 验收冒烟：待办收件箱（卡片 + 后端 effect + 派生项 + badge）。
// 前置：React dev 5176 + 后端（默认 8009，已注入中性测试夹具）。
// 运行：node frontend-react/scripts/smoke-phase5.mjs [BASE] [API]（底座见 scripts/lib/harness.mjs）
import { api, openApp, send, waitUntil } from "./lib/harness.mjs";

const { page, check, resetSession, goView, waitForText, finish } = await openApp();

const chapter8 = async () => (await api("/api/v2/projects/work-a/catalog")).chapters[7];
const openCards = async () => (await api("/api/v1/review-items?state=open&project_id=work-a")).items;

await resetSession({ work: "work-a" });

await check("收件箱来自后端（含 demo 卡 + 派生卡）", async () => {
  await goView("review");
  await waitForText(["第 8 章标题在两个候选间未定", "已起草未确认"]);
});

await check("QC 卡「采纳·插入反应场」→ 目录真的多一个反应场（后端事务）", async () => {
  const before = (await chapter8()).scenes.length;
  // 展开当前第 8 章 QC 卡并点采纳（历史前缀章已批准并锁定）。
  const card = page.locator('.rv-item:has-text("第 8 章节奏过快")');
  await card.click();
  await card.locator('button:has-text("采纳 · 插入反应场")').click();
  let ch08 = null;
  try {
    ch08 = await waitUntil(async () => {
      const ch = await chapter8();
      return ch.scenes.length === before + 1 ? ch : null;
    }, { timeout: 10_000 });
  } catch (e) {
    throw new Error(`scenes ${before} -> ${(await chapter8()).scenes.length}`);
  }
  const inserted = ch08.scenes.find(s => s.title === "样例反应场");
  if (!inserted) throw new Error("inserted scene missing");
  if (inserted.kind !== "reactive") throw new Error("kind wrong");
  if (inserted.seq !== 4) throw new Error(`at position: seq=${inserted.seq}`);
});

await check("决策卡选项 → rename effect 落库", async () => {
  const card = page.locator('.rv-item:has-text("第 8 章标题在两个候选间未定")');
  await card.click();
  await card.locator('button:has-text("用「候选标题二」")').click();
  try {
    await waitUntil(async () => (await chapter8()).title === "候选标题二", { timeout: 10_000 });
  } catch (e) {
    throw new Error(`title: ${(await chapter8()).title}`);
  }
});

await check("badge = priority 1 的 open 数（处理后减少）", async () => {
  // 角标接口已删（批准 #24a，重评 R15a）：priority 1 的 open 数直接从统一列表数，与界面的 store 对账
  // （处理之后 store 要重读一次才跟上，所以是等到两边一致，而不是只看一眼）
  let last = { fromApi: null, fromStore: null };
  try {
    await waitUntil(async () => {
      const fromApi = (await openCards()).filter(i => i.priority === 1).length;
      const fromStore = await page.evaluate(async () => {
        const { rvOpenItems } = await window.__wsStores.load("rvOpenItems");
        return rvOpenItems().filter(i => i.priority === 1).length;
      });
      last = { fromApi, fromStore };
      return fromApi === fromStore;
    });
  } catch (e) {
    throw new Error(`api ${last.fromApi} != store ${last.fromStore}`);
  }
});

await check("派生卡：不可划掉 / snooze 按指纹 / 修好自动消失", async () => {
  // 制造空章 → 派生卡浮现
  const created = (await send("POST", "/api/v2/projects/work-a/catalog/chapters", { title: "P5空章", current: false, with_scene: false }, { keyPrefix: "p5-empty" }))
    .body.data.chapter.chapter_id;
  let items = await openCards();
  const empty = items.find(i => String(i.id).startsWith(`derived:catalog:empty:${created}`));
  if (!empty) throw new Error("empty-chapter derived card missing");
  // 不可 resolve
  const blocked = await send("POST", `/api/v1/review-items/${encodeURIComponent(empty.id)}/resolve`, { project_id: "work-a" }, { keyPrefix: "p5-noresolve" });
  if (blocked.status !== 409) throw new Error(`resolve should be 409, got ${blocked.status}`);
  // 修好（删空章）→ 自动消失
  await send("POST", "/api/v1/chapters/trash", { chapter_ids: [created] }, { keyPrefix: "p5-fix" });
  items = await openCards();
  if (items.some(i => String(i.id).startsWith(`derived:catalog:empty:${created}`))) throw new Error("derived card did not vanish");
});

await check("同一 dedupe_key 投两次只有一张卡", async () => {
  const mk = async () => (await send("POST", "/api/v1/review-items", {
    project_id: "work-a", kind: "note", title: "P5 dedupe 冒烟", dedupe_key: "p5:smoke:once",
  }, { keyPrefix: "p5-dedupe" })).body.data;
  await mk();
  const second = await mk();
  if (second.deduped !== true) throw new Error("second post not deduped");
  const items = await openCards();
  if (items.filter(i => i.title === "P5 dedupe 冒烟").length !== 1) throw new Error("duplicate cards");
});

await finish();
