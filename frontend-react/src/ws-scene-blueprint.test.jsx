// AI 起草台「AI 写前笔记」（ws-scene-blueprint.jsx + ws-scene-blueprint-store.js）：蓝图按来源分两组摆
// （照第 10 步 / AI 加的），能改成我的版本、恢复 AI 版、重做；起草任务在跑或只读时不给写。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
}));
vi.mock("./ws-notify.jsx", async (importOriginal) => ({
  ...(await importOriginal()),
  wsConfirm: vi.fn(async () => true),
}));

const SID = "work-a_ch01_sc01";
const PATH = `/api/v1/scenes/${SID}/blueprint`;

const AI_VIEW = {
  scene_id: SID,
  scene_form: "proactive",
  running: false,
  blueprint: {
    row_id: "bp_ai_1",
    created_at: "2026-10-16T08:00:00Z",
    source: "ai",
    variant: "craft",
    fields: [
      { key: "visible_desire", value: "林昭要在雨停前读完那封旧信", origin: "plan" },
      { key: "forced_choice", value: "当场拆穿，还是装作没看见", origin: "ai" },
      { key: "image_anchor", value: "窗台上积水里的灯影", origin: "ai" },
      { key: "next_scene_pull", value: "抽屉锁孔里有新的划痕", origin: "plan" },
    ],
    situation_tags: ["对峙审问"],
    design_changed: null,
  },
  character_pressure: {
    row_id: "pressure_1",
    created_at: "2026-10-16T08:00:00Z",
    fields: [{ key: "hidden_fear", value: "老友骗了她" }],
  },
};

const MINE_VIEW = {
  ...AI_VIEW,
  blueprint: {
    ...AI_VIEW.blueprint,
    row_id: "bp_mine_1",
    source: "author",
    fields: AI_VIEW.blueprint.fields.map((f) => ({ ...f, origin: "author", value: f.key === "image_anchor" ? "案卷柜上的雨水" : f.value })),
  },
};

describe("AI 写前笔记", () => {
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

  async function setup(view) {
    const client = await import("./lib/client.js");
    client.apiGet.mockImplementation((url) => (url === PATH ? Promise.resolve(view) : Promise.resolve({})));
    const { SceneBlueprintNote } = await import("./ws-scene-blueprint.jsx");
    return { client, SceneBlueprintNote };
  }

  async function mount(node) {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    mounted.push({ root, host });
    await act(async () => root.render(node));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    return host;
  }

  const button = (host, text) => [...host.querySelectorAll("button")].find((b) => b.textContent.includes(text));

  it("AI 版分两组：照第 10 步 / AI 加的；人物压力与风格选样标签另列", async () => {
    const { SceneBlueprintNote } = await setup(AI_VIEW);
    const host = await mount(<SceneBlueprintNote sid={SID} />);

    const plan = host.querySelector('[data-testid="scene-blueprint-plan"]');
    const added = host.querySelector('[data-testid="scene-blueprint-ai"]');
    expect(plan.textContent).toContain("照第 10 步");
    expect(plan.textContent).toContain("看得见的想要");
    expect(plan.textContent).not.toContain("意象");
    expect(added.textContent).toContain("AI 加的");
    expect(added.textContent).toContain("被迫的选择");
    expect(added.textContent).toContain("窗台上积水里的灯影");
    expect(host.querySelector(".sbn-pressure").textContent).toContain("藏着的恐惧");
    expect(host.textContent).toContain("对峙审问");
    expect(button(host, "改成我的版本")).toBeTruthy();
    expect(button(host, "恢复 AI 版")).toBeFalsy();
  });

  it("改成我的版本：送改过的格子，回来按「我的版本」一组显示，并能恢复 AI 版", async () => {
    const { client, SceneBlueprintNote } = await setup(AI_VIEW);
    client.apiPut.mockResolvedValue(MINE_VIEW);
    client.apiPost.mockResolvedValue(AI_VIEW);
    const host = await mount(<SceneBlueprintNote sid={SID} />);

    await act(async () => { button(host, "改成我的版本").click(); });
    const anchor = host.querySelector('textarea[aria-label="写前笔记 · 意象"]');
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    await act(async () => {
      setValue.call(anchor, "案卷柜上的雨水");
      anchor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { button(host, "保存我的版本").click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(client.apiPut).toHaveBeenCalledWith(PATH, {
      fields: {
        visible_desire: "林昭要在雨停前读完那封旧信",
        forced_choice: "当场拆穿，还是装作没看见",
        image_anchor: "案卷柜上的雨水",
        next_scene_pull: "抽屉锁孔里有新的划痕",
      },
    });
    expect(host.querySelector('[data-testid="scene-blueprint-author"]').textContent).toContain("案卷柜上的雨水");
    expect(host.querySelector('[data-testid="scene-blueprint-ai"]')).toBeNull();

    await act(async () => { button(host, "恢复 AI 版").click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(client.apiPost).toHaveBeenCalledWith(`${PATH}/restore-ai`, {});
    expect(host.querySelector('[data-testid="scene-blueprint-ai"]')).toBeTruthy();
  });

  it("还没有笔记：「先出笔记」不用确认就让 AI 做；模型没配好时就地说明", async () => {
    const { client, SceneBlueprintNote } = await setup({ ...AI_VIEW, blueprint: null, character_pressure: null });
    client.apiPost.mockRejectedValue(Object.assign(new Error("场景蓝图需要先配好模型。"), { code: "SCENE_BLUEPRINT_LLM_REQUIRED", status: 409 }));
    const host = await mount(<SceneBlueprintNote sid={SID} />);

    expect(host.textContent).toContain("还没有写前笔记");
    await act(async () => { button(host, "先出笔记").click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(client.apiPost).toHaveBeenCalledWith(`${PATH}/generate`, {});
    expect(host.querySelector('[data-testid="scene-blueprint-error"]').textContent).toContain("场景蓝图需要先配好模型");
  });

  it("起草任务在跑时按钮全灰；只读时不给按钮", async () => {
    const { SceneBlueprintNote } = await setup({ ...AI_VIEW, running: true });
    const host = await mount(<SceneBlueprintNote sid={SID} />);
    expect(host.textContent).toContain("起草任务在跑");
    expect([...host.querySelectorAll(".sbn-actions button")].every((b) => b.disabled)).toBe(true);

    const readOnly = await mount(<SceneBlueprintNote sid={SID} editable={false} collapsible defaultOpen={false} />);
    expect(readOnly.querySelector(".sbn-actions")).toBeNull();
    expect(readOnly.querySelector('[data-testid="scene-blueprint-plan"]')).toBeNull();
    await act(async () => { button(readOnly, "展开").click(); });
    expect(readOnly.querySelector('[data-testid="scene-blueprint-plan"]')).toBeTruthy();
  });
});
