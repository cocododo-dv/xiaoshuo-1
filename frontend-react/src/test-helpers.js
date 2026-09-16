// 单测共享底座：给 mock 后的 lib/client.js 装一个「按 URL 路由」的 apiGet，
// 让各 store 在 import 期触发的拉取（projects / catalog / trash / review / audit）
// 都拿到确定性形状的后端数据。写动词（apiPost/apiPatch/apiDelete）默认 resolve，
// 具体用例再按需 mockRejectedValueOnce 制造失败以验证回滚/告警。
//
// 注意：本文件不被 vitest include（无 .test/.spec 后缀），仅作纯函数工具被各 spec 引入。
// 它从不 import lib/client.js —— 由调用方把「已 mock 的 client 模块实例」传进来，
// 避免和 vi.mock 的 hoist/resetModules 语义打架。

/** 默认作品：返回一个中性真实作品，使 WsWorks 由 __loading__ 占位切到真实激活作品。 */
export const DEFAULT_PROJECT = {
  project_id: "prj-main",
  title: "北岸手记",
  genre: "悬疑",
  synopsis_line: "一桩跨越三代的旧案。",
  is_demo: false,
  target_word_count: 100000,
  stats: { words_total: 38000, words_today: 0, streak_days: 3 },
};

/** 默认目录：一章一场，形状对齐后端 catalog 端点（slug/chapter_id/scene_id/brief）。 */
export const DEFAULT_CHAP = {
  slug: "ch01",
  chapter_id: "c1",
  no: "01",
  title: "盐场的早班",
  state: "writing",
  act: "act1",
  tension: 0.3,
  pov: "示例POV",
  current: true,
  words: { cur: 0, target: 4000 },
  scenes: [
    {
      slug: "ch01s1",
      scene_id: "s1",
      title: "交班",
      kind: "proactive",
      state: "todo",
      words: 0,
      brief: { goal: "替父亲点名", conflict: "老工人欲言又止", setback: "有人喊错姓" },
    },
  ],
};

/** 默认回收站条目：一条软删场景。 */
export const DEFAULT_TRASH = {
  id: "scene:s9",
  kind: "scene",
  title: "被删的场景",
  removed_at: "2026-06-01T00:00:00Z",
  restorable: true,
};

/** 默认收件箱卡片（优先处理）。 */
export const DEFAULT_REVIEW_CARD = {
  id: "rv1",
  kind: "decision",
  priority: 1,
  title: "需要你拍板：第 3 章结尾",
  where: "第 3 章",
  source: "质检",
  detail: "两个结尾候选，挑一个。",
  occurred_at: "2026-06-08T00:00:00Z",
  live: false,
  actions: [{ label: "知道了", intent: "quiet", op: "resolve" }],
};

/** 默认审计 finding（设定漂移，未裁决）。 */
export const DEFAULT_FINDING = {
  finding_id: "f1",
  kind: "drift",
  status: "open",
  text: "道具材质前后不一致",
  evidence: JSON.stringify({ subject: "道具材质", value: "黄铜", source: "ch03" }),
  updated_at: "2026-06-08T00:00:00Z",
};

/**
 * 给已 mock 的 client 模块装 URL 路由。
 * @param {object} client - 由调用方 `await import("./lib/client.js")` 得到的 mock 模块。
 * @param {object} [opts] - 覆盖默认数据：{ projects, catalog, trash, reviewOpen, reviewSnoozed, findings, snowflakeWorkspace }。
 */
export function installApiRouter(client, opts = {}) {
  const projects = opts.projects ?? [DEFAULT_PROJECT];
  const catalog = opts.catalog ?? [DEFAULT_CHAP];
  const trash = opts.trash ?? [];
  const reviewOpen = opts.reviewOpen ?? [];
  const reviewSnoozed = opts.reviewSnoozed ?? [];
  const findings = opts.findings ?? [];
  const snowWorkspace = opts.snowflakeWorkspace ?? {};

  client.apiGet.mockImplementation((url) => {
    if (url === "/api/v2/projects") return Promise.resolve({ items: projects });
    if (/\/api\/v2\/projects\/[^/]+\/catalog(\?|$)/.test(url)) return Promise.resolve({ chapters: catalog });
    if (/\/api\/v2\/projects\/[^/]+\/snowflake-workspace(\?|$)/.test(url)) return Promise.resolve(snowWorkspace);
    if (url.includes("/writing-stats")) return Promise.resolve({ words_total: 38000, words_today: 0, streak_days: 3 });
    if (url.includes("/dashboard")) return Promise.resolve({});
    if (url.startsWith("/api/v2/trash")) return Promise.resolve({ items: trash });
    if (url.includes("/review-items")) {
      return Promise.resolve({ items: url.includes("state=snoozed") ? reviewSnoozed : reviewOpen });
    }
    if (url.includes("/longform/audit")) return Promise.resolve({ findings });
    return Promise.resolve({});
  });
  client.apiPost.mockResolvedValue({});
  client.apiPatch.mockResolvedValue({});
  // vi.mock 的模块代理对工厂没定义的导出会在「访问」时抛错（不是返回 undefined）：
  // 只有工厂里给了 apiPut 的 spec（ws-snow-sync 的要点编辑）才重置它，其余 spec 不碰。
  if (Object.prototype.hasOwnProperty.call(client, "apiPut") && typeof client.apiPut === "function") client.apiPut.mockResolvedValue({});
  client.apiDelete.mockResolvedValue({});
}
