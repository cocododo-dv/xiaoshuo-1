// Phase 3 验收冒烟：目录统一（catalog API 唯一真相源）。
// 前置：React dev 5176 + 后端（参数2，默认 8009，已注入中性测试夹具）。
// 运行：node frontend-react/scripts/smoke-phase3.mjs [BASE] [API]（底座见 scripts/lib/harness.mjs）
import { api, openApp, waitUntil } from "./lib/harness.mjs";

const { page, check, resetSession, goView, finish } = await openApp({ acceptDialogs: false });

const catalog = () => api("/api/v2/projects/work-a/catalog");
const chapter8 = async () => (await catalog()).chapters[7];

await resetSession({ work: "work-a" });

await check("目录来自后端（tide 10 章，含戏剧卡）", async () => {
  const res = await page.evaluate(async () => {
    const { WsCatalog } = await window.__wsStores.load("WsCatalog");
    const chs = WsCatalog.get();
    const ch1 = chs[0];
    const first = ch1 && ch1.scenes[0];
    return {
      n: chs.length, title: ch1 && ch1.title, spine: ch1 && ch1.drama && ch1.drama.spine,
      sid: first && first.sid, backendId: first && first.backendId, legacySid: first && first.legacySid,
    };
  });
  if (res.n !== 10) throw new Error(`chapters: ${res.n}`);
  if (res.title !== "样章01") throw new Error(`title: ${res.title}`);
  if (!res.spine) throw new Error("drama.spine missing");
  // 阶段 X：场景 sid = 稳定的 scene_id（身份跟着行走）；位置式旧 slug 只留作 legacySid
  if (!res.sid || res.sid !== res.backendId) throw new Error(`sid: ${res.sid} / ${res.backendId}`);
  if (res.legacySid !== "ch01s1") throw new Error(`legacySid: ${res.legacySid}`);
});

await check("主页（dashboard 兜底 + 目录同源）渲染", async () => {
  const title = await page.textContent(".hm-title");
  if (!title.includes("样例长卷")) throw new Error(title);
  const body = await page.textContent(".hm-top");
  if (!body.includes("章")) throw new Error("progress missing");
});

await check("编排台改章题 → 后端落库 + 主页一致", async () => {
  // 第 1 章已批准并锁定；通过编排台编辑当前进行中的第 8 章，覆盖真实 UI 保存路径。
  // 等后端落库要在 Node 侧轮询：page.waitForFunction 不等异步谓词（以前这里当场就返回，偶尔读到改名前的章题）。
  await goView("author", '.arr-card:has-text("样章08")');
  await page.click('.arr-card:has-text("样章08")');
  const titleInput = page.locator('input[aria-label="章节标题"]');
  await titleInput.fill("P3改名·样章08");
  await titleInput.press("Enter");
  try {
    await waitUntil(async () => (await chapter8()).title === "P3改名·样章08");
  } catch (e) {
    throw new Error(`backend title: ${(await chapter8()).title}`);
  }
  await goView("home", ".hm-chaps");
  try {
    await page.waitForFunction(() => (document.querySelector(".hm-chaps")?.textContent || "").includes("P3改名·样章08"));
  } catch (e) {
    throw new Error("home title not refreshed");
  }
  // 仍经编排台还原，避免后续检查继承临时标题。
  await goView("author", 'input[aria-label="章节标题"]');
  await page.fill('input[aria-label="章节标题"]', "样章08");
  await page.press('input[aria-label="章节标题"]', "Enter");
  try {
    await waitUntil(async () => (await chapter8()).title === "样章08");
  } catch (e) {
    throw new Error(`restore: backend title ${(await chapter8()).title}`);
  }
});

await check("写作器加场景 → 后端可见", async () => {
  const before = (await chapter8()).scenes.length;
  await page.evaluate(async () => {
    const { WsCatalog } = await window.__wsStores.load("WsCatalog");
    const cur = WsCatalog.get()[7];
    WsCatalog.addScene(cur.id, "P3冒烟新场景");
  });
  let after = null;
  try {
    after = await waitUntil(async () => {
      const ch = await chapter8();
      return ch.scenes.length === before + 1 ? ch : null;
    });
  } catch (e) {
    throw new Error(`scenes: ${before} -> ${(await chapter8()).scenes.length}`);
  }
  if (!after.scenes.some(s => s.title === "P3冒烟新场景")) throw new Error("new scene title missing");
});

await check("删场景 → v1 trash 生效（后端目录消失）", async () => {
  // 目录 store 先重读到新场景的后端 id（乐观新建时它还是临时 sid）
  await waitUntil(() => page.evaluate(async () => {
    const { WsCatalog } = await window.__wsStores.load("WsCatalog");
    const victim = WsCatalog.get()[7].scenes.find(s => s.title === "P3冒烟新场景");
    return !!(victim && victim.backendId);
  }), { message: "目录 store 没有重读到新场景" });
  await page.evaluate(async () => {
    const { WsCatalog } = await window.__wsStores.load("WsCatalog");
    const cur = WsCatalog.get()[7];
    const victim = cur.scenes.find(s => s.title === "P3冒烟新场景");
    WsCatalog.removeScene(cur.id, victim.sid);
  });
  await waitUntil(async () => !(await chapter8()).scenes.some(s => s.title === "P3冒烟新场景"), { message: "scene still present" });
});

await check("写作器正文保存 → words rollup 全链路后端", async () => {
  const statsBefore = await api("/api/v2/projects/work-a/writing-stats");
  await goView("writer", ".wr-editor, [contenteditable=true]");
  // 直接经 WrDocs 保存（等价于编辑器自动保存路径）
  await page.evaluate(async () => {
    const { WsCatalog, WrDocs } = await window.__wsStores.load("WsCatalog", "WrDocs");
    const w = WsCatalog.writingScene();
    await WrDocs.save(w.scene.sid, `<p>样例正文第一段：占位句。</p><p>样例正文第二段：占位句。</p><p>这是 P3 冒烟新增的第三段，用来验证字数增量上报。本轮标记：${Date.now().toString(36)}</p>`);
  });
  await waitUntil(async () => (await api("/api/v2/projects/work-a/writing-stats")).words_total !== statsBefore.words_total, {
    message: "words_total unchanged",
  });
  await waitUntil(async () => {
    const sc = (await chapter8()).scenes.find(s => s.state === "writing");
    return !!(sc && sc.words > 0);
  }, { message: "scene words_current not updated" });
});

await check("跨会话正文水合（清缓存重载后编辑器有服务端正文）", async () => {
  await resetSession({ work: "work-a" });
  await goView("writer", ".wr-editor, [contenteditable=true]");
  try {
    await page.waitForFunction(() => {
      const el = document.querySelector(".wr-editor, [contenteditable=true]");
      return !!el && el.innerText.includes("P3 冒烟新增的第三段");
    });
  } catch (e) {
    const text = await page.evaluate(() => { const el = document.querySelector(".wr-editor, [contenteditable=true]"); return el ? el.innerText : ""; });
    throw new Error(`editor text: ${text.slice(0, 80)}…`);
  }
});

await finish();
