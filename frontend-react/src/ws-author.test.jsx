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
      scenes: [
        { sid: "s1", backendId: "b1", title: "开场", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" },
        { sid: "s2", backendId: "b2", title: "构思的场", kind: "主动", state: "todo", goal: "目标", obstacle: "冲突", turn: "挫折", design: { origin: "snowflake", owner: "plan" } },
      ],
    })];
    await act(async () => root.render(<WsAuthor />));

    expect(byText("button", "交给 AI")).toBeTruthy();
    expect(host.querySelector('input[aria-label="场景标题"]')).toHaveProperty("disabled", false);
    expect(host.querySelector('[data-testid="arr-scene-edit-plan"]')).not.toBeNull();

    await act(async () => click(host.querySelector('[data-testid="author-scene-select-mode"]')));

    expect(byText("button", "交给 AI")).toBeUndefined();
    expect(byText("button", "自己写")).toBeUndefined();
    expect(host.querySelector(".arr-scene-more")).toBeNull();
    expect(host.querySelector('input[aria-label="场景标题"]')).toHaveProperty("disabled", true);
    // 雪花的场：三拍不再是能点开的按钮，直达构思的链接也收起——点哪儿都只是「选中」
    expect(host.querySelector('[data-testid="arr-scene-edit-plan"]')).toBeNull();
    expect(host.querySelector("button.arr-beats")).toBeNull();
    await act(async () => click(host.querySelectorAll(".arr-scene")[1].querySelector(".arr-beats")));
    expect(host.textContent).toContain("已选 1 / 2 场");
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
    // 雪花的场：三拍与 POV 是整句文字，不是只读输入框（以前的只读框把长句截在 240px，还白占四个 Tab 位）
    expect(planRow.querySelectorAll(".arr-gmc-input")).toHaveLength(0);
    const planBeats = planRow.querySelector('[data-testid="arr-scene-plan-owned"]');
    expect(planBeats.textContent).toContain("查到寄信人");
    expect(planBeats.textContent).toContain("线索断在码头");
    expect(planBeats.textContent).toContain("林昭");
    expect(planRow.querySelector('[data-testid="arr-scene-edit-plan"]')).not.toBeNull();
    expect(planRow.querySelector(".arr-scene-grip").getAttribute("draggable")).toBe("false");
    expect(planRow.querySelector(".arr-cyc")).toHaveProperty("disabled", true);
    expect(planRow.querySelector('input[aria-label="场景标题"]')).toHaveProperty("disabled", false);
    const handInputs = [...handRow.querySelectorAll(".arr-gmc-input")];
    expect(handInputs).toHaveLength(4);
    expect(handInputs.every((input) => !input.readOnly && !input.disabled)).toBe(true);
    expect(handRow.querySelector(".arr-scene-grip").getAttribute("draggable")).toBe("true");
    expect(handRow.querySelector(".arr-cyc")).toHaveProperty("disabled", false);
    expect(handRow.querySelector('[data-testid="arr-scene-edit-plan"]')).toBeNull();

    // 只读不是摆设：点三拍只是展开全文、点形态不切换——都不会把一个注定被后端 409 的改动乐观写进目录
    const beatsToggle = planBeats.querySelector(".arr-beats");
    await act(async () => click(beatsToggle));
    expect(beatsToggle.getAttribute("aria-expanded")).toBe("true");
    await act(async () => click(planRow.querySelector(".arr-cyc")));
    // 失焦但没改：题名与手加场的三拍都不写
    const planTitle = planRow.querySelector('input[aria-label="场景标题"]');
    await act(async () => { planTitle.focus(); planTitle.blur(); handInputs[0].focus(); handInputs[0].blur(); });
    expect(WsCatalog.set).not.toHaveBeenCalled();

    // 手加的场：三拍就在这里改，失焦写回
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    await act(async () => { setter.call(handInputs[0], "找到后门"); handInputs[0].dispatchEvent(new Event("input", { bubbles: true })); handInputs[0].focus(); handInputs[0].blur(); });
    expect(WsCatalog.set).toHaveBeenCalledTimes(1);
    expect(WsCatalog.set.mock.calls[0][0][0].scenes.find((sc) => sc.sid === "SC_hand").goal).toBe("找到后门");
    WsCatalog.set.mockClear();

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
    // 视角分布只在结构镜头的摘要条上出现一次（体检不再重复一遍）
    expect(doctor).not.toContain("视角分布");
    expect(lens.querySelector(".arr-lens-summary").textContent).toContain("林昭 3 场");
    // 没有「卷 3 / 进度 0%」这种要么是常数、要么是假数的统计块
    expect(host.querySelector(".arr-ov-stats")).toBeNull();
    // 章的状态按各场读：写完一场的章是「写作中」，一场没动的仍是「规划中」
    expect(cards[0].querySelector(".arr-card-state").textContent).toBe("写作中");
    expect(cards[1].querySelector(".arr-card-state").textContent).toBe("规划中");

    // 点场 = 进章节详情并落在那一场上
    await act(async () => click(lens.querySelectorAll(".arr-spine-cell")[1]));
    expect(host.querySelector(".arr-shell").getAttribute("data-mode")).toBe("detail");
    expect(host.querySelectorAll(".arr-scene")[1].className).toContain("is-active");
  });

  it("章级张力 / 线索没有编辑入口：旧数据里有也不再画「故事弧线」「线索织布机」，体检不报张力、线索", async () => {
    catalogState.ready = true;
    catalogState.chapters = [
      chapter("ch01", "旧章", { tension: 0.8, tensionSet: true, threads: [{ name: "旧信", role: "新引" }] }),
      chapter("ch02", "旧章二", { tension: 0.4, tensionSet: true }),
    ];
    localStorage.setItem("arr.lens", JSON.stringify("arc"));   // 旧版记住的镜头：落回结构镜头
    await act(async () => root.render(<WsAuthor />));
    expect([...host.querySelectorAll(".arr-arc .seg-btn")].map((node) => node.textContent)).toEqual(["结构", "节奏镜头"]);
    expect(host.querySelector('[data-testid="arr-spine-lens"]')).not.toBeNull();
    const doctor = host.querySelector(".arr-doctor").textContent;
    expect(doctor).not.toContain("张力");
    expect(doctor).not.toContain("线索");
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

  it("总览页头和其他页同一个语法：标题上面不再挂作品名小字，页头动作是同一个（默认）尺寸", async () => {
    installSnowSync();
    catalogState.ready = true;
    catalogState.chapters = [planChapter("ch01", "雨夜来信", { scenes: [planScene("SC1", 1)] })];
    await act(async () => root.render(<WsAuthor />));
    const head = host.querySelector(".arr-ov-head .ws-page-head");
    expect(head.querySelector("h1").textContent).toBe("章节编排");
    expect(head.querySelector(".ws-page-crumb")).toBeNull();
    expect(head.textContent).not.toContain("测试作品");
    const buttons = [...head.querySelectorAll(".ws-page-actions > .btn")];
    const texts = buttons.map((b) => b.textContent.trim());
    for (const t of ["整理章节结构", "刷新", "多选", "新建章节"]) expect(texts.some((x) => x.startsWith(t))).toBe(true);
    for (const b of buttons) expect(b.classList.contains("btn-sm")).toBe(false);
  });

  it("空目录：构思已经就绪时，第一扇门就是「整理章节结构」", async () => {
    installSnowSync();
    catalogState.ready = true;
    catalogState.chapters = [];
    await act(async () => root.render(<WsAuthor />));
    // 同一张面板在全站只叫一个名字（构思页头、章节编排页头、空目录的第一扇门）
    expect(host.querySelector('[data-testid="author-empty-open-plan"]').textContent.trim()).toBe("整理章节结构");
    await act(async () => click(host.querySelector('[data-testid="author-empty-open-plan"]')));
    await act(async () => {});
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).not.toBeNull();
  });

  it("戏剧卡：AI 编排写入 / 目录重拉带来新值时框里显示新值；失焦没改就不写回（以前会把刚应用的建议冲掉）", async () => {
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章", { drama: { promise: "旧的承诺" } })];
    await act(async () => root.render(<WsAuthor />));
    const promise = () => host.querySelector('textarea[aria-label="核心承诺"]');
    expect(promise().value).toBe("旧的承诺");

    // 服务端来了新值（plan/apply 之后的重拉）
    catalogState.chapters = [chapter("ch01", "第一章", { drama: { promise: "AI 补上的承诺" } })];
    await act(async () => root.render(<WsAuthor />));
    expect(promise().value).toBe("AI 补上的承诺");
    await act(async () => { promise().focus(); promise().blur(); });
    expect(WsCatalog.set).not.toHaveBeenCalled();

    // 真改了才写回
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    await act(async () => { setter.call(promise(), "作者自己的承诺"); promise().dispatchEvent(new Event("input", { bubbles: true })); promise().focus(); promise().blur(); });
    expect(WsCatalog.set).toHaveBeenCalledTimes(1);
    expect(WsCatalog.set.mock.calls[0][0][0].drama.promise).toBe("作者自己的承诺");
  });

  it("戏剧卡一格都没写时收成一行（场景看板排在它前面）；点开才出现各格", async () => {
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章", { scenes: [{ sid: "s1", backendId: "b1", title: "开场", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" }] })];
    await act(async () => root.render(<WsAuthor />));
    const toggle = host.querySelector(".arr-drama-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.textContent).toContain("可选 · 0/6");
    expect(host.querySelector('textarea[aria-label="核心承诺"]')).toBeNull();
    const body = host.querySelector(".arr-ed-body");
    const order = [...body.children].map((node) => node.className);
    expect(order.findIndex((name) => name.includes("arr-scenes"))).toBeLessThan(order.findIndex((name) => name.includes("arr-drama")));
    await act(async () => click(toggle));
    expect(host.querySelector('textarea[aria-label="核心承诺"]')).not.toBeNull();
  });

  it("戏剧卡开过就不因内容清空而收起：清空唯一写过的一格、Tab 到下一格，焦点还在下一格里", async () => {
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章", { drama: { promise: "唯一写过的一格" } })];
    WsCatalog.set.mockImplementationOnce((next) => { catalogState.chapters = next; });
    await act(async () => root.render(<WsAuthor />));
    expect(host.querySelector(".arr-drama").className).toContain("is-open");

    const promise = host.querySelector('textarea[aria-label="核心承诺"]');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    await act(async () => { promise.focus(); setter.call(promise, ""); promise.dispatchEvent(new Event("input", { bubbles: true })); });
    // Tab 到下一格：焦点先进「章节问题」，失焦写回让内容变空，目录随之重拉
    await act(async () => { host.querySelector('textarea[aria-label="章节问题"]').focus(); });
    await act(async () => root.render(<WsAuthor />));
    expect(WsCatalog.set).toHaveBeenCalledTimes(1);
    expect(catalogState.chapters[0].drama.promise).toBe("");
    expect(host.querySelector(".arr-drama").className).toContain("is-open");
    expect(document.activeElement).toBe(host.querySelector('textarea[aria-label="章节问题"]'));
  });

  it("输入法选词的那一下回车不算「改完了」：章节标题不失焦、不写目录；真正的回车才写回", async () => {
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章")];
    await act(async () => root.render(<WsAuthor />));
    const title = host.querySelector('input[aria-label="章节标题"]');
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    await act(async () => { title.focus(); setter.call(title, "半截"); title.dispatchEvent(new Event("input", { bubbles: true })); });
    await act(async () => { title.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", isComposing: true, bubbles: true })); });
    expect(document.activeElement).toBe(title);
    expect(WsCatalog.set).not.toHaveBeenCalled();
    await act(async () => { title.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(WsCatalog.set).toHaveBeenCalledTimes(1);
    expect(WsCatalog.set.mock.calls[0][0][0].title).toBe("半截");
  });

  it("体检抽屉开着时窗口宽过断点：抽屉自己收起，焦点陷阱不留在看不见的抽屉上", async () => {
    const listeners = new Set();
    const mq = {
      matches: true,
      addEventListener: (type, fn) => listeners.add(fn),
      removeEventListener: (type, fn) => listeners.delete(fn),
    };
    const other = { matches: false, addEventListener: () => {}, removeEventListener: () => {} };
    const queries = [];
    Object.defineProperty(window, "matchMedia", { configurable: true, value: (query) => { queries.push(query); return query === "(max-width: 1360px)" ? mq : other; } });
    try {
      localStorage.setItem("arr.mode", JSON.stringify("detail"));
      localStorage.setItem("arr.picked", JSON.stringify("ch01"));
      catalogState.ready = true;
      catalogState.chapters = [chapter("ch01", "第一章")];
      await act(async () => root.render(<WsAuthor />));

      await act(async () => click(host.querySelector('[data-testid="arr-ctx-toggle"]')));
      expect(host.querySelector("#arr-ctx").className).toContain("is-open");
      expect(queries).toContain("(max-width: 1360px)");           // 与 ws-author.css 的抽屉断点是同一个
      expect(listeners.size).toBe(1);

      mq.matches = false;                                          // 最大化 / 缩放：宽过 1360
      await act(async () => { listeners.forEach((fn) => fn({ matches: false })); });
      expect(host.querySelector("#arr-ctx").className).not.toContain("is-open");
      expect(host.querySelector('[data-testid="arr-ctx-toggle"]').getAttribute("aria-expanded")).toBe("false");
      expect(listeners.size).toBe(0);                              // 关上就不再听
    } finally {
      delete window.matchMedia;
    }
  });

  it("新建章节只有一份配方（WsCatalog.addChapter）：页头接在当前章后面，卷尾接在那一卷最后，建好就打开", async () => {
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章"), chapter("ch02", "第二章", { act: "act2" })];
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    let made = 0;
    WsCatalog.addChapter.mockImplementation(() => {
      made += 1;
      const created = chapter(`ch-new-${made}`, `新章 ${made}`, { state: "planned", words: { cur: 0, target: 0 } });
      catalogState.chapters = [catalogState.chapters[0], created, ...catalogState.chapters.slice(1)];
      return created;
    });
    await act(async () => root.render(<WsAuthor />));

    await act(async () => click(byText("button", "在卷二新建章节")));
    expect(WsCatalog.addChapter).toHaveBeenLastCalledWith({ act: "act2" });

    await act(async () => root.render(<WsAuthor />));
    await act(async () => click(host.querySelector(".arr-ed-crumb .arr-back")));
    await act(async () => click([...host.querySelectorAll(".ws-page-actions button")].find((node) => node.textContent.includes("新建章节"))));
    // 刚才建好、打开过的那一章就是「当前章」：页头的新建接在它后面
    expect(WsCatalog.addChapter).toHaveBeenLastCalledWith({ afterId: "ch-new-1" });
    await act(async () => root.render(<WsAuthor />));
    expect(host.querySelector(".arr-shell").getAttribute("data-mode")).toBe("detail");
    expect(host.querySelector('input[aria-label="章节标题"]').value).toBe("新章 2");
    // 视图自己不再拼章（张力 / 占位 / 4000 字目标都不会从这里冒出来）
    expect(WsCatalog.set).not.toHaveBeenCalled();
  });

  it("手建的章用抓手上的方向键挪：同卷换位；构思分出来的章抓手只是标记", async () => {
    installSnowSync();
    catalogState.ready = true;
    catalogState.chapters = [
      chapter("ch01", "手建一"), chapter("ch02", "手建二"),
      planChapter("ch03", "构思的章", { act: "act2", scenes: [planScene("SC1", 1)] }),
    ];
    await act(async () => root.render(<WsAuthor />));
    const cards = [...host.querySelectorAll('[data-testid="arr-chapter-card"]')];
    expect(cards[2].querySelector("button.arr-card-grip")).toBeNull();
    const grip = cards[1].querySelector("button.arr-card-grip");
    await act(async () => { grip.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true })); });
    expect(WsCatalog.set).toHaveBeenCalledTimes(1);
    expect(WsCatalog.set.mock.calls[0][0].map((c) => c.id)).toEqual(["ch02", "ch01", "ch03"]);
  });

  it("章节详情的序列栏：手建的章按 Alt + 方向键挪（光按方向键不挪），挪完焦点还在那一章；构思分出来的章不动", async () => {
    installSnowSync();
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [
      chapter("ch01", "手建一"), chapter("ch02", "手建二"),
      planChapter("ch03", "构思的章", { act: "act2", scenes: [planScene("SC1", 1)] }),
    ];
    await act(async () => root.render(<WsAuthor />));
    const railRow = (id) => host.querySelector(`.arr-rail-row[data-arr-move="ch:${id}"]`);
    const press = (node, init) => node.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, ...init }));

    await act(async () => { railRow("ch02").focus(); press(railRow("ch02"), { key: "ArrowUp" }); });
    expect(WsCatalog.set).not.toHaveBeenCalled();
    await act(async () => { press(railRow("ch03"), { key: "ArrowUp", altKey: true }); });
    expect(WsCatalog.set).not.toHaveBeenCalled();
    expect(railRow("ch03").hasAttribute("aria-keyshortcuts")).toBe(false);

    await act(async () => { railRow("ch01").focus(); press(railRow("ch01"), { key: "ArrowDown", altKey: true }); });
    expect(WsCatalog.set).toHaveBeenCalledTimes(1);
    expect(WsCatalog.set.mock.calls[0][0].map((c) => c.id)).toEqual(["ch02", "ch01", "ch03"]);
    expect(document.activeElement).toBe(railRow("ch01"));

    // 到了卷尾再按一下：归到下一卷。那一行在另一卷的列表里重建，焦点要找回来，不然键盘挪位只能挪一格
    await act(async () => { press(railRow("ch01"), { key: "ArrowDown", altKey: true }); });
    expect(WsCatalog.set).toHaveBeenCalledTimes(2);
    expect(WsCatalog.set.mock.calls[1][0].map((c) => `${c.id}:${c.act}`)).toEqual(["ch02:act1", "ch01:act2", "ch03:act2"]);
    expect(document.activeElement).toBe(railRow("ch01"));
  });

  it("跨视图的去处都走外壳的 go：构思第 10 步那一场、写作台、AI 起草台、回收站", async () => {
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "雨夜来信", {
      scenes: [{ sid: "SC_plan", backendId: "SC_plan_b", title: "旧信到了", kind: "主动", state: "todo", goal: "查到寄信人", obstacle: "邮局不肯查", turn: "线索断在码头",
        povName: "林昭", design: { origin: "snowflake", owner: "plan" } }],
    })];
    const go = vi.fn(() => true);
    await act(async () => root.render(<WsAuthor go={go} />));

    await act(async () => click(host.querySelector('[data-testid="arr-scene-edit-plan"]')));
    expect(go).toHaveBeenLastCalledWith("snowflake", [
      { type: "ws:snow-step", detail: "planning" },
      { type: "ws:snow-scene", detail: "SC_plan_b" },
    ]);
    await act(async () => click(byText("button", "交给 AI")));
    expect(go).toHaveBeenLastCalledWith("scene", [{ type: "ws:scene-enqueue", detail: { sid: "SC_plan" } }]);
    await act(async () => click(byText("button", "自己写")));
    expect(go).toHaveBeenLastCalledWith("writer", [{ type: "ws:writer-scene", detail: "SC_plan" }]);
    await act(async () => click(byText(".arr-scenes-actions button", "回收站")));
    expect(go).toHaveBeenLastCalledWith("trash");
    expect(window.location.hash).not.toBe("#snowflake");   // 有外壳就不自己改 hash
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

describe("章节编排 · 键盘焦点跟着视图走（2026-09-21 a11y 复审）", () => {
  it("从全书编排点开一章，焦点落在这一章的详情上；回到全书编排，焦点回到那一章的标题按钮", async () => {
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章"), chapter("ch02", "第二章")];
    await act(async () => root.render(<WsAuthor />));
    // 章名「第二章」是系统占位：行上只写一遍章号「第 2 章」（不再是「02 第二章」）
    const title = [...host.querySelectorAll(".arr-card-title")].find((node) => node.textContent === "第 2 章");
    await act(async () => { title.focus(); });
    await act(async () => click(title));

    expect(host.querySelector(".arr-shell").getAttribute("data-mode")).toBe("detail");
    const detail = host.querySelector(".arr-ed");
    expect(detail.getAttribute("aria-label")).toBe("第 2 章");
    expect(document.activeElement).toBe(detail);                     // 过去掉到 <body>

    await act(async () => click(host.querySelector(".arr-ed-crumb .arr-back")));
    expect(host.querySelector(".arr-shell").getAttribute("data-mode")).toBe("overview");
    expect(document.activeElement.classList.contains("arr-card-title")).toBe(true);
    expect(document.activeElement.textContent).toBe("第 2 章");
  });

  it("体检抽屉开着时是模态对话框（role=dialog + aria-modal，名字来自抽屉标题）；收起后只是一块有名字的区域", async () => {
    localStorage.setItem("arr.mode", JSON.stringify("detail"));
    localStorage.setItem("arr.picked", JSON.stringify("ch01"));
    catalogState.ready = true;
    catalogState.chapters = [chapter("ch01", "第一章")];
    await act(async () => root.render(<WsAuthor />));
    const ctx = host.querySelector("#arr-ctx");
    expect(ctx.getAttribute("role")).toBeNull();
    expect(ctx.getAttribute("aria-label")).toBe("章节体检与视角时空");

    await act(async () => click(host.querySelector('[data-testid="arr-ctx-toggle"]')));
    expect(ctx.className).toContain("is-open");
    expect(ctx.getAttribute("role")).toBe("dialog");
    expect(ctx.getAttribute("aria-modal")).toBe("true");
    expect(document.getElementById(ctx.getAttribute("aria-labelledby")).textContent).toBe("本章体检");
    expect(ctx.contains(document.activeElement)).toBe(true);

    // 输入法组字中的 Esc 不收抽屉；真正的 Esc 收起
    await act(async () => { document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", isComposing: true, bubbles: true })); });
    expect(ctx.className).toContain("is-open");
    await act(async () => { document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(ctx.className).not.toContain("is-open");
    expect(ctx.getAttribute("role")).toBeNull();
  });
});
