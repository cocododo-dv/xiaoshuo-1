// Phase 7 验收冒烟：待办收件箱的服务端 dedupe（同一事项重复触发只有一张卡）。
// 运行：node frontend-react/scripts/smoke-phase7.mjs [BASE] [API]（底座见 scripts/lib/harness.mjs）
import { api, openApp, send } from "./lib/harness.mjs";

const { check, resetSession, finish } = await openApp();

await resetSession({ work: "work-a" });

await check("onceTask：同一事项重复触发只有一张卡", async () => {
  const create = () => send("POST", "/api/v1/review-items", {
    project_id: "work-a",
    kind: "qc",
    priority: 2,
    title: "补铺垫：第二组脚印",
    source: "章节编排",
    where: "第 9 章",
    dedupe_key: "task:backfill:ch09:脚印",
  }, { keyPrefix: "p7-task" });
  const first = await create();
  const second = await create();
  if (first.status !== 200 || second.status !== 200) throw new Error(`create status: ${first.status}/${second.status}`);
  if (first.body.data.deduped !== false || second.body.data.deduped !== true) {
    throw new Error(`dedupe flags: ${first.body.data.deduped}/${second.body.data.deduped}`);
  }
  const items = (await api("/api/v1/review-items?state=open&project_id=work-a")).items;
  if (items.filter(i => i.dedupe_key === "task:backfill:ch09:脚印").length !== 1) throw new Error("duplicate task cards");
});

await finish();
