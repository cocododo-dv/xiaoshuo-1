// 阶段 X「一条书脊」——雪花整理出来的章与场，在目录 store、共用设计卡、同步状态这三处的契约。
// 作者的原话：「雪花生成的章节感觉是孤立的，没有同步到 AI 起草台和写作台」。这里守的是：
//   · 目录把整张设计卡带给台子（不只是三拍），幕永远是 act1/2/3（否则章节编排上看不见这一章）；
//   · 场景的身份 = 后端 scene_id，不跟着位置走；位置式旧 sid / 乐观创建的临时 sid 仍解析得到同一场；
//   · 「现在该写哪一场」只有一条规则；
//   · 设计卡按形态给三拍命名、后续三拍、待同步提示与「在构思里改」；
//   · 同步状态只认已确认的规划，非雪花作品不再追问。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_CHAP, installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));
vi.mock("./ws-snow.jsx", () => ({ S2_BE_STEPS: [] }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 5000, interval: 25 };
const mounted = [];

const scene = (id, legacy, extra = {}) => ({
  slug: id, legacy_slug: legacy, scene_id: id, title: `场 ${legacy}`, summary: `${legacy} 的整句摘要`, kind: "proactive",
  state: "todo", words: 0, brief: { goal: "目标", conflict: "冲突", setback: "挫折" },
  pov_character_id: "c1", pov_character_name: "林昭", exit_change: "", hook: "",
  ...extra,
});

/* 真实后端的形状：两章雪花整理出来的章（幕是 act2，带脊柱标记与章摘要），第二场是带后续三拍的反应场 */
const SNOW_CATALOG = [
  {
    ...DEFAULT_CHAP, slug: "ch01", chapter_id: "P_CH01", no: "01", title: "雨夜来信", state: "planned", current: true,
    act: "act1", origin: "snowflake", summary: "她被停职了。", goal: "断掉退路", spine: "灾一", words: { cur: 0, target: null },
    scenes: [
      scene("P_SC_a", "ch01s1", { state: "done" }),
      scene("P_SC_b", "ch01s2", {
        kind: "reactive", brief: { reaction: "她在雨里站了很久", dilemma: "装作没看见，还是查到底", decision: "去档案馆调那份卷宗" },
        hook: "信封里还有第二张车票。", exit_change: "从回避转为追查",
        design: {
          origin: "snowflake", crucible: "停职通知明早生效", location: "雨城旧码头", story_time: "第二日 · 晨",
          cast: [{ character_id: "c2", name: "沈越" }], reader_emotion: "悲壮", must_include: "你欠这座城一个交代。",
          must_withhold: "", cost: "放弃安稳的工作", length_band: "1300-1600", rendering_mode: "full",
          followup: { goal: "翻进档案馆" }, exception_reason: "", protagonist: "林昭", is_chapter_last: true, desk_edited: false,
        },
        work: { run_status: "ready", has_final: false, has_words: false },
      }),
    ],
  },
  {
    ...DEFAULT_CHAP, slug: "ch02", chapter_id: "P_CH02", no: "02", title: "旧案卷宗", state: "planned", current: false,
    act: 2, origin: "snowflake", summary: "", goal: "", spine: "", words: { cur: 0, target: null },
    scenes: [scene("P_SC_c", "ch02s1")],
  },
];

async function loadCatalog(opts) {
  const client = await import("./lib/client.js");
  installApiRouter(client, { catalog: SNOW_CATALOG, ...(opts || {}) });
  const mod = await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(mod.WsCatalog.get().length).toBeGreaterThan(0), T);
  return { WsCatalog: mod.WsCatalog, client };
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
  vi.restoreAllMocks();
});

describe("目录 store：设计卡、幕、场景身份、落点", () => {
  it("整张设计卡随目录到达；幕归一成 act1/2/3（整数幕的章不会从编排台上消失）", async () => {
    const { WsCatalog } = await loadCatalog();
    const [first, second] = WsCatalog.get();
    expect(first.origin).toBe("snowflake");
    expect([first.summary, first.goal, first.spine]).toEqual(["她被停职了。", "断掉退路", "灾一"]);
    expect(second.act).toBe("act2");
    const reactive = first.scenes[1];
    expect(reactive.summary).toBe("ch01s2 的整句摘要");
    expect(reactive.design).toMatchObject({
      origin: "snowflake", crucible: "停职通知明早生效", location: "雨城旧码头", storyTime: "第二日 · 晨",
      readerEmotion: "悲壮", cost: "放弃安稳的工作", lengthBand: "1300-1600", chapterLast: true,
    });
    expect(reactive.design.cast).toEqual([{ id: "c2", name: "沈越" }]);
    expect(reactive.design.followup.goal).toBe("翻进档案馆");
    // 旧后端 / 夹具没给 design：是一张空卡，不是 undefined
    expect(first.scenes[0].design.origin).toBe("manual");
    expect(first.scenes[0].work).toEqual({ runStatus: "", hasFinal: false, hasWords: false });
  });

  it("场景 sid = 后端 scene_id；位置式旧 sid 仍解析到「现在排在那个位置上的场」", async () => {
    const { WsCatalog } = await loadCatalog();
    expect(WsCatalog.get()[0].scenes.map(s => s.sid)).toEqual(["P_SC_a", "P_SC_b"]);
    expect(WsCatalog.sceneById("P_SC_b").scene.title).toBe("场 ch01s2");
    expect(WsCatalog.sceneById("ch02s1").scene.sid).toBe("P_SC_c");
    expect(WsCatalog.sceneById("ch09s9")).toBeNull();
    expect(WsCatalog.sidForBackendId("P_SC_c")).toBe("P_SC_c");
    await expect(WsCatalog.__backendSceneId("ch01s2")).resolves.toBe("P_SC_b");
  });

  it("现在该写哪一场：当前章里在写的 → 第一场没写完的 → 末场；写作台与主页共用", async () => {
    const { WsCatalog } = await loadCatalog();
    expect(WsCatalog.focusScene().scene.sid).toBe("P_SC_b");
    expect(WsCatalog.writingScene().scene.sid).toBe("P_SC_b");
  });

  it("不再有「全书任何一场在写的场都赢过当前章」：别的章里标着在写的占位场抢不走落点", async () => {
    const stray = { ...SNOW_CATALOG[1], scenes: [scene("P_SC_c", "ch02s1", { state: "writing" })] };
    const { WsCatalog } = await loadCatalog({ catalog: [SNOW_CATALOG[0], stray] });
    expect(WsCatalog.focusScene().scene.sid).toBe("P_SC_b");
  });

  it("乐观创建的临时 sid 在目录重拉后仍找得到同一场（别名），不会让刚建完就在写的作者丢了落点", async () => {
    const { WsCatalog, client } = await loadCatalog();
    const grown = [SNOW_CATALOG[0], { ...SNOW_CATALOG[1], scenes: [...SNOW_CATALOG[1].scenes, scene("P_SC_new", "ch02s2", { title: "新场景" })] }];
    installApiRouter(client, { catalog: grown }); // 建场之后的那次目录重拉读到的是这一份
    client.apiPost.mockImplementation((url) => (
      /\/catalog\/chapters\/P_CH02\/scenes$/.test(url)
        ? Promise.resolve({ scene: { scene_id: "P_SC_new" } })
        : Promise.resolve({})
    ));
    WsCatalog.addScene("ch02", "新场景");
    const temp = WsCatalog.get()[1].scenes[1].sid;
    expect(temp.startsWith("tmp_ch02_")).toBe(true); // 临时 sid 不长成位置式旧 slug 的样子
    await vi.waitFor(() => expect(WsCatalog.get()[1].scenes[1].sid).toBe("P_SC_new"), T);
    expect(WsCatalog.sceneById(temp).scene.sid).toBe("P_SC_new");
  });

  it("一次性迁移：按位置式旧 sid 落地的本机键挪到稳定 sid 上（每部作品一次）", async () => {
    window.localStorage.setItem("wr-doc:ch01s2::prj-main", "<p>旧缓存</p>");
    window.localStorage.setItem("scn-run:ch02s1::prj-main", JSON.stringify({ state: "ready" }));
    window.localStorage.setItem("scn-queue:v1::prj-main", JSON.stringify(["ch02s1", "ch01s2"]));
    await loadCatalog();
    expect(window.localStorage.getItem("wr-doc:P_SC_b::prj-main")).toBe("<p>旧缓存</p>");
    expect(window.localStorage.getItem("wr-doc:ch01s2::prj-main")).toBeNull();
    expect(JSON.parse(window.localStorage.getItem("scn-run:P_SC_c::prj-main"))).toEqual({ state: "ready" });
    expect(JSON.parse(window.localStorage.getItem("scn-queue:v1::prj-main"))).toEqual(["P_SC_c", "P_SC_b"]);
    expect(window.localStorage.getItem("ws_sid_migrated_v1::prj-main")).toBeTruthy();
  });
});

describe("场景设计卡（写作台与 AI 起草台共用的同一张）", () => {
  it("反应场按 反应 / 两难 / 决定 命名三拍，后续三拍、坩埚、事实行、章摘要都在卡上", async () => {
    const { WsCatalog } = await loadCatalog();
    const { SceneDesignCard, sceneDesignModel } = await import("./ws-scene-design.jsx");
    const model = sceneDesignModel(WsCatalog.sceneById("P_SC_b"));
    expect(model.beats.map(b => b.label)).toEqual(["反应", "两难", "决定"]);
    expect(model.followup).toEqual([{ key: "goal", label: "目标", text: "翻进档案馆" }]);
    expect(model.stamp).toBe("CH 01 · SC 02");
    expect(model.lengthLabel).toBe("1300–1600 字");
    expect(model.facts).toEqual([
      { k: "POV", v: "林昭" }, { k: "时间", v: "第二日 · 晨" }, { k: "地点", v: "雨城旧码头" }, { k: "出场", v: "沈越" },
    ]);

    const onEditPlan = vi.fn();
    const host = await render(<SceneDesignCard model={model} onEditPlan={onEditPlan} onEditCard={vi.fn()} />);
    const text = host.textContent;
    ["停职通知明早生效", "接 · 目标", "信封里还有第二张车票。", "从回避转为追查", "你欠这座城一个交代。", "她被停职了。", "灾一", "章末"].forEach((part) => {
      expect(text).toContain(part);
    });
    expect(text).toContain("来自构思");
    expect(host.querySelector('[data-testid="scene-design-edit-card"]')).toBeNull();
    await click(host.querySelector('[data-testid="scene-design-edit-plan"]'));
    expect(onEditPlan).toHaveBeenCalledTimes(1);
  });

  it("手建的场：来自章节编排，给「编辑卡」；没填的拍子明写「待规划」；compact 只留三拍与事实行", async () => {
    const { WsCatalog } = await loadCatalog({
      catalog: [{ ...SNOW_CATALOG[0], scenes: [scene("P_SC_a", "ch01s1", { brief: { goal: "（本场目标待规划）", conflict: "", setback: "" } })] }],
    });
    const { SceneDesignCard, sceneDesignModel } = await import("./ws-scene-design.jsx");
    const model = sceneDesignModel(WsCatalog.sceneById("P_SC_a"));
    expect(model.beatsFilled).toBe(0);
    expect(model.origin).toBe("manual");
    const host = await render(<SceneDesignCard model={model} variant="compact" onEditPlan={vi.fn()} onEditCard={vi.fn()} />);
    expect(host.textContent).toContain("来自章节编排");
    expect(host.querySelectorAll(".sdc-beat-v.is-empty").length).toBe(3);
    expect(host.querySelector('[data-testid="scene-design-edit-plan"]')).toBeNull();
    expect(host.querySelector('[data-testid="scene-design-edit-card"]')).not.toBeNull();
    expect(host.querySelector(".sdc-chapter")).toBeNull();
  });

  it("正文上方那张（compact）可以收起：只留头部一行，偏好记在本机；整张的（抽屉 / 预检）没有收起", async () => {
    const { WsCatalog } = await loadCatalog();
    const { SceneDesignCard, sceneDesignModel } = await import("./ws-scene-design.jsx");
    const model = sceneDesignModel(WsCatalog.sceneById("P_SC_b"));
    const host = await render(<SceneDesignCard model={model} variant="compact" />);
    expect(host.querySelectorAll(".sdc-beat").length).toBeGreaterThan(0);
    await click(host.querySelector('[data-testid="scene-design-toggle"]'));
    expect(host.querySelectorAll(".sdc-beat").length).toBe(0);
    expect(host.textContent).toContain("来自构思");
    expect(window.localStorage.getItem("ws_scene_design_collapsed_v1")).toBe("1");
    // 偏好跨挂载保留
    const again = await render(<SceneDesignCard model={model} variant="compact" />);
    expect(again.querySelectorAll(".sdc-beat").length).toBe(0);
    const full = await render(<SceneDesignCard model={model} />);
    expect(full.querySelector('[data-testid="scene-design-toggle"]')).toBeNull();
    expect(full.querySelectorAll(".sdc-beat").length).toBeGreaterThan(0);
  });

  it("这张卡落后于已确认的构思：给一条提示和「同步这一场」", async () => {
    const { WsCatalog } = await loadCatalog();
    const { SceneDesignCard, sceneDesignModel } = await import("./ws-scene-design.jsx");
    const onSync = vi.fn();
    const host = await render(
      <SceneDesignCard model={sceneDesignModel(WsCatalog.sceneById("P_SC_b"))} sync={{ pending: true, busy: false, onSync }} />,
    );
    const banner = host.querySelector('[data-testid="scene-design-sync"]');
    expect(banner.textContent).toContain("构思里这一场已经更新");
    await click(banner.querySelector("button"));
    expect(onSync).toHaveBeenCalledTimes(1);
  });

  it("回构思第 10 步并对准这一场：与成稿中心同一组意图", async () => {
    const { planIntentsForScene } = await import("./ws-scene-design.jsx");
    expect(planIntentsForScene("P_SC_b")).toEqual([
      { type: "ws:snow-step", detail: "planning" }, { type: "ws:snow-scene", detail: "P_SC_b" },
    ]);
  });
});

describe("WsDesignSync（场景卡落后于已确认的构思了吗）", () => {
  const STATUS_URL = "/api/v2/projects/prj-main/snowflake-workspace/resync-status";

  async function loadSync(pendingScenes) {
    const { client } = await loadCatalog();
    const base = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (
      url === STATUS_URL ? Promise.resolve({ pending_count: pendingScenes.length, pending_scenes: pendingScenes }) : base(url)
    ));
    const { WsDesignSync } = await import("./ws-design-sync.jsx");
    return { WsDesignSync, client };
  }

  it("只认已确认的规划：还在改的草稿不算待同步，不在台子上打扰作者", async () => {
    const { WsDesignSync } = await loadSync([
      { scene_id: "P_SC_b", plan_status: "approved", changed_fields: ["writer_brief_json"], desk_edited: true },
      { scene_id: "P_SC_c", plan_status: "draft", changed_fields: ["scene_goal"] },
    ]);
    await WsDesignSync.refresh();
    expect(WsDesignSync.pendingFor("P_SC_b")).toEqual({ fields: ["writer_brief_json"], deskEdited: true });
    expect(WsDesignSync.pendingFor("P_SC_c")).toBeNull();
    expect(WsDesignSync.pendingCount()).toBe(1);
  });

  it("同步这一场：显式回流这一场 → 目录重拉 → 状态重拉", async () => {
    const { WsDesignSync, client } = await loadSync([{ scene_id: "P_SC_b", plan_status: "approved", changed_fields: [] }]);
    await WsDesignSync.refresh();
    const catalogCalls = () => client.apiGet.mock.calls.filter(([url]) => /\/catalog$/.test(url)).length;
    const before = catalogCalls();
    await WsDesignSync.syncScenes(["P_SC_b"]);
    expect(client.apiPost).toHaveBeenCalledWith("/api/v2/projects/prj-main/snowflake-workspace/resync", { scene_ids: ["P_SC_b"] });
    expect(catalogCalls()).toBeGreaterThan(before);
    expect(WsDesignSync.isBusy("P_SC_b")).toBe(false);
  });

  it("同步失败：告诉作者卡没有改动，忙态收回；非雪花作品（supported:false）之后不再追问", async () => {
    const { WsDesignSync, client } = await loadSync([]);
    client.apiPost.mockRejectedValueOnce(new Error("boom"));
    await WsDesignSync.syncScenes(["P_SC_b"]);
    expect(window.alert).toHaveBeenCalled();
    expect(WsDesignSync.isBusy("P_SC_b")).toBe(false);

    WsDesignSync.__reset();
    client.apiGet.mockImplementation((url) => (
      url === STATUS_URL ? Promise.resolve({ supported: false, pending_count: 0, pending_scenes: [] }) : Promise.resolve({})
    ));
    await WsDesignSync.refresh();
    const asked = () => client.apiGet.mock.calls.filter(([url]) => url === STATUS_URL).length;
    const once = asked();
    await WsDesignSync.refresh();
    expect(asked()).toBe(once);
  });
});
