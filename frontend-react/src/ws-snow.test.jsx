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
// 视图从 ws-snow-sync.jsx 直接 import SnowSync；分章面板（与章节编排共用）仍读 window.SnowSync。
// 每个用例把自己的假 SnowSync 挂在 window 上，这里的模块 mock 转发过去（用例没给的方法读出来是 undefined）。
vi.mock("./ws-snow-sync.jsx", () => ({
  SnowSync: new Proxy({}, { get: (_target, name) => (window.SnowSync ? window.SnowSync[name] : undefined) }),
}));

import { WsSnowflake, WsConstruct, S2_STEPS, S2_BE_STEPS, s2PlanSlots, s2PlanState, s2PlanAuto, s2StaleMap, s2UpstreamDrift, s2NormalizeState, s2ReorderScenes } from "./ws-snow.jsx";
import { WS_SNOW_STEPS } from "./ws-nav.js";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const mounted = [];

async function renderSnow() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(<WsSnowflake initialStep="paragraph" />));
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

  it("点击“整理章节结构”打开分章预览面板，而不是直接落库", async () => {
    // P2：这个按钮以前是 window.confirm 加三条互不相同的落库路径，选哪条取决于闸门
    // 状态 —— 做得越完整反而掉进最差的那条，而且确认框说「并入 12 章」实际写 1 章。
    // 现在它只做一件事：打开预览，让作者按下确认之前就看得见会得到什么。
    const host = await renderSnow();

    const button = host.querySelector('[data-testid="snow-materialize-top"]');
    expect(button).toBeTruthy();
    // 第 10 步还没确认：它是次要按钮，这一页唯一的实心主动作是「确认本步」
    expect(button.classList.contains("btn-accent")).toBe(false);
    expect(button.classList.contains("btn-ghost")).toBe(true);
    expect(host.querySelector(".snow-strip").textContent).not.toContain("越早回头修订越省力");

    await act(async () => button.click());

    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).toBeTruthy();
    expect(window.SnowSync.chapterPreview).toHaveBeenCalledTimes(1);
    // 预览阶段绝不落库
    expect(catalog.adoptOutline).not.toHaveBeenCalled();
    expect(window.SnowSync.materialize).not.toHaveBeenCalled();
  });

  it("SNOW-01：同步层广播 ws:snow-chapter-plan（每次水合 / 自动保存后都会发）不再把分章面板弹出来", async () => {
    // 以前视图把这个事件名当「打开面板」的命令，而 SnowSync 在每次捕获工作台时都用它广播分章状态——
    // 结果面板在落地、刷新、09/10 自动保存后自己弹出来。命令改成视图内的回调，广播只归同步层。
    const host = await renderSnow();
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:snow-chapter-plan", { detail: "new-book" })); });
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).toBeNull();
    expect(window.SnowSync.chapterPreview).not.toHaveBeenCalled();
  });

  it("右栏只有一个评分：后端的评定；没有按关键词计数的「实时自评」，也没有页脚的「自检 0/3」", async () => {
    window.SnowSync.health = () => ({ paragraph: { beStatus: "approved", score: 88, status: "pass", filled: 2, total: 2, gateSatisfied: true, missingFields: [], nextActions: ["建议：让灾难二更疼一点"] } });
    const host = await renderSnow();
    const checks = host.querySelector('[data-testid="snow-step-checks"]');
    expect(checks.textContent).toContain("后端 88 分");
    expect(checks.textContent).toContain("结构达标");
    expect(checks.textContent).toContain("让灾难二更疼一点");
    expect(checks.textContent).not.toContain("建议：");
    expect(host.textContent).not.toContain("实时自评");
    expect(host.textContent).not.toContain("控制塔");
    expect(host.textContent).not.toMatch(/自检 \d+\/\d+/);
    // 人工清单仍在，但只是阅读辅助：role=checkbox，可切换
    const box = checks.querySelector('[role="checkbox"]');
    expect(box.getAttribute("aria-checked")).toBe("false");
    await act(async () => box.click());
    expect(checks.querySelector('[role="checkbox"]').getAttribute("aria-checked")).toBe("true");
  });

  /* 一页只有一个实心主按钮（VIS-11）：空着的一步是 AI 生成；写了还没落定的是「确认本步」；
     落定之后、第 10 步也确认过了，才轮到页头的「整理章节结构」 */
  const seedParagraph = (extra = {}) => {
    const saved = JSON.parse(window.localStorage.getItem("ws_snow_state_v2::new-book"));
    window.localStorage.setItem("ws_snow_state_v2::new-book", JSON.stringify({
      ...saved, ...extra,
      scaffolds: { ...saved.scaffolds, paragraph: { premiseF: "合成前提：守信的人", setup: "合成开端" } },
    }));
  };
  const accents = (host) => [...host.querySelectorAll(".btn-accent")].map(b => b.getAttribute("data-testid"));

  it("第 10 步确认之后，眼前这一步也落定了，「整理章节结构」才成为页头唯一的实心主按钮", async () => {
    seedParagraph({ states: { planning: "done", paragraph: "done" } });
    window.SnowSync.health = () => ({ paragraph: { beStatus: "approved", gateSatisfied: true, missingFields: [], nextActions: [] } });
    const host = await renderSnow();
    expect(accents(host)).toEqual(["snow-materialize-top"]);
  });

  it("第 10 步确认过，但眼前这一步改过还没确认：主按钮是页脚「确认本步」，页头和 AI 工具条都退成次要", async () => {
    seedParagraph({ states: { planning: "done" } });
    const host = await renderSnow();
    expect(accents(host)).toEqual(["snow-confirm-step"]);
    expect(host.querySelector('[data-testid="snow-materialize-top"]').classList.contains("btn-ghost")).toBe(true);
    expect(host.querySelector('[data-testid="snow-ai-generate"]').classList.contains("btn-ghost")).toBe(true);
  });

  it("服务器已确认的一步，页头标签和计数「已确认」、按钮「确认本步」同一个词（不再叫「已批准」）", async () => {
    const saved = JSON.parse(window.localStorage.getItem("ws_snow_state_v2::new-book"));
    window.localStorage.setItem("ws_snow_state_v2::new-book", JSON.stringify({ ...saved, states: { paragraph: "done" } }));
    window.SnowSync.health = () => ({ paragraph: { beStatus: "approved", gateSatisfied: true, missingFields: [], nextActions: [] } });
    const host = await renderSnow();
    const pill = host.querySelector('[data-testid="snow-confirmed-pill"]');
    expect(pill.textContent).toBe("已确认");
    expect(host.querySelector(".sf-head-side").textContent).not.toContain("已批准");
    // 已经落定的一步：页脚「确认本步」退成次要按钮，不和页头 / AI 工具条抢同一种实心红
    const confirmBtn = host.querySelector('[data-testid="snow-confirm-step"]');
    expect(confirmBtn.textContent).toContain("确认本步");
    expect(confirmBtn.classList.contains("btn-accent")).toBe(false);
  });

  it("写了内容、还没确认的一步，页脚「确认本步」是这一页唯一的实心主按钮", async () => {
    seedParagraph();
    const host = await renderSnow();
    expect(accents(host)).toEqual(["snow-confirm-step"]);
  });

  it("还空着的一步：唯一的实心主按钮是「AI 生成本步」，「确认本步」退成次要", async () => {
    const host = await renderSnow();
    expect(accents(host)).toEqual(["snow-ai-generate"]);
    expect(host.querySelector('[data-testid="snow-confirm-step"]').classList.contains("btn-ghost")).toBe(true);
  });

  it("读不到服务器：画布上方一条危险提示说全原因并给重试；页头确认数写「—」；页脚只剩一句短状态", async () => {
    // 以前唯一的迹象是页脚一行「仅本机已保存 · 服务器同步失败 读不到服…」——原因被截成三个字，
    // 新浏览器里页面还是 0/10 的空白稿，作者很容易以为构思没了
    const message = "读不到服务器上的构思版本，已暂停上行以免覆盖服务器内容；本机版本已保留";
    window.SnowSync.syncState = () => ({ phase: "error", pendingSteps: [], error: { scope: "hydrate", code: 500, message, offline: false } });
    window.SnowSync.retry = vi.fn(async () => ({ phase: "synced" }));
    const host = await renderSnow();
    const notice = host.querySelector('[data-testid="snow-sync-notice"]');
    expect(notice.getAttribute("role")).toBe("alert");
    expect(notice.getAttribute("data-tone")).toBe("danger");
    expect(notice.querySelector(".ws-notice-title").textContent).toBe("暂时读不到服务器");
    expect(notice.textContent).toContain(message); // 原因全文，不再截成三个字
    expect(notice.textContent).toContain("不代表服务器上的构思没了");
    expect(host.querySelector('[data-testid="snow-progress"]').textContent).toBe("—/10 已确认");
    const foot = host.querySelector('[data-testid="snow-sync-status"]');
    expect(foot.textContent).toBe("同步失败重试");
    expect(foot.querySelector("em")).toBeNull();
    expect(foot.title).toBe(message); // 悬停仍能看到全文
    await act(async () => host.querySelector('[data-testid="snow-sync-notice-retry"]').click());
    expect(window.SnowSync.retry).toHaveBeenCalledTimes(1);
  });

  it("同步正常时没有提示条，确认数照常；本机保存失败时提示条给「立即导出」而不是重试", async () => {
    window.SnowSync.syncState = () => ({ phase: "synced", pendingSteps: [], error: null, lastSyncedAt: Date.now() });
    let host = await renderSnow();
    expect(host.querySelector('[data-testid="snow-sync-notice"]')).toBeNull();
    expect(host.querySelector('[data-testid="snow-progress"]').textContent).toMatch(/^\d+\/10 已确认$/);

    window.SnowSync.syncState = () => ({ phase: "error", pendingSteps: [], error: { scope: "local", message: "本机自动保存失败，请先导出构思" } });
    host = await renderSnow();
    const notice = host.querySelector('[data-testid="snow-sync-notice"]');
    expect(notice.textContent).toContain("本机保存失败");
    expect(notice.querySelector('[data-testid="snow-sync-notice-retry"]')).toBeNull();
    expect([...notice.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["立即导出"]);
    expect(host.querySelector('[data-testid="snow-progress"]').textContent).toMatch(/^\d+\/10 已确认$/); // 本机失败不影响服务器上的确认数
  });

  it("方向键只在焦点落在页面或步骤列表上时翻步：页签上的 ← / → 只切页签", async () => {
    const host = await renderSnow();
    const title = () => host.querySelector(".snow-canvas-title").textContent;
    expect(title()).toBe("一段话概括");
    const tab = [...host.querySelectorAll('[role="tab"]')][0];
    tab.focus();
    await act(async () => { tab.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true })); });
    expect(title()).toBe("一段话概括");
    const step = host.querySelector('[data-testid="snow-step-paragraph"]');
    step.focus();
    await act(async () => { step.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true })); });
    expect(title()).toBe("角色摘要表");
    expect(host.querySelector('[data-testid="snow-step-characters"]').getAttribute("aria-current")).toBe("step");
  });

  it("模态框开着时全局快捷键一律不响：遮罩上按下鼠标不丢焦点；焦点就算落到 body，←/→ 也不在面板背后换步、⌘↵ 不确认背后那一步", async () => {
    window.SnowSync.needsReconfirm = vi.fn(() => false);
    const host = await renderSnow();
    const title = () => host.querySelector(".snow-canvas-title").textContent;
    await act(async () => host.querySelector('[data-testid="snow-materialize-top"]').click());
    const panel = host.querySelector('[data-testid="chapter-plan-panel"]');
    expect(panel).toBeTruthy();
    // 并一章 → 面板里有未确认的调整，点遮罩不关；遮罩上 mousedown 的默认动作（把焦点移到 body）被拦下
    await act(async () => host.querySelector('[data-testid="chapter-plan-merge-1"]').click());
    const scrim = host.querySelector(".sf-chapterplan-scrim");
    const down = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    await act(async () => { scrim.dispatchEvent(down); });
    expect(down.defaultPrevented).toBe(true);
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).toBeTruthy();
    // 焦点仍可能因为别的原因落到 body（比如点了面板外的空白）：页面级按键照样不能穿过模态框
    await act(async () => { document.activeElement.blur(); });
    expect(document.activeElement).toBe(document.body);
    const key = (init) => act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init })); });
    await key({ key: "ArrowRight" });
    expect(title()).toBe("一段话概括");
    await key({ key: "Enter", ctrlKey: true });
    expect(window.SnowSync.needsReconfirm).not.toHaveBeenCalled();
    expect(title()).toBe("一段话概括");
    expect(host.querySelector('[data-testid="snow-step-paragraph"]').className).not.toContain("s-done");
    // 面板关掉之后，页面上的 → 照常翻步（守卫只在模态框开着时生效）
    await act(async () => host.querySelector('[data-testid="chapter-plan-panel"] .wr-drawer-x').click());
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).toBeNull();
    await act(async () => { document.activeElement.blur(); });
    await key({ key: "ArrowRight" });
    expect(title()).toBe("角色摘要表");
  });

  it("窄屏的「本步上下文」抽屉：焦点移进抽屉；Esc / × 关上后焦点回到 ⓘ 按钮（不留在隐藏的抽屉里、不掉到 body）；抽屉开着时 ←/→ 不翻步", async () => {
    const original = window.matchMedia;
    window.matchMedia = vi.fn((query) => ({ matches: true, media: query, addEventListener() {}, removeEventListener() {} }));
    try {
      const host = await renderSnow();
      const title = () => host.querySelector(".snow-canvas-title").textContent;
      const aside = host.querySelector("#snow-ctx");
      const openBtn = () => host.querySelector(".sf-ctx-open");
      expect(aside.getAttribute("role")).toBe("dialog");
      // 打开：焦点进抽屉
      openBtn().focus();
      await act(async () => openBtn().click());
      expect(openBtn().getAttribute("aria-expanded")).toBe("true");
      expect(aside.contains(document.activeElement)).toBe(true);
      // 抽屉开着、焦点落到 body 时 → 也不翻步
      await act(async () => { document.activeElement.blur(); });
      await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true })); });
      expect(title()).toBe("一段话概括");
      // body 上的 Esc 关抽屉，焦点回到按钮
      await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })); });
      expect(openBtn().getAttribute("aria-expanded")).toBe("false");
      expect(document.activeElement).toBe(openBtn());
      // 抽屉里的 Esc
      await act(async () => openBtn().click());
      expect(aside.contains(document.activeElement)).toBe(true);
      await act(async () => { document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })); });
      expect(openBtn().getAttribute("aria-expanded")).toBe("false");
      expect(document.activeElement).toBe(openBtn());
      // ×：共享的安静图标按钮（CloseButton），不是浏览器默认样式的裸 <button>
      await act(async () => openBtn().click());
      const x = aside.querySelector(".wr-drawer-x");
      expect(x.getAttribute("aria-label")).toBe("关闭本步上下文");
      for (const c of ["btn", "btn-quiet", "btn-icon"]) expect(x.classList.contains(c)).toBe(true);
      await act(async () => x.click());
      expect(openBtn().getAttribute("aria-expanded")).toBe("false");
      expect(document.activeElement).toBe(openBtn());
    } finally {
      window.matchMedia = original;
    }
  });

  it("对话框走共享的 WsDialog：导入结构（更多菜单里）与「上游改了什么」都是真对话框，Esc 关闭", async () => {
    window.SnowSync.health = () => ({ paragraph: { beStatus: "stale", staleAcceptedAt: null, staleReason: "合成的失效原因", stepRunId: "r2", inputRefs: { one_sentence_summary: "r1-old" } }, logline: { beStatus: "approved", stepRunId: "r1-new", inputRefs: {} } });
    window.SnowSync.upstreamChanges = vi.fn(async () => [{ feKey: "logline", oldVersion: 1, newVersion: 2, oldFound: true, oldText: "旧的一句", newText: "新的一句" }]);
    window.SnowSync.importCanonicalPlan = vi.fn(async () => ({ readyToMaterialize: true }));
    const host = await renderSnow();
    // 上游 diff
    await act(async () => host.querySelector('[data-testid="snow-stale-diff"]').click());
    await vi.waitFor(() => expect(document.querySelector('[data-testid="snow-upstream-diff-item"]')).toBeTruthy());
    const diff = document.querySelector('[data-testid="snow-upstream-diff"]');
    expect(diff.getAttribute("role")).toBe("dialog");
    expect(diff.textContent).toContain("合成的失效原因");
    expect(diff.textContent).toContain("新的一句");
    await act(async () => { document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })); });
    expect(document.querySelector('[data-testid="snow-upstream-diff"]')).toBeNull();
    // 导入结构：从「更多」菜单打开，提交走 SnowSync.importCanonicalPlan
    await act(async () => host.querySelector(".sf-more-btn").click());
    await act(async () => document.querySelector('[data-testid="snow-import-open"]').click());
    const dialog = document.querySelector('[data-testid="snow-import-dialog"]');
    expect(dialog.getAttribute("role")).toBe("dialog");
    const json = document.querySelector('[data-testid="snow-import-json"]');
    expect(document.activeElement).toBe(json);
    const setNative = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    await act(async () => { setNative.call(json, '{"steps":{}}'); json.dispatchEvent(new Event("input", { bubbles: true })); });
    // 粘了内容之后点遮罩不关，焦点也不掉到 body（对话框走 portal，遮罩的 mousedown 照样冒泡到视图根上被拦下）
    const importDown = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    await act(async () => { dialog.parentElement.dispatchEvent(importDown); });
    expect(importDown.defaultPrevented).toBe(true);
    expect(document.querySelector('[data-testid="snow-import-dialog"]')).toBeTruthy();
    await act(async () => document.querySelector('[data-testid="snow-import-submit"]').click());
    expect(window.SnowSync.importCanonicalPlan).toHaveBeenCalledWith(null, { steps: {} });
    expect(document.querySelector('[data-testid="snow-import-dialog"]')).toBeNull();
  });

  it("清空十步构思：在「更多」菜单的危险区，先开对话框说清后果；确认后每一步清空前的内容都留在「历史」里", async () => {
    window.localStorage.setItem("ws_snow_state_v2::new-book", JSON.stringify({
      drafts: { logline: "一句合成的一句话概括" },
      scaffolds: { paragraph: { setup: "合成的铺垫", d1: "合成的灾难一" } },
      states: { logline: "done", paragraph: "done" },
    }));
    const host = await renderSnow();
    expect([...host.querySelectorAll("button")].some(b => b.textContent.trim() === "重置")).toBe(false);
    await act(async () => host.querySelector('.sf-more-btn').click());
    await act(async () => document.querySelector('[data-testid="snow-reset-open"]').click());
    const dialog = document.querySelector('[data-testid="snow-reset-dialog"]');
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(dialog.textContent).toContain("服务器会为每一步另存一版空稿");
    expect(dialog.textContent).not.toContain("示例稿");
    await act(async () => document.querySelector('[data-testid="snow-reset-confirm"]').click());
    expect(document.querySelector('[data-testid="snow-reset-dialog"]')).toBeNull();
    expect(host.querySelector(".sf-beat-text").value).toBe("");
    expect(host.querySelector('[data-testid="undo-toast"]').textContent).toContain("可以逐步回滚");
    const tab = [...host.querySelectorAll('[role="tab"]')].find(b => b.textContent.includes("历史"));
    await act(async () => tab.click());
    const rows = [...host.querySelectorAll(".hist-row")].map(r => r.textContent);
    expect(rows[0]).toContain("清空十步构思");
    expect(rows.filter(r => r.includes("清空前留底")).length).toBe(2);
    expect(host.querySelectorAll(".hist-restore").length).toBe(2);
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


/* —— 阶段 M：09 是一张能随手挪的表；10 有钩子 / 离场变化；分诊随水合回来；略过写回服务端 —— */
describe("阶段 M · 09/10 交互", () => {
  const CACHE = "ws_snow_state_v2::new-book";
  const threeScenes = () => ({
    scaffolds: {
      characters: { sel: "c1", chars: { c1: { name: "林岑", role: "主角", goal: "", ambition: "", values: "", conflict: "", epiphany: "" } } },
      scenes: { lines: [], list: [
        { id: "S01", type: "proactive", line: "main", pov: "c1", place: "码头", event: "取账本", crucible: "退不出的困局", fn: "起疑", spine: "", chapter: "第一章 雨夜来信" },
        { id: "S02", type: "reactive", line: "main", pov: "c1", place: "旅馆", event: "消化挫败", crucible: "无人可信", fn: "转向", spine: "", chapter: "第一章 雨夜来信" },
        { id: "S03", type: "proactive", line: "main", pov: "c1", place: "旧屋", event: "找证人", crucible: "证人也在撒谎", fn: "逼近", spine: "灾一", chapter: "第二章 旧屋回声" },
      ] },
      planning: { sel: "S01", plans: { S01: { mode: "proactive", goal: "拿到账本", conflict: "三轮受阻", setback: "账本被烧" } } },
    },
    states: { audience: "done", logline: "done", paragraph: "done", scenes: "done" },
  });

  beforeEach(() => {
    window.localStorage.clear();
    catalog.get.mockReturnValue([]);
    vi.spyOn(window, "alert").mockImplementation(() => {});
    window.SnowSync = { chapterPreview: vi.fn(), materialize: vi.fn(), skipStep: vi.fn(async () => ({ beStatus: "skipped" })) };
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

  async function renderAt(step) {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    mounted.push({ root, host });
    await act(async () => root.render(<WsSnowflake initialStep={step} />));
    return host;
  }
  const rowIds = (host) => [...host.querySelectorAll(".sf-scene-row .sc-no")].map(el => el.getAttribute("title"));

  it("s2ReorderScenes：把一场挪到另一位置，其余顺序不变；越界或原地是无操作且不改原数组", () => {
    const list = [{ id: "S01" }, { id: "S02" }, { id: "S03" }, { id: "S04" }];
    expect(s2ReorderScenes(list, 0, 2).map(s => s.id)).toEqual(["S02", "S03", "S01", "S04"]);
    expect(s2ReorderScenes(list, 3, 0).map(s => s.id)).toEqual(["S04", "S01", "S02", "S03"]);
    expect(s2ReorderScenes(list, 1, 1).map(s => s.id)).toEqual(["S01", "S02", "S03", "S04"]);
    expect(s2ReorderScenes(list, 1, 9).map(s => s.id)).toEqual(["S01", "S02", "S03", "S04"]);
    expect(s2ReorderScenes(list, -1, 0).map(s => s.id)).toEqual(["S01", "S02", "S03", "S04"]);
    expect(list.map(s => s.id)).toEqual(["S01", "S02", "S03", "S04"]);
    expect(s2ReorderScenes(undefined, 0, 1)).toEqual([]);
  });

  it("09 场景表：同一章的第一场前有只读章头；「在这一场后面插一场」插在原位之后并继承线 / POV / 地点；拖放换位走同一纯函数", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes()));
    const host = await renderAt("scenes");
    expect(rowIds(host)).toEqual(["S01", "S02", "S03"]);
    expect(host.querySelector('[data-testid="snow-scene-chapter-0"]').textContent).toBe("第一章 雨夜来信");
    expect(host.querySelector('[data-testid="snow-scene-chapter-1"]')).toBeNull();     // 同章第二场不重复章头
    expect(host.querySelector('[data-testid="snow-scene-chapter-2"]').textContent).toBe("第二章 旧屋回声");
    // 阶段 Z：章头是一扇门——点它开分章面板（章归属只在那里改），不再只是一句提示
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).toBeNull();
    // 灾难标记：S03 显式标了灾一；其余两场的功能栏里没有灾难写法，不推断
    expect([...host.querySelectorAll(".sc-spine")].map(el => el.value)).toEqual(["", "", "灾一"]);
    await act(async () => host.querySelector('[data-testid="snow-scene-chapter-2"]').click());
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).not.toBeNull();
    await act(async () => host.querySelector('[data-testid="chapter-plan-panel"] .wr-drawer-x').click());
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).toBeNull();

    await act(async () => host.querySelector('[data-testid="snow-scene-insert-0"]').click());
    const ids = rowIds(host);
    expect(ids).toHaveLength(4);
    expect(ids[0]).toBe("S01");
    expect(ids[2]).toBe("S02");
    expect(ids[3]).toBe("S03");
    expect(["S01", "S02", "S03"]).not.toContain(ids[1]);
    const inserted = host.querySelector('[data-testid="snow-scene-row-1"]');
    expect(inserted.querySelector(".sc-pov").value).toBe("c1");            // 继承前一场的 POV
    expect(inserted.querySelector(".sc-in-place").value).toBe("码头");      // 继承地点
    expect(inserted.querySelector(".sc-in-event").value).toBe("");          // 事件留白，等作者写

    // 拖放：把第 4 行（S03）放到第 1 行之前
    const drag = (i, type) => act(async () => {
      const ev = new Event(type, { bubbles: true, cancelable: true });
      host.querySelector(`[data-testid="snow-scene-row-${i}"]`).dispatchEvent(ev);
    });
    await drag(3, "dragstart");
    await drag(0, "drop");
    expect(rowIds(host)).toEqual(["S03", "S01", ids[1], "S02"]);
  });

  it("10 场景规划：钩子 / 离场变化有输入框；存档的分诊随水合回来并显示在当前场上", async () => {
    const seeded = threeScenes();
    window.localStorage.setItem(CACHE, JSON.stringify(seeded));
    window.SnowSync.triageItems = vi.fn(() => ({ at: 1, source: "workspace", items: {
      S01: { status: "maybe", score: 55, notes: "坩埚说得太笼统", fix_steps: ["把困局写成具体的退路被断"], missing_fields: [], repair_patch: {} },
    } }));
    const host = await renderAt("planning");
    const hook = host.querySelector('[data-testid="snow-plan-hook"]');
    const exit = host.querySelector('[data-testid="snow-plan-exit-change"]');
    expect(hook).toBeTruthy();
    expect(exit).toBeTruthy();
    expect(host.querySelector(".sf-triage-badge").textContent).toBe("需修补");
    expect(host.querySelector(".sf-triage-notes").textContent).toBe("坩埚说得太笼统");
    expect(host.querySelector(".sf-triage-fixes li").textContent).toBe("把困局写成具体的退路被断");

    // 钩子 / 离场变化是会自己长高的多行框（长句子不再被一行截住）
    const proto = hook.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setNative = Object.getOwnPropertyDescriptor(proto, "value").set;
    await act(async () => { setNative.call(hook, "烧账本的人留下了她的名字"); hook.dispatchEvent(new Event("input", { bubbles: true })); });
    expect(host.querySelector('[data-testid="snow-plan-hook"]').value).toBe("烧账本的人留下了她的名字");
    // 三拍在场景卡细节之前：主三拍紧跟场景头，细节收在后面（收起时也在 DOM 里）
    const gcs = host.querySelector(".sf-gcs");
    const details = host.querySelector('[data-testid="snow-plan-details"]');
    expect(gcs.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(details.contains(host.querySelector('[data-testid="snow-plan-hook"]'))).toBe(true);
  });

  it("略过此步：必填步的按钮禁用（不开理由框、不打服务端）；可略过的步在页脚浮层里写一句理由再写回服务端", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes()));
    const prompt = vi.spyOn(window, "prompt").mockReturnValue("不该用到");
    const findSkip = (host) => [...host.querySelectorAll("button")].find(b => b.textContent.trim() === "略过此步");
    const typeReason = async (host, text) => {
      const input = host.querySelector('[data-testid="snow-skip-reason"]');
      const setNative = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
      await act(async () => { setNative.call(input, text); input.dispatchEvent(new Event("input", { bubbles: true })); });
    };

    const essential = await renderAt("logline");
    expect(findSkip(essential).disabled).toBe(true);
    await act(async () => findSkip(essential).click());
    expect(essential.querySelector('[data-testid="snow-skip-reason"]')).toBeNull();
    expect(window.SnowSync.skipStep).not.toHaveBeenCalled();

    const optional = await renderAt("characters");
    await act(async () => findSkip(optional).click());
    // 没写理由：确认按钮点不动
    expect(optional.querySelector('[data-testid="snow-skip-confirm"]').disabled).toBe(true);
    await typeReason(optional, "先按梗概走，人物表等第二稿");
    await act(async () => optional.querySelector('[data-testid="snow-skip-confirm"]').click());
    expect(window.SnowSync.skipStep).toHaveBeenCalledWith("new-book", "characters", "先按梗概走，人物表等第二稿");
    expect(prompt).not.toHaveBeenCalled();

    // 服务端拒绝：本地不标略过，浮层留着让作者看见
    window.SnowSync.skipStep.mockRejectedValueOnce(new Error("SNOWFLAKE_STEP_NOT_SKIPPABLE"));
    const again = await renderAt("synopsis");
    await act(async () => findSkip(again).click());
    await typeReason(again, "梗概之后再补");
    await act(async () => again.querySelector('[data-testid="snow-skip-confirm"]').click());
    expect(window.SnowSync.skipStep).toHaveBeenCalledTimes(2);
    expect(again.querySelector('[data-testid="snow-skip-reason"]')).not.toBeNull();
    expect(again.querySelector('[data-testid="snow-step-synopsis"]').className).not.toContain("s-skip");
    // 取消理由框：什么都不发生
    const cancelled = await renderAt("backstory");
    await act(async () => findSkip(cancelled).click());
    await typeReason(cancelled, "写了又不想略过");
    await act(async () => cancelled.querySelector('[data-testid="snow-skip-cancel"]').click());
    expect(cancelled.querySelector('[data-testid="snow-skip-reason"]')).toBeNull();
    expect(window.SnowSync.skipStep).toHaveBeenCalledTimes(2);
  });

  it("07 章节表按数组顺序显示；第二幕已有章时「添加第一幕章节」插在第一幕最后一章之后，而不是存成最后一章", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify({ scaffolds: { outline: { chapters: [
      { id: "01", act: 1, title: "合成一章", summary: "a", spine: "" },
      { id: "02", act: 2, title: "合成二章", summary: "b", spine: "" },
    ] } } }));
    const host = await renderAt("outline");
    const titles = () => [...host.querySelectorAll(".sf-ch-title")].map(el => el.value);
    expect(titles()).toEqual(["合成一章", "合成二章"]);
    const addAct1 = [...host.querySelectorAll(".sf-ch-add")].find(b => b.textContent.includes("第一幕"));
    await act(async () => addAct1.click());
    expect(titles()).toEqual(["合成一章", "（待补）", "合成二章"]);
    expect([...host.querySelectorAll(".sf-ch-no")].map(el => el.textContent)).toEqual(["第 1 章", "第 2 章", "第 3 章"]);
    // 章名就是「第 N 章」占位（或空着）时，左边不再并排写一遍章号——与分章面板同一条规则
    const setTitle = async (i, text) => act(async () => {
      const input = host.querySelectorAll(".sf-ch-title")[i];
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input, text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await setTitle(0, "第 1 章");
    await setTitle(1, "");
    expect([...host.querySelectorAll(".sf-ch-no")].map(el => el.textContent)).toEqual(["", "", "第 3 章"]);
    expect(host.querySelectorAll(".sf-ch-title")[1].placeholder).toBe("第 2 章（未命名）");
    // 章序是只读的：不再有能改章 id 的输入框（改一个字就换 key、丢焦点）
    expect(host.querySelector(".sf-ch-id")).toBeNull();
    // 07 的门开的是同一张分章面板
    await act(async () => host.querySelector('[data-testid="snow-materialize"]').click());
    expect(host.querySelectorAll('[data-testid="chapter-plan-panel"]').length).toBe(1);
  });

  it("09 灾难标记：没有显式标记时按「功能」一栏推断，只显示、不写回", async () => {
    const seeded = threeScenes();
    seeded.scaffolds.scenes.list[1].fn = "灾难一·一幕高潮";
    window.localStorage.setItem(CACHE, JSON.stringify(seeded));
    const host = await renderAt("scenes");
    const spine = host.querySelectorAll(".sc-spine")[1];
    expect(spine.value).toBe("");
    expect(spine.options[0].textContent).toBe("灾一（推断）");
    expect(spine.className).toContain("is-inferred");
    expect(host.querySelector(".sf-scene-stats").textContent).toContain("2 灾难场");
  });

  it("阶段 R：10 有题名 / 必须出现 / 破例理由 / 篇幅带输入框；主动场也能选概述两段但没有略过；破例即按已规划计", async () => {
    const seeded = threeScenes();
    window.localStorage.setItem(CACHE, JSON.stringify(seeded));
    const host = await renderAt("planning");
    for (const id of ["snow-plan-title", "snow-plan-must-include", "snow-plan-exception", "snow-plan-length", "snow-plan-length-custom", "snow-plan-render", "snow-plan-verdict"]) {
      expect(host.querySelector(`[data-testid="${id}"]`), id).toBeTruthy();
    }
    const renderOpts = [...host.querySelectorAll('[data-testid="snow-plan-render"] .sf-plan-render-opt')].map(b => b.textContent);
    expect(renderOpts).toEqual(["完整场", "概述两段"]); // S01 是主动场：可以概述，没有「略过」
    expect(s2PlanState({ exception: "全书收尾的叙述交代" }, "proactive")).toBe(2);
    expect(s2PlanState({ goal: "g" }, "proactive")).toBe(1);
    const longBtn = [...host.querySelectorAll('[data-testid="snow-plan-length"] .sf-plan-render-opt')].find(b => b.textContent === "长");
    await act(async () => { longBtn.click(); });
    expect([...host.querySelectorAll('[data-testid="snow-plan-length"] .sf-plan-render-opt')].find(b => b.textContent === "长").className).toContain("is-on");
    // 概述两段：篇幅带固定 200–500，篇幅选择器让位
    const summaryBtn = [...host.querySelectorAll('[data-testid="snow-plan-render"] .sf-plan-render-opt')].find(b => b.textContent === "概述两段");
    await act(async () => { summaryBtn.click(); });
    expect(host.querySelector('[data-testid="snow-plan-length"]')).toBeNull();
  });

  it("阶段 R：作者裁定——点「待删」经 SnowSync.saveTriageVerdict 写回服务端并高亮；失败回滚到上一次裁定", async () => {
    const seeded = threeScenes();
    window.localStorage.setItem(CACHE, JSON.stringify(seeded));
    window.SnowSync.saveTriageVerdict = vi.fn(async () => ({ triage_id: "t1", scene_plan_id: "sp1", recommended_status: "maybe" }));
    const host = await renderAt("planning");
    const cutBtn = host.querySelector('[data-testid="snow-verdict-cut"]');
    expect(cutBtn).toBeTruthy();
    await act(async () => { cutBtn.click(); });
    await vi.waitFor(() => expect(window.SnowSync.saveTriageVerdict).toHaveBeenCalledWith("new-book", expect.objectContaining({ row_uid: "S01", status: "cut" })));
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-verdict-cut"]').className).toContain("is-on"));
    expect(host.querySelector(".sf-triage-badge").textContent).toBe("待删");

    window.SnowSync.saveTriageVerdict = vi.fn(async () => { throw new Error("网络断了"); });
    await act(async () => { host.querySelector('[data-testid="snow-verdict-pass"]').click(); });
    await vi.waitFor(() => expect(host.querySelector('[data-testid="snow-verdict-cut"]').className).toContain("is-on"));
    expect(host.querySelector('[data-testid="snow-verdict-pass"]').className).not.toContain("is-on");
  });
});


/* —— SNOW-20：外部跳步就地处理（不再按步骤换 key 重挂整张视图），以及打开构思时落在哪一步 —— */
describe("SNOW-20 · 就地换步与落点", () => {
  const CACHE = "ws_snow_state_v2::new-book";
  const ALL = ["audience", "logline", "paragraph", "characters", "synopsis", "backstory", "outline", "profile", "scenes", "planning"];
  const doneUpTo = (n) => Object.fromEntries(ALL.slice(0, n).map(k => [k, "done"]));
  const threeScenes = (states = {}) => ({
    scaffolds: {
      characters: { sel: "c1", chars: { c1: { name: "合成主角", role: "主角", goal: "", ambition: "", values: "", conflict: "", epiphany: "" } } },
      scenes: { lines: [], list: [
        { id: "S01", type: "proactive", line: "main", pov: "c1", place: "合成码头", event: "合成事件一", crucible: "合成坩埚一", fn: "起疑", spine: "" },
        { id: "S02", type: "reactive", line: "main", pov: "c1", place: "合成旅馆", event: "合成事件二", crucible: "合成坩埚二", fn: "转向", spine: "" },
        { id: "S03", type: "proactive", line: "main", pov: "c1", place: "合成旧屋", event: "合成事件三", crucible: "合成坩埚三", fn: "逼近", spine: "灾一" },
      ] },
      planning: { sel: "S01", plans: {} },
    },
    states,
  });

  beforeEach(() => {
    window.localStorage.clear();
    catalog.get.mockReturnValue([]);
    window.__snowSceneTarget = null;
    window.__snowStepTarget = null;
    window.SnowSync = { chapterPreview: vi.fn(), materialize: vi.fn() };
  });
  afterEach(async () => {
    while (mounted.length) {
      const { root, host } = mounted.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
    try { delete window.SnowSync; } catch (e) {}
    window.__snowSceneTarget = null;
    window.__snowStepTarget = null;
  });

  async function mount(element) {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    mounted.push({ root, host });
    await act(async () => root.render(element));
    return host;
  }
  const title = (host) => host.querySelector(".snow-canvas-title").textContent;
  const fire = (type, detail) => act(async () => { window.dispatchEvent(new CustomEvent(type, { detail })); });

  it("步骤目录与外壳导航里的那份（ws-nav WS_SNOW_STEPS）一致：键、后端键、序号、名字", () => {
    expect(WS_SNOW_STEPS.map(s => [s.key, s.be, s.num, s.name]))
      .toEqual(S2_STEPS.map(s => [s.key, Object.fromEntries(S2_BE_STEPS)[s.key], s.num, s.name]));
  });

  it("成稿中心「回第 10 步」：先换步再选场，在同一个视图实例里完成（页面节点不换、目标被消费）", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes(doneUpTo(3))));
    window.SnowSync.rowUidForSceneId = vi.fn((workId, sceneId) => (sceneId === "sc-backend-2" ? "S02" : ""));
    const host = await mount(<WsConstruct />);
    const page = host.querySelector(".snow-page");
    expect(title(host)).toBe("角色摘要表");
    await fire("ws:snow-step", "planning");
    await fire("ws:snow-scene", "sc-backend-2");
    expect(title(host)).toBe("场景规划");
    expect(host.querySelector(".sf-plan-cur-id").textContent).toBe("S02");
    expect(window.SnowSync.rowUidForSceneId).toHaveBeenCalledWith("new-book", "sc-backend-2");
    expect(window.__snowSceneTarget).toBeNull();
    // 以前 WsConstruct 按目标步骤给 WsSnowflake 换 key：这里会是一个新的页面节点
    expect(host.querySelector(".snow-page")).toBe(page);
  });

  it("场景目标在水合之前对不上（对照表还没来）：先挂着，水合回来再选中那一场", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes()));
    let mapped = "";
    window.SnowSync.rowUidForSceneId = vi.fn(() => mapped);
    const host = await mount(<WsConstruct />);
    await fire("ws:snow-step", "planning");
    await fire("ws:snow-scene", "sc-backend-3");
    expect(title(host)).toBe("场景规划");
    expect(host.querySelector(".sf-plan-cur-id").textContent).toBe("S01");
    expect(window.__snowSceneTarget).toBe("sc-backend-3");
    mapped = "S03";
    await fire("ws:snow-hydrated", "new-book");
    expect(host.querySelector(".sf-plan-cur-id").textContent).toBe("S03");
    expect(window.__snowSceneTarget).toBeNull();
  });

  it("挂载前留下的目标步骤（旧握手 window.__snowStepTarget）：落在那一步，并被清掉", async () => {
    window.__snowStepTarget = "outline";
    const host = await mount(<WsConstruct />);
    expect(title(host)).toBe("长篇大纲");
    expect(window.__snowStepTarget).toBeNull();
  });

  it("没有外部目标时落在还没确认的第一步", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes(doneUpTo(3))));
    const host = await mount(<WsSnowflake />);
    expect(title(host)).toBe("角色摘要表");
  });

  it("后端判定需复核的步骤先于还没确认的步骤", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes(doneUpTo(3))));
    window.SnowSync.health = () => ({ logline: { beStatus: "stale", staleAcceptedAt: null, stepRunId: "r2", inputRefs: {} } });
    const host = await mount(<WsSnowflake />);
    expect(title(host)).toBe("一句话概括");
  });

  it("十步都确认过：回到这部作品上次看的那一步", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes(doneUpTo(10))));
    window.localStorage.setItem("ws_snow_last_step_v1", JSON.stringify({ "new-book": "outline", "other-book": "scenes" }));
    const host = await mount(<WsSnowflake />);
    expect(title(host)).toBe("长篇大纲");
    // 换步之后记下这一步（按作品分开记）
    await act(async () => host.querySelector('[data-testid="snow-step-profile"]').click());
    expect(JSON.parse(window.localStorage.getItem("ws_snow_last_step_v1"))).toEqual({ "new-book": "profile", "other-book": "scenes" });
  });

  it("本机缓存还没水合：第一次水合回来按服务端真相重定一次落点；之后的水合不再挪", async () => {
    window.SnowSync.hydrated = () => false;
    const host = await mount(<WsSnowflake />);
    expect(title(host)).toBe("读者定位");
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes(doneUpTo(8))));
    await fire("ws:snow-hydrated", "new-book");
    expect(title(host)).toBe("场景列表");
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes(doneUpTo(9))));
    await fire("ws:snow-hydrated", "new-book");
    expect(title(host)).toBe("场景列表");
  });

  it("作者在水合回来之前已经动过这一页：不再改落点", async () => {
    window.SnowSync.hydrated = () => false;
    const host = await mount(<WsSnowflake />);
    await act(async () => { host.querySelector(".snow-page").dispatchEvent(new Event("pointerdown", { bubbles: true })); });
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes(doneUpTo(8))));
    await fire("ws:snow-hydrated", "new-book");
    expect(title(host)).toBe("读者定位");
  });

  it("SNOW-26：编辑器是 memo 的——目录读回章之后，07 的「去 AI 起草台」仍然出现（目录状态从视图传下去，不在编辑器里自己读）", async () => {
    const host = await mount(<WsSnowflake initialStep="outline" />);
    expect(host.querySelector('[data-testid="snow-go-draft"]')).toBeNull();
    catalog.get.mockReturnValue([{ id: "CH01", scenes: [] }]);
    await fire("ws:catalog-changed", null);
    expect(host.querySelector('[data-testid="snow-go-draft"]')).not.toBeNull();
  });

  it("分章面板里「在构思里改这一场」：关面板，就地跳到第 10 步并选中那一场", async () => {
    window.localStorage.setItem(CACHE, JSON.stringify(threeScenes(doneUpTo(3))));
    window.SnowSync.chapterPreview = vi.fn(async () => ({
      strategy: "keep_current",
      chapters: [{ row_uid: "c1", chapter_seq: 1, act: 1, title: "合成一章", spine: "", chapter_goal: "", scene_count: 2, scenes: [
        { scene_plan_id: "sp1", scene_id: "S01", story_index: 1, title: "合成事件一", primary_form: "proactive", planned: true },
        { scene_plan_id: "sp3", scene_id: "S03", story_index: 3, title: "合成事件三", primary_form: "proactive", planned: true },
      ] }],
      unassigned: [], removed_scenes: [], warnings: [],
      totals: { chapter_count: 1, scene_count: 2, unassigned_count: 0 },
    }));
    const host = await mount(<WsSnowflake initialStep="scenes" />);
    const page = host.querySelector(".snow-page");
    await act(async () => host.querySelector('[data-testid="snow-materialize-top"]').click());
    await vi.waitFor(() => expect(host.querySelector('[data-testid="chapter-plan-scene-edit-3"]')).toBeTruthy());
    await act(async () => host.querySelector('[data-testid="chapter-plan-scene-edit-3"]').click());
    expect(host.querySelector('[data-testid="chapter-plan-panel"]')).toBeNull();
    expect(title(host)).toBe("场景规划");
    expect(host.querySelector(".sf-plan-cur-id").textContent).toBe("S03");
    expect(host.querySelector(".snow-page")).toBe(page);
  });
});
