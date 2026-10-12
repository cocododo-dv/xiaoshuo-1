// WsAuthorAi store 层单测（章节编排的「章节蓝图」卡）：蓝图读写形状、没有模型时 409 的 author_action 透传
// （不再有 200 + fallback 的规则结果）、别的 409 照常是错误；最后是「章节蓝图」卡的界面。
// 方向 / 补全 / 待补清单 / AI 体检 2026-10 删了（round 2 · B2-5），它们的用例随之删掉。
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

/* 没有可用模型：后端 409 CHAPTER_PLAN_LLM_NOT_CONFIGURED，details 里带 author_action */
const NO_MODEL = () => Object.assign(new Error("章节编排的 AI 需要先启用真实模型。"), {
  code: "CHAPTER_PLAN_LLM_NOT_CONFIGURED",
  status: 409,
  details: { node_id: "chapter_story_architecture", author_action: { title: "需要先启用真实模型", message: "配置模型后再试。", target_view: "config", primary_button_label: "去系统配置" } },
});

describe("WsAuthorAi（章节编排的章节蓝图 store）", () => {
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

  it("别的 409（锁章，哪怕带着 author_action）不当成「没有模型」：错误照常挂出、异常照常上抛", async () => {
    const { mod, client } = await loadStore();
    client.apiPost.mockRejectedValueOnce(Object.assign(new Error("终稿已锁定"), {
      code: "APPROVED_CHAPTER_LOCKED", status: 409, details: { author_action: { target_view: "manuscripts" } },
    }));
    await expect(mod.WsAuthorAi.generateArchitecture("c1")).rejects.toThrow("终稿已锁定");
    const snap = mod.WsAuthorAi.snapshot("c1");
    expect(snap.authorAction).toBeNull();
    expect(snap.action.error.code).toBe("APPROVED_CHAPTER_LOCKED");
  });
});

/* ---------- 界面：「章节蓝图」卡；没有模型时只给「去系统配置」 ---------- */
describe("章节编排 · 章节蓝图卡", () => {
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

  async function mountCard(node) {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    mounted.push({ root, host });
    await act(async () => root.render(node));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    return host;
  }

  const CH = { id: "ch01", backendId: "c1", title: "盐场的早班", scenes: [{ sid: "s1", backendId: "b1", title: "交班", design: { owner: "desk" } }] };

  it("一张「章节蓝图」卡，没有页签、没有方向 / 补全；没有模型时「AI 生成蓝图」只给「去系统配置」，不伪造蓝图", async () => {
    const { client } = await loadStore();
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url.endsWith("/architecture") ? Promise.resolve({ architecture: null }) : base(url)));
    client.apiPost.mockRejectedValue(NO_MODEL());
    const { ArrBlueprintCard } = await import("./ws-author-ai.jsx");
    const configure = vi.fn();
    const host = await mountCard(<ArrBlueprintCard ch={CH} locked={false} onConfigureModel={configure} />);

    expect(host.querySelector(".card-title").textContent).toBe("章节蓝图");
    expect(host.querySelectorAll('[role="tab"]')).toHaveLength(0);
    expect(host.textContent).not.toMatch(/方向|补全|体检/);
    expect(host.textContent).toContain("这一章还没有蓝图。");

    await act(async () => { [...host.querySelectorAll("button")].find((b) => b.textContent.includes("AI 生成蓝图")).click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); });
    expect(client.apiPost).toHaveBeenCalledWith("/api/v2/projects/prj-main/catalog/chapters/c1/architecture/generate", {});
    expect(host.textContent).toContain("这一章还没有蓝图。");
    const setup = [...host.querySelectorAll("button")].filter((b) => b.textContent === "去系统配置");
    expect(setup).toHaveLength(1);
    await act(async () => { setup[0].click(); });
    expect(configure).toHaveBeenCalled();
  });

  it("作者写的蓝图之后设计又改过，说一句「设计改过，蓝图可能过时」", async () => {
    const { client } = await loadStore();
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url.endsWith("/architecture")
      ? Promise.resolve({ architecture: { ...ARCH_ROW, llm_call_id: null, design_changed: { reason: "design_changed", at: "2026-09-30", message: "这份蓝图写好之后，构思或参考书又改过。" } } })
      : base(url)));
    const { ArrBlueprintCard } = await import("./ws-author-ai.jsx");
    const host = await mountCard(<ArrBlueprintCard ch={CH} locked={false} onConfigureModel={vi.fn()} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const note = host.querySelector('[data-testid="arr-ai-design-changed"]');
    expect(note.textContent).toContain("设计改过，蓝图可能过时");
    expect(note.textContent).toContain("构思或参考书又改过");
  });
});
