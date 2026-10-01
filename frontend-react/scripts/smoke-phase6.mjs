// Phase 6 验收冒烟：资料库（实体/关系/时间线/编辑落库/idea 卡入库）。
// 运行：node frontend-react/scripts/smoke-phase6.mjs [BASE] [API]（底座见 scripts/lib/harness.mjs）
import { api, openApp, send, waitUntil } from "./lib/harness.mjs";

const { page, check, resetSession, goView, waitForText, finish } = await openApp();

const characterName = async (id) => ((await api("/api/v2/projects/work-a/library")).characters.find(x => x.character_id === id) || {}).name;

/* 资料库 store 改名（LIB_persist：PATCH 落库后重读） */
async function renameCharacter(id, name) {
  const ok = await page.evaluate(async ({ entryId, next }) => {
    const { LIB_persist } = await window.__wsStores.load("LIB_persist");
    return LIB_persist({ [entryId]: { name: next } });
  }, { entryId: id, next: name });
  if (!ok) throw new Error(`LIB_persist(${id}) 没有保存成功`);
}

await resetSession({ work: "work-a" });

await check("资料库来自后端（人物/世界/大事记齐全）", async () => {
  await goView("library");
  // 资料库是懒加载的路由，它的 store 在页面挂上、第一个订阅者到来时才去后端读：冷启动（模块编译）加读取要 2 s 上下，
  // 以前固定等 1.8 s 偶尔落空（下一项「人物改名」随之找不到条目）。这里等 store 真的读到条目。
  const counts = await waitUntil(() => page.evaluate(async () => {
    const { libLive } = await window.__wsStores.load("libLive");
    const all = libLive().entries;
    if (!all.length) return null;
    return {
      people: all.filter(e => e.cat === "people").length,
      world: all.filter(e => e.cat === "world").length,
      events: all.filter(e => e.cat === "events").length,
      linCen: all.find(e => e.name === "角色甲") || null,
    };
  }), { message: "资料库 store 没有读到条目" });
  if (counts.people < 5) throw new Error(`people: ${counts.people}`);
  if (counts.world < 5) throw new Error(`world: ${counts.world}`);
  if (counts.events < 2) throw new Error(`events: ${counts.events}`);
  if (!counts.linCen || !counts.linCen.facts.length) throw new Error("角色甲 facts missing");
  if (!counts.linCen.links.length) throw new Error("角色甲 links missing");
  try {
    await waitForText("角色甲");
  } catch (e) {
    throw new Error("library view missing 角色甲");
  }
});

await check("人物改名落库（character_id 不变）", async () => {
  await renameCharacter("lin-cen", "角色甲·改");
  try {
    await waitUntil(async () => (await characterName("lin-cen")) === "角色甲·改");
  } catch (e) {
    throw new Error(`name: ${await characterName("lin-cen")}`);
  }
  // 还原
  await renameCharacter("lin-cen", "角色甲");
  await waitUntil(async () => (await characterName("lin-cen")) === "角色甲", { message: "name not restored" });
});

await check("建关系 → 关系图投影即时可见", async () => {
  const graphBefore = await api("/api/v2/projects/work-a/library/graph");
  await send("POST", "/api/v2/projects/work-a/library/relations", {
    from_ref: "character:a-ke", to_ref: "entity:old-archive", kind: "works_at", note: "P6 冒烟新边",
  }, { keyPrefix: "p6-rel" });
  const graphAfter = await api("/api/v2/projects/work-a/library/graph");
  if (graphAfter.edges.length !== graphBefore.edges.length + 1) throw new Error("edge not added");
  if (!graphAfter.edges.some(e => e.note === "P6 冒烟新边")) throw new Error("edge note missing");
});

await check("时间线按章排序数据完整", async () => {
  const timeline = await api("/api/v2/projects/work-a/library/timeline");
  if (timeline.items.length < 2) throw new Error(`events: ${timeline.items.length}`);
  if (!timeline.items.every(e => e.label)) throw new Error("labels missing");
});

await check("idea 卡「确认入库」→ 实体进库进图（D5 半自动）", async () => {
  await send("POST", "/api/v1/review-items", {
    project_id: "work-a", kind: "idea", priority: 2,
    title: "发现新地点：盐雾灯塔", source: "资料派生", where: "成稿归档 · 模拟",
    dedupe_key: "derive:p6smoke:盐雾灯塔",
    actions: [
      { label: "确认入库", intent: "primary", op: "resolve", effect: { type: "create_entity", name: "盐雾灯塔", kind: "location", summary: "P6 冒烟生成" } },
      { label: "忽略", intent: "quiet", op: "resolve" },
    ],
  }, { keyPrefix: "p6-idea" });
  // 在收件箱里点确认
  await goView("review");
  await page.click('.rv-item:has-text("盐雾灯塔")');
  await page.click('.rv-item:has-text("盐雾灯塔") button:has-text("确认入库")');
  await waitUntil(async () => (await api("/api/v2/projects/work-a/library")).entities.some(e => e.name === "盐雾灯塔"), {
    message: "entity not created",
  });
  const graph = await api("/api/v2/projects/work-a/library/graph");
  if (!graph.nodes.some(n => n.name === "盐雾灯塔")) throw new Error("node missing in graph");
});

await finish();
