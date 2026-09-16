// 阶段 T（2026-09-16）：教练页的「本步要点」卡与它和生成的接缝。
// 此前教练 / 候选 / AI 生成互不知情。这里守住视图层的契约：
// 1) 教练页渲染 SnowSync 镜像里的要点（活动条目、继承的上游全书级、已撤可恢复）；撤下 / 加条 / 切范围 / 继承开关
//    都走 SnowSync.saveDirectionBrief（作者要的完整列表，缺席 = 撤下）；
// 2) 发消息后把回包的 direction_brief 落镜像；
// 3) 「以此为方向生成」以 direction_kind=coach_reply 走 generate；「生成时带入」关掉 → use_direction_brief=false；
// 4) 要点改过而本稿没跟上 → 「按最新要点重新生成」以 source=fe_brief_regen 走 generate。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const catalog = vi.hoisted(() => ({ get: vi.fn(() => []), adoptOutline: vi.fn(async () => 2) }));
vi.mock("./ws-catalog.jsx", () => ({ WsCatalog: catalog }));
vi.mock("./ws-works.jsx", () => ({
  wsKey: (base) => `${base}::brief-book`,
  WsWorks: { activeId: () => "brief-book", active: () => ({ id: "brief-book", title: "要点之书" }) },
}));
vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(async () => ({})),
  apiPost: vi.fn(async () => ({})),
  apiPatch: vi.fn(async () => ({})),
  apiPut: vi.fn(async () => ({})),
  apiDelete: vi.fn(async () => ({})),
}));

import { WsSnowflake } from "./ws-snow.jsx";
import * as client from "./lib/client.js";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const T = { timeout: 5000, interval: 25 };
const mounted = [];

const BRIEF = {
  step_key: "one_sentence_summary", revision: 2, inherit_upstream: true, active_count: 2,
  lines: [
    { line_id: "dl_1", kind: "decision", scope: "step", text: "主角是被动卷入", origin: "coach", status: "active" },
    { line_id: "dl_2", kind: "pending", scope: "step", text: "结局是否团圆", origin: "author", status: "active" },
    { line_id: "dl_3", kind: "rejection", scope: "step", text: "不要热血", origin: "coach", status: "dismissed", dismissed_by: "author" },
  ],
  inherited: [{ step_key: "book_brief", step_label: "读者定位", line_id: "dl_0", kind: "constraint", text: "基调冷" }],
};
const TURN = {
  turn_id: "turn-1", step_key: "one_sentence_summary", message: "主角被动一点", reply: "先把主角的被动写实。",
  suggestions: ["第一灾之前他不出手"], candidate_patch: null, source: "llm", created_at: "2026-09-16T00:00:00Z",
};

async function renderSnow() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(<WsSnowflake initialStep="logline" onOverview={vi.fn()} />));
  return host;
}

async function openCoach(host) {
  const tab = Array.from(host.querySelectorAll('[role="tab"]')).find(b => (b.textContent || "").includes("教练"));
  expect(tab).toBeTruthy();
  await act(async () => tab.click());
  await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-brief-card"]')).toBeTruthy(), T);
  return host.querySelector('[data-testid="snow-brief-card"]');
}

describe("阶段 T · 教练页的本步要点", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    client.apiGet.mockImplementation(async (url) => (String(url).includes("/snowflake-workspace") ? { assistant_history: [TURN], steps: [], direction_briefs: {} } : {}));
    client.apiPost.mockImplementation(async (url) => {
      if (String(url).endsWith("/assistant")) {
        return { reply: "记下了。", suggestions: [], source: "llm", turn_id: "turn-2", assistant_history: [TURN, { ...TURN, turn_id: "turn-2", message: "结局苦乐参半", reply: "记下了。" }],
          direction_brief: { ...BRIEF, revision: 3 }, brief_delta: { added: ["dl_9"], updated: [], superseded: ["dl_2"], kept: 1 } };
      }
      return { step: { step_key: "one_sentence_summary", draft: {}, health: {} }, workspace: { direction_briefs: {} } };
    });
    window.SnowSync = {
      directionBrief: vi.fn(() => BRIEF),
      briefUsage: vi.fn(() => ({ hasBrief: true, stale: true, disabled: false, usedRevision: 1, currentRevision: 2 })),
      saveDirectionBrief: vi.fn(async () => BRIEF),
      setDirectionBrief: vi.fn(),
      captureBriefs: vi.fn(),
      canonDraft: vi.fn(() => ({})),
      pushCanon: vi.fn(() => ({})),
      applyServerStep: vi.fn(() => null),
      applyCanonPatch: vi.fn(() => null),
      chapterPreview: vi.fn(async () => ({})),
      materialize: vi.fn(async () => ({})),
    };
  });

  afterEach(async () => {
    while (mounted.length) {
      const { root, host } = mounted.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
    try { delete window.SnowSync; } catch (e) {}
  });

  it("渲染活动条目、继承的上游要点与已撤条目；撤下一条 = 提交缺了它的完整列表", async () => {
    const host = await renderSnow();
    const card = await openCoach(host);
    const lines = card.querySelectorAll('[data-testid="snow-brief-line"]');
    expect(lines.length).toBe(2);
    expect(lines[0].textContent).toContain("决定");
    expect(lines[0].textContent).toContain("主角是被动卷入");
    expect(lines[1].textContent).toContain("你"); // 作者改过的条目有归属标记
    expect(card.querySelector('[data-testid="snow-brief-inherited"]').textContent).toContain("基调冷");
    expect(card.querySelector('[data-testid="snow-brief-inherited"]').textContent).toContain("读者定位");
    expect(card.querySelector('[data-testid="snow-brief-stale"]').textContent).toContain("第 1 版");

    await act(async () => lines[0].querySelector('[data-testid="snow-brief-dismiss"]').click());
    expect(window.SnowSync.saveDirectionBrief).toHaveBeenCalledTimes(1);
    const [workId, feKey, payload] = window.SnowSync.saveDirectionBrief.mock.calls[0];
    expect(workId).toBe("brief-book");
    expect(feKey).toBe("logline");
    expect(payload.lines.map(l => l.line_id)).toEqual(["dl_2"]);
    expect(payload.inherit_upstream).toBeUndefined();

    // 已撤条目：展开后可恢复（带 line_id + status=active 回传）
    await act(async () => card.querySelector('[data-testid="snow-brief-dismissed-toggle"]').click());
    await act(async () => card.querySelector('[data-testid="snow-brief-restore"]').click());
    const restored = window.SnowSync.saveDirectionBrief.mock.calls[1][2].lines;
    expect(restored.find(l => l.line_id === "dl_3")).toMatchObject({ status: "active", text: "不要热血" });
  });

  it("加一条自己的要点、切换范围、关掉继承都走 saveDirectionBrief", async () => {
    const host = await renderSnow();
    const card = await openCoach(host);
    const input = card.querySelector('[data-testid="snow-brief-add-text"]');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    await act(async () => { setter.call(input, "第一章不出现凶手"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    await act(async () => card.querySelector('[data-testid="snow-brief-add"]').click());
    const added = window.SnowSync.saveDirectionBrief.mock.calls[0][2].lines;
    expect(added.length).toBe(3);
    expect(added[2]).toMatchObject({ kind: "decision", scope: "step", text: "第一章不出现凶手", status: "active" });
    expect(added[2].line_id).toBeUndefined();

    await act(async () => card.querySelectorAll('[data-testid="snow-brief-scope"]')[0].click());
    expect(window.SnowSync.saveDirectionBrief.mock.calls[1][2].lines[0]).toMatchObject({ line_id: "dl_1", scope: "book" });

    const inherit = card.querySelector('[data-testid="snow-brief-inherit"]');
    await act(async () => inherit.click());
    const inheritCall = window.SnowSync.saveDirectionBrief.mock.calls[2][2];
    expect(inheritCall.inherit_upstream).toBe(false);
    expect(inheritCall.lines).toBeUndefined();
  });

  it("发消息后把回包的 direction_brief 落镜像；「以此为方向生成」以 coach_reply 走 generate", async () => {
    const host = await renderSnow();
    const card = await openCoach(host);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-coach-adopt"]')).toBeTruthy(), T);

    const textarea = host.querySelector(".sf-coach-input textarea");
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    await act(async () => { setter.call(textarea, "结局苦乐参半"); textarea.dispatchEvent(new Event("input", { bubbles: true })); });
    const send = Array.from(host.querySelectorAll(".sf-coach-input button")).find(b => (b.textContent || "").includes("发送"));
    await act(async () => send.click());
    await vi.waitFor(() => expect(window.SnowSync.setDirectionBrief).toHaveBeenCalledTimes(1), T);
    const assistantCall = client.apiPost.mock.calls.find(c => String(c[0]).endsWith("/assistant"));
    expect(assistantCall[1]).toMatchObject({ step_key: "one_sentence_summary", message: "结局苦乐参半" });
    expect(window.SnowSync.setDirectionBrief.mock.calls[0][1]).toBe("logline");
    expect(window.SnowSync.setDirectionBrief.mock.calls[0][2].revision).toBe(3);

    await act(async () => host.querySelector('[data-testid="snow-coach-adopt"]').click());
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(c => String(c[0]).endsWith("/generate"))).toBe(true), T);
    const generate = client.apiPost.mock.calls.find(c => String(c[0]).endsWith("/generate"));
    expect(generate[0]).toContain("/steps/one_sentence_summary/generate");
    expect(generate[1]).toMatchObject({ require_llm: true, direction_text: "先把主角的被动写实。", direction_kind: "coach_reply", source: "fe_coach_adopt", use_direction_brief: true });
    expect(card).toBeTruthy();
  });

  it("「生成时带入」关掉 → generate 带 use_direction_brief=false 且提示消失；按最新要点重新生成 = fe_brief_regen", async () => {
    const host = await renderSnow();
    const card = await openCoach(host);
    await act(async () => card.querySelector('[data-testid="snow-brief-regen"]').click());
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(c => String(c[0]).endsWith("/generate"))).toBe(true), T);
    expect(client.apiPost.mock.calls.find(c => String(c[0]).endsWith("/generate"))[1]).toMatchObject({ source: "fe_brief_regen", use_direction_brief: true });

    // 生成后视图切回「编辑」页；重新打开教练页再关「生成时带入」
    const card2 = await openCoach(host);
    expect(card2.querySelector('[data-testid="snow-brief-stale"]')).toBeTruthy();
    await act(async () => card2.querySelector('[data-testid="snow-brief-use"]').click());
    expect(window.localStorage.getItem("ws_snow_use_brief::brief-book")).toBe("0");
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-brief-stale"]')).toBeNull(), T);
    expect(host.querySelector('[data-testid="snow-brief-card"]')).toBeTruthy();

    // 再次生成（走教练回复）：带入已关
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-coach-adopt"]')).toBeTruthy(), T);
    client.apiPost.mockClear();
    await act(async () => host.querySelector('[data-testid="snow-coach-adopt"]').click());
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(c => String(c[0]).endsWith("/generate"))).toBe(true), T);
    expect(client.apiPost.mock.calls.find(c => String(c[0]).endsWith("/generate"))[1].use_direction_brief).toBe(false);
  });
});
