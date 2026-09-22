// AI 起草台的左栏 = 全书书脊（阶段 X）。
// 过去它是一份手工挑出来的运行队列：雪花刚「整理章节结构」，作者走进起草台看到的是
// 「运行队列还是空的」——章和场明明都在目录里，这张台子却像没收到。现在：
//   · 目录里的章与场全部列在左栏，落点 = 「现在该写哪一场」，中间直接是这一场的预检与设计卡；
//   · 只是点开看看（transient）不落盘、不算在办；交给 AI（整章入列 / 入列意图 / 开始起草 / 跑过管线）才是在办；
//   · 「在办」筛选只留在办的场；在办的行沿用原队列的移出 / 多选契约（见 ws-scene-queue.test.jsx）。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_CHAP, installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
  cancelRunJob: vi.fn(), getLatestSceneRunJob: vi.fn(),
}));
vi.mock("./ws-snow.jsx", () => ({ S2_BE_STEPS: [], s2ExportState: () => null }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 5000, interval: 25 };
const mounted = [];
const QUEUE_KEY = "scn-queue:v1::prj-main";

const scene = (id, title, extra = {}) => ({
  slug: id, legacy_slug: "", scene_id: id, title, summary: `${title}——整句摘要`, kind: "proactive", state: "todo", words: 0,
  brief: { goal: "目标", conflict: "冲突", setback: "挫折" }, pov_character_name: "林昭",
  design: { origin: "snowflake", crucible: "非面对不可", location: "雨城", cast: [], followup: {} },
  ...extra,
});
const BOOK = [
  { ...DEFAULT_CHAP, slug: "ch01", chapter_id: "P_CH01", no: "01", title: "雨夜来信", state: "planned", current: true,
    origin: "snowflake", spine: "灾一", scenes: [scene("SC_a", "旧信到了", { state: "done" }), scene("SC_b", "翻出案卷"), scene("SC_c", "登门对质")] },
  { ...DEFAULT_CHAP, slug: "ch02", chapter_id: "P_CH02", no: "02", title: "旧案卷宗", state: "planned", current: false,
    origin: "snowflake", scenes: [scene("SC_d", "档案馆夜访"), scene("SC_e", "父亲的谎")] },
];
const RUN_STATES_URL = /^\/api\/v1\/scene-run-states\?/;

async function loadScene(opts = {}) {
  const client = await import("./lib/client.js");
  installApiRouter(client, { catalog: BOOK, ...opts });
  client.getLatestSceneRunJob.mockRejectedValue(Object.assign(new Error("no job"), { status: 404 }));
  const base = client.apiGet.getMockImplementation();
  client.apiGet.mockImplementation((url) => (
    RUN_STATES_URL.test(url)
      ? Promise.resolve({ items: (opts.runStateSceneIds || []).map((id) => ({ scene_id: id })) })
      : base(url)
  ));
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const mod = await import("./ws-scene.jsx");
  return { ...mod, client };
}

async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return host;
}
const click = (node) => act(async () => node.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const idle = (host) => [...host.querySelectorAll('[data-testid="scene-spine-item"]')];
const pinned = (host) => [...host.querySelectorAll('[data-testid="scene-queue-item"]')];
const sidOf = (row) => row.getAttribute("data-scene-sid");
const queue = () => JSON.parse(window.localStorage.getItem(QUEUE_KEY) || "[]");

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  vi.spyOn(window, "alert").mockImplementation(() => {});
});
afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  const { clearViewIntents } = await import("./ws-view-intents.js");
  clearViewIntents("scene");
  vi.restoreAllMocks();
});

describe("AI 起草台 · 全书书脊", () => {
  it("在办清单是空的也不再是一块空白：全书的章与场都在左栏，落点是现在该写的那一场，中间是它的设计卡", async () => {
    const { WsScene } = await loadScene();
    const host = await render(<WsScene t={{}} go={vi.fn()} />);
    expect(host.textContent).not.toContain("运行队列还是空的");
    expect(host.querySelectorAll('[data-testid="scene-spine-chapter"]').length).toBe(2);
    expect(idle(host).map(sidOf)).toEqual(["SC_a", "SC_b", "SC_c", "SC_d", "SC_e"]);
    expect(pinned(host)).toEqual([]);
    // 场的状态词与写作台大纲、章节编排、成稿中心同一份（ws-labels）：写完 = 已完成；没动过的场不挂标签
    const chips = idle(host).map((row) => { const chip = row.querySelector(".scn2-chip"); return chip ? chip.textContent.trim() : null; });
    expect(chips).toEqual(["已完成", null, null, null, null]);
    // 落点：当前章第一场没写完的（SC_a 已完成）
    expect(host.querySelector(".scn2-qrow.is-active").getAttribute("data-scene-sid")).toBe("SC_b");
    const card = host.querySelector('[data-testid="scene-design-card"]');
    expect(card.getAttribute("data-origin")).toBe("snowflake");
    expect(card.textContent).toContain("非面对不可");
    expect(host.textContent).toContain("三拍齐了");
    expect(host.textContent).not.toContain("参考画像未绑定"); // 预检清单里不再有写死的假条目
    // 只是落在这一场上：不落盘、不算在办
    expect(queue()).toEqual([]);
    expect(host.querySelector('[data-testid="scene-spine-filter-active"]').textContent).toContain("0");
  });

  it("书脊上的状态词：跑完归档的场和目录里写完的场是同一个词；管线中间态照旧是起草台自己的", async () => {
    const { spineChip } = await import("./ws-scene-spine.jsx");
    expect(spineChip({ fromRun: false, pinned: false, st: "done" })).toEqual({ label: "已完成", tone: "ok" });
    expect(spineChip({ fromRun: true, pinned: true, st: "archived" })).toEqual({ label: "已完成", tone: "ok" }); // 以前这里叫「已归档」
    expect(spineChip({ fromRun: false, pinned: true, st: "done" }).label).toBe("已完成");
    expect(spineChip({ fromRun: false, pinned: false, st: "writing" })).toEqual({ label: "写作中", tone: "accent" }); // 以前叫「在写」
    expect(spineChip({ fromRun: false, pinned: false, st: "todo" }).label).toBe("待写");
    // 交给了 AI 还没开跑的没写的场，和排队中的场一个词
    expect(spineChip({ fromRun: false, pinned: true, st: "todo" }).label).toBe("待起草");
    expect(spineChip({ fromRun: true, pinned: true, st: "ready" }).label).toBe("待复核");
    expect(spineChip({ fromRun: true, pinned: true, st: "running" }).label).toBe("运行中");
  });

  it("在书脊上点开别的场只是看看：换了落点，仍然不落盘", async () => {
    const { WsScene } = await loadScene();
    const host = await render(<WsScene t={{}} go={vi.fn()} />);
    await click(idle(host).find(row => sidOf(row) === "SC_d"));
    expect(host.querySelector(".scn2-qrow.is-active").getAttribute("data-scene-sid")).toBe("SC_d");
    await click(idle(host).find(row => sidOf(row) === "SC_e"));
    expect(host.querySelector(".scn2-qrow.is-active").getAttribute("data-scene-sid")).toBe("SC_e");
    expect(queue()).toEqual([]);
    expect(pinned(host)).toEqual([]);
  });

  it("整章入列：本章没写完的场交给 AI——成为在办、落盘；「在办」筛选只留它们", async () => {
    const { WsScene } = await loadScene();
    const host = await render(<WsScene t={{}} go={vi.fn()} />);
    await click(host.querySelectorAll('[data-testid="scene-spine-enqueue-chapter"]')[0]);
    expect(pinned(host).map(sidOf)).toEqual(["SC_b", "SC_c"]); // 已完成的 SC_a 不入列
    expect(queue().sort()).toEqual(["SC_b", "SC_c"]);
    expect(host.querySelector(".scn2-qrow.is-active").getAttribute("data-scene-sid")).toBe("SC_b");

    await click(host.querySelector('[data-testid="scene-spine-filter-active"]'));
    expect(host.querySelectorAll('[data-testid="scene-spine-chapter"]').length).toBe(1);
    expect(idle(host)).toEqual([]);
    expect(pinned(host).map(sidOf)).toEqual(["SC_b", "SC_c"]);
    await click(host.querySelector('[data-testid="scene-spine-filter-all"]'));
    expect(idle(host).map(sidOf)).toEqual(["SC_a", "SC_d", "SC_e"]);
  });

  it("跑过管线的场从后端恢复成在办：正落在这一场上时就地转正，不重复出一条", async () => {
    const { WsScene } = await loadScene({ runStateSceneIds: ["SC_b"] });
    const host = await render(<WsScene t={{}} go={vi.fn()} />);
    await vi.waitFor(() => expect(pinned(host).map(sidOf)).toEqual(["SC_b"]), T);
    expect(host.querySelectorAll('[data-scene-sid="SC_b"]').length).toBe(1);
    expect(queue()).toEqual(["SC_b"]);
  });

  it("「交给 AI」的入列意图（章节编排 / 写作台 / 待办）把这一场变成在办并选中", async () => {
    const { WsScene } = await loadScene();
    const host = await render(<WsScene t={{}} go={vi.fn()} />);
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:scene-enqueue", { detail: { sid: "SC_e" } })); });
    expect(pinned(host).map(sidOf)).toEqual(["SC_e"]);
    expect(queue()).toEqual(["SC_e"]);
    expect(host.querySelector(".scn2-qrow.is-active").getAttribute("data-scene-sid")).toBe("SC_e");
  });

  it("「在构思里改」只有一个入口（裁决条上），回构思第 10 步并对准这一场", async () => {
    const go = vi.fn();
    const { WsScene } = await loadScene();
    const host = await render(<WsScene t={{}} go={go} />);
    // 设计卡就摆在台面上，卡头不再重复一枚「在构思里改」
    expect(host.querySelector('[data-testid="scene-design-card"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="scene-design-edit-plan"]')).toBeNull();
    const doors = [...host.querySelectorAll("button")].filter(b => b.textContent.includes("在构思里改"));
    expect(doors.map(b => b.getAttribute("data-testid"))).toEqual(["scene-decide-edit-plan"]);
    await click(doors[0]);
    expect(go).toHaveBeenCalledWith("snowflake", [
      { type: "ws:snow-step", detail: "planning" }, { type: "ws:snow-scene", detail: "SC_b" },
    ]);
  });

  it("阶段 Y：雪花整理出来的场，决策条上不再让作者去章节编排「编辑场景卡」——设计只在构思里改", async () => {
    const go = vi.fn();
    const { WsScene } = await loadScene();
    const host = await render(<WsScene t={{}} go={go} />);
    const button = host.querySelector('[data-testid="scene-decide-edit-plan"]');
    expect(button.textContent).toBe("在构思里改");
    expect([...host.querySelectorAll(".scn2-decide-acts button")].map(b => b.textContent)).not.toContain("编辑场景卡");
    await click(button);
    expect(go).toHaveBeenCalledWith("snowflake", [
      { type: "ws:snow-step", detail: "planning" }, { type: "ws:snow-scene", detail: "SC_b" },
    ]);
  });
});

describe("AI 起草台 · 不问没进过管线的场", () => {
  it("只是点开看看、从没跑过的场不去问 latest（不再每点一场就多一条 404）；交给 AI 之后才问", async () => {
    const { WsScene, client } = await loadScene();
    const host = await render(<WsScene t={{}} go={vi.fn()} />);
    await vi.waitFor(() => expect(client.apiGet.mock.calls.some(([url]) => RUN_STATES_URL.test(url))).toBe(true), T);
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); });
    expect(host.querySelector('[data-testid="scene-run-job-control"]')).not.toBeNull();
    expect(client.getLatestSceneRunJob).not.toHaveBeenCalled();

    await click(host.querySelectorAll('[data-testid="scene-spine-enqueue-chapter"]')[0]);
    await vi.waitFor(() => expect(client.getLatestSceneRunJob).toHaveBeenCalledWith("SC_b", expect.anything()), T);
  });

  it("后端清单里有的场（跑过管线）照常问 latest", async () => {
    const { WsScene, client } = await loadScene({ runStateSceneIds: ["SC_b"] });
    await render(<WsScene t={{}} go={vi.fn()} />);
    await vi.waitFor(() => expect(client.getLatestSceneRunJob).toHaveBeenCalledWith("SC_b", expect.anything()), T);
  });

  it("读不到后端清单时不能断定「没跑过」：照常问 latest", async () => {
    const { WsScene, client } = await loadScene();
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (RUN_STATES_URL.test(url) ? Promise.reject(new Error("offline")) : base(url)));
    await render(<WsScene t={{}} go={vi.fn()} />);
    await vi.waitFor(() => expect(client.getLatestSceneRunJob).toHaveBeenCalledWith("SC_b", expect.anything()), T);
  });
});
