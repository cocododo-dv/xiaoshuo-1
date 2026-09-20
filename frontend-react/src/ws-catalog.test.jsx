// WsCatalog / WsTrashStore store 层单测：乐观写穿 + 失败回滚/告警。
// 与 ws-works.test.jsx 同款：mock lib/client.js，按 URL 路由喂确定性后端数据。
//
// 断言取向：只断「可观测结果」+「非去重的写动词调用」。store 的 catFetch/trashFetch 带
// in-flight 去重（并发时复用同一 promise、不再发请求），所以失败兜底是否「又发了一次 apiGet」
// 不可靠地依赖时序——改为断言回滚后的最终状态（标题被服务端原值覆盖）与 alert 触发，
// 它们对去重免疫且仍可证伪（破坏 catRecover 即转红）。所有 waitFor 给足超时以耐 CI 负载。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter, DEFAULT_CHAP, DEFAULT_PROJECT, DEFAULT_TRASH } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

// ws-catalog 的依赖链会经 ws-snow-sync.jsx 拉到 ws-snow.jsx，而后者只被取用
// S2_BE_STEPS（FE↔BE 步骤映射）。mock 成空，避免为测 store 契约而拉入整张雪花视图模块。
vi.mock("./ws-snow.jsx", () => ({ S2_BE_STEPS: [] }));

const T = { timeout: 5000, interval: 25 };

// 等 active 从 __loading__ 翻成真实作品 id（写穿路径都依赖它确定）。
async function settleActive() {
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
}

async function loadCatalog(opts) {
  const client = await import("./lib/client.js");
  installApiRouter(client, opts);
  const mod = await import("./ws-catalog.jsx");
  await settleActive();
  await vi.waitFor(() => expect(mod.WsCatalog.get().length).toBeGreaterThan(0), T);
  return { mod, client };
}

describe("WsCatalog（目录乐观写 + 失败回滚）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("冷启动未完成时中央写闸拒绝任何目录覆盖", async () => {
    const client = await import("./lib/client.js");
    let resolveCatalog;
    const pendingCatalog = new Promise((resolve) => { resolveCatalog = resolve; });
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [DEFAULT_PROJECT] });
      if (url === "/api/v2/projects/prj-main/catalog") return pendingCatalog;
      if (url.includes("/writing-stats")) return Promise.resolve({ words_total: 0, words_today: 0, streak_days: 0 });
      return Promise.resolve({});
    });
    client.apiPost.mockResolvedValue({});
    client.apiPatch.mockResolvedValue({});
    const mod = await import("./ws-catalog.jsx");
    await settleActive();

    expect(mod.WsCatalog.ready()).toBe(false);
    expect(mod.WsCatalog.get()).toBe(mod.WsCatalog.get());
    expect(mod.WsCatalog.set([])).toBe(false);
    expect(window.alert).toHaveBeenCalled();

    resolveCatalog({ chapters: [DEFAULT_CHAP] });
    await vi.waitFor(() => expect(mod.WsCatalog.ready()).toBe(true), T);
    expect(mod.WsCatalog.get()).toHaveLength(1);
  });

  it("renameScene 同步改缓存并 PATCH 到后端场景端点", async () => {
    const { mod, client } = await loadCatalog();
    const { WsCatalog } = mod;
    client.apiPatch.mockClear();

    WsCatalog.renameScene("ch01", "ch01s1", "新场景标题");

    // 乐观值同步可见（视图层零等待）
    expect(WsCatalog.sceneById("ch01s1").scene.title).toBe("新场景标题");

    // diff 引擎异步派发：只发变化字段，命中后端 scene_id（PATCH 不参与 fetch 去重）
    await vi.waitFor(() =>
      expect(client.apiPatch).toHaveBeenCalledWith(
        "/api/v2/projects/prj-main/catalog/scenes/s1",
        { title: "新场景标题" }
      ), T);
  });

  it("PATCH 失败时告警并以服务端为准回滚乐观改动", async () => {
    const { mod, client } = await loadCatalog();
    const { WsCatalog } = mod;
    client.apiPatch.mockRejectedValueOnce(new Error("boom"));

    WsCatalog.renameScene("ch01", "ch01s1", "会被回滚的标题");
    expect(WsCatalog.sceneById("ch01s1").scene.title).toBe("会被回滚的标题");

    // 失败 → catRecover：window.alert（仅此路径调用，强可证伪）
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalled(), T);
    // 且最终以服务端原值（"交班"）覆盖乐观值——回滚到位
    await vi.waitFor(() =>
      expect(WsCatalog.sceneById("ch01s1").scene.title).toBe("交班"), T);
  });

  it("目录刷新失败保留旧缓存，并暴露可重试错误而不是伪装成空目录", async () => {
    const { mod, client } = await loadCatalog();
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    const route = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (
      url === "/api/v2/projects/prj-main/catalog"
        ? Promise.reject(new Error("catalog offline"))
        : route(url)
    ));

    await mod.WsCatalog.__refresh();

    expect(mod.WsCatalog.get()).toHaveLength(1);
    expect(mod.WsCatalog.loadError()).toBeInstanceOf(Error);
    expect(mod.WsCatalog.loadError().message).toContain("catalog offline");
    expect(warning).toHaveBeenCalled();
  });

  it("设 povName 经 set() diff 只 PATCH pov_character_name（POV 设置入口契约）", async () => {
    const { mod, client } = await loadCatalog();
    const { WsCatalog } = mod;
    client.apiPatch.mockClear();

    const next = WsCatalog.get().map((c) =>
      c.id === "ch01"
        ? { ...c, scenes: c.scenes.map((s) => (s.sid === "ch01s1" ? { ...s, povName: "林深" } : s)) }
        : c);
    WsCatalog.set(next);

    // 乐观值同步可见
    expect(WsCatalog.sceneById("ch01s1").scene.povName).toBe("林深");
    // diff 只发 pov_character_name（后端按名 find-or-create），命中后端 scene_id
    await vi.waitFor(() =>
      expect(client.apiPatch).toHaveBeenCalledWith(
        "/api/v2/projects/prj-main/catalog/scenes/s1",
        { pov_character_name: "林深" }
      ), T);
  });

  it("removeChapters 把整批章压成一次 chapters/trash 调用（不是逐章打一枪）", async () => {
    const second = {
      ...DEFAULT_CHAP,
      slug: "ch02", chapter_id: "c2", no: "02", title: "第二章", current: false,
      scenes: [{ ...DEFAULT_CHAP.scenes[0], slug: "ch02s1", scene_id: "s2" }],
    };
    const third = {
      ...DEFAULT_CHAP,
      slug: "ch03", chapter_id: "c3", no: "03", title: "第三章", current: false,
      scenes: [{ ...DEFAULT_CHAP.scenes[0], slug: "ch03s1", scene_id: "s3" }],
    };
    const { mod, client } = await loadCatalog({ catalog: [DEFAULT_CHAP, second, third] });
    client.apiPost.mockClear();

    expect(mod.WsCatalog.removeChapters(["ch01", "ch03"])).toBe(true);

    // 乐观缓存同步只剩中间那章
    expect(mod.WsCatalog.get().map((c) => c.id)).toEqual(["ch02"]);
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/chapters/trash", { chapter_ids: ["c1", "c3"] },
    ), T);
    const trashCalls = client.apiPost.mock.calls.filter(([url]) => url === "/api/v1/chapters/trash");
    expect(trashCalls).toHaveLength(1);
    // 被删章下的场不再单独进场景桶（否则后端会以「章下已有单独回收的场景」挡下整章删除）
    expect(client.apiPost.mock.calls.some(([url]) => url === "/api/v1/scenes/trash")).toBe(false);
    // 纯删除不改存活章次序：不该再追发一次等价于现状的 chapter-order
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(client.apiPost.mock.calls.map(([url]) => url)).toEqual(["/api/v1/chapters/trash"]);
  });

  it("removeScenes 跨章批量删场只发一次 scenes/trash，带全部后端 scene_id", async () => {
    const twoScenes = {
      ...DEFAULT_CHAP,
      scenes: [
        DEFAULT_CHAP.scenes[0],
        { ...DEFAULT_CHAP.scenes[0], slug: "ch01s2", scene_id: "s2", title: "回潮" },
      ],
    };
    const { mod, client } = await loadCatalog({ catalog: [twoScenes] });
    client.apiPost.mockClear();

    expect(mod.WsCatalog.removeScenes(["ch01s1", "ch01s2"])).toBe(true);

    expect(mod.WsCatalog.get()[0].scenes).toHaveLength(0);
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/trash", { scene_ids: ["s1", "s2"] },
    ), T);
    expect(client.apiPost.mock.calls.filter(([url]) => url === "/api/v1/scenes/trash")).toHaveLength(1);
  });

  it("后端把删除放进 blocked 时告警并把被挡下的章放回目录（不假装删掉了）", async () => {
    const second = {
      ...DEFAULT_CHAP,
      slug: "ch02", chapter_id: "c2", no: "02", title: "已批准章", current: false, state: "approved",
      scenes: [{ ...DEFAULT_CHAP.scenes[0], slug: "ch02s1", scene_id: "s2" }],
    };
    const { mod, client } = await loadCatalog({ catalog: [DEFAULT_CHAP, second] });
    client.apiPost.mockImplementation((url) => (
      url === "/api/v1/chapters/trash"
        ? Promise.resolve({ processed: [], blocked: [{ chapter_id: "c2", code: "CHAPTER_APPROVED_LOCK", message: "章节已批准终稿" }] })
        : Promise.resolve({})
    ));

    mod.WsCatalog.removeChapters(["ch02"]);
    expect(mod.WsCatalog.get().map((c) => c.id)).toEqual(["ch01"]);   // 乐观值先行

    await vi.waitFor(() => expect(window.alert).toHaveBeenCalled(), T);
    expect(String(window.alert.mock.calls[0][0])).toContain("章节已批准终稿");
    // 以服务端为准重拉：被挡下的章回到目录，而不是留在界面上装作已删
    await vi.waitFor(() => expect(mod.WsCatalog.get().map((c) => c.id)).toEqual(["ch01", "ch02"]), T);
  });

  it("构思分出来的章：结构归属随目录到达；在台子上改名后，本机的雪花缓存接过服务端的章表（阶段 Z）", async () => {
    const planned = {
      ...DEFAULT_CHAP,
      origin: "snowflake",
      tension: null,
      structure: { owner: "plan", row_uid: "chrow_1", scene_range: { first: 6, last: 12 }, planned_scene_count: 7, title_auto: true },
      scenes: [{ ...DEFAULT_CHAP.scenes[0], design: { origin: "snowflake", owner: "plan", story_index: 6 } }],
    };
    const { mod, client } = await loadCatalog({ catalog: [planned] });
    const [chapter] = mod.WsCatalog.get();
    expect(chapter.structure).toEqual({ owner: "plan", rowUid: "chrow_1", sceneRange: { first: 6, last: 12 }, plannedSceneCount: 7, titleAuto: true });
    expect(chapter.tensionSet).toBe(false);        // 没设过张力：镜头和体检不拿 0.3 的默认值当事实
    expect(chapter.scenes[0].design.storyIndex).toBe(6);

    const adopt = vi.fn(async () => true);
    window.SnowSync = { adoptServerChapters: adopt };
    client.apiPatch.mockResolvedValue({ chapter: {}, changed: true, plan_title_synced: true });
    mod.WsCatalog.set(mod.WsCatalog.get().map((c) => ({ ...c, title: "旧案重开" })));
    await vi.waitFor(() => expect(adopt).toHaveBeenCalledWith("prj-main"), T);
    expect(client.apiPatch).toHaveBeenCalledWith("/api/v2/projects/prj-main/catalog/chapters/c1", { title: "旧案重开" });

    // 后端没说写穿（手建的章、值没变）就不去动雪花缓存
    adopt.mockClear();
    client.apiPatch.mockResolvedValue({ chapter: {}, changed: true, plan_title_synced: false });
    mod.WsCatalog.set(mod.WsCatalog.get().map((c) => ({ ...c, promise: "读者知道旧信是谁寄的" })));
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith("/api/v2/projects/prj-main/catalog/chapters/c1", { promise: "读者知道旧信是谁寄的" }), T);
    expect(adopt).not.toHaveBeenCalled();
    delete window.SnowSync;
  });

  it("手建的章（旧载荷没有 structure）归台面：可拖、可改，章名不写穿", async () => {
    const { mod } = await loadCatalog();
    expect(mod.WsCatalog.get()[0].structure).toEqual({ owner: "desk", rowUid: "", sceneRange: null, plannedSceneCount: 0, titleAuto: false });
    expect(mod.WsCatalog.get()[0].tensionSet).toBe(true);
  });

  it("章节拖拽顺序通过完整真实 ID 集合持久化", async () => {
    const second = {
      ...DEFAULT_CHAP,
      slug: "ch02",
      chapter_id: "c2",
      no: "02",
      title: "第二章",
      current: false,
      scenes: [{ ...DEFAULT_CHAP.scenes[0], slug: "ch02s1", scene_id: "s2" }],
    };
    const { mod, client } = await loadCatalog({ catalog: [DEFAULT_CHAP, second] });
    client.apiPost.mockClear();

    const [first, next] = mod.WsCatalog.get();
    mod.WsCatalog.set([next, first]);

    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapter-order",
      { chapter_ids: ["c2", "c1"] },
    ), T);
  });
});

describe("WsTrashStore（回收站乐观恢复 + 失败告警）", () => {
  async function loadTrash(trash = [DEFAULT_TRASH]) {
    const client = await import("./lib/client.js");
    installApiRouter(client, { trash });
    const mod = await import("./ws-catalog.jsx");
    await settleActive();
    await vi.waitFor(() => expect(mod.WsTrashStore.list().length).toBeGreaterThan(0), T);
    return { mod, client };
  }

  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("restore 调用后端 restore 端点（id 经 encodeURIComponent）", async () => {
    const { mod, client } = await loadTrash();
    client.apiPost.mockClear();

    const ok = mod.WsTrashStore.restore("scene:s9");
    expect(ok).toBe(true); // 乐观返回

    await vi.waitFor(() =>
      expect(client.apiPost).toHaveBeenCalledWith("/api/v2/trash/scene%3As9/restore", {}), T);
  });

  it("restore 失败时告警", async () => {
    const { mod, client } = await loadTrash();
    client.apiPost.mockRejectedValueOnce(new Error("restore failed"));

    mod.WsTrashStore.restore("scene:s9");

    // 失败兜底：restore().catch 调 window.alert（强可证伪）
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalled(), T);
  });

  it("clear 子项优先清理，部分失败时返回 false 并明确告警", async () => {
    const chapterTrash = {
      ...DEFAULT_TRASH,
      id: "chapter:c9",
      kind: "chapter",
      title: "被删的章节",
    };
    const workTrash = {
      ...DEFAULT_TRASH,
      id: "work:p9",
      kind: "work",
      title: "被删的作品",
    };
    const { mod, client } = await loadTrash([workTrash, chapterTrash, DEFAULT_TRASH]);
    client.apiDelete.mockImplementation((url) => (
      url.includes("chapter%3Ac9") ? Promise.reject(new Error("chapter busy")) : Promise.resolve({})
    ));

    await expect(mod.WsTrashStore.clear()).resolves.toBe(false);

    expect(client.apiDelete.mock.calls.map(([url]) => url)).toEqual([
      "/api/v2/trash/scene%3As9",
      "/api/v2/trash/chapter%3Ac9",
      "/api/v2/trash/work%3Ap9",
    ]);
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("1 条仍需重试"));
  });
});

