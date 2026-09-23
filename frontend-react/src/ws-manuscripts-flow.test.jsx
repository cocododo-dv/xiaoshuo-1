import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fixture = vi.hoisted(() => ({ catalog: [], projectId: "p1", catalogReady: true, catalogError: null, removeScenes: null }));
const flow = vi.hoisted(() => ({
  refresh: vi.fn().mockResolvedValue({}),
  body: vi.fn(() => null),
  snapshot: vi.fn(),
  aggregate: vi.fn().mockResolvedValue({ status: "created" }),
  setReviewState: vi.fn().mockResolvedValue({}),
  confirmRead: vi.fn().mockResolvedValue({ body_hash: "hash-1" }),
  approveFinal: vi.fn().mockResolvedValue({ approved_chapter_id: "c1" }),
  reopenFinal: vi.fn().mockResolvedValue({ reopened_chapter_id: "c1" }),
  decideCanonCandidate: vi.fn().mockResolvedValue({}),
  verifySceneCanon: vi.fn().mockResolvedValue({}),
  createCanonCandidate: vi.fn().mockResolvedValue({}),
  extractSceneCanon: vi.fn().mockResolvedValue({}),
}));
const catalogRefresh = vi.hoisted(() => vi.fn().mockResolvedValue({}));
const worksRefresh = vi.hoisted(() => vi.fn().mockResolvedValue({}));

vi.mock("./ws-catalog.jsx", () => ({
  WsCatalog: {
    get: () => fixture.catalog,
    ready: () => fixture.catalogReady,
    loadError: () => fixture.catalogError,
    reset: vi.fn(),
    removeScenes: (...args) => fixture.removeScenes(...args),
    __refresh: catalogRefresh,
  },
  useCatalogChapters: () => fixture.catalog,
}));
vi.mock("./ws-works.jsx", () => ({
  WsWorks: {
    activeId: () => fixture.projectId,
    active: () => ({ id: fixture.projectId, title: "测试长篇", genre: "悬疑", wordsTarget: 100000, chaptersTotal: 2 }),
    __refresh: worksRefresh,
  },
}));
vi.mock("./ws-review.jsx", () => ({ rvPush: vi.fn() }));
/* 诊断计数与诊断页签：这里只验成稿中心把它们接在哪儿；面板本身在 ws-manuscripts-diagnosis.test.jsx */
const diagFx = vi.hoisted(() => ({ chapters: {}, scenes: {} }));
vi.mock("./ws-diagnosis-summary.jsx", () => ({
  useDiagnosisSummary: () => ({
    loaded: () => true,
    totals: () => null,
    chapterCounts: (id) => (diagFx.chapters[id] ? { open: 0, blocking: 0, ...diagFx.chapters[id] } : { open: 0, blocking: 0 }),
    sceneCounts: (id) => (diagFx.scenes[id] ? { open: 0, blocking: 0, ...diagFx.scenes[id] } : { open: 0, blocking: 0 }),
  }),
  announceDiagnosisChanged: vi.fn(),
}));
vi.mock("./ws-manuscripts-diagnosis.jsx", async () => {
  const React = await import("react");
  return { ManuDiagnosis: ({ chapter }) => React.createElement("div", { "data-testid": "manuscript-diagnosis-stub" }, chapter ? chapter.backendId : "") };
});
/* 像不像（作品汇总的 scene_finals）：只验成稿中心把角标挂在哪儿；读数本身在 ws-fidelity-*.test */
const fidFx = vi.hoisted(() => ({ project: null, load: vi.fn() }));
vi.mock("./ws-fidelity-store.js", () => ({
  useFidelityStore: () => {},
  fidLoadProject: (...args) => fidFx.load(...args),
  fidProject: () => (fidFx.project ? { phase: "ready", data: fidFx.project, error: null } : null),
}));
vi.mock("./ws-manuscripts-store.jsx", () => ({
  WsManuStore: flow,
  manuscriptChapterEligible: () => true,
  manuscriptDisplayState: (state) => state,
}));
/* 批准 / 重新打开 / 退回对话框走 WsDialog，挂在 document.body 上：对话框相关的查询用 document */
const dialog = () => document.querySelector('[role="dialog"]');

import { WsManuscripts } from "./ws-manuscripts.jsx";
import { rvPush } from "./ws-review.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const mounted = [];

function chapter(state) {
  return {
    id: "ch01", backendId: "c1", n: "01", title: "盐场的早班", state,
    current: true, words: { cur: 3600, target: 4000 },
    scenes: [{ sid: "ch01s1", backendId: "s1", title: "交班", state: "done" }],
  };
}

const COMPLETE_BODY = {
  completion: "complete",
  missingSceneIds: [],
  canonContinuity: {
    complete: true,
    scene_count: 1,
    synced_scene_count: 1,
    pending_scene_ids: [],
    missing_final_scene_ids: [],
    pending_candidate_count: 0,
    scenes: [{
      scene_id: "s1", scene_seq: 1, final_scene_row_id: "final_s1_v1",
      status: "synced", complete: true, pending_count: 0, candidates: [], extraction: {},
    }],
  },
  scenes: [{ sceneId: "s1", live: true, paras: ["潮水退去，她看清了闸门上的名字。"] }],
};

function readySnapshot(body = COMPLETE_BODY) {
  return { status: "ready", body, error: null };
}

/* strict：包一层 React.StrictMode（npm run dev 就是这样跑的：新挂载的 effect 会被连跑两遍） */
async function renderPage(state, go = vi.fn(), { strict = false } = {}) {
  fixture.catalog = Array.isArray(state) ? state : [chapter(state)];
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  const page = <WsManuscripts go={go} />;
  await act(async () => root.render(strict ? <React.StrictMode>{page}</React.StrictMode> : page));
  await act(async () => Promise.resolve());
  return host;
}

async function click(node) {
  await act(async () => node.click());
  await act(async () => Promise.resolve());
}

async function typeTextarea(node, value) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
  await act(async () => {
    setter.call(node, value);
    node.dispatchEvent(new Event("input", { bubbles: true }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  fixture.projectId = "p1";
  fixture.catalogReady = true;
  fixture.catalogError = null;
  flow.refresh.mockResolvedValue({});
  flow.body.mockReturnValue(COMPLETE_BODY);
  flow.snapshot.mockReturnValue(readySnapshot());
  flow.setReviewState.mockResolvedValue({});
  flow.confirmRead.mockResolvedValue({ body_hash: "hash-1" });
  flow.approveFinal.mockResolvedValue({ approved_chapter_id: "c1" });
  flow.reopenFinal.mockResolvedValue({ reopened_chapter_id: "c1" });
  flow.decideCanonCandidate.mockResolvedValue({});
  flow.verifySceneCanon.mockResolvedValue({});
  flow.createCanonCandidate.mockResolvedValue({});
  flow.extractSceneCanon.mockResolvedValue({});
  catalogRefresh.mockResolvedValue({});
  worksRefresh.mockResolvedValue({});
  delete window.WrDocVersions;
  fidFx.project = null;
});

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
});

describe("成稿中心权威章节流", () => {
  it("批准按钮先要求逐项通读确认，再按 read-confirm → approve-final 顺序提交", async () => {
    const host = await renderPage("review");
    await click(host.querySelector('[data-testid="approve-final-open"]'));

    const check = document.querySelector('[data-testid="approve-read-confirm"]');
    const confirm = document.querySelector('[data-testid="approve-final-confirm"]');
    expect(dialog().textContent).toContain("绑定当前服务端正文哈希");
    // 焦点进了对话框（落在通读确认勾选框上）
    expect(document.activeElement).toBe(check);
    expect(confirm.disabled).toBe(true);

    await click(check);
    expect(confirm.disabled).toBe(false);
    await click(confirm);

    expect(flow.confirmRead).toHaveBeenCalledWith("p1", "c1", "");
    expect(flow.approveFinal).toHaveBeenCalledWith("p1", "c1", "");
    expect(flow.confirmRead.mock.invocationCallOrder[0]).toBeLessThan(flow.approveFinal.mock.invocationCallOrder[0]);
    expect(catalogRefresh).toHaveBeenCalledWith("p1");
  });

  it("重新打开终稿必须填写审计理由，不能直接做本地状态回滚", async () => {
    const host = await renderPage("approved");
    await click(host.querySelector('[data-testid="reopen-final-open"]'));

    const confirm = document.querySelector('[data-testid="reopen-final-confirm"]');
    const area = document.querySelector('textarea[placeholder*="打破终稿锁"]');
    expect(confirm.disabled).toBe(true);
    expect(dialog().textContent).toContain("后已批准章节会失效");

    await typeTextarea(area, "第三场时间线需要纠正");
    expect(confirm.disabled).toBe(false);
    await click(confirm);

    expect(flow.reopenFinal).toHaveBeenCalledWith("p1", "c1", "第三场时间线需要纠正");
    expect(catalogRefresh).toHaveBeenCalledWith("p1");
  });

  it("送入审阅等待服务端目录 PATCH 成功后再刷新权威目录", async () => {
    const host = await renderPage("draft");
    const button = [...host.querySelectorAll("button")].find((node) => node.textContent.includes("送入审阅"));
    await click(button);

    expect(flow.setReviewState).toHaveBeenCalledWith("p1", "c1", "review");
    expect(catalogRefresh).toHaveBeenCalledWith("p1");
    expect(host.textContent).toContain("状态已由服务端确认");
  });

  it("服务端正文失败时显示错误与重试，潮汐演示章也不回退示例正文", async () => {
    fixture.projectId = "prj-main";
    flow.snapshot.mockReturnValue({
      status: "error",
      body: null,
      error: { code: "UPSTREAM_DOWN", message: "成稿服务暂时不可用" },
    });
    flow.body.mockReturnValue(null);

    const host = await renderPage("review");

    expect(host.textContent).toContain("服务端正文加载失败");
    expect(host.textContent).toContain("成稿服务暂时不可用");
    expect(host.textContent).not.toContain("纸箱在阁楼上放了十二年");
    expect(host.querySelector('[data-testid="approve-final-open"]').disabled).toBe(true);

    const callsBeforeRetry = flow.refresh.mock.calls.length;
    await click(host.querySelector('[data-testid="manuscript-retry"]'));
    expect(flow.refresh).toHaveBeenCalledTimes(callsBeforeRetry + 1);
  });

  it("部分稿保留缺失场景占位，不显示章节结束且关闭送审/批准", async () => {
    const partial = {
      completion: "partial",
      missingSceneIds: ["s1"],
      scenes: [{ sceneId: "s1", live: false, paras: [] }],
    };
    flow.snapshot.mockReturnValue(readySnapshot(partial));
    flow.body.mockReturnValue(partial);

    const host = await renderPage("review");

    expect(host.textContent).toContain("这一场尚无服务端归档正文");
    expect(host.textContent).not.toContain("章节结束");
    expect(host.querySelector('[data-testid="approve-final-open"]').disabled).toBe(true);
  });

  it("正史页展示正文证据，并把接受动作写回服务端正史账本", async () => {
    const pendingBody = {
      ...COMPLETE_BODY,
      canonContinuity: {
        complete: false,
        scene_count: 1,
        synced_scene_count: 0,
        pending_scene_ids: ["s1"],
        missing_final_scene_ids: [],
        pending_candidate_count: 1,
        scenes: [{
          scene_id: "s1", scene_seq: 1, final_scene_row_id: "final_s1_v1",
          status: "pending_review", complete: false, pending_count: 1,
          extraction: { extraction_outcome: "completed_events" },
          candidates: [{
            candidate_id: "fc1", event_type: "character_state", raw_entity_ref: "林远",
            fact_key: "injury", fact_value: "右臂骨折", status: "pending",
            entity_resolution_status: "exact", entity_options: [],
            evidence: { text: "他的右臂已经折断。" },
          }],
        }],
      },
    };
    flow.snapshot.mockReturnValue(readySnapshot(pendingBody));
    flow.body.mockReturnValue(pendingBody);
    const host = await renderPage("review");

    await click(host.querySelector('[data-testid="manuscript-canon-tab"]'));
    expect(host.textContent).toContain("他的右臂已经折断");
    expect(host.querySelector('[data-testid="approve-final-open"]').disabled).toBe(true);

    await click(host.querySelector('[data-testid="canon-candidate-accept"]'));
    expect(flow.decideCanonCandidate).toHaveBeenCalledWith("p1", "c1", "fc1", {
      action: "accept",
      selected_entity_id: null,
      expected_final_scene_row_id: "final_s1_v1",
    });
  });

  it("本章导出会二次核验，服务端失败后显示失败且不下载", async () => {
    const host = await renderPage("approved");
    const button = host.querySelector('[data-testid="chapter-export"]');
    expect(button.disabled).toBe(false);

    flow.snapshot.mockReturnValue({ status: "error", body: null, error: { message: "导出前正文核验失败" } });
    await click(button);

    expect(host.textContent).toContain("导出前正文核验失败");
    expect(button.textContent).toContain("导出本章");
  });

  it("本章导出忙碌期间防双击，只发起一次服务端核验", async () => {
    const host = await renderPage("approved");
    flow.refresh.mockClear();
    let resolveRefresh;
    flow.refresh.mockReturnValueOnce(new Promise(resolve => { resolveRefresh = resolve; }));
    const button = host.querySelector('[data-testid="chapter-export"]');

    await act(async () => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(flow.refresh).toHaveBeenCalledTimes(1);
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain("导出中");

    await act(async () => {
      resolveRefresh({});
      await Promise.resolve();
      await Promise.resolve();
    });
  });

  it("版本历史请求失败会显示错误并可重试", async () => {
    window.WrDocVersions = {
      list: vi.fn()
        .mockRejectedValueOnce(new Error("版本服务暂时不可用"))
        .mockResolvedValueOnce([]),
      paras: vi.fn(),
      diff: vi.fn(),
    };
    const host = await renderPage("review");
    const diffTab = [...host.querySelectorAll("button")].find(node => node.textContent === "对比");
    await click(diffTab);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(host.textContent).toContain("版本服务暂时不可用");
    await click(host.querySelector('[data-testid="manuscript-diff-history-retry"]'));
    expect(window.WrDocVersions.list).toHaveBeenCalledTimes(2);
  });

  it("开发模式连跑两遍挂载 effect 也只拉一次版本列表；幂等冲突说成中文", async () => {
    let rejectList;
    window.WrDocVersions = {
      list: vi.fn(() => new Promise((resolve, reject) => { rejectList = reject; })),
      paras: vi.fn(),
      diff: vi.fn(),
    };
    fixture.catalog = [chapter("review")];
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    mounted.push({ root, host });
    await act(async () => root.render(<React.StrictMode><WsManuscripts go={vi.fn()} /></React.StrictMode>));
    await act(async () => Promise.resolve());
    await click([...host.querySelectorAll("button")].find((node) => node.textContent === "对比"));
    expect(window.WrDocVersions.list).toHaveBeenCalledTimes(1);

    await act(async () => {
      rejectList(Object.assign(new Error("request with the same idempotency key is still running"), { code: "IDEMPOTENCY_REQUEST_IN_PROGRESS" }));
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    });
    expect(host.textContent).toContain("上一次读取还没结束");
    expect(host.textContent).not.toContain("idempotency");
  });
});

describe("成稿中心 · 阶段、下一步与空态", () => {
  const PARTIAL = {
    completion: "partial",
    missingSceneIds: ["s2"],
    canonContinuity: { complete: false, scenes: [], pending_scene_ids: [], missing_final_scene_ids: ["s2"], pending_candidate_count: 0 },
    scenes: [{ sceneId: "s1", live: true, paras: ["第一场的正文。"] }, { sceneId: "s2", live: false, paras: [] }],
  };
  function planned(extra = {}) {
    return {
      id: "ch01", backendId: "c1", n: "01", title: "盐场的早班", state: "planned",
      current: true, words: { cur: 1800, target: 4000 },
      scenes: [
        { sid: "sid-1", backendId: "s1", title: "交班", state: "done" },
        { sid: "sid-2", backendId: "s2", title: "夜渡", state: "todo" },
      ],
      ...extra,
    };
  }

  it("目录停在「规划」但已有字的章算写作中；下一步是去写作台写缺的那一场，缺场提示只说一次", async () => {
    flow.snapshot.mockReturnValue(readySnapshot(PARTIAL));
    const go = vi.fn();
    const host = await renderPage([planned()], go);

    expect(host.querySelector(".ms-list-grouphd").textContent).toContain("写作中");
    expect(host.textContent).not.toContain("计划中");
    expect(host.textContent).not.toContain("待聚合");
    // 缺场说明只在页脚出现一次
    const occurrences = host.textContent.split("仍缺").length - 1;
    expect(occurrences).toBe(1);

    const next = [...host.querySelectorAll(".ms-reader-foot button")].find((b) => b.textContent.includes("去写作台续写"));
    expect(next).toBeTruthy();
    await click(next);
    expect(go).toHaveBeenCalledWith("writer", [{ type: "ws:writer-scene", detail: "sid-2" }]);
  });

  it("后端的 todo 状态不会让页面崩掉；进度条按目录全序画、不重复编号", async () => {
    const todo = { ...planned({ id: "ch02", backendId: "c2", n: "02", title: "雾里的灯", state: "todo", words: { cur: 0 } }), scenes: [] };
    const host = await renderPage([planned(), todo]);
    const cells = [...host.querySelectorAll(".ms-progress-cell")];
    expect(cells.map((c) => c.getAttribute("title"))).toEqual([
      "第 1 章 · 盐场的早班：写作中",
      "第 2 章 · 雾里的灯：待写",
    ]);
    // 进度条只是看的，不是第二个选章器
    expect(host.querySelector(".ms-progress button")).toBeNull();
    expect(host.querySelector(".ms-progress").getAttribute("role")).toBe("img");
  });

  it("页头和其他页同一个语法（PageHeader）：页名是「成稿中心」，作品名和进度是 meta，进度条与导出在动作位", async () => {
    const host = await renderPage([planned()]);
    const head = host.querySelector('[data-testid="manuscripts-head"]');
    expect(head.classList.contains("ws-page-head")).toBe(true);
    // 整页只有一个 h1，而且是页名，不是作品名（以前作品名做 h1，和主页撞成一样的标题）
    expect([...host.querySelectorAll("h1")].map((h) => h.textContent)).toEqual(["成稿中心"]);
    expect(head.querySelector("h1").classList.contains("ws-page-title")).toBe(true);
    const meta = head.querySelector(".ws-page-meta");
    expect(meta.querySelector(".ms-hero-work").textContent).toBe("测试长篇");
    expect(meta.textContent).toMatch(/已定稿 0\/\d+ 章/);
    expect(meta.textContent).toContain("定稿字数");
    expect(head.querySelector(".ws-page-actions .ms-progress")).not.toBeNull();
    expect(head.querySelector(".ws-page-actions .ms-export-btn").textContent).toContain("统一导出");
    // 整书导出是次要动作：页头不放第二个实心主按钮（主按钮是阅读器页脚里这一章的下一步）
    expect(head.querySelector(".ms-export-btn").classList.contains("btn-accent")).toBe(false);
  });

  it("目录还没到时显示读取中，不先说「成稿中心还是空的」；目录失败给重试", async () => {
    fixture.catalogReady = false;
    let host = await renderPage([]);
    expect(host.textContent).toContain("正在读取章节目录");
    expect(host.textContent).not.toContain("成稿中心还是空的");

    fixture.catalogError = { message: "目录服务暂时不可用" };
    host = await renderPage([]);
    expect(host.textContent).toContain("目录没有加载出来");
    expect(host.textContent).toContain("目录服务暂时不可用");
  });

  it("统一导出：没有定稿时默认「当前章」，「已定稿」不可选，状态只说一行", async () => {
    const host = await renderPage("review");
    await click(host.querySelector(".ms-export-btn"));
    const pop = host.querySelector(".ms-export-pop");
    const scope = [...pop.querySelectorAll('[role="radiogroup"]')][0];
    const approved = [...scope.querySelectorAll('[role="radio"]')].find((b) => b.textContent.includes("已定稿"));
    const current = [...scope.querySelectorAll('[role="radio"]')].find((b) => b.textContent.includes("当前章"));
    expect(approved.disabled).toBe(true);
    expect(current.getAttribute("aria-checked")).toBe("true");
    expect(pop.textContent).not.toContain("该范围内没有章节");
  });

  it("统一导出：打开时焦点进面板（读屏报出对话框名）；焦点离开这一块就收起，只是掉到空处不收", async () => {
    const host = await renderPage("review");
    const btn = host.querySelector(".ms-export-btn");
    await act(async () => { btn.focus(); });
    await click(btn);
    const pop = host.querySelector(".ms-export-pop");
    expect(document.activeElement).toBe(pop);                       // 过去焦点留在按钮上
    expect(pop.getAttribute("aria-label")).toBe("统一导出");
    const current = [...pop.querySelectorAll('[role="radio"]')].find((b) => b.textContent.includes("当前章"));

    await act(async () => { document.activeElement.blur(); });      // 掉到 <body>（例如点了面板空白）
    expect(host.querySelector(".ms-export-pop")).not.toBeNull();
    await act(async () => { current.focus(); });
    await act(async () => { btn.focus(); });                        // Shift+Tab 回到按钮：还在这一块里
    expect(host.querySelector(".ms-export-pop")).not.toBeNull();

    const outside = document.createElement("button");
    document.body.appendChild(outside);
    try {
      await act(async () => { outside.focus(); });                  // Tab 出去
      expect(host.querySelector(".ms-export-pop")).toBeNull();
      expect(btn.getAttribute("aria-expanded")).toBe("false");
    } finally {
      outside.remove();
    }
  });

  it("正史页把提取结果说成中文，不再露出 not_invoked / SCENE 01 这样的英文键", async () => {
    const pending = {
      ...COMPLETE_BODY,
      canonContinuity: {
        complete: false, scene_count: 1, synced_scene_count: 0, pending_scene_ids: ["s1"],
        missing_final_scene_ids: [], pending_candidate_count: 0,
        scenes: [{
          scene_id: "s1", scene_seq: 1, final_scene_row_id: "final_s1_v1",
          status: "pending_extraction", complete: false, pending_count: 0, candidates: [],
          extraction: { extraction_outcome: "not_invoked", extraction_reason: "awaiting_extraction", requires_scene_confirmation: true },
        }],
      },
    };
    flow.snapshot.mockReturnValue(readySnapshot(pending));
    const host = await renderPage("review");
    await click(host.querySelector('[data-testid="manuscript-canon-tab"]'));
    const panel = host.querySelector('[data-testid="manuscript-canon-panel"]');
    expect(panel.textContent).toContain("还没提取（等待提取）");
    expect(panel.textContent).toContain("第 1 场");
    expect(panel.textContent).not.toContain("not_invoked");
    expect(panel.textContent).not.toContain("SCENE");
    expect(panel.textContent).not.toContain("CANON LEDGER");
  });

  it("场景三问的判定是中文；标待删先确认，取消就不动目录", async () => {
    const removeScenes = vi.fn(() => true);
    fixture.removeScenes = removeScenes;
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const ch = chapter("review");
    ch.scenes[0].storyCheck = { verdict: "no", crucible_identified: false, shape_landed: true };
    const host = await renderPage([ch]);
    const structure = [...host.querySelectorAll('[role="radio"]')].find((b) => b.textContent === "结构");
    await click(structure);
    const badge = host.querySelector('[data-testid="ms-story-check"]');
    expect(badge.textContent).toContain("不成立");
    expect(badge.textContent).not.toMatch(/\bNo\b/);
    await click(host.querySelector('[data-testid="ms-story-check-cut"]'));
    await act(async () => { await Promise.resolve(); });
    expect(confirmSpy).toHaveBeenCalled();
    expect(removeScenes).not.toHaveBeenCalled();
  });

  it("标待删确认后走目录 store（ES 导入，不读 window.WsCatalog）移入回收站", async () => {
    const removeScenes = vi.fn(() => true);
    fixture.removeScenes = removeScenes;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const ch = chapter("review");
    ch.scenes[0].storyCheck = { verdict: "no", crucible_identified: false, shape_landed: false };
    const host = await renderPage([ch]);
    const structure = [...host.querySelectorAll('[role="radio"]')].find((b) => b.textContent === "结构");
    await click(structure);
    await click(host.querySelector('[data-testid="ms-story-check-cut"]'));
    await act(async () => { await Promise.resolve(); });
    expect(removeScenes).toHaveBeenCalledWith(["ch01s1"]);
  });
});

describe("成稿中心 · 拆分后的页头、状态行与对话框", () => {
  it("阅读器页头只给章号定位，不再挂一个恒定的「v1」；下面是完成的场数，和左栏列表同一个词", async () => {
    const host = await renderPage("review");
    expect(host.querySelector(".ms-reader-eyebrow").textContent).toBe("第 1 章");
    expect(host.querySelector(".ms-reader-sub").textContent).toContain("已完成 1/1 场");
    expect(host.querySelector(".ms-reader-sub").textContent).not.toContain("已归档");
    expect(host.querySelector(".ms-list-meta").textContent).toContain("已完成 1/1 场"); // 以前左栏叫「写完」
    expect(host.textContent).not.toMatch(/\bv1\b/i);
    expect(host.textContent).not.toContain("控制塔");
  });

  it("状态行只说最近一次动作的结果：导出失败后再刷新汇总，旧错误不再压住新提示", async () => {
    const host = await renderPage("approved");
    flow.snapshot.mockReturnValue({ status: "error", body: null, error: { message: "导出前正文核验失败" } });
    await click(host.querySelector('[data-testid="chapter-export"]'));
    expect(host.querySelector(".ms-status").textContent).toContain("导出前正文核验失败");

    flow.snapshot.mockReturnValue(readySnapshot());
    flow.aggregate.mockResolvedValue({ status: "created" });
    await click(host.querySelector('[data-testid="chapter-aggregate"]'));
    expect(flow.aggregate).toHaveBeenCalledWith("c1");
    expect(host.textContent).toContain("章节汇总已生成");
    expect(host.textContent).not.toContain("导出前正文核验失败");
  });

  it("换一章时，上一章的提示不跟过来", async () => {
    const other = { ...chapter("draft"), id: "ch02", backendId: "c2", n: "02", title: "雾里的灯" };
    const host = await renderPage([chapter("draft"), other]);
    const submit = [...host.querySelectorAll("button")].find((node) => node.textContent.includes("送入审阅"));
    await click(submit);
    expect(host.textContent).toContain("状态已由服务端确认");

    const row = [...host.querySelectorAll('[data-testid="manuscript-chapter-item"]')].find((node) => node.getAttribute("data-chapter-id") === "c2");
    await click(row);
    expect(host.querySelector(".ms-reader-title").textContent).toBe("雾里的灯");
    expect(host.textContent).not.toContain("状态已由服务端确认");
  });

  it("批准对话框按 Esc 关闭；服务端处理中 Esc 关不掉", async () => {
    const host = await renderPage("review");
    await click(host.querySelector('[data-testid="approve-final-open"]'));
    expect(dialog()).not.toBeNull();
    await act(async () => {
      document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(dialog()).toBeNull();

    let resolveRead;
    flow.confirmRead.mockReturnValueOnce(new Promise((resolve) => { resolveRead = resolve; }));
    await click(host.querySelector('[data-testid="approve-final-open"]'));
    await click(document.querySelector('[data-testid="approve-read-confirm"]'));
    await click(document.querySelector('[data-testid="approve-final-confirm"]'));
    await act(async () => {
      document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(dialog()).not.toBeNull();
    await act(async () => { resolveRead({}); await Promise.resolve(); await Promise.resolve(); });
  });
});

describe("成稿中心 · 对话框焦点、在途动作与章名（复审修补）", () => {
  const button = (root, text) => [...root.querySelectorAll("button")].find((node) => node.textContent.trim() === text);
  const chapterRow = (host, backendId) => [...host.querySelectorAll('[data-testid="manuscript-chapter-item"]')]
    .find((node) => node.getAttribute("data-chapter-id") === backendId);
  const flushFocus = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

  it("开发模式（StrictMode）下批准对话框一打开焦点就在里面，取消后回到打开它的按钮", async () => {
    const host = await renderPage("review", vi.fn(), { strict: true });
    const opener = host.querySelector('[data-testid="approve-final-open"]');
    opener.focus();
    await click(opener);
    await flushFocus();
    expect(document.activeElement).toBe(document.querySelector('[data-testid="approve-read-confirm"]'));

    await click(button(dialog(), "取消"));
    await flushFocus();
    expect(dialog()).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("开发模式下退回对话框的焦点落在理由框上；定位到场是方向键可切换的单选，只有选中的一场占 Tab 位", async () => {
    const ch = chapter("review");
    ch.scenes = [...ch.scenes, { sid: "ch01s2", backendId: "s2", title: "夜渡", state: "done" }];
    const host = await renderPage([ch], vi.fn(), { strict: true });
    const opener = button(host.querySelector(".ms-reader-foot"), "退回小修");
    opener.focus();
    await click(opener);
    await flushFocus();
    const area = dialog().querySelector("textarea");
    expect(document.activeElement).toBe(area);

    const group = dialog().querySelector('[role="radiogroup"][aria-label="定位到场"]');
    const radios = [...group.querySelectorAll('[role="radio"]')];
    expect(radios.map((r) => r.tabIndex)).toEqual([0, -1]);
    await act(async () => { radios[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true })); });
    expect(radios[1].getAttribute("aria-checked")).toBe("true");
    expect(radios.map((r) => r.tabIndex)).toEqual([-1, 0]);
    expect(document.activeElement).toBe(radios[1]);

    await typeTextarea(area, "第二场的交代太满");
    await click(button(dialog(), "退回并生成待办"));
    expect(rvPush).toHaveBeenCalledWith(expect.objectContaining({
      title: "退回小修：第 1 章 · 盐场的早班",
      where: "第 1 章 · 夜渡",
    }));
    expect(rvPush.mock.calls[0][0].actions[0].scene).toBe("ch01s2");
  });

  it("章名是占位的「第 1 章」时，进度条提示与退回待办的标题都不写两遍", async () => {
    const host = await renderPage([{ ...chapter("review"), title: "第 1 章" }]);
    expect(host.querySelector(".ms-progress-cell").getAttribute("title")).toBe("第 1 章：审阅中");

    await click(button(host.querySelector(".ms-reader-foot"), "退回小修"));
    await typeTextarea(dialog().querySelector("textarea"), "开头交代太多");
    await click(button(dialog(), "退回并生成待办"));
    expect(rvPush).toHaveBeenCalledWith(expect.objectContaining({
      title: "退回小修：第 1 章",
      where: "第 1 章 · 交班",
      detail: "开头交代太多",
    }));
  });

  it("导出本章用完整章名：文件名（也是文件里的一级标题）不截成「…」", async () => {
    const longTitle = "灯塔看守人在第七个无风的夜里数完了所有石阶然后决定下山";
    const host = await renderPage([{ ...chapter("approved"), title: longTitle }]);
    const names = [];
    const hadCreate = URL.createObjectURL;
    const hadRevoke = URL.revokeObjectURL;
    URL.createObjectURL = vi.fn(() => "blob:manuscript");
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function record() { names.push(this.download); });
    try {
      await click(host.querySelector('[data-testid="chapter-export"]'));
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      // manuCompile 用同一个书名串写文件名和 `# ` 标题
      expect(names).toEqual([`测试长篇 · 第 1 章 · ${longTitle}.md`]);
      expect(host.querySelector(".ms-status").textContent).toContain("本章已导出");
    } finally {
      URL.createObjectURL = hadCreate;
      URL.revokeObjectURL = hadRevoke;
    }
  });

  it("刷新汇总在途时换章：结果不报到新章头上；回到原章时按钮仍在忙，结果落回原章", async () => {
    const other = { ...chapter("draft"), id: "ch02", backendId: "c2", n: "02", title: "雾里的灯" };
    const host = await renderPage([chapter("draft"), other]);
    let resolveAggregate;
    flow.aggregate.mockReturnValueOnce(new Promise((resolve) => { resolveAggregate = resolve; }));
    const aggregateButton = () => host.querySelector('[data-testid="chapter-aggregate"]');

    await click(aggregateButton());
    expect(aggregateButton().disabled).toBe(true);

    await click(chapterRow(host, "c2"));
    expect(host.querySelector(".ms-reader-title").textContent).toBe("雾里的灯");
    expect(aggregateButton().disabled).toBe(false);
    await click(chapterRow(host, "c1"));
    expect(aggregateButton().disabled).toBe(true);
    await click(chapterRow(host, "c2"));

    await act(async () => { resolveAggregate({ status: "created" }); await Promise.resolve(); await Promise.resolve(); });
    expect(host.textContent).not.toContain("章节汇总已生成");

    await click(chapterRow(host, "c1"));
    expect(aggregateButton().disabled).toBe(false);
    expect(host.querySelector(".ms-status").textContent).toContain("章节汇总已生成");
  });

  it("停在「对比」上退回小修：本章没有对比了就回到正文，不留一个没有选中项的分段", async () => {
    window.WrDocVersions = { list: vi.fn().mockResolvedValue([]), paras: vi.fn(), diff: vi.fn() };
    const host = await renderPage("review");
    const checked = () => host.querySelector('.ms-reader-tools [role="radio"][aria-checked="true"]');
    await click(button(host.querySelector(".ms-reader-tools"), "对比"));
    expect(checked().textContent).toBe("对比");

    catalogRefresh.mockImplementationOnce(async () => { fixture.catalog = [chapter("draft")]; return {}; });
    await click(button(host.querySelector(".ms-reader-foot"), "退回小修"));
    await typeTextarea(dialog().querySelector("textarea"), "第二段节奏太慢");
    await click(button(dialog(), "退回并生成待办"));

    expect(button(host.querySelector(".ms-reader-tools"), "对比")).toBeUndefined();
    expect(checked() && checked().textContent).toBe("正文");
  });

  it("批准失败后关掉再打开批准对话框，不先看到上一次的错误", async () => {
    flow.confirmRead.mockRejectedValueOnce(new Error("通读确认没有通过"));
    const host = await renderPage("review");
    await click(host.querySelector('[data-testid="approve-final-open"]'));
    await click(document.querySelector('[data-testid="approve-read-confirm"]'));
    await click(document.querySelector('[data-testid="approve-final-confirm"]'));
    expect(dialog().querySelector('[role="alert"]').textContent).toContain("通读确认没有通过");

    await click(button(dialog(), "取消"));
    expect(dialog()).toBeNull();
    await click(host.querySelector('[data-testid="approve-final-open"]'));
    expect(dialog().querySelector('[role="alert"]')).toBeNull();
    expect(document.querySelector('[data-testid="approve-read-confirm"]').checked).toBe(false);
  });
});


describe("成稿中心 · 诊断计数与诊断页签", () => {
  it("左栏章行与页签标出还开着的发现数；结构页签的场景行给「诊断 N」并带深改姿态进写作台；诊断页签挂面板", async () => {
    diagFx.chapters = { c1: { open: 3, blocking: 1 } };
    diagFx.scenes = { s1: { open: 3, blocking: 1 } };
    const go = vi.fn();
    const host = await renderPage("writing", go);
    expect(host.querySelector('[data-testid="manuscript-chapter-item"]').textContent).toContain("诊断 3");
    const tab = [...host.querySelectorAll('[role="radio"]')].find((node) => node.textContent.includes("诊断"));
    expect(tab.textContent).toContain("诊断 3");

    const structure = [...host.querySelectorAll('[role="radio"]')].find((node) => node.textContent === "结构");
    await act(async () => structure.click());
    const chip = host.querySelector('[data-testid="ms-scene-diag"]');
    expect(chip.textContent).toBe("诊断 3");
    await act(async () => chip.click());
    expect(go).toHaveBeenLastCalledWith("writer", [
      { type: "ws:writer-scene", detail: "ch01s1" },
      { type: "ws:writer-posture", detail: "deep" },
    ]);

    await act(async () => tab.click());
    expect(host.querySelector('[data-testid="manuscript-diagnosis-stub"]').textContent).toBe("c1");
    diagFx.chapters = {};
    diagFx.scenes = {};
  });
});

describe("成稿中心 · 像不像", () => {
  it("正文的场头与结构页签的场景行挂这一场最新的终稿角标；页头一句几场在作者范围内", async () => {
    fidFx.project = {
      bound: true,
      scene_finals: { s1: { reading_id: "r1", percentile: 41.6, within_range: true, reliable: true, created_at: "2026-09-23T10:00:00" } },
    };
    const host = await renderPage("review");
    expect(fidFx.load).toHaveBeenCalledWith("p1", { force: true });
    expect(host.querySelector('[data-testid="manuscripts-fidelity-summary"]').textContent).toBe("像不像：1 场终稿里 1 场在作者范围内");
    const headBadge = host.querySelector('.ms-scene-head [data-testid="ms-scene-fidelity"]');
    expect(headBadge.textContent).toBe("作者范围内 · 第 42 位");
    expect(headBadge.getAttribute("title")).toContain("前 90 位");

    const structure = [...host.querySelectorAll('[role="radio"]')].find((node) => node.textContent === "结构");
    await click(structure);
    const row = host.querySelector('.ms-struct-scenes li');
    expect(row.querySelector('[data-testid="ms-scene-fidelity"]').textContent).toBe("作者范围内 · 第 42 位");
    // 小标都在同一格里：行里的直接子元素个数不随有没有角标而变
    expect(row.children.length).toBe(5);
  });

  it("超出范围 / 量不准各有说法；作品没用参考书的文风时什么也不挂", async () => {
    fidFx.project = { bound: true, scene_finals: { s1: { percentile: 96, within_range: false, reliable: true } } };
    let host = await renderPage("review");
    expect(host.querySelector('[data-testid="ms-scene-fidelity"]').textContent).toBe("超出范围 · 第 96 位");
    expect(host.querySelector('[data-testid="manuscripts-fidelity-summary"]').textContent).toBe("像不像：1 场终稿里 0 场在作者范围内");

    fidFx.project = { bound: false, scene_finals: { s1: { percentile: 30, within_range: true, reliable: true } } };
    host = await renderPage("review");
    expect(host.querySelector('[data-testid="ms-scene-fidelity"]')).toBeNull();
    expect(host.querySelector('[data-testid="manuscripts-fidelity-summary"]')).toBeNull();
  });
});
