import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const catalog = vi.hoisted(() => ({
  get: vi.fn(() => []),
  adoptOutline: vi.fn(async () => 2),
}));

vi.mock("./ws-catalog.jsx", () => ({ WsCatalog: catalog }));
vi.mock("./ws-works.jsx", () => ({
  wsKey: (base) => `${base}::new-book`,
  WsWorks: {
    activeId: () => "new-book",
    active: () => ({ id: "new-book", title: "真正的新书" }),
  },
}));

import { WsSnowflake, s2PlanSlots, s2PlanState, s2PlanAuto, s2StaleMap, s2UpstreamDrift, s2NormalizeState } from "./ws-snow.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const mounted = [];

async function renderSnow() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(<WsSnowflake initialStep="paragraph" onOverview={vi.fn()} />));
  return host;
}

describe("真实新项目的雪花顶部主操作", () => {
  beforeEach(() => {
    window.localStorage.clear();
    catalog.get.mockReturnValue([]);
    catalog.adoptOutline.mockClear();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(window, "alert").mockImplementation(() => {});
    // 分章面板打开即拉后端预览（算法在后端，前端不再持有第二套）
    window.SnowSync = {
      chapterPreview: vi.fn(async () => ({
        strategy: "spine_anchor",
        chapters: [
          { row_uid: "c1", chapter_seq: 1, act: 1, title: "雨夜来信", spine: "灾一", chapter_goal: "信件迫使主角回乡",
            scene_count: 1, scenes: [{ scene_plan_id: "sp1", scene_id: "SC1", scene_seq: 1, title: "第一场", primary_form: "proactive", spine: "灾一", anchored: true, planned: true }] },
          { row_uid: "c2", chapter_seq: 2, act: 1, title: "旧屋回声", spine: "", chapter_goal: "旧证词出现裂缝",
            scene_count: 1, scenes: [{ scene_plan_id: "sp2", scene_id: "SC2", scene_seq: 1, title: "第二场", primary_form: "reactive", spine: "", anchored: false, planned: true }] },
        ],
        unassigned: [],
        removed_scenes: [],
        warnings: [],
        totals: { chapter_count: 2, scene_count: 2, unassigned_count: 0 },
      })),
      materialize: vi.fn(async () => ({ created_chapter_count: 2 })),
    };
    window.localStorage.setItem("ws_snow_state_v2::new-book", JSON.stringify({
      scaffolds: {
        outline: {
          chapters: [
            { id: "01", act: 1, title: "雨夜来信", summary: "信件迫使主角回乡", spine: "灾一" },
            { id: "02", act: 1, title: "旧屋回声", summary: "旧证词出现裂缝", spine: "" },
          ],
        },
      },
    }));
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

  it("点击“整理为章节结构”打开分章预览面板，而不是直接落库", async () => {
    // P2：这个按钮以前是 window.confirm 加三条互不相同的落库路径，选哪条取决于闸门
    // 状态 —— 做得越完整反而掉进最差的那条，而且确认框说「并入 12 章」实际写 1 章。
    // 现在它只做一件事：打开预览，让作者按下确认之前就看得见会得到什么。
    const host = await renderSnow();

    const button = host.querySelector('[data-testid="snow-materialize-top"]');
    expect(button).toBeTruthy();

    await act(async () => button.click());

    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).toBeTruthy();
    expect(window.SnowSync.chapterPreview).toHaveBeenCalledTimes(1);
    // 预览阶段绝不落库
    expect(catalog.adoptOutline).not.toHaveBeenCalled();
    expect(window.SnowSync.materialize).not.toHaveBeenCalled();
  });
});


/* —— 阶段 E：10 的覆盖格按 09 的形态数槽；本地失效图只是乐观预判（后端优先的合并在视图里） —— */
describe("阶段 E · 场景规划覆盖格与本地失效图", () => {
  it("s2PlanSlots / s2PlanState：传入 09 的类型时按它取三槽，存储的 plan.mode 只是兜底", () => {
    const legacyReactivePlan = { mode: "reactive", reaction: "手抖", dilemma: "报警或沉默", decision: "去找证人", goal: "", conflict: "", setback: "" };
    expect(s2PlanSlots(legacyReactivePlan)).toEqual(["reaction", "dilemma", "decision"]);
    expect(s2PlanState(legacyReactivePlan)).toBe(2);
    // 09 把这一场切回主动：格子必须按 GCS 数槽——三个 RDD 槽再满也算「未规划」
    expect(s2PlanSlots(legacyReactivePlan, "proactive")).toEqual(["goal", "conflict", "setback"]);
    expect(s2PlanState(legacyReactivePlan, "proactive")).toBe(0);
    expect(s2PlanState({ mode: "proactive", goal: "拿到账本", conflict: "", setback: "" }, "reactive")).toBe(0);
    expect(s2PlanState({ mode: "proactive", goal: "拿到账本", conflict: "三轮受阻", setback: "" }, "proactive")).toBe(1);
    expect(s2PlanState(null, "proactive")).toBe(0);
  });

  it("s2PlanAuto：逐场覆盖与三槽填满都按 09 的类型判", () => {
    const scenes = { list: [
      { id: "S01", type: "proactive" },
      { id: "S02", type: "proactive" },   // 09 已切回主动，存储的 plan 还是 RDD
    ] };
    const planning = { plans: {
      S01: { mode: "proactive", goal: "拿到账本", conflict: "三轮受阻", setback: "账本被烧" },
      S02: { mode: "reactive", reaction: "手抖", dilemma: "报警或沉默", decision: "去找证人" },
    } };
    const auto = s2PlanAuto(planning, scenes);
    const byTitle = Object.fromEntries(auto.map(a => [a.t, a]));
    expect(byTitle["逐场覆盖"].val).toBe("1/2 场已规划");
    expect(byTitle["三槽填满"].val).toBe("1/2 场三槽齐");
    expect(byTitle["链条衔接"].pass).toBe(true);
  });

  it("E3 第二步：需复核只来自后端 status=stale（未确认仍有效）；漂移上游按 input_refs 对照当前 step_run_id", () => {
    const health = {
      audience: { beStatus: "approved", stepRunId: "run_brief_v1", inputRefs: {} },
      logline: { beStatus: "approved", stepRunId: "run_logline_v2", inputRefs: { book_brief: "run_brief_v1" } },
      paragraph: { beStatus: "stale", staleAcceptedAt: null, staleReason: "one_sentence_summary 改了被消费字段 ['summary']",
        stepRunId: "run_para_v1", inputRefs: { book_brief: "run_brief_v1", one_sentence_summary: "run_logline_v1" } },
      // 作者已「确认仍有效」：后端刷新了它消费的版本，不再进需复核图
      characters: { beStatus: "stale", staleAcceptedAt: "2026-09-13T10:00:00Z", stepRunId: "run_chars_v1", inputRefs: { one_sentence_summary: "run_logline_v2" } },
      // 上游有了新版本但后端没判定失效（消费的字段没变）：只是漂移提示，不算需复核
      synopsis: { beStatus: "approved", stepRunId: "run_syn_v1", inputRefs: { one_sentence_summary: "run_logline_v1" } },
      // 后端 stale 但没有 input_refs 记录（旧数据）：仍需复核，漂移列表为空
      outline: { beStatus: "stale", staleAcceptedAt: null, stepRunId: "run_out_v1", inputRefs: {} },
    };
    expect(s2UpstreamDrift(health, "paragraph")).toEqual(["logline"]);   // book_brief 没变，不在列表里
    expect(s2UpstreamDrift(health, "synopsis")).toEqual(["logline"]);
    expect(s2UpstreamDrift(health, "characters")).toEqual([]);
    expect(s2StaleMap(health)).toEqual({ paragraph: ["logline"], outline: [] });
    expect(s2StaleMap({})).toEqual({});
    expect(s2StaleMap(undefined)).toEqual({});
  });

  it("E3 第二步：旧缓存里的 revs / confirmRevs 被归一化丢弃，不再进入状态", () => {
    const normalized = s2NormalizeState({ drafts: {}, states: { logline: "done" }, revs: { logline: 3 }, confirmRevs: { paragraph: { logline: 2 } } });
    expect(normalized).not.toHaveProperty("revs");
    expect(normalized).not.toHaveProperty("confirmRevs");
    expect(normalized.states.logline).toBe("done");
  });
});
