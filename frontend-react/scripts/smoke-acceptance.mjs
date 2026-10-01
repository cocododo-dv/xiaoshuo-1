// FE-ALIGN 全局验收冒烟：新建空白书全链路 + 跨会话持久 + 回收站往返。
// （验收①②：构思暂存本地→物化后全部业务数据进后端；清缓存重载后一切都在）
// 运行：node frontend-react/scripts/smoke-acceptance.mjs [BASE] [API]（底座见 scripts/lib/harness.mjs）
import { api, openApp, send, waitUntil } from "./lib/harness.mjs";

const { page, check, resetSession, goView, finish } = await openApp();
const TITLE = "验收之书-" + Date.now().toString(36); // 每轮唯一，避免撞上一轮残留

await resetSession();

let pid = null;

await check("① 新建空白书 → 落库", async () => {
  await page.evaluate(async (t) => {
    const { WsWorks } = await window.__wsStores.load("WsWorks");
    WsWorks.create({ title: t, mark: "验", accent: "slate" });
  }, TITLE);
  const row = await waitUntil(async () => ((await api("/api/v2/projects")).items || []).find(w => w.title === TITLE), {
    message: "project not in backend",
  });
  pid = row.project_id;
  // 新书拿到正式 id、成了当前作品，目录也读好了（下一步往它的目录里写）
  await waitUntil(() => page.evaluate(async (id) => {
    const { WsWorks, WsCatalog } = await window.__wsStores.load("WsWorks", "WsCatalog");
    return WsWorks.activeId() === id && WsCatalog.ready();
  }, pid), { message: "新书没有成为当前作品" });
});

await check("② 编排：建章建场 → 后端目录", async () => {
  await page.evaluate(async () => {
    const { WsCatalog } = await window.__wsStores.load("WsCatalog");
    const chs = WsCatalog.get();
    WsCatalog.set([...chs, { id: "tmp1", n: "01", title: "验收第一章", state: "writing", scenes: [
      { title: "开场", kind: "主动", state: "writing", goal: "目标", obstacle: "阻碍", turn: "挫折" },
    ] }]);
  });
  const tree = await waitUntil(async () => {
    const t = await api(`/api/v2/projects/${pid}/catalog`);
    return t.chapters.length && t.chapters[0].scenes.length ? t : null;
  }, { message: "chapter / scene not in the backend catalog" });
  if (tree.chapters.length !== 1) throw new Error(`chapters: ${tree.chapters.length}`);
  if (tree.chapters[0].title !== "验收第一章") throw new Error("title wrong");
  if (tree.chapters[0].scenes[0].title !== "开场") throw new Error("scene wrong");
  // 目录 store 重读到了后端给的场景 id（下一步按它存正文）
  await waitUntil(() => page.evaluate(async () => {
    const { WsCatalog } = await window.__wsStores.load("WsCatalog");
    const first = WsCatalog.get()[0];
    return !!(first && first.scenes[0] && first.scenes[0].backendId);
  }), { message: "目录 store 没有重读到新场景" });
});

let wordsAfterWrite = 0;
await check("③ 写作：正文写穿 author-drafts → 统计上涨", async () => {
  await page.evaluate(async () => {
    const { WsCatalog, WrDocs } = await window.__wsStores.load("WsCatalog", "WrDocs");
    const sid = WsCatalog.get()[0].scenes[0].sid;
    await WrDocs.save(sid, "<p>验收正文：潮水在夜里退去，露出一行脚印。这是真实保存到后端的一段话。</p>");
  });
  // 统计按保存增量记账（新场景的占位正文计入基线），只验「涨了且连更=1」
  const stats = await waitUntil(async () => {
    const s = await api(`/api/v2/projects/${pid}/writing-stats`);
    return s.words_total > 0 ? s : null;
  }, { message: "words_total did not grow" });
  if (stats.streak_days !== 1) throw new Error(`streak: ${stats.streak_days}`);
  wordsAfterWrite = stats.words_total;
});

await check("④ 待办：投递卡 → effect 改目录（后端事务闭环）", async () => {
  const tree = await api(`/api/v2/projects/${pid}/catalog`);
  const cid = tree.chapters[0].chapter_id;
  await send("POST", "/api/v1/review-items", {
    project_id: pid, kind: "decision", priority: 1, title: "验收：定章题",
    dedupe_key: "acc:rename", options: ["验收·定稿章题"],
    actions: [{ label: "用这个", intent: "primary", op: "resolve", effect: { type: "rename_chapter", chapter_id: cid, title: "验收·定稿章题" } }],
  }, { keyPrefix: "acc-card" });
  await goView("review");
  await page.click('.rv-item:has-text("验收：定章题")');
  await page.click('.rv-item:has-text("验收：定章题") button:has-text("用这个")');
  await waitUntil(async () => (await api(`/api/v2/projects/${pid}/catalog`)).chapters[0].title === "验收·定稿章题", {
    message: "chapter title not renamed by the card effect",
  });
});

await check("⑥ 跨会话：清缓存重载 → 目录/正文/统计/章题都在", async () => {
  await resetSession({ work: pid });
  // 本机缓存是空的：正文只能从服务端水合回来（WrDocs.hydrate 显式等这一次水合）
  const snap = await page.evaluate(async () => {
    const { WsCatalog, WrDocs } = await window.__wsStores.load("WsCatalog", "WrDocs");
    const chs = WsCatalog.get();
    const sid = chs[0] && chs[0].scenes[0] ? chs[0].scenes[0].sid : null;
    const doc = sid ? (await WrDocs.hydrate(sid)) || "" : "";
    return { title: chs[0] && chs[0].title, doc };
  });
  if (snap.title !== "验收·定稿章题") throw new Error(`title: ${snap.title}`);
  if (!snap.doc.includes("潮水在夜里退去")) throw new Error("doc not hydrated");
});

await check("⑦ 回收站：删整部 → 恢复 → 数据无损", async () => {
  await page.evaluate(async (p) => {
    const { WsWorks } = await window.__wsStores.load("WsWorks");
    WsWorks.remove(p);
  }, pid);
  await waitUntil(async () => !((await api("/api/v2/projects")).items || []).some(w => w.project_id === pid), {
    message: "still listed after remove",
  });
  await send("POST", `/api/v2/trash/${encodeURIComponent("work:" + pid)}/restore`, {}, { keyPrefix: "acc-restore" });
  const items = (await api("/api/v2/projects")).items;
  const row = items.find(w => w.project_id === pid);
  if (!row) throw new Error("not restored");
  if (row.stats.words_total !== wordsAfterWrite) throw new Error(`stats lost: ${row.stats.words_total} != ${wordsAfterWrite}`);
  const tree = await api(`/api/v2/projects/${pid}/catalog`);
  if (tree.chapters[0].title !== "验收·定稿章题") throw new Error("catalog lost");
});

// 清理：验收书不留库（软删 + 回收站彻底清除）
if (pid) {
  try {
    await send("DELETE", `/api/v2/projects/${pid}`, undefined, { keyPrefix: "acc-clean" });
    await send("DELETE", `/api/v2/trash/${encodeURIComponent("work:" + pid)}`, undefined, { keyPrefix: "acc-purge" });
  } catch (e) { /* 清理失败不影响结论 */ }
}

await finish();
