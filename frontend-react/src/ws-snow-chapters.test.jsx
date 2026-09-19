import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  WsChapterPlanPanel, applyChapterNames, buildChapterPlanPayload, buildChapterTitlesRequest, chapterActRuns,
  homeChapterFor, isAutoChapterTitle, mergeChapterIntoPrevious, moveSceneToChapter, rhythmSummary,
  scaleExplanation, splitChapterAt,
} from "./ws-snow-chapters.jsx";

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
  /* 章是场景列表上连续的一段：只有章界上的场能挪，而且挪过去之后仍然要保持故事序。
     旧实现把任何一场「追加到目标章末尾」——末场移到下一章会排到那一章所有场的后面，
     作者手调一次章界，目录里的顺序就乱一次。 */
  it("末场移到下一章：成为下一章的第一场（不是追加到末尾）", () => {
    const next = moveSceneToChapter(draft(), 0, 1, 1);
    expect(next.chapters[0].scenes.map(s => s.scenePlanId)).toEqual(["sp1"]);
    expect(next.chapters[1].scenes.map(s => s.scenePlanId)).toEqual(["sp2", "sp3"]);
  });

  it("首场并入上一章：成为上一章的最后一场", () => {
    const next = moveSceneToChapter(draft(), 1, 0, 0);
    expect(next.chapters[0].scenes.map(s => s.scenePlanId)).toEqual(["sp1", "sp2", "sp3"]);
    expect(next.chapters[1].scenes).toHaveLength(0);
  });

  it("不在章界上的场挪不动——那会打破「章是连续的一段」", () => {
    const before = draft();
    expect(moveSceneToChapter(before, 0, 0, 1)).toBe(before); // 首场不能往后跳
    const three = draft();
    three.chapters[0].scenes.push({ scenePlanId: "sp9", title: "第三场" });
    expect(moveSceneToChapter(three, 0, 1, 1)).toBe(three);   // 中间的场不能跳
    expect(moveSceneToChapter(three, 0, 2, 0)).toBe(three);   // 挪到自己这一章 = 不动
  });

  it("灾难标记跟着场走：带标记的末场移走后，章上的标记一起过去", () => {
    const d = draft();
    d.chapters[0].spine = "灾一";
    d.chapters[0].scenes[1].spine = "灾一";
    d.chapters[1].spine = "";
    const next = moveSceneToChapter(d, 0, 1, 1);
    expect(next.chapters.map(c => c.spine)).toEqual(["", "灾一"]);
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

describe("分章面板 · 拆章 / 并章 / 归位", () => {
  const ordered = () => ({
    strategy: "keep_current",
    chapters: [
      { rowUid: "c1", title: "一", act: 1, spine: "", chapterGoal: "", scenes: [1, 2, 3].map(i => ({ scenePlanId: `sp${i}`, storyIndex: i, spine: i === 3 ? "灾一" : "" })) },
      { rowUid: "c2", title: "二", act: 2, spine: "", chapterGoal: "", scenes: [4, 5].map(i => ({ scenePlanId: `sp${i}`, storyIndex: i, spine: "" })) },
    ],
    unassigned: [],
    warnings: [],
  });

  it("从某一场另起一章：它和后面的场进一章新章，幕沿用原章，灾难标记跟着场", () => {
    const next = splitChapterAt(ordered(), 0, 1);
    expect(next.chapters.map(c => c.scenes.map(s => s.scenePlanId))).toEqual([["sp1"], ["sp2", "sp3"], ["sp4", "sp5"]]);
    expect(next.chapters[1].rowUid.startsWith("new:")).toBe(true);
    expect(next.chapters[1].act).toBe(1);
    expect(next.chapters.map(c => c.spine)).toEqual(["", "灾一", ""]);
  });

  it("占位章名「第 N 章」跟着章序重编，作者起的名字不动", () => {
    const d = ordered();
    d.chapters[0].title = "第 1 章";
    d.chapters[1].title = "磁带";
    let next = splitChapterAt(d, 0, 1);
    expect(next.chapters.map(c => c.title)).toEqual(["第 1 章", "第 2 章", "磁带"]);
    next = mergeChapterIntoPrevious(next, 1);
    expect(next.chapters.map(c => c.title)).toEqual(["第 1 章", "磁带"]);
  });

  it("从首场「另起一章」没有意义：不动", () => {
    const before = ordered();
    expect(splitChapterAt(before, 0, 0)).toBe(before);
    expect(splitChapterAt(before, 0, 9)).toBe(before);
  });

  it("与上一章合并：场接在上一章后面，这一章从章表里拿掉", () => {
    const next = mergeChapterIntoPrevious(ordered(), 1);
    expect(next.chapters).toHaveLength(1);
    expect(next.chapters[0].scenes.map(s => s.scenePlanId)).toEqual(["sp1", "sp2", "sp3", "sp4", "sp5"]);
    const before = ordered();
    expect(mergeChapterIntoPrevious(before, 0)).toBe(before);
  });

  it("拆了再并，回到原样的归属（总场数守恒、顺序不变）", () => {
    let d = splitChapterAt(ordered(), 0, 2);
    d = mergeChapterIntoPrevious(d, 1);
    expect(d.chapters.map(c => c.scenes.map(s => s.storyIndex))).toEqual([[1, 2, 3], [4, 5]]);
  });

  it("未分配的场按场景列表的序号归位，而不是被追加到章末", () => {
    const d = ordered();
    const stray = { scenePlanId: "sp9", storyIndex: 2.5 };
    d.unassigned = [stray];
    expect(homeChapterFor(d, stray)).toBe(0);
    const next = moveSceneToChapter(d, -1, 0, 0);
    expect(next.chapters[0].scenes.map(s => s.scenePlanId)).toEqual(["sp1", "sp2", "sp9", "sp3"]);
    expect(homeChapterFor(d, { scenePlanId: "x", storyIndex: 9 })).toBe(1);
  });

  it("提交载荷声明这是整张章表，并带上章摘要", () => {
    const d = ordered();
    d.chapters[0].summary = "推到悬崖";
    const payload = buildChapterPlanPayload(d);
    expect(payload.replace_chapters).toBe(true);
    expect(payload.chapters[0].summary).toBe("推到悬崖");
  });
});

describe("分章面板 · AI 起章名（阶段 W）", () => {
  const named = () => ({
    strategy: "from_scenes",
    chapters: [
      { rowUid: "new:1", title: "第 1 章", act: 1, spine: "灾一", chapterGoal: "", summary: "第 2 场的摘要",
        scenes: [{ scenePlanId: "sp1", storyIndex: 1, summary: "第 1 场的摘要" }, { scenePlanId: "sp2", storyIndex: 2, summary: "第 2 场的摘要" }] },
      { rowUid: "c2", title: "磁带", act: 2, spine: "", chapterGoal: "", summary: "他听见了自己的声音。",
        scenes: [{ scenePlanId: "sp3", storyIndex: 3, summary: "第 3 场的摘要" }] },
      { rowUid: "new:3", title: "", act: 3, spine: "", chapterGoal: "", summary: "", scenes: [] },
    ],
    unassigned: [], warnings: [],
  });

  it("只有空的 / 「第 N 章」/「（待补）」算没起名", () => {
    expect(["", "第 3 章", "第12章", "（待补）"].map(isAutoChapterTitle)).toEqual([true, true, true, true]);
    expect(["旧日志", "第三章 雪夜"].map(isAutoChapterTitle)).toEqual([false, false]);
  });

  it("请求带的是面板此刻的章表（含还没确认的 new:* 章），空章不送去起名", () => {
    expect(buildChapterTitlesRequest(named())).toEqual([
      { row_uid: "new:1", title: "第 1 章", act: 1, spine: "灾一", scene_plan_ids: ["sp1", "sp2"] },
      { row_uid: "c2", title: "磁带", act: 2, spine: "", scene_plan_ids: ["sp3"] },
    ]);
  });

  it("只替系统起的占位名；从场上抄来的章摘要换成 AI 写的，作者写过的章名与摘要一个字不动", () => {
    const before = named();
    const { draft: next, applied } = applyChapterNames(before, [
      { row_uid: "new:1", title: "旧日志", summary: "他确认记录被人遮过。" },
      { row_uid: "c2", title: "不该覆盖作者的章名", summary: "也不该覆盖作者的摘要" },
      { row_uid: "ghost", title: "幽灵章" },
    ]);
    expect(applied).toBe(1);
    expect(next.chapters.map(c => [c.title, c.summary])).toEqual([
      ["旧日志", "他确认记录被人遮过。"], ["磁带", "他听见了自己的声音。"], ["", ""],
    ]);
    expect(before.chapters[0].title).toBe("第 1 章"); // 不改传入对象
  });

  it("作者自己写过的章摘要不被 AI 的摘要替掉；renameAll 才连作者的章名一起重起", () => {
    const d = named();
    d.chapters[0].summary = "我自己写的一句。";
    expect(applyChapterNames(d, [{ row_uid: "new:1", title: "旧日志", summary: "AI 的摘要" }]).draft.chapters[0].summary)
      .toBe("我自己写的一句。");
    const all = applyChapterNames(d, [{ row_uid: "c2", title: "回声" }], { renameAll: true });
    expect(all.draft.chapters[1].title).toBe("回声");
  });

  it("一个可用的章名都没有：返回原对象，不标脏", () => {
    const d = named();
    const result = applyChapterNames(d, [{ row_uid: "new:1", title: "  " }]);
    expect(result.applied).toBe(0);
    expect(result.draft).toBe(d);
  });

  it("面板：点「AI 起章名」→ 章名填进面板、可改、确认时随整张章表提交；都起过名字时按钮点不动", async () => {
    const preview = {
      ...panelPreview({ status: "ready", blockers: [], warnings: [], items: [] }),
      strategy: "from_scenes",
      chapters: [
        { row_uid: "new:1", chapter_seq: 1, act: 1, title: "第 1 章", spine: "", chapter_goal: "", summary: "第 1 场",
          scenes: [{ scene_plan_id: "sp1", story_index: 1, title: "第 1 场", summary: "第 1 场", primary_form: "proactive", planned: true }] },
        { row_uid: "new:2", chapter_seq: 2, act: 2, title: "第 2 章", spine: "", chapter_goal: "", summary: "第 2 场",
          scenes: [{ scene_plan_id: "sp2", story_index: 2, title: "第 2 场", summary: "第 2 场", primary_form: "proactive", planned: true }] },
      ],
      chapter_table: { count: 0, authored: false, saved: false },
    };
    const chapterTitles = vi.fn(async () => ({
      titles: [{ row_uid: "new:1", title: "旧日志", summary: "他确认记录被人遮过。" }, { row_uid: "new:2", title: "磁带", summary: "" }],
      named_count: 2, remaining_count: 0, notice: null,
    }));
    const materialize = vi.fn(async () => ({ created_chapter_count: 2 }));
    window.SnowSync = { chapterPreview: vi.fn(async () => preview), chapterTitles, materialize };
    const host = await renderPanel();
    const button = host.querySelector('[data-testid="chapter-plan-name"]');
    expect(button.disabled).toBe(false);
    await act(async () => { button.click(); await new Promise(resolve => setTimeout(resolve, 10)); });
    expect(chapterTitles).toHaveBeenCalledWith([
      { row_uid: "new:1", title: "第 1 章", act: 1, spine: "", scene_plan_ids: ["sp1"] },
      { row_uid: "new:2", title: "第 2 章", act: 2, spine: "", scene_plan_ids: ["sp2"] },
    ]);
    const titles = [...host.querySelectorAll(".sf-chapterplan-title")].map(el => el.value);
    expect(titles).toEqual(["旧日志", "磁带"]);
    expect(host.querySelector('[data-testid="chapter-plan-summary-0"]').value).toBe("他确认记录被人遮过。");
    expect(host.querySelector('[data-testid="chapter-plan-name-note"]').textContent).toContain("AI 起了 2 个章名");
    expect(host.querySelector('[data-testid="chapter-plan-name"]').disabled).toBe(true); // 没有占位名了

    await act(async () => { host.querySelector('[data-testid="chapter-plan-confirm"]').click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    const payload = materialize.mock.calls[0][1];
    expect(payload.chapters.map(c => [c.title, c.summary])).toEqual([["旧日志", "他确认记录被人遮过。"], ["磁带", "第 2 场"]]);
  });

  it("面板：LLM 没配好时如实报错，章名原样不动", async () => {
    window.SnowSync = {
      chapterPreview: vi.fn(async () => ({ ...panelPreview({ status: "ready", blockers: [], warnings: [], items: [] }),
        chapters: [{ row_uid: "c1", chapter_seq: 1, act: 1, title: "第 1 章", spine: "", chapter_goal: "",
          scenes: [{ scene_plan_id: "sp1", story_index: 1, title: "第 1 场", primary_form: "proactive", planned: true }] }] })),
      chapterTitles: vi.fn(async () => { throw Object.assign(new Error("雪花工作台的 AI 生成需要先启用真实模型。"), { code: "SNOWFLAKE_LLM_NOT_CONFIGURED", status: 409 }); }),
    };
    const host = await renderPanel();
    await act(async () => { host.querySelector('[data-testid="chapter-plan-name"]').click(); await new Promise(resolve => setTimeout(resolve, 10)); });
    expect(host.textContent).toContain("需要先启用真实模型");
    expect(host.querySelector(".sf-chapterplan-title").value).toBe("第 1 章");
  });
});

describe("分章面板 · 每章场数的来历", () => {
  it("参考书、作品设置、作者填写、默认值各有一句人话", () => {
    expect(scaleExplanation({ source: "reference", scenes_per_chapter: 12, scene_count: 17, hinge_min_chapters: 4, reference_hint: { chapter_chars_median: 17948 } }))
      .toBe("参考书一章约 1.8 万字 ≈ 12 场 · 三个灾难各自收束一章，所以至少 4 章");
    expect(scaleExplanation({ source: "project_target", target_chapter_count: 20, scenes_per_chapter: 3, scene_count: 60, hinge_min_chapters: 4 }))
      .toBe("作品设置：目标 20 章");
    expect(scaleExplanation({ source: "request_per_chapter", scenes_per_chapter: 4, scene_count: 17, hinge_min_chapters: 4 }))
      .toBe("按你填的每章约 4 场");
    expect(scaleExplanation({ source: "default", scenes_per_chapter: 3, scene_count: 17, hinge_min_chapters: 4 })).toBe("默认每章约 3 场");
    expect(scaleExplanation(null)).toBe("");
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

  it("打开时让服务端挑方案（auto）；按场景分章时给出「每章约 N 场」并说明这个数从哪来", async () => {
    const fromScenes = (per) => ({
      ...panelPreview({ status: "ready", blockers: [], warnings: [], items: [] }),
      strategy: "from_scenes",
      chapters: [{ row_uid: "new:1", chapter_seq: 1, act: 1, title: "第 1 章", spine: "灾一", chapter_goal: "", summary: "推到悬崖",
        scenes: [{ scene_plan_id: "sp1", story_index: 1, function: "起势/建置", title: "旧信抵达", summary: "旧信抵达", primary_form: "proactive", spine: "", planned: true }] }],
      scale: { source: per === 12 ? "reference" : "request_per_chapter", scenes_per_chapter: per, target_chapter_count: 0, scene_count: 17, hinge_min_chapters: 4,
        reference_hint: per === 12 ? { chapter_chars_median: 17948, scenes_per_chapter: 12 } : null },
      chapter_table: { count: 2, authored: false, saved: false },
      replaces_chapter_count: 2,
    });
    window.SnowSync = { chapterPreview: vi.fn(async (strategy, options) => fromScenes((options && options.scenesPerChapter) || 12)) };
    const host = await renderPanel();
    expect(window.SnowSync.chapterPreview).toHaveBeenCalledWith("auto", {});
    const scale = host.querySelector('[data-testid="chapter-plan-scale"]');
    expect(scale.textContent).toContain("参考书一章约 1.8 万字 ≈ 12 场");
    expect(scale.textContent).toContain("至少 4 章");
    expect(scale.textContent).toContain("替换现有的 2 章章表");
    // 07 里只有占位章：「倒进 07 章表」两种分法点不动；没有分过章：「已保存的分章」点不动
    expect(host.querySelector('[data-testid="chapter-plan-strategy-spine_anchor"]').disabled).toBe(true);
    expect(host.querySelector('[data-testid="chapter-plan-strategy-keep_current"]').disabled).toBe(true);
    // 新章还没落库，AI 看不见它们
    expect(host.querySelector('[data-testid="chapter-plan-suggest"]').disabled).toBe(true);
    // 场带着场景列表里的序号和功能标签
    expect(host.textContent).toContain("01");
    expect(host.textContent).toContain("起势/建置");

    const input = host.querySelector('[data-testid="chapter-plan-per-chapter"]');
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
      setter.call(input, "4");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { host.querySelector('[data-testid="chapter-plan-rescale"]').click(); await new Promise(resolve => setTimeout(resolve, 10)); });
    expect(window.SnowSync.chapterPreview).toHaveBeenLastCalledWith("from_scenes", { scenesPerChapter: 4 });
    expect(host.querySelector('[data-testid="chapter-plan-scale"]').textContent).toContain("按你填的每章约 4 场");
  });

  it("面板里拆章 / 并章：确认时交整张章表（replace_chapters），新章用 new:* 身份", async () => {
    const preview = {
      ...panelPreview({ status: "ready", blockers: [], warnings: [], items: [] }),
      strategy: "keep_current",
      chapters: [
        { row_uid: "c1", chapter_seq: 1, act: 1, title: "雨夜来信", spine: "", chapter_goal: "",
          scenes: [1, 2, 3].map(i => ({ scene_plan_id: `sp${i}`, story_index: i, title: `第 ${i} 场`, primary_form: "proactive", planned: true })) },
        { row_uid: "c2", chapter_seq: 2, act: 1, title: "旧案卷宗", spine: "", chapter_goal: "",
          scenes: [{ scene_plan_id: "sp4", story_index: 4, title: "第 4 场", primary_form: "proactive", planned: true }] },
      ],
      chapter_table: { count: 2, authored: true, saved: true },
    };
    const materialize = vi.fn(async () => ({ created_chapter_count: 2 }));
    window.SnowSync = { chapterPreview: vi.fn(async () => preview), materialize };
    const onDone = vi.fn();
    const host = await renderPanel({ onDone });
    await act(async () => host.querySelector('[data-testid="chapter-plan-split-0-2"]').click());
    await act(async () => host.querySelector('[data-testid="chapter-plan-merge-2"]').click());
    await act(async () => { host.querySelector('[data-testid="chapter-plan-confirm"]').click(); await new Promise(resolve => setTimeout(resolve, 0)); });
    const payload = materialize.mock.calls[0][1];
    expect(payload.replace_chapters).toBe(true);
    expect(payload.chapters.map(c => c.row_uid.startsWith("new:") ? "new" : c.row_uid)).toEqual(["c1", "new"]);
    const newUid = payload.chapters[1].row_uid;
    expect(payload.assignments.map(a => [a.scene_plan_id, a.chapter_row_uid])).toEqual([
      ["sp1", "c1"], ["sp2", "c1"], ["sp3", newUid], ["sp4", newUid],
    ]);
    expect(onDone).toHaveBeenCalled();
  });

  it("后端报「章不是连续的一段」时，提醒旁边就有「按场景重新分章」", async () => {
    window.SnowSync = {
      chapterPreview: vi.fn(async () => ({
        ...panelPreview({ status: "ready", blockers: [], warnings: [], items: [] }),
        strategy: "keep_current",
        warnings: [{ kind: "chapter_order_conflict", severity: "advisory", message: "「第 10 场」在场景列表里排在前面几章的场之前。" }],
        chapter_table: { count: 1, authored: true, saved: true },
      })),
    };
    const host = await renderPanel();
    const fix = host.querySelector('[data-testid="chapter-plan-fix-order"]');
    expect(fix).toBeTruthy();
    await act(async () => { fix.click(); await new Promise(resolve => setTimeout(resolve, 10)); });
    expect(window.SnowSync.chapterPreview).toHaveBeenLastCalledWith("from_scenes", {});
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
