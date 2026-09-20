import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const catalogState = vi.hoisted(() => ({ ready: false, chapters: [], error: null }));

vi.mock("./ws-catalog.jsx", () => ({
  WsCatalog: {
    ready: vi.fn(() => catalogState.ready),
    loadError: vi.fn(() => catalogState.error),
    get: vi.fn(() => catalogState.chapters),
    set: vi.fn(),
    __refresh: vi.fn(async () => {}),
    addChapter: vi.fn(),
    removeChapters: vi.fn(),
    removeScenes: vi.fn(),
  },
  useCatalogChapters: () => catalogState.chapters,
}));

vi.mock("./ws-works.jsx", () => ({
  wsKey: (key) => key,
  WsWorks: {
    activeId: () => "project-1",
    active: () => ({ id: "project-1", title: "测试作品" }),
  },
}));

vi.mock("./ws-chapter-run.jsx", () => ({
  ArrChapterRunAction: () => <button type="button">运行本章</button>,
}));

import { WsCatalog } from "./ws-catalog.jsx";
import { WsAuthor } from "./ws-author.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let host;
let root;

/* 一章的最小合法形状（视图直接读 words/drama/threads/scenes，缺一即崩） */
function chapter(id, title, extra = {}) {
  return {
    id,
    backendId: "backend-" + id,
    act: "act1",
    title,
    state: "writing",
    current: false,
    tension: 0.3,
    words: { cur: 0, target: 4000 },
    drama: {},
    threads: [],
    scenes: [],
    ...extra,
  };
}

const click = (node) => node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
const byText = (selector, text) => [...host.querySelectorAll(selector)].find((node) => node.textContent.includes(text));

beforeEach(() => {
  localStorage.clear();
  catalogState.ready = false;
  catalogState.chapters = [];
  catalogState.error = null;
  vi.clearAllMocks();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  delete window.SnowSync;
});

describe("章节编排 · 服务端目录真相", () => {
  it("冷启动目录未就绪时只显示加载态，绝不把空数组写回覆盖服务端", async () => {
    await act(async () => root.render(<WsAuthor />));

    expect(host.textContent).toContain("正在从服务端加载章节目录");
    expect(WsCatalog.set).not.toHaveBeenCalled();

    catalogState.ready = true;
    catalogState.chapters = [{
      id: "ch01",
      backendId: "chapter-1",
      act: "act1",
      title: "服务端真实章节",
      state: "writing",
      current: true,
      tension: 0.3,
      words: { cur: 0, target: 4000 },
      drama: {},
      threads: [],
      scenes: [],
    }];
    await act(async () => root.render(<WsAuthor />));

    expect(host.textContent).toContain("服务端真实章节");
    expect(WsCatalog.set).not.toHaveBeenCalled();
  });

  it("已批准终稿在编排台全字段只读，并引导先重新打开", async () => {
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [{
      id: "ch01",
      backendId: "chapter-1",
      act: "act1",
      title: "锁定章节",
      state: "approved",
      current: false,
      tension: 0.5,
      pov: "林岑",
      words: { cur: 1200, target: 4000 },
      drama: { promise: "承诺", spine: "推进", arc: "转变", problem: "问题", aftertaste: "余味", ending: "章末", forbidden: "无", notes: "备注" },
      threads: [],
      scenes: [{ sid: "s1", backendId: "scene-1", title: "锁定场景", kind: "主动", state: "done", goal: "目标", obstacle: "阻碍", turn: "出口" }],
    }];

    await act(async () => root.render(<WsAuthor />));

    expect(host.textContent).toContain("章节结构与场景卡均为只读");
    expect(host.querySelector('input[aria-label="章节标题"]')).toHaveProperty("disabled", true);
    expect(host.querySelector('input[aria-label="场景标题"]')).toHaveProperty("disabled", true);
    expect([...host.querySelectorAll("button")].find((node) => node.textContent.includes("新场景"))).toHaveProperty("disabled", true);
    expect(WsCatalog.set).not.toHaveBeenCalled();
  });

  it("全书编排多选批量删章：一次 set() 摘掉所选，已批准终稿不可勾选", async () => {
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章"), chapter("ch02", "第二章"), chapter("ch03", "锁定章", { state: "approved" })];
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    await act(async () => root.render(<WsAuthor />));

    await act(async () => click(byText("button", "多选")));
    const boxes = [...host.querySelectorAll('.arr-card-check input')];
    expect(boxes).toHaveLength(3);
    expect(boxes[2]).toHaveProperty("disabled", true);       // 已批准终稿：勾不动

    await act(async () => { boxes[0].click(); });
    await act(async () => { boxes[1].click(); });
    expect(host.textContent).toContain("已选 2 / 2 章");

    await act(async () => click(host.querySelector('[data-testid="author-batch-delete-chapters"]')));

    expect(confirmSpy).toHaveBeenCalled();
    expect(WsCatalog.set).toHaveBeenCalledTimes(1);
    expect(WsCatalog.set.mock.calls[0][0].map((c) => c.id)).toEqual(["ch03"]);
  });

  it("多选是独占模式：进入后头部不再摆新建/切视图，删完给出回收站回执", async () => {
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章"), chapter("ch02", "第二章")];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const go = vi.fn();
    await act(async () => root.render(<WsAuthor go={go} />));

    expect(byText("button", "新建章节")).toBeTruthy();
    await act(async () => click(byText("button", "多选")));

    // 选择态里只剩「选择」这一件事：与选择无关的动作全部收起
    expect(byText("button", "新建章节")).toBeUndefined();
    expect(byText("button", "章节详情")).toBeUndefined();
    expect(host.querySelector('[data-testid="author-chapter-select-exit"]')).toBeTruthy();

    await act(async () => { host.querySelector(".arr-card-check input").click(); });
    await act(async () => click(host.querySelector('[data-testid="author-batch-delete-chapters"]')));

    // 删完给回执，并把「东西去哪了」指出来
    const toast = host.querySelector('[data-testid="undo-toast"]');
    expect(toast.textContent).toContain("移入回收站");
    await act(async () => click(toast.querySelector('[data-testid="undo-toast-action"]')));
    expect(go).toHaveBeenCalledWith("trash");
    // 删除完成即退出多选，头部动作回来
    expect(byText("button", "新建章节")).toBeTruthy();
  });

  it("场景多选态下行内编辑与分流按钮全部收起，避免一次点击有四种含义", async () => {
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章", {
      scenes: [{ sid: "s1", backendId: "b1", title: "开场", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" }],
    })];
    await act(async () => root.render(<WsAuthor />));

    expect(byText("button", "交给 AI")).toBeTruthy();
    expect(host.querySelector('input[aria-label="场景标题"]')).toHaveProperty("disabled", false);

    await act(async () => click(host.querySelector('[data-testid="author-scene-select-mode"]')));

    expect(byText("button", "交给 AI")).toBeUndefined();
    expect(byText("button", "自己写")).toBeUndefined();
    expect(host.querySelector(".arr-scene-more")).toBeNull();
    expect(host.querySelector('input[aria-label="场景标题"]')).toHaveProperty("disabled", true);
  });

  it("批量删除被作者取消时不写目录（confirm 是真闸门，不是装饰）", async () => {
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章"), chapter("ch02", "第二章")];
    vi.spyOn(window, "confirm").mockReturnValue(false);
    await act(async () => root.render(<WsAuthor />));

    await act(async () => click(byText("button", "多选")));
    await act(async () => { host.querySelector(".arr-card-check input").click(); });
    await act(async () => click(host.querySelector('[data-testid="author-batch-delete-chapters"]')));

    expect(WsCatalog.set).not.toHaveBeenCalled();
  });

  it("章节详情场景看板多选批量删场：只摘本章所选场景", async () => {
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章", {
      scenes: [
        { sid: "s1", backendId: "b1", title: "开场", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" },
        { sid: "s2", backendId: "b2", title: "追击", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" },
        { sid: "s3", backendId: "b3", title: "收束", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" },
      ],
    })];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await act(async () => root.render(<WsAuthor />));

    await act(async () => click(host.querySelector('[data-testid="author-scene-select-mode"]')));
    const boxes = [...host.querySelectorAll(".arr-scene-check input")];
    expect(boxes).toHaveLength(3);
    await act(async () => { boxes[0].click(); });
    await act(async () => { boxes[2].click(); });
    expect(host.textContent).toContain("已选 2 / 3 场");

    await act(async () => click(host.querySelector('[data-testid="author-batch-delete-scenes"]')));

    expect(WsCatalog.set).toHaveBeenCalledTimes(1);
    expect(WsCatalog.set.mock.calls[0][0][0].scenes.map((s) => s.sid)).toEqual(["s2"]);
  });

  it("雪花整理出来的场：设计只读、不能拖、不能切形态，给一条直达构思第 10 步那一场的链接；题名照常能改，手加的场照常可编辑", async () => {
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "雨夜来信", {
      scenes: [
        { sid: "SC_plan", backendId: "SC_plan", title: "旧信到了", kind: "主动", state: "todo", goal: "查到寄信人", obstacle: "邮局不肯查", turn: "线索断在码头",
          povName: "林昭", design: { origin: "snowflake", owner: "plan" } },
        { sid: "SC_hand", backendId: "SC_hand", title: "手加的一场", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "",
          design: { origin: "manual", owner: "desk" } },
      ],
    })];
    const { clearViewIntents, flushViewIntents } = await import("./ws-view-intents.js");
    clearViewIntents("snowflake");
    await act(async () => root.render(<WsAuthor />));

    const rows = [...host.querySelectorAll(".arr-scene")];
    const [planRow, handRow] = rows;
    const planInputs = [...planRow.querySelectorAll(".arr-gmc-input")];
    expect(planInputs).toHaveLength(4);
    expect(planInputs.every((input) => input.readOnly)).toBe(true);
    expect(planRow.querySelector(".arr-scene-grip").getAttribute("draggable")).toBe("false");
    expect(planRow.querySelector(".arr-cyc")).toHaveProperty("disabled", true);
    expect(planRow.querySelector('input[aria-label="场景标题"]')).toHaveProperty("disabled", false);
    expect([...handRow.querySelectorAll(".arr-gmc-input")].every((input) => !input.readOnly)).toBe(true);
    expect(handRow.querySelector(".arr-scene-grip").getAttribute("draggable")).toBe("true");
    expect(handRow.querySelector(".arr-cyc")).toHaveProperty("disabled", false);
    expect(handRow.querySelector('[data-testid="arr-scene-edit-plan"]')).toBeNull();

    // 只读不是摆设：失焦不会把一个注定被后端 409 的改动乐观写进目录
    await act(async () => { planInputs[0].focus(); planInputs[0].blur(); });
    await act(async () => click(planRow.querySelector(".arr-cyc")));
    expect(WsCatalog.set).not.toHaveBeenCalled();

    // 「在构思里改」：回构思第 10 步并对准这一场
    const seen = [];
    const onStep = (event) => seen.push(["step", event.detail]);
    const onScene = (event) => seen.push(["scene", event.detail]);
    window.addEventListener("ws:snow-step", onStep);
    window.addEventListener("ws:snow-scene", onScene);
    await act(async () => click(planRow.querySelector('[data-testid="arr-scene-edit-plan"]')));
    expect(window.location.hash).toBe("#snowflake");
    flushViewIntents("snowflake");
    window.removeEventListener("ws:snow-step", onStep);
    window.removeEventListener("ws:snow-scene", onScene);
    expect(seen).toEqual([["step", "planning"], ["scene", "SC_plan"]]);
    window.location.hash = "";
  });

  /* —— 阶段 Z「一张章表、两扇门」 —— */
  const planChapter = (id, title, extra = {}) => chapter(id, title, {
    origin: "snowflake", state: "planned", words: { cur: 0, target: 0 }, tensionSet: false,
    structure: { owner: "plan", rowUid: "row-" + id, sceneRange: null, plannedSceneCount: 0, titleAuto: false },
    ...extra,
  });
  const planScene = (sid, no, extra = {}) => ({
    sid, backendId: sid, title: sid, summary: sid + " 的整句摘要", kind: "主动", state: "todo",
    goal: "目标", obstacle: "阻碍", turn: "挫折", povName: "林昭", exitChange: sid + " 的离场变化",
    design: { origin: "snowflake", owner: "plan", storyIndex: no, storyTime: "第 " + no + " 天", location: "雨城码头" },
    ...extra,
  });
  const installSnowSync = (extra = {}) => {
    window.SnowSync = {
      readyToMaterialize: () => true,
      resyncStatus: () => ({ pendingCount: 0 }),
      chapterPreview: vi.fn(async () => ({
        strategy: "keep_current", chapters: [{ row_uid: "row-ch01", title: "雨夜来信", act: 1, spine: "灾一", chapter_goal: "", summary: "",
          scenes: [{ scene_plan_id: "sp1", scene_id: "SC1", story_index: 1, title: "旧信到了", summary: "旧信到了", primary_form: "proactive", planned: true }] }],
        unassigned: [], warnings: [], chapter_table: { count: 1, authored: true, saved: true },
      })),
      materialize: vi.fn(async () => ({ created_chapter_count: 0, trashed_empty_chapters: [{ chapter_id: "x" }] })),
      feStepKey: (beKey) => ({ scene_details: "planning", scene_list: "scenes" }[beKey] || ""),
      ...extra,
    };
  };

  it("全书编排：构思分出来的章不能拖、卡上写着第几到第几场；手建的章照常拖；结构镜头按故事序画出每一场", async () => {
    installSnowSync();
    catalogState.ready = true;
    catalogState.chapters = [
      planChapter("ch01", "雨夜来信", { spine: "灾一", structure: { owner: "plan", rowUid: "r1", sceneRange: { first: 1, last: 2 }, plannedSceneCount: 2, titleAuto: false },
        scenes: [planScene("SC1", 1), planScene("SC2", 2, { kind: "反应", state: "done" })] }),
      planChapter("ch02", "第 2 章", { act: "act2", structure: { owner: "plan", rowUid: "r2", sceneRange: { first: 3, last: 3 }, plannedSceneCount: 1, titleAuto: true },
        scenes: [planScene("SC3", 3)] }),
      chapter("ch03", "番外", { act: "act3", structure: { owner: "desk", rowUid: "", sceneRange: null, plannedSceneCount: 0, titleAuto: false } }),
    ];
    await act(async () => root.render(<WsAuthor />));

    const cards = [...host.querySelectorAll('[data-testid="arr-chapter-card"]')];
    expect(cards.map((card) => card.getAttribute("data-structure-owner"))).toEqual(["plan", "plan", "desk"]);
    expect(cards.map((card) => card.getAttribute("draggable"))).toEqual(["false", "false", "true"]);
    expect(cards[0].textContent).toContain("第 1–2 场");
    expect(cards[1].textContent).toContain("第 3 场");

    // 默认镜头 = 结构；没有章级张力 / 线索数据时，那两个镜头根本不出现（不拿 0.3 的默认值画平线）
    const lensTabs = [...host.querySelectorAll(".arr-arc .seg-btn")].map((node) => node.textContent);
    expect(lensTabs).toEqual(["结构", "节奏镜头"]);
    const lens = host.querySelector('[data-testid="arr-spine-lens"]');
    expect([...lens.querySelectorAll(".arr-spine-cell")].map((cell) => cell.textContent)).toEqual(["1", "2", "3"]);
    expect(lens.querySelector(".arr-spine-cell.is-hinge").textContent).toBe("2"); // 灾一落在这一章的最后一场上
    expect(lens.querySelector(".arr-spine-cell.k-reactive.s-done")).not.toBeNull();

    // 体检：没设目标不报超额、没有「张力曲线健康」；没起名的章是一项带门的待办
    const doctor = host.querySelector(".arr-doctor").textContent;
    expect(doctor).not.toContain("字数超额");
    expect(doctor).not.toContain("张力曲线健康");
    expect(doctor).toContain("还没起名的章");
    expect(doctor).toContain("林昭 3 场");

    // 点场 = 进章节详情并落在那一场上
    await act(async () => click(lens.querySelectorAll(".arr-spine-cell")[1]));
    expect(host.querySelector(".arr-shell").getAttribute("data-mode")).toBe("detail");
    expect(host.querySelectorAll(".arr-scene")[1].className).toContain("is-active");
  });

  it("旧数据里真的有章级张力 / 线索时，那两个镜头才出现", async () => {
    catalogState.ready = true;
    catalogState.chapters = [
      chapter("ch01", "旧章", { tension: 0.8, tensionSet: true, threads: [{ name: "旧信", role: "新引" }] }),
      chapter("ch02", "旧章二", { tension: 0.4, tensionSet: true }),
    ];
    await act(async () => root.render(<WsAuthor />));
    expect([...host.querySelectorAll(".arr-arc .seg-btn")].map((node) => node.textContent)).toEqual(["结构", "节奏镜头", "故事弧线", "线索织布机"]);
  });

  it("「整理章节结构」就在章节编排里开：同一张面板、同一条落库路径，确认后给回执", async () => {
    installSnowSync();
    catalogState.ready = true;
    catalogState.chapters = [planChapter("ch01", "雨夜来信", { scenes: [planScene("SC1", 1)] })];
    await act(async () => root.render(<WsAuthor />));

    await act(async () => click(host.querySelector('[data-testid="author-open-plan"]')));
    await act(async () => {});
    const panel = host.querySelector('[data-testid="chapter-plan-panel"]');
    expect(panel).not.toBeNull();
    expect(window.SnowSync.chapterPreview).toHaveBeenCalledWith("auto", {});
    expect(panel.textContent).toContain("旧信到了");

    // 面板里的一场 → 构思第 10 步的那一场
    const { clearViewIntents, flushViewIntents } = await import("./ws-view-intents.js");
    clearViewIntents("snowflake");
    const seen = [];
    const onStep = (event) => seen.push(["step", event.detail]);
    const onScene = (event) => seen.push(["scene", event.detail]);
    window.addEventListener("ws:snow-step", onStep);
    window.addEventListener("ws:snow-scene", onScene);
    await act(async () => click(panel.querySelector('[data-testid="chapter-plan-scene-edit-1"]')));
    flushViewIntents("snowflake");
    window.removeEventListener("ws:snow-step", onStep);
    window.removeEventListener("ws:snow-scene", onScene);
    expect(seen).toEqual([["step", "planning"], ["scene", "SC1"]]);
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).toBeNull();
    window.location.hash = "";

    // 再开一次，确认写入：走 SnowSync.materialize，回执说清顺手做了什么
    await act(async () => click(host.querySelector('[data-testid="author-open-plan"]')));
    await act(async () => {});
    await act(async () => click(host.querySelector('[data-testid="chapter-plan-confirm"]')));
    await act(async () => {});
    expect(window.SnowSync.materialize).toHaveBeenCalledTimes(1);
    expect(window.SnowSync.materialize.mock.calls[0][1].replace_chapters).toBe(true);
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).toBeNull();
    expect(host.querySelector('[data-testid="undo-toast"]').textContent).toContain("章节结构已按这一版写入目录 · 1 个变空的旧章已移入回收站");
  });

  it("章节详情：构思条说得出这一章是什么，入口 / 出口、视角 · 时空取自各场，体检不再摆假的对勾", async () => {
    installSnowSync();
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch02"));
    catalogState.ready = true;
    catalogState.chapters = [
      planChapter("ch01", "雨夜来信", { scenes: [planScene("SC1", 1, { exitChange: "她决定回雨城" })] }),
      planChapter("ch02", "第 2 章", {
        act: "act2", spine: "灾二", summary: "林昭连夜赶回雨城。",
        structure: { owner: "plan", rowUid: "r2", sceneRange: { first: 2, last: 3 }, plannedSceneCount: 2, titleAuto: true },
        scenes: [planScene("SC2", 2, { summary: "林昭在旧案卷里翻到一页缺页" }), planScene("SC3", 3, { povName: "顾行", goal: "（本场目标待规划）", exitChange: "林昭决定公开旧信" })],
      }),
    ];
    await act(async () => root.render(<WsAuthor />));

    const strip = host.querySelector('[data-testid="arr-plan-strip"]');
    expect(strip.textContent).toContain("卷二");
    expect(strip.textContent).toContain("灾二");
    expect(strip.textContent).toContain("第 2–3 场");
    expect(strip.textContent).toContain("还没起名");
    expect(strip.textContent).toContain("林昭连夜赶回雨城。");

    const handoff = host.querySelector('[data-testid="arr-handoff"]').textContent;
    expect(handoff).toContain("她决定回雨城");            // 承接 = 上一章最后一场的离场变化
    expect(handoff).toContain("林昭在旧案卷里翻到一页缺页");      // 入 = 本章第一场
    expect(handoff).toContain("林昭决定公开旧信");            // 出 = 本章最后一场的离场变化
    expect(handoff).toContain("取自首尾两场");

    const spacetime = host.querySelector('[data-testid="arr-ctx-spacetime"]').textContent;
    expect(spacetime).toContain("林昭 1 · 顾行 1");
    expect(spacetime).toContain("第 2 天 → 第 3 天");
    expect(spacetime).toContain("雨城码头");

    const checks = host.querySelector(".arr-checks").textContent;
    expect(checks).toContain("三拍已规划1/2");
    expect(checks).toContain("未设目标");
    expect(checks).toContain("与构思同步已同步");
    expect(checks).not.toContain("与上一章出口对齐");
    expect(checks).not.toContain("线索待交接");

    // 第二扇门就在构思条上
    await act(async () => click(strip.querySelector('[data-testid="arr-plan-open"]')));
    await act(async () => {});
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).not.toBeNull();
  });

  it("构思分出来的章：清空章名落回「第 N 章」（还算没起名），删除前说清楚构思里的分章还在", async () => {
    installSnowSync();
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch02"));
    catalogState.ready = true;
    catalogState.chapters = [planChapter("ch01", "雨夜来信"), planChapter("ch02", "旧案重开", { scenes: [planScene("SC2", 2)] })];
    await act(async () => root.render(<WsAuthor />));

    const title = host.querySelector('input[aria-label="章节标题"]');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    await act(async () => { setter.call(title, "  "); title.dispatchEvent(new Event("input", { bubbles: true })); title.focus(); title.blur(); });
    expect(WsCatalog.set).toHaveBeenCalledTimes(1);
    expect(WsCatalog.set.mock.calls[0][0].find((c) => c.id === "ch02").title).toBe("第 2 章");

    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await act(async () => click(host.querySelector(".arr-del-ch")));
    expect(confirm.mock.calls[0][0]).toContain("构思的分章还在");
    expect(confirm.mock.calls[0][0]).toContain("整理章节结构");
    expect(WsCatalog.set).toHaveBeenCalledTimes(1); // 取消 = 什么都不删
  });

  it("构思的闸门此刻没过（某一步待重新确认）：目录里已有构思分出来的章，门不能跟着消失；纯手建的书没有这扇门", async () => {
    installSnowSync({ readyToMaterialize: () => false });
    catalogState.ready = true;
    catalogState.chapters = [planChapter("ch01", "雨夜来信", { scenes: [planScene("SC1", 1)] })];
    await act(async () => root.render(<WsAuthor />));
    expect(host.querySelector('[data-testid="author-open-plan"]')).not.toBeNull();

    catalogState.chapters = [chapter("ch01", "手建的章", { structure: { owner: "desk", rowUid: "", sceneRange: null, plannedSceneCount: 0, titleAuto: false } })];
    await act(async () => root.render(<WsAuthor key="manual" />));
    expect(host.querySelector('[data-testid="author-open-plan"]')).toBeNull();
    expect(host.querySelector(".arr-doctor").textContent).not.toContain("与构思一致");
  });

  it("空目录：构思已经就绪时，第一扇门就是「把构思整理成章节」", async () => {
    installSnowSync();
    catalogState.ready = true;
    catalogState.chapters = [];
    await act(async () => root.render(<WsAuthor />));
    await act(async () => click(host.querySelector('[data-testid="author-empty-open-plan"]')));
    await act(async () => {});
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).not.toBeNull();
  });

  it("目录请求失败与真空作品分开呈现，并提供真实重试", async () => {
    catalogState.error = new Error("network down");
    await act(async () => root.render(<WsAuthor />));

    expect(host.textContent).toContain("章节目录加载失败");
    expect(host.textContent).not.toContain("还没有章节结构");
    const retry = [...host.querySelectorAll("button")].find((node) => node.textContent.includes("重试加载"));
    await act(async () => retry.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(WsCatalog.__refresh).toHaveBeenCalledTimes(1);
    expect(WsCatalog.set).not.toHaveBeenCalled();
  });
});
