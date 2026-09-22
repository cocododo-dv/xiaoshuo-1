// 成稿中心 · 诊断页签：章级「AI 通读本章」+ 各场计数 + 落到各场的通读发现（带 signal_id 深链进写作台）。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({ apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn() }));
vi.mock("./ws-works.jsx", () => ({ WsWorks: { activeId: () => "prj-main" } }));

import { apiGet, apiPost } from "./lib/client.js";
import { ManuDiagnosis } from "./ws-manuscripts-diagnosis.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 4000, interval: 20 };

const CHAPTER = {
  id: "ch01", backendId: "c1", n: "01", title: "盐场的早班", state: "writing",
  scenes: [
    { sid: "ch01s1", backendId: "s1", title: "交班", state: "done" },
    { sid: "ch01s2", backendId: "s2", title: "开口", state: "writing" },
  ],
};

function payload(overrides = {}) {
  return {
    chapter_id: "c1",
    ai: { status: "stale", evaluation_id: "chapter_eval_1", overall_score: 0.52, revision_brief: [{ action: "让最后一场回答第一场的问题。" }], created_at: "2026-09-22T10:00:00Z" },
    chapter_findings: [
      { signal_id: "ai:ending_drive:aaaa1111", source: "ai", dimension: "ending_drive", label: "收束驱动", severity: "blocking", issue: "本章开头的承诺到结尾没有兑现。", recommendation: "让最后一场回答第一场的问题。", evidence: null, context: "", stale: false },
    ],
    scenes: [
      { scene_id: "s1", scene_seq: 1, title: "", text_layer: "author_draft", summary: { open: 2, by_severity: { blocking: 0 } }, ai_status: "not_run", review_status: "not_run", findings_from_chapter: [] },
      {
        scene_id: "s2", scene_seq: 2, title: "", text_layer: "author_draft", summary: { open: 1, by_severity: { blocking: 1 } }, ai_status: "current", review_status: "not_run",
        findings_from_chapter: [
          { signal_id: "ai:choice_pressure:bbbb2222", source: "ai", dimension: "choice_pressure", label: "抉择压力", severity: "revision", issue: "第二场的选择说出来了，没有落成动作。", recommendation: "让证据袋真的离开她的手。", evidence: { paragraph_index: 1, excerpt: "把证据袋放在桌上", start: 5, end: 12 }, stale: false, origin: { kind: "chapter" } },
        ],
      },
    ],
    summary: { open: 4, chapter_level: 1, scenes: 2, scenes_with_findings: 2, blocking: 2 },
    ...overrides,
  };
}

const mounted = [];
async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return host;
}
const click = (node) => act(async () => node.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const button = (host, text) => [...host.querySelectorAll("button")].find((node) => node.textContent.includes(text));

beforeEach(() => { vi.clearAllMocks(); apiGet.mockResolvedValue(payload()); });
afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
});

describe("成稿中心 · 诊断页签", () => {
  it("读章级诊断：改前的通读、整章的判断、各场的计数与落到那一场的通读发现；去写作台 / 看这一处带深链", async () => {
    const go = vi.fn();
    const host = await render(<ManuDiagnosis chapter={CHAPTER} go={go} />);
    await vi.waitFor(() => expect(host.textContent).toContain("本章开头的承诺到结尾没有兑现"), T);
    expect(apiGet).toHaveBeenCalledWith("/api/v1/chapters/c1/deep-review");
    expect(host.textContent).toContain("这是改前的通读");
    expect(host.textContent).toContain("总分 52");
    expect(host.textContent).toContain("让最后一场回答第一场的问题。");
    expect(host.textContent).toContain("开着 2");
    expect(host.textContent).toContain("开着 1 · 阻断 1");
    expect(host.textContent).toContain("深评过");
    expect(host.textContent).toContain("第二场的选择说出来了");
    expect(button(host, "AI 通读本章")).toBeUndefined();
    expect(button(host, "重新通读")).not.toBeUndefined();

    const goButtons = [...host.querySelectorAll("button")].filter((node) => node.textContent.includes("去写作台看"));
    expect(goButtons).toHaveLength(2);
    await click(goButtons[1]);
    expect(go).toHaveBeenLastCalledWith("writer", [
      { type: "ws:writer-scene", detail: "ch01s2" },
      { type: "ws:writer-posture", detail: "deep" },
    ]);
    await click(button(host, "在写作台看这一处"));
    expect(go).toHaveBeenLastCalledWith("writer", [
      { type: "ws:writer-scene", detail: "ch01s2" },
      { type: "ws:writer-posture", detail: { posture: "deep", signal_id: "ai:choice_pressure:bbbb2222" } },
    ]);
  });

  it("「重新通读」调 POST，面板换成新的结果并广播 ws:diagnosis-changed；无模型时给「去系统设置」", async () => {
    const go = vi.fn();
    const changed = vi.fn();
    window.addEventListener("ws:diagnosis-changed", changed);
    const fresh = payload({ ai: { status: "current", evaluation_id: "chapter_eval_2", overall_score: 0.7, revision_brief: [], created_at: "2026-09-22T11:00:00Z" }, chapter_findings: [] });
    apiPost.mockResolvedValue(fresh);
    const host = await render(<ManuDiagnosis chapter={CHAPTER} go={go} />);
    await vi.waitFor(() => expect(host.textContent).toContain("这是改前的通读"), T);
    /* 通读之后面板广播 ws:diagnosis-changed，自己也会按事件重拉一次：服务端此刻给的已是新结果 */
    apiGet.mockResolvedValue(fresh);
    await click(button(host, "重新通读"));
    await vi.waitFor(() => expect(host.textContent).toContain("对着现在各场的正文"), T);
    expect(apiPost).toHaveBeenCalledWith("/api/v1/chapters/c1/deep-review", { scope: "all" });
    expect(host.textContent).toContain("总分 70");
    expect(host.textContent).not.toContain("本章开头的承诺");
    expect(changed).toHaveBeenCalled();
    window.removeEventListener("ws:diagnosis-changed", changed);

    apiPost.mockRejectedValueOnce(Object.assign(new Error("需要模型"), { code: "WRITER_DEEP_REVIEW_LLM_REQUIRED", status: 409, details: { author_action: { target_view: "config" } } }));
    await click(button(host, "重新通读"));
    await vi.waitFor(() => expect(host.textContent).toContain("没有可用的模型"), T);
    await click(button(host, "去系统设置"));
    expect(go).toHaveBeenLastCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
  });

  it("还没同步到服务器的章：只说一句，不发请求", async () => {
    const host = await render(<ManuDiagnosis chapter={{ ...CHAPTER, backendId: "" }} go={vi.fn()} />);
    expect(host.textContent).toContain("还没有同步到服务器");
    expect(apiGet).not.toHaveBeenCalled();
  });

  it("改前的通读且服务端算得出改过的场：给「只通读改过的 N 场」（POST scope=changed）与「整章重新通读」；沿用的发现标出来；没改过时说一句", async () => {
    const go = vi.fn();
    const stale = payload({
      ai: { status: "stale", evaluation_id: "chapter_eval_1", overall_score: 0.52, revision_brief: [], created_at: "2026-09-22T10:00:00Z", scope: "all", reviewed_scene_ids: ["s1", "s2"], carried_scene_ids: [], carried_from: null, changed_scene_ids: ["s2"], changed_count: 1, incremental_available: true },
      scenes: [
        { scene_id: "s1", scene_seq: 1, title: "", text_layer: "author_draft", summary: { open: 1, by_severity: { blocking: 0 } }, ai_status: "not_run", review_status: "not_run", findings_from_chapter: [], changed_since_review: false, carried: false },
        { scene_id: "s2", scene_seq: 2, title: "", text_layer: "author_draft", summary: { open: 0, by_severity: { blocking: 0 } }, ai_status: "not_run", review_status: "not_run", findings_from_chapter: [], changed_since_review: true, carried: false },
      ],
    });
    apiGet.mockResolvedValue(stale);
    const incremental = payload({
      ai: { status: "current", evaluation_id: "chapter_eval_2", overall_score: 0.6, revision_brief: [], created_at: "2026-09-22T11:00:00Z", scope: "changed", reviewed_scene_ids: ["s2"], carried_scene_ids: ["s1"], carried_from: "chapter_eval_1", changed_scene_ids: [], changed_count: 0, incremental_available: false },
      chapter_findings: [],
      scenes: [
        {
          scene_id: "s1", scene_seq: 1, title: "", text_layer: "author_draft", summary: { open: 1, by_severity: { blocking: 0 } }, ai_status: "not_run", review_status: "not_run", changed_since_review: false, carried: true,
          findings_from_chapter: [
            { signal_id: "ai:information_rhythm:cccc3333", source: "ai", dimension: "information_rhythm", label: "信息节奏", severity: "taste", issue: "钟响来得太早。", recommendation: "把钟响挪后。", evidence: { paragraph_index: 1, excerpt: "三声钟响", start: 0, end: 4 }, stale: false, origin: { kind: "chapter", carried_from: "chapter_eval_1" } },
          ],
        },
        { scene_id: "s2", scene_seq: 2, title: "", text_layer: "author_draft", summary: { open: 0, by_severity: { blocking: 0 } }, ai_status: "not_run", review_status: "not_run", findings_from_chapter: [], changed_since_review: false, carried: false },
      ],
      diagnosis_rollup: { project_id: "prj-main", chapter_id: "c1", chapters: { c1: { open: 1, blocking: 0, chapter_level: 0, chapter_level_blocking: 0, scenes: 2, scenes_with_findings: 1, ai_status: "current" } }, scenes: { s1: { chapter_id: "c1", open: 1, blocking: 0 }, s2: { chapter_id: "c1", open: 0, blocking: 0 } } },
    });
    apiPost.mockResolvedValue(incremental);
    const changed = vi.fn();
    window.addEventListener("ws:diagnosis-changed", changed);
    const host = await render(<ManuDiagnosis chapter={CHAPTER} go={go} />);
    await vi.waitFor(() => expect(host.textContent).toContain("改过 1 场：第 2 场"), T);
    expect(host.textContent).toContain("通读后改过");
    expect(button(host, "整章重新通读")).not.toBeUndefined();
    apiGet.mockResolvedValue(incremental);
    await click(button(host, "只通读改过的 1 场"));
    await vi.waitFor(() => expect(host.textContent).toContain("上次只通读了改过的 1 场，其余 1 场沿用更早的通读"), T);
    expect(apiPost).toHaveBeenCalledWith("/api/v1/chapters/c1/deep-review", { scope: "changed" });
    expect(host.textContent).toContain("沿用上次通读");
    expect(host.textContent).toContain("钟响来得太早");
    expect(changed).toHaveBeenCalled();
    expect(changed.mock.calls[0][0].detail.rollup.chapter_id).toBe("c1");
    window.removeEventListener("ws:diagnosis-changed", changed);

    /* 现在是对着现在的正文：只剩「重新通读」；服务端说没改过时给一句 */
    expect(button(host, "只通读改过的")).toBeUndefined();
    apiPost.mockResolvedValueOnce({ ...incremental, notice: { code: "CHAPTER_REVIEW_UP_TO_DATE", message: "上次通读之后没有场改过字，不必再通读。" } });
    await click(button(host, "重新通读"));
    await vi.waitFor(() => expect(host.textContent).toContain("不必再通读"), T);
  });
});
