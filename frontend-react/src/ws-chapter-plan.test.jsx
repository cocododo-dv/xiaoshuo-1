// WsChapterPlan store 层单测（章节编排 LLM 规划，设计文档 2026-07-16 §7）：
// 蓝图读写形状、fallback 的 author_action 透传、apply 成功后目录重拉收敛、
// apply 失败（锁章 409）不动目录缓存且错误可观测。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
}));

// 同 ws-catalog.test：截断 SnowSync 对整张雪花视图的传递依赖
vi.mock("./ws-snow.jsx", () => ({ S2_BE_STEPS: [] }));

const T = { timeout: 5000, interval: 25 };

const ARCH_ROW = {
  row_id: "planning_arch_c1_abc",
  payload: {
    chapter_promise: "读者看到线索反噬提问者",
    escalation_path: ["证物指向家人", "证词逼她对峙"],
    reveal_plan: ["工牌名字被磨掉"],
    payoff_target: "她烧掉第一封信",
    character_shift: "旁观到介入",
    ending_question: "她还能信自己的档案吗",
  },
  created_by: "operator",
  llm_call_id: "llm_call_x",
  created_at: "2026-07-16T00:00:00Z",
  status: "active",
};

async function loadStore() {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  client.apiPut.mockResolvedValue({});
  const mod = await import("./ws-chapter-plan.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  return { mod, client };
}

const catalogGetCount = (client) =>
  client.apiGet.mock.calls.filter(([url]) => url === "/api/v2/projects/prj-main/catalog").length;

describe("WsChapterPlan（章节编排 LLM 规划 store）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("loadArchitecture 拉后端蓝图并适配为视图形状；空蓝图为 null", async () => {
    const { mod, client } = await loadStore();
    client.apiGet.mockImplementationOnce(() => Promise.resolve({ architecture: ARCH_ROW }));
    const arch = await mod.WsChapterPlan.loadArchitecture("c1");
    expect(client.apiGet).toHaveBeenCalledWith("/api/v2/projects/prj-main/catalog/chapters/c1/architecture");
    expect(arch.promise).toBe("读者看到线索反噬提问者");
    expect(arch.escalation.length).toBe(2);
    expect(arch.fromLlm).toBe(true);
    expect(mod.WsChapterPlan.snapshot("c1").arch.status).toBe("ready");

    client.apiGet.mockImplementationOnce(() => Promise.resolve({ architecture: null }));
    const empty = await mod.WsChapterPlan.loadArchitecture("c2");
    expect(empty).toBeNull();
    expect(mod.WsChapterPlan.snapshot("c2").arch.status).toBe("ready");
  });

  it("saveArchitecture 用后端字段名 PUT，成功后蓝图就地更新", async () => {
    const { mod, client } = await loadStore();
    client.apiPut.mockResolvedValueOnce({ architecture: { ...ARCH_ROW, created_by: "author", llm_call_id: null } });
    const view = {
      promise: "改写后的承诺",
      escalation: ["一", "", "二"],
      reveals: [],
      payoff: "p",
      shift: "s",
      endingQuestion: "q",
    };
    const saved = await mod.WsChapterPlan.saveArchitecture("c1", view);
    expect(client.apiPut).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapters/c1/architecture",
      {
        chapter_promise: "改写后的承诺",
        escalation_path: ["一", "二"],   // 空串被过滤
        reveal_plan: [],
        payoff_target: "p",
        character_shift: "s",
        ending_question: "q",
      },
    );
    expect(saved.fromLlm).toBe(false);
    expect(mod.WsChapterPlan.snapshot("c1").arch.data.createdBy).toBe("author");
  });

  it("generateArchitecture 离线 fallback：透传 author_action，不伪造蓝图", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockResolvedValueOnce({
      source: "fallback",
      architecture: null,
      author_action: { title: "需要先启用真实模型", target_view: "config" },
    });
    const result = await mod.WsChapterPlan.generateArchitecture("c1");
    expect(result).toBeNull();
    const snap = mod.WsChapterPlan.snapshot("c1");
    expect(snap.authorAction.target_view).toBe("config");
    expect(snap.arch.data).toBeNull();
  });

  it("requestFill 保存补丁/notes/gaps/dropped；fallback 标 offline", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockResolvedValueOnce({
      source: "llm",
      llm_call_id: "llm_1",
      patch: { drama: { spine: "旧工牌把调查推向父亲" }, scenes: [{ scene_id: "s1", set: { conflict: "祖父半夜起身" } }], append_scenes: [] },
      notes: [{ scene_id: "s1", field: "kind", suggestion: "建议反应场", reason: "太密" }],
      gaps: ["POV 无法推断"],
      dropped: [{ scene_id: "s1", field: "goal", reason: "field_not_empty" }],
      degraded_slots: ["snowflake_canon"],
    });
    const fill = await mod.WsChapterPlan.requestFill("c1", {});
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapters/c1/plan/fill",
      { mode: "fill" },
    );
    expect(fill.patch.scenes[0].set.conflict).toBe("祖父半夜起身");
    expect(fill.patch.drama.spine).toContain("旧工牌");
    expect(fill.dropped[0].reason).toBe("field_not_empty");
    expect(fill.offline).toBe(false);

    client.apiPost.mockResolvedValueOnce({
      source: "fallback",
      gaps: ["第 1 场：待补 conflict"],
      author_action: { target_view: "config" },
    });
    const offline = await mod.WsChapterPlan.requestFill("c1", {});
    expect(offline.offline).toBe(true);
    expect(offline.gaps[0]).toContain("conflict");
    expect(mod.WsChapterPlan.snapshot("c1").authorAction.target_view).toBe("config");
  });

  it("带 candidate 的 requestFill 走 adopt 模式", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockResolvedValueOnce({ source: "llm", patch: { scenes: [], append_scenes: [] }, notes: [], gaps: [], dropped: [] });
    const candidate = { label: "双场对撞", scene_plan: [] };
    await mod.WsChapterPlan.requestFill("c1", { candidate });
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapters/c1/plan/fill",
      { mode: "adopt", candidate },
    );
  });

  it("戏剧卡补丁会进入逐项确认，并按勾选结果回传 apply", async () => {
    const { mod: ui } = await loadStore();
    const rows = ui.cpPatchRows(
      {
        drama: { promise: "读者发现旧工牌指向父亲", spine: "调查转向家人" },
        scenes: [],
        append_scenes: [],
      },
      (id) => id,
    );
    expect(rows.map((row) => row.label)).toEqual([
      "章节戏剧卡 · 核心承诺",
      "章节戏剧卡 · 主线推进",
    ]);
    const patch = ui.cpRowsToPatch(rows, { [rows[0].key]: true, [rows[1].key]: false });
    expect(patch).toEqual({
      drama: { promise: "读者发现旧工牌指向父亲" },
      scenes: [],
      append_scenes: [],
    });
  });

  it("applyPatch 成功：记录 applied、清空已消费补丁、重拉目录收敛", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/plan/fill")) {
        return Promise.resolve({ source: "llm", patch: { drama: { spine: "推进" }, scenes: [{ scene_id: "s1", set: { conflict: "x" } }], append_scenes: [] }, notes: [], gaps: [], dropped: [] });
      }
      if (url.endsWith("/plan/apply")) {
        return Promise.resolve({ applied: { drama: 1, scenes: 1, appended: 1 }, skipped: [{ scene_id: "s1", field: "goal", reason: "field_not_empty" }], chapter: {} });
      }
      return Promise.resolve({});
    });
    await mod.WsChapterPlan.requestFill("c1", {});
    const before = catalogGetCount(client);
    const patch = { drama: { spine: "推进" }, scenes: [{ scene_id: "s1", set: { conflict: "x" } }], append_scenes: [] };
    const applied = await mod.WsChapterPlan.applyPatch("c1", patch);
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapters/c1/plan/apply",
      { patch },
    );
    expect(applied).toEqual({ drama: 1, scenes: 1, appended: 1, skipped: [{ scene_id: "s1", field: "goal", reason: "field_not_empty" }] });
    const snap = mod.WsChapterPlan.snapshot("c1");
    expect(snap.fill).toBeNull();
    await vi.waitFor(() => expect(catalogGetCount(client)).toBeGreaterThan(before), T);
  });

  it("applyPatch 失败（锁章 409）：错误可观测、目录不重拉、异常上抛", async () => {
    const { mod, client } = await loadStore();
    const lockError = Object.assign(new Error("approved chapter is locked"), { code: "APPROVED_CHAPTER_LOCKED" });
    client.apiPost.mockRejectedValueOnce(lockError);
    const before = catalogGetCount(client);
    await expect(
      mod.WsChapterPlan.applyPatch("c1", { scenes: [], append_scenes: [] }),
    ).rejects.toThrow("approved chapter is locked");
    const snap = mod.WsChapterPlan.snapshot("c1");
    expect(snap.action.error.code).toBe("APPROVED_CHAPTER_LOCKED");
    expect(snap.action.busy).toBe(false);
    expect(catalogGetCount(client)).toBe(before);
  });

  it("requestCandidates fallback：author_action 透传、candidates 置空", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockResolvedValueOnce({ source: "fallback", candidates: [], author_action: { target_view: "config" } });
    const result = await mod.WsChapterPlan.requestCandidates("c1", "更贴近家庭线");
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapters/c1/plan/candidates",
      { direction_hint: "更贴近家庭线" },
    );
    expect(result).toBeNull();
    expect(mod.WsChapterPlan.snapshot("c1").authorAction.target_view).toBe("config");
  });

  it("requestReview 保存 findings 与来源（fallback 规则版也可用）", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockResolvedValueOnce({
      source: "fallback",
      findings: [{ code: "BRIEF_INCOMPLETE", severity: "warn", scene_id: "s1", evidence: "缺三拍" }],
      author_action: { target_view: "config" },
    });
    const review = await mod.WsChapterPlan.requestReview("c1");
    expect(review.source).toBe("fallback");
    expect(review.findings[0].code).toBe("BRIEF_INCOMPLETE");
  });
});
