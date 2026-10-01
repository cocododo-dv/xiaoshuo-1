// Phase 4 验收冒烟：回收站（三级软删 + 整体恢复）。
// 前置：React dev 5176 + 后端（参数2，默认 8009，已注入中性测试夹具）。
// 运行：node frontend-react/scripts/smoke-phase4.mjs [BASE] [API]（底座见 scripts/lib/harness.mjs）
import { api, openApp, send, waitUntil } from "./lib/harness.mjs";

const { page, check, resetSession, goView, finish } = await openApp();

const projectIds = async () => ((await api("/api/v2/projects")).items || []).map(i => i.project_id);

await resetSession({ work: "work-a" });

await check("删整部书（样例短卷）→ 切换器消失", async () => {
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  await page.click('.ws-wsw-item:has-text("样例短卷") .ws-wsw-del');
  // 删除作品的确认是应用内确认框（ws-notify.jsx），不是浏览器 confirm
  await page.click('[data-testid="ws-confirm-ok"]');
  await waitUntil(async () => !(await projectIds()).includes("work-b"), { message: "still in backend list" });
  await page.keyboard.press("Escape");
  await page.click(".ws-brand");
  await page.waitForSelector(".ws-wsw");
  try {
    await page.waitForFunction(() => !(document.querySelector(".ws-wsw-list")?.textContent || "").includes("样例短卷"));
  } catch (e) {
    throw new Error("still in switcher");
  } finally {
    await page.keyboard.press("Escape");
  }
});

await check("回收站可见整部条目 → 恢复 → 数据无损", async () => {
  try {
    await goView("trash", 'tr:has-text("样例短卷")');
  } catch (e) {
    throw new Error("trash view missing the work");
  }
  await page.click('tr:has-text("样例短卷") button:has-text("恢复")');
  await waitUntil(async () => (await projectIds()).includes("work-b"), { message: "not restored in backend" });
  const tree = await api("/api/v2/projects/work-b/catalog");
  if (tree.chapters.length !== 3) throw new Error(`salt chapters: ${tree.chapters.length}`);
  const stats = await api("/api/v2/projects/work-b/writing-stats");
  if (stats.words_total <= 0) throw new Error("stats lost");
});

await check("删场景 → 回收站条目 → 恢复（正文保留）", async () => {
  await goView("home", ".hm-title");
  const sceneId = await page.evaluate(async () => {
    const { WsCatalog } = await window.__wsStores.load("WsCatalog");
    const ch = WsCatalog.get()[7]; // tide ch08
    const victim = ch.scenes[ch.scenes.length - 1];
    WsCatalog.removeScene(ch.id, victim.sid);
    return victim.backendId;
  });
  await waitUntil(async () => ((await api("/api/v2/trash?project_id=work-a")).items || []).some(i => i.id === `scene:${sceneId}`), {
    message: "scene entry missing in trash",
  });
  await goView("trash");
  await page.click('tbody tr:has-text("场景") button:has-text("恢复")');
  await waitUntil(async () => (await api("/api/v2/projects/work-a/catalog")).chapters[7].scenes.some(s => s.scene_id === sceneId), {
    message: "scene not restored",
  });
});

await check("永久清除后无残影", async () => {
  // 建一部临时书 → 删 → 永久清除
  const created = await send("POST", "/api/v2/projects", { title: "P4临时书", outline_text: "临时" }, { keyPrefix: "p4-smoke" });
  const pid = created.body.data.project.project_id;
  await send("DELETE", `/api/v2/projects/${pid}`, undefined, { keyPrefix: "p4-del" });
  await send("DELETE", `/api/v2/trash/work:${pid}`, undefined, { keyPrefix: "p4-purge" });
  if ((await projectIds()).includes(pid)) throw new Error("project survived purge");
  const trash = await api("/api/v2/trash");
  if (trash.items.some(i => i.id === `work:${pid}`)) throw new Error("trash entry survived purge");
});

await finish();
