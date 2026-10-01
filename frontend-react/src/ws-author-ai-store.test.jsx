// WsAuthorAi store 层单测（章节编排的 AI 编排，设计文档 2026-07-16 §7）：
// 蓝图读写形状、没有模型时 409 的 author_action 透传（不再有 200 + fallback 的规则结果）与一键补全的待补清单、
// apply 成功后目录重拉收敛、apply 失败（锁章 409）不动目录缓存且错误可观测；最后是「AI 编排」卡与 AI 体检的界面。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter, settleActiveWork } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
}));

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
  const mod = await import("./ws-author-ai-store.js");
  await settleActiveWork("prj-main", T);
  return { mod, client };
}

const catalogGetCount = (client) =>
  client.apiGet.mock.calls.filter(([url]) => url === "/api/v2/projects/prj-main/catalog").length;

/* 没有可用模型：后端 409 CHAPTER_PLAN_LLM_NOT_CONFIGURED，details 里带 author_action */
const NO_MODEL = () => Object.assign(new Error("章节编排的 AI 需要先启用真实模型。"), {
  code: "CHAPTER_PLAN_LLM_NOT_CONFIGURED",
  status: 409,
  details: { node_id: "chapter_plan_review", author_action: { title: "需要先启用真实模型", message: "配置模型后再试。", target_view: "config", primary_button_label: "去系统配置" } },
});

describe("WsAuthorAi（章节编排的 AI 编排 store）", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("loadArchitecture 拉后端蓝图并适配为视图形状；空蓝图为 null", async () => {
    const { mod, client } = await loadStore();
    client.apiGet.mockImplementationOnce(() => Promise.resolve({ architecture: ARCH_ROW }));
    const arch = await mod.WsAuthorAi.loadArchitecture("c1");
    expect(client.apiGet).toHaveBeenCalledWith("/api/v2/projects/prj-main/catalog/chapters/c1/architecture");
    expect(arch.promise).toBe("读者看到线索反噬提问者");
    expect(arch.escalation.length).toBe(2);
    expect(arch.fromLlm).toBe(true);
    expect(arch.designChanged).toBeNull();
    expect(mod.WsAuthorAi.snapshot("c1").arch.status).toBe("ready");

    client.apiGet.mockImplementationOnce(() => Promise.resolve({ architecture: null }));
    const empty = await mod.WsAuthorAi.loadArchitecture("c2");
    expect(empty).toBeNull();
    expect(mod.WsAuthorAi.snapshot("c2").arch.status).toBe("ready");
  });

  it("作者写的蓝图留下来之后构思又改过：读回来的 design_changed 挂在蓝图上", async () => {
    const { mod, client } = await loadStore();
    const changed = { reason: "design_changed", at: "2026-09-30T08:00:00Z", message: "这份蓝图写好之后，构思或参考书又改过。" };
    client.apiGet.mockImplementationOnce(() => Promise.resolve({ architecture: { ...ARCH_ROW, llm_call_id: null, design_changed: changed } }));
    const arch = await mod.WsAuthorAi.loadArchitecture("c1");
    expect(arch.designChanged).toEqual(changed);
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
    const saved = await mod.WsAuthorAi.saveArchitecture("c1", view);
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
    expect(mod.WsAuthorAi.snapshot("c1").arch.data.createdBy).toBe("author");
  });

  it("generateArchitecture 没有模型：409 的 author_action 挂进桶、返回 null，不伪造蓝图、不算失败", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockRejectedValueOnce(NO_MODEL());
    const result = await mod.WsAuthorAi.generateArchitecture("c1");
    expect(result).toBeNull();
    const snap = mod.WsAuthorAi.snapshot("c1");
    expect(snap.authorAction.target_view).toBe("config");
    expect(snap.arch.data).toBeNull();
    expect(snap.action).toEqual({ busy: false, kind: null, error: null });
  });

  it("requestFill 保存补丁/notes/gaps/dropped；没有模型时不给补丁，改读待补清单（GET plan/gaps，按空槽列出）", async () => {
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
    const fill = await mod.WsAuthorAi.requestFill("c1", {});
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapters/c1/plan/fill",
      { mode: "fill" },
    );
    expect(fill.patch.scenes[0].set.conflict).toBe("祖父半夜起身");
    expect(fill.patch.drama.spine).toContain("旧工牌");
    expect(fill.dropped[0].reason).toBe("field_not_empty");
    expect(fill).not.toHaveProperty("offline");

    client.apiPost.mockRejectedValueOnce(NO_MODEL());
    const gapsUrl = "/api/v2/projects/prj-main/catalog/chapters/c1/plan/gaps";
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === gapsUrl
      ? Promise.resolve({ source: "rules", gaps: ["第 1 场：待补 conflict"] })
      : base(url)));
    const offline = await mod.WsAuthorAi.requestFill("c1", {});
    expect(offline).toBeNull();
    expect(client.apiGet).toHaveBeenCalledWith(gapsUrl);
    const snap = mod.WsAuthorAi.snapshot("c1");
    expect(snap.fill).toBeNull();
    expect(snap.gaps).toEqual({ source: "rules", gaps: ["第 1 场：待补 conflict"], items: null });
    expect(snap.authorAction.target_view).toBe("config");
    expect(snap.action.error).toBeNull();

    // 模型接上之后再补全：待补清单与 author_action 都收掉
    client.apiPost.mockResolvedValueOnce({ source: "llm", patch: { drama: {}, scenes: [], append_scenes: [] }, notes: [], gaps: [], dropped: [] });
    await mod.WsAuthorAi.requestFill("c1", {});
    expect(mod.WsAuthorAi.snapshot("c1").gaps).toBeNull();
    expect(mod.WsAuthorAi.snapshot("c1").authorAction).toBeNull();
  });

  it("带 candidate 的 requestFill 走 adopt 模式", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockResolvedValueOnce({ source: "llm", patch: { scenes: [], append_scenes: [] }, notes: [], gaps: [], dropped: [] });
    const candidate = { label: "双场对撞", scene_plan: [] };
    await mod.WsAuthorAi.requestFill("c1", { candidate });
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
    await mod.WsAuthorAi.requestFill("c1", {});
    const before = catalogGetCount(client);
    const patch = { drama: { spine: "推进" }, scenes: [{ scene_id: "s1", set: { conflict: "x" } }], append_scenes: [] };
    const applied = await mod.WsAuthorAi.applyPatch("c1", patch);
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapters/c1/plan/apply",
      { patch },
    );
    expect(applied).toEqual({ drama: 1, scenes: 1, appended: 1, skipped: [{ scene_id: "s1", field: "goal", reason: "field_not_empty" }] });
    const snap = mod.WsAuthorAi.snapshot("c1");
    expect(snap.fill).toBeNull();
    await vi.waitFor(() => expect(catalogGetCount(client)).toBeGreaterThan(before), T);
  });

  it("applyPatch 失败（锁章 409）：错误可观测、目录不重拉、异常上抛", async () => {
    const { mod, client } = await loadStore();
    const lockError = Object.assign(new Error("approved chapter is locked"), { code: "APPROVED_CHAPTER_LOCKED" });
    client.apiPost.mockRejectedValueOnce(lockError);
    const before = catalogGetCount(client);
    await expect(
      mod.WsAuthorAi.applyPatch("c1", { scenes: [], append_scenes: [] }),
    ).rejects.toThrow("approved chapter is locked");
    const snap = mod.WsAuthorAi.snapshot("c1");
    expect(snap.action.error.code).toBe("APPROVED_CHAPTER_LOCKED");
    expect(snap.action.busy).toBe(false);
    expect(catalogGetCount(client)).toBe(before);
  });

  it("requestCandidates 没有模型：author_action 透传、不给方向", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockRejectedValueOnce(NO_MODEL());
    const result = await mod.WsAuthorAi.requestCandidates("c1", "更贴近家庭线");
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapters/c1/plan/candidates",
      { direction_hint: "更贴近家庭线" },
    );
    expect(result).toBeNull();
    expect(mod.WsAuthorAi.snapshot("c1").candidates).toBeNull();
    expect(mod.WsAuthorAi.snapshot("c1").authorAction.target_view).toBe("config");
  });

  it("requestReview 保存 AI 的 findings；没有模型时不拿规则凑一份体检", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockResolvedValueOnce({
      source: "llm",
      findings: [{ code: "BRIEF_INCOMPLETE", severity: "warn", scene_id: "s1", evidence: "缺三拍" }],
      degraded_slots: [],
    });
    const review = await mod.WsAuthorAi.requestReview("c1");
    expect(review.findings[0].code).toBe("BRIEF_INCOMPLETE");
    expect(review).not.toHaveProperty("source");

    client.apiPost.mockRejectedValueOnce(NO_MODEL());
    await mod.WsAuthorAi.requestReview("c2");
    expect(mod.WsAuthorAi.snapshot("c2").review).toBeNull();
    expect(mod.WsAuthorAi.snapshot("c2").authorAction.target_view).toBe("config");
  });

  it("别的 409（锁章，哪怕带着 author_action）不当成「没有模型」：错误照常挂出、异常照常上抛", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockRejectedValueOnce(Object.assign(new Error("终稿已锁定"), {
      code: "APPROVED_CHAPTER_LOCKED", status: 409, details: { author_action: { target_view: "manuscripts" } },
    }));
    await expect(mod.WsAuthorAi.requestReview("c1")).rejects.toThrow("终稿已锁定");
    const snap = mod.WsAuthorAi.snapshot("c1");
    expect(snap.authorAction).toBeNull();
    expect(snap.action.error.code).toBe("APPROVED_CHAPTER_LOCKED");
  });
});

/* ---------- 界面：没有模型时只给「去系统配置」与一份明说不是 AI 的待补清单 ---------- */
describe("章节编排 · AI 编排卡与 AI 体检（没有模型）", () => {
  const mounted = [];
  beforeEach(() => { vi.resetModules(); });
  afterEach(async () => {
    while (mounted.length) {
      const { root, host } = mounted.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
  });

  async function mountArrange(node) {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    mounted.push({ root, host });
    await act(async () => root.render(node));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    return host;
  }

  const CH = { id: "ch01", backendId: "c1", title: "盐场的早班", scenes: [{ sid: "s1", backendId: "b1", title: "交班", design: { owner: "desk" } }] };

  it("一键补全：不再说「AI 还没接上」并把规则清单当 AI 结果；给「去系统配置」和一份按空槽列出的待补清单（旧后端只给 gaps 行时照旧列）", async () => {
    const { client } = await loadStore();
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url.endsWith("/plan/gaps")) return Promise.resolve({ source: "rules", gaps: ["章节戏剧卡：待补 promise"] });
      if (url.endsWith("/architecture")) return Promise.resolve({ architecture: null });
      return base(url);
    });
    client.apiPost.mockRejectedValue(NO_MODEL());
    const { ArrAiArrange, ArrAiHealth } = await import("./ws-author-ai.jsx");
    const configure = vi.fn();
    const host = await mountArrange(<><ArrAiArrange ch={CH} locked={false} onConfigureModel={configure} /><ArrAiHealth ch={CH} locked={false} onConfigureModel={configure} /></>);

    await act(async () => { [...host.querySelectorAll('[role="tab"]')].find((t) => t.textContent.includes("补全")).click(); });
    await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent.includes("一键补全")).click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });

    expect(host.textContent).not.toContain("AI 还没接上");
    const gaps = host.querySelector('[data-testid="arr-ai-gaps"]');
    expect(gaps.textContent).toContain("不是 AI 的建议");
    expect(gaps.textContent).toContain("章节戏剧卡：待补 promise");
    const setup = [...host.querySelectorAll("button")].filter((b) => b.textContent === "去系统配置");
    expect(setup.length).toBeGreaterThan(0);
    await act(async () => { setup[0].click(); });
    expect(configure).toHaveBeenCalled();

    // AI 体检：没有模型时不列任何「体检结果」
    await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent === "开始体检").click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(host.querySelector(".arr-ai-findings")).toBeNull();
    expect(host.textContent).not.toContain("没有发现结构性问题");
  });

  it("待补清单照结构化的 items 画：第几场、中文栏名，不印内部 id / 英文槽名；构思侧的场给「去构思第 10 步补」", async () => {
    const { client } = await loadStore();
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url.endsWith("/plan/gaps")) {
        return Promise.resolve({
          source: "rules",
          gaps: ["章节戏剧卡：待补 promise", "交班（b1）：待补 conflict, pov——在构思第 10 步补", "回潮（b2）：待补 goal"],
          items: [
            { scope: "chapter", scene_id: null, scene_label: "章节戏剧卡", fields: [{ key: "promise", label: "核心承诺" }], fill_in: null },
            { scope: "scene", scene_id: "b1", scene_label: "第 1 场 · 交班", fields: [{ key: "conflict", label: "冲突" }, { key: "pov", label: "视角" }], fill_in: "snowflake_step_10" },
            { scope: "scene", scene_id: "b2", scene_label: "第 2 场 · 回潮", fields: [{ key: "goal", label: "目标" }], fill_in: null },
          ],
        });
      }
      if (url.endsWith("/architecture")) return Promise.resolve({ architecture: null });
      return base(url);
    });
    client.apiPost.mockRejectedValue(NO_MODEL());
    const ch = { ...CH, scenes: [{ ...CH.scenes[0], design: { owner: "plan" } }, { sid: "s2", backendId: "b2", title: "回潮", design: { owner: "desk" } }] };
    const editPlan = vi.fn();
    const { ArrAiArrange } = await import("./ws-author-ai.jsx");
    const host = await mountArrange(<ArrAiArrange ch={ch} locked={false} onConfigureModel={vi.fn()} onEditPlan={editPlan} />);

    await act(async () => { [...host.querySelectorAll('[role="tab"]')].find((t) => t.textContent.includes("补全")).click(); });
    await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent.includes("一键补全")).click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });

    const rows = [...host.querySelectorAll('[data-testid="arr-ai-gap"]')].map((li) => li.textContent);
    expect(rows).toEqual(["章节戏剧卡：核心承诺", "第 1 场 · 交班：冲突、视角 去构思第 10 步补 ↗", "第 2 场 · 回潮：目标"]);
    const gaps = host.querySelector('[data-testid="arr-ai-gaps"]');
    expect(gaps.textContent).toContain("不是 AI 的建议");
    expect(gaps.textContent).not.toMatch(/promise|conflict|pov|b1|b2/);
    // 只有构思侧拥有设计的那一场有门；点它去构思第 10 步的那一场（给的是目录里的整张卡）
    const doors = host.querySelectorAll('[data-testid="arr-ai-gap-plan"]');
    expect(doors).toHaveLength(1);
    await act(async () => { doors[0].click(); });
    expect(editPlan).toHaveBeenCalledWith(ch.scenes[0]);
  });

  it("待补清单：后端说没有空着的格子时照实说", async () => {
    const { client } = await loadStore();
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url.endsWith("/plan/gaps")) return Promise.resolve({ source: "rules", gaps: [], items: [] });
      if (url.endsWith("/architecture")) return Promise.resolve({ architecture: null });
      return base(url);
    });
    client.apiPost.mockRejectedValue(NO_MODEL());
    const { ArrAiArrange } = await import("./ws-author-ai.jsx");
    const host = await mountArrange(<ArrAiArrange ch={CH} locked={false} onConfigureModel={vi.fn()} />);
    await act(async () => { [...host.querySelectorAll('[role="tab"]')].find((t) => t.textContent.includes("补全")).click(); });
    await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent.includes("一键补全")).click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    expect(host.querySelector('[data-testid="arr-ai-gaps"]').textContent).toBe("戏剧卡与场景卡都没有空着的格子。");
  });

  it("蓝图页签：作者写的蓝图之后设计又改过，说一句「设计改过，蓝图可能过时」", async () => {
    const { client } = await loadStore();
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url.endsWith("/architecture")
      ? Promise.resolve({ architecture: { ...ARCH_ROW, llm_call_id: null, design_changed: { reason: "design_changed", at: "2026-09-30", message: "这份蓝图写好之后，构思或参考书又改过。" } } })
      : base(url)));
    const { ArrAiArrange } = await import("./ws-author-ai.jsx");
    const host = await mountArrange(<ArrAiArrange ch={CH} locked={false} onConfigureModel={vi.fn()} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const note = host.querySelector('[data-testid="arr-ai-design-changed"]');
    expect(note.textContent).toContain("设计改过，蓝图可能过时");
    expect(note.textContent).toContain("构思或参考书又改过");
  });
});
