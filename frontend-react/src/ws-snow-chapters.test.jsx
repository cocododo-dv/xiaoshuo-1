import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WsChapterPlanPanel, buildChapterPlanPayload, chapterActRuns, moveSceneToChapter, rhythmSummary } from "./ws-snow-chapters.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const mounted = [];

async function renderPanel(props = {}, options = {}) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  const panel = <WsChapterPlanPanel onClose={vi.fn()} onDone={vi.fn()} {...props} />;
  await act(async () => root.render(options.strict ? <React.StrictMode>{panel}</React.StrictMode> : panel));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
  return host;
}

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  vi.restoreAllMocks();
  try { delete window.SnowSync; } catch (e) {}
});

const panelPreview = (gate = null) => ({
  strategy: "spine_anchor",
  chapters: [{
    row_uid: "c1", chapter_seq: 1, act: 1, title: "雨夜来信", spine: "灾一", chapter_goal: "逼主角回乡",
    scenes: [{ scene_plan_id: "sp1", title: "旧信抵达", primary_form: "proactive", spine: "灾一", anchored: true, planned: true }],
  }],
  unassigned: [], warnings: [],
  materialization_gate: gate,
});

/* 分章面板的纯逻辑：本地态编辑 → 提交载荷。
   面板的全部价值在于「按下确认之前就看得见会得到什么」，所以这里守的是
   「屏幕上显示的归属与顺序，就是发给后端的归属与顺序」这条等式。 */

const draft = () => ({
  strategy: "spine_anchor",
  chapters: [
    { rowUid: "c1", title: "雨夜来信", act: 1, spine: "", chapterGoal: "把她拉回雨城", scenes: [
      { scenePlanId: "sp1", title: "收到旧信" },
      { scenePlanId: "sp2", title: "决定回去" },
    ] },
    { rowUid: "c2", title: "旧案卷宗", act: 1, spine: "灾一", chapterGoal: "翻出案卷", scenes: [
      { scenePlanId: "sp3", title: "封存的卷宗" },
    ] },
  ],
  unassigned: [{ scenePlanId: "sp4", title: "没分到的一场" }],
  warnings: [],
});

describe("分章面板 · 提交载荷", () => {
  it("章内顺序即 scene_seq，且逐章从 1 重新计数", () => {
    const payload = buildChapterPlanPayload(draft());
    expect(payload.assignments).toEqual([
      { scene_plan_id: "sp1", chapter_row_uid: "c1", scene_seq: 1 },
      { scene_plan_id: "sp2", chapter_row_uid: "c1", scene_seq: 2 },
      { scene_plan_id: "sp3", chapter_row_uid: "c2", scene_seq: 1 },
    ]);
  });

  it("未分配的场不进 assignments —— 它们不该被静默塞进某一章", () => {
    const payload = buildChapterPlanPayload(draft());
    expect(payload.assignments.some(a => a.scene_plan_id === "sp4")).toBe(false);
  });

  it("章的标题/幕/脊柱/章目标随分章一起提交（作者可在面板里直接改）", () => {
    const d = draft();
    d.chapters[0].title = "改过的章名";
    const payload = buildChapterPlanPayload(d);
    expect(payload.chapters[0]).toEqual({
      row_uid: "c1", title: "改过的章名", act: 1, spine: "", chapter_goal: "把她拉回雨城",
    });
  });
});

describe("分章面板 · 移动场景", () => {
  it("移到下一章：从源章移除并追加到目标章末尾", () => {
    const next = moveSceneToChapter(draft(), 0, 0, 1);
    expect(next.chapters[0].scenes.map(s => s.scenePlanId)).toEqual(["sp2"]);
    expect(next.chapters[1].scenes.map(s => s.scenePlanId)).toEqual(["sp3", "sp1"]);
  });

  it("未分配区 → 某一章：从未分配移除，assignments 随即包含它", () => {
    const next = moveSceneToChapter(draft(), -1, 0, 0);
    expect(next.unassigned).toHaveLength(0);
    expect(buildChapterPlanPayload(next).assignments.map(a => a.scene_plan_id))
      .toEqual(["sp1", "sp2", "sp4", "sp3"]);
  });

  it("越界目标不改变任何东西（不能把场移丢）", () => {
    const before = draft();
    const next = moveSceneToChapter(before, 0, 0, 99);
    expect(next).toBe(before);
  });

  it("不改动传入对象（面板靠不可变更新驱动重渲染）", () => {
    const before = draft();
    const snapshot = JSON.stringify(before);
    moveSceneToChapter(before, 0, 0, 1);
    expect(JSON.stringify(before)).toBe(snapshot);
  });

  it("反复搬动不丢场：总场数守恒", () => {
    let d = draft();
    const total = d.chapters.reduce((n, c) => n + c.scenes.length, 0) + d.unassigned.length;
    d = moveSceneToChapter(d, -1, 0, 1);
    d = moveSceneToChapter(d, 1, 0, 0);
    d = moveSceneToChapter(d, 0, 2, 1);
    const after = d.chapters.reduce((n, c) => n + c.scenes.length, 0) + d.unassigned.length;
    expect(after).toBe(total);
  });
});

describe("分章面板 · 节奏体检（P3）", () => {
  const rhythm = (over = {}) => ({
    scene_counts: [2, 2, 1, 3, 2, 2],
    mean_scenes_per_chapter: 2,
    min_scenes: 1,
    max_scenes: 3,
    empty_chapter_count: 0,
    acts: [
      { act: 1, chapter_count: 3, scene_count: 5 },
      { act: 2, chapter_count: 2, scene_count: 5 },
      { act: 3, chapter_count: 1, scene_count: 2 },
    ],
    spine_placement: [
      { spine: "灾一", placed: true, on_hinge: true },
      { spine: "灾二", placed: true, on_hinge: true },
      { spine: "灾三", placed: true, on_hinge: true },
    ],
    ...over,
  });

  it("报每章场数区间、均值和三幕配比", () => {
    const lines = rhythmSummary(rhythm());
    expect(lines[0]).toBe("每章 1–3 场 · 均值 2");
    expect(lines).toContain("第 2 幕：2 章 / 5 场");
  });

  it("三个灾难都在铰链上时明确说出来（沉默不等于合格）", () => {
    expect(rhythmSummary(rhythm())).toContain("三个灾难都落在幕的铰链上");
  });

  it("有灾难偏离或缺失时不给「都合格」的结论", () => {
    const offHinge = rhythm({ spine_placement: [
      { spine: "灾一", placed: true, on_hinge: false },
      { spine: "灾二", placed: true, on_hinge: true },
      { spine: "灾三", placed: false },
    ] });
    expect(rhythmSummary(offHinge)).not.toContain("三个灾难都落在幕的铰链上");
  });

  it("没有体检数据时返回空数组，不编造结论", () => {
    expect(rhythmSummary(null)).toEqual([]);
  });
});


describe("分章面板 · 幕分段", () => {
  /* 交错的章表：模型给的 act 是 [1, 2, 1]，数组顺序（= 阅读顺序 = 保存顺序）是 A、B、C。 */
  const interleaved = [
    { rowUid: "cA", title: "A", act: 1, scenes: [] },
    { rowUid: "cB", title: "B", act: 2, scenes: [] },
    { rowUid: "cC", title: "C", act: 1, scenes: [] },
  ];

  it("按数组顺序切段，不把同名幕合并到一起", () => {
    const runs = chapterActRuns(interleaved);
    expect(runs.map(r => [r.act, r.chapters.map(c => c.title)])).toEqual([
      [1, ["A"]],
      [2, ["B"]],
      [1, ["C"]],
    ]);
  });

  it("屏幕上的章序 === 提交载荷里的章序", () => {
    // 这才是面板的全部价值：作者确认的顺序就是写进目录的顺序。
    // 按幕重新分组时显示的是 A、C、B，提交的却是 A、B、C —— 所见非所存。
    const onScreen = chapterActRuns(interleaved).flatMap(r => r.chapters.map(c => c.rowUid));
    const submitted = buildChapterPlanPayload({ chapters: interleaved, unassigned: [] })
      .chapters.map(c => c.row_uid);
    expect(onScreen).toEqual(submitted);
  });

  it("每一章的 index 指回它在数组里的真实位置（移动按钮和标题编辑都靠它）", () => {
    const runs = chapterActRuns(interleaved);
    expect(runs.flatMap(r => r.chapters.map(c => c.index))).toEqual([0, 1, 2]);
  });

  it("幕连续时仍然合成一段", () => {
    const runs = chapterActRuns([
      { rowUid: "c1", act: 1, scenes: [] },
      { rowUid: "c2", act: 1, scenes: [] },
      { rowUid: "c3", act: 3, scenes: [] },
    ]);
    expect(runs.map(r => r.act)).toEqual([1, 3]);
    expect(runs[0].chapters).toHaveLength(2);
  });

  it("空章表不产出任何段", () => {
    expect(chapterActRuns([])).toEqual([]);
    expect(chapterActRuns(null)).toEqual([]);
  });
});

describe("分章面板 · 物化闸门衔接", () => {
  it("StrictMode 重放挂载副作用时复用同一预览请求，不触发幂等在途冲突", async () => {
    window.SnowSync = {
      chapterPreview: vi.fn(async () => {
        await new Promise(resolve => setTimeout(resolve, 20));
        return panelPreview({ status: "ready", blockers: [], warnings: [], items: [] });
      }),
    };

    const host = await renderPanel({}, { strict: true });
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); });

    expect(window.SnowSync.chapterPreview).toHaveBeenCalledTimes(1);
    expect(host.textContent).not.toContain("同一请求");
    expect(host.querySelector('[data-testid="chapter-plan-confirm"]')).toBeTruthy();
  });

  it("阶段 K：07 没出章表时，面板提供「按场景生成章表」，提议后重新拉预览", async () => {
    let previewCalls = 0;
    window.SnowSync = {
      chapterPreview: vi.fn(async () => {
        previewCalls += 1;
        if (previewCalls === 1) {
          throw Object.assign(new Error("07 长篇大纲还没有可用章节，先去把章列出来再分章。"), { code: "SNOWFLAKE_CHAPTER_PLAN_EMPTY", status: 409 });
        }
        return panelPreview({ status: "ready", blockers: [], warnings: [], items: [] });
      }),
      chapterPropose: vi.fn(async () => ({ created_chapter_count: 1 })),
    };
    const host = await renderPanel();
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)); });
    const button = host.querySelector('[data-testid="chapter-plan-propose"]');
    expect(button).toBeTruthy();
    expect(button.textContent).toContain("按场景生成章表");
    await act(async () => { button.click(); await new Promise(resolve => setTimeout(resolve, 10)); });
    expect(window.SnowSync.chapterPropose).toHaveBeenCalledWith({});
    expect(window.SnowSync.chapterPreview).toHaveBeenCalledTimes(2);
    expect(host.querySelector('[data-testid="chapter-plan-confirm"]')).toBeTruthy();
    // 有章表之后按钮变成「重排」，需要确认
    expect(host.querySelector('[data-testid="chapter-plan-propose"]').textContent).toContain("按场景重排章表");
  });

  it("预览返回必修阻断时禁用确认，并提供回到具体雪花步骤的动作", async () => {
    const onGoToStep = vi.fn();
    window.SnowSync = {
      chapterPreview: vi.fn(async () => panelPreview({
        status: "blocked",
        blockers: ["场景细化需要先确认。"],
        warnings: [],
        items: [{
          id: "blocker:unapproved_required_step:scene_details",
          severity: "blocker",
          kind: "unapproved_required_step",
          message: "场景细化需要先确认，才能整理章节结构。",
          step_key: "scene_details",
          primary_action: { type: "jump_to_step", label: "去补这一步", step_key: "scene_details" },
        }],
      })),
    };

    const host = await renderPanel({ onGoToStep });
    const confirm = host.querySelector('[data-testid="chapter-plan-confirm"]');
    expect(confirm.disabled).toBe(true);
    expect(host.textContent).toContain("场景细化需要先确认，才能整理章节结构。");
    const jump = [...host.querySelectorAll("button")].find(button => button.textContent.includes("去补这一步"));
    expect(jump).toBeTruthy();
    await act(async () => jump.click());
    expect(onGoToStep).toHaveBeenCalledWith("scene_details");
  });

  it("确认时后端返回更新后的闸门详情：保留预览并把具体阻断项转成可操作提示", async () => {
    const error = Object.assign(new Error("雪花工作台还没有通过整理前的检查。"), {
      code: "SNOWFLAKE_NOT_READY",
      details: {
        materialization_gate: {
          status: "blocked",
          blockers: ["场景清单需要先确认。"],
          warnings: [],
          items: [{
            id: "blocker:unapproved_required_step:scene_list",
            severity: "blocker",
            kind: "unapproved_required_step",
            message: "场景清单需要先确认，才能整理章节结构。",
            step_key: "scene_list",
            primary_action: { type: "jump_to_step", label: "去补这一步", step_key: "scene_list" },
          }],
        },
      },
    });
    window.SnowSync = {
      chapterPreview: vi.fn(async () => panelPreview({ status: "ready", blockers: [], warnings: [], items: [] })),
      materialize: vi.fn(async () => { throw error; }),
    };

    const host = await renderPanel();
    const confirm = host.querySelector('[data-testid="chapter-plan-confirm"]');
    expect(confirm.disabled).toBe(false);
    await act(async () => confirm.click());

    expect(host.textContent).toContain("场景清单需要先确认，才能整理章节结构。");
    expect(confirm.disabled).toBe(true);
    expect(host.querySelector('[role="alert"]')).toBeTruthy();
  });

  it("同步模块意外未装配时显示中文恢复提示，不泄漏原始 TypeError", async () => {
    window.SnowSync = {};
    const host = await renderPanel();
    expect(host.textContent).toContain("雪花同步模块尚未就绪，请刷新页面后重试。");
    expect(host.textContent).not.toContain("Cannot read properties of undefined");
    expect(host.querySelector('[role="alert"]')).toBeTruthy();
  });
});
