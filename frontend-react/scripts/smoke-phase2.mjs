// Phase 2 验收冒烟：WsWorks 接真（列表/创建/profile/统计 来自后端）。
// 前置：React dev 5176 + 后端（参数2，默认 8009，已注入中性测试夹具）。
// 运行：node frontend-react/scripts/smoke-phase2.mjs [BASE] [API]（底座见 scripts/lib/harness.mjs）
import { api, openApp, send, waitUntil } from "./lib/harness.mjs";

const { page, check, resetSession, finish } = await openApp({ acceptDialogs: false });

// 干净起步：清掉缓存影子，等书架从后端读回来
await resetSession();

await check("书架来自后端（两部 demo 种子）", async () => {
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  const list = await page.textContent(".ws-wsw-list");
  if (!list.includes("样例长卷") || !list.includes("样例短卷")) throw new Error(`switcher: ${list.slice(0, 120)}`);
  await page.keyboard.press("Escape");
});

await check("切换器进度数字来自 writing-stats（2.7 万字）", async () => {
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  const row = await page.textContent('.ws-wsw-row:has-text("样例长卷")');
  if (!row.includes("2.7 万字")) throw new Error(`row: ${row}`);
  await page.keyboard.press("Escape");
});

await check("新建作品落库（换会话仍在）", async () => {
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  await page.click(".ws-wsw-new");
  await page.waitForSelector(".ws-nw");
  await page.fill(".ws-nw-input", "P2冒烟之书");
  await page.click(".ws-nw-foot .btn-accent");
  // 等 POST 落库、书架换上正式 id（临时作品不算数）
  const row = await waitUntil(async () => ((await api("/api/v2/projects")).items || []).find(i => i.title === "P2冒烟之书"), {
    message: "created work not in backend",
  });
  await waitUntil(() => page.evaluate(async (id) => {
    const { WsWorks } = await window.__wsStores.load("WsWorks");
    return WsWorks.activeId() === id;
  }, row.project_id), { message: "新作品没有拿到正式 id" });
  // 模拟"换浏览器"：清空本地缓存影子，仅保留 api base，重载后从后端取
  await resetSession();
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  const list = await page.textContent(".ws-wsw-list");
  if (!list.includes("P2冒烟之书")) throw new Error("created work missing after cache reset");
  await page.keyboard.press("Escape");
});

await check("demo 作品主页正常渲染（本地目录种子 + 服务端统计）", async () => {
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  await page.click('.ws-wsw-row:has-text("样例长卷")');
  try {
    await page.waitForFunction(() => (document.querySelector(".hm-title") || {}).textContent?.includes("样例长卷"));
  } catch (e) {
    throw new Error(`title: ${await page.textContent(".hm-title").catch(() => "")}`);
  }
});

await check("档案更新走 PATCH profile（改简介后端可读回）", async () => {
  const synopsisOf = async () => (((await api("/api/v2/projects")).items || []).find(i => i.project_id === "work-b") || {}).synopsis_line;
  await page.evaluate(async () => {
    const { WsWorks } = await window.__wsStores.load("WsWorks");
    WsWorks.update("work-b", { sub: "P2 冒烟改写的简介" });
  });
  try {
    await waitUntil(async () => (await synopsisOf()) === "P2 冒烟改写的简介");
  } catch (e) {
    throw new Error(`synopsis_line: ${await synopsisOf()}`);
  }
  // 还原，避免污染 demo（seed 重跑也会复位）
  await page.evaluate(async () => {
    const { WsWorks } = await window.__wsStores.load("WsWorks");
    WsWorks.update("work-b", { sub: "样例作品乙：用于测试与端到端验证的短篇结构样例，正文与设定均为占位文本。" });
  });
  await waitUntil(async () => (await synopsisOf()) !== "P2 冒烟改写的简介", { message: "synopsis_line not restored" });
});

// 清理：本轮与历史泄漏的「P2冒烟之书」软删 + 回收站彻底清除（残留会污染共享 dev 库）
try {
  for (const w of ((await api("/api/v2/projects")).items || []).filter(i => i.title === "P2冒烟之书")) {
    await send("DELETE", `/api/v2/projects/${w.project_id}`, undefined, { keyPrefix: "p2-clean" });
    await send("DELETE", `/api/v2/trash/${encodeURIComponent("work:" + w.project_id)}`, undefined, { keyPrefix: "p2-purge" });
  }
} catch (e) { /* 清理失败不影响结论 */ }

await finish();
