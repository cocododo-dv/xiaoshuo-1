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

  it("addChapter 是唯一的新建配方：不带张力 / 线索 / 占位 / 4000 字目标，接在指定章后面，只建一场空白场", async () => {
    const second = { ...DEFAULT_CHAP, slug: "ch02", chapter_id: "c2", no: "02", title: "第二章", current: false, act: "act2",
      scenes: [{ ...DEFAULT_CHAP.scenes[0], slug: "ch02s1", scene_id: "s2" }] };
    const { mod, client } = await loadCatalog({ catalog: [DEFAULT_CHAP, second] });
    client.apiPost.mockImplementation((url) => (
      url.endsWith("/catalog/chapters")
        ? Promise.resolve({ chapter: { chapter_id: "c-new" } })
        : Promise.resolve({ scene: { scene_id: "s-new" } })
    ));

    const created = mod.WsCatalog.addChapter({ afterId: "ch01" });
    // 乐观缓存：接在第一章后面、沿用它的卷，id 不占用后面那一章现在的位置式 slug
    expect(mod.WsCatalog.get().map((c) => c.id)).toEqual(["ch01", created.id, "ch02"]);
    expect(created.id).not.toBe("ch02");
    expect(created).toMatchObject({ act: "act1", state: "planned", current: false, words: { cur: 0, target: 0 } });
    expect(created.scenes).toHaveLength(1);
    expect(created.scenes[0]).toMatchObject({ goal: "", obstacle: "", turn: "" });

    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => url.endsWith("/catalog/chapters"))).toBe(true), T);
    const body = client.apiPost.mock.calls.find(([url]) => url.endsWith("/catalog/chapters"))[1];
    for (const key of ["tension", "threads", "time_label", "place", "entry", "exit", "align", "promise", "pov"]) {
      expect(body, key).not.toHaveProperty(key);
    }
    expect(body).toMatchObject({ act: "act1", words_target: null, with_scene: false });
    expect(Object.values(body.drama).every((v) => v === "")).toBe(true);
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapter-order", { chapter_ids: ["c1", "c-new", "c2"] },
    ), T);
  });

  it("addScene 与 addChapter 同一份配方：新场的三拍是空的，不写占位目标", async () => {
    const { mod, client } = await loadCatalog();
    client.apiPost.mockImplementation((url) => (
      /\/catalog\/chapters\/c1\/scenes$/.test(url)
        ? Promise.resolve({ scene: { scene_id: "s-new" } })
        : Promise.resolve({})
    ));

    mod.WsCatalog.addScene("ch01", "新的一场");
    const added = mod.WsCatalog.get()[0].scenes.slice(-1)[0];
    expect(added).toMatchObject({ title: "新的一场", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" });

    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => /\/catalog\/chapters\/c1\/scenes$/.test(url))).toBe(true), T);
    const body = client.apiPost.mock.calls.find(([url]) => /\/catalog\/chapters\/c1\/scenes$/.test(url))[1];
    // 可证伪：配方里再写回「（本场目标待规划）」，goal 就不是空串
    expect(body).toMatchObject({ title: "新的一场", kind: "proactive", brief: { goal: "", conflict: "", setback: "" } });
  });

  it("addChapter：只给卷就接在那一卷最后；写作台的无参调用接在全书最后；已批准终稿之前不插", async () => {
    const approved = { ...DEFAULT_CHAP, slug: "ch02", chapter_id: "c2", no: "02", title: "定稿章", current: false, state: "approved", act: "act1",
      scenes: [{ ...DEFAULT_CHAP.scenes[0], slug: "ch02s1", scene_id: "s2" }] };
    const third = { ...DEFAULT_CHAP, slug: "ch03", chapter_id: "c3", no: "03", title: "第三章", current: false, act: "act2",
      scenes: [{ ...DEFAULT_CHAP.scenes[0], slug: "ch03s1", scene_id: "s3" }] };
    const { mod, client } = await loadCatalog({ catalog: [DEFAULT_CHAP, approved, third] });
    let made = 0;
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/catalog/chapters")) { made += 1; return Promise.resolve({ chapter: { chapter_id: `c-new-${made}` } }); }
      return Promise.resolve({ scene: { scene_id: `s-new-${made}` } });
    });
    const inAct = mod.WsCatalog.addChapter({ act: "act1" });
    expect(mod.WsCatalog.get().map((c) => c.id).indexOf(inAct.id)).toBe(2);   // 卷一最后（在定稿章之后）
    const blocked = mod.WsCatalog.addChapter({ afterId: "ch01" });
    // 插在第一章后面会把已批准终稿往后挤：最早只能接在定稿章之后
    expect(mod.WsCatalog.get().map((c) => c.id).indexOf(blocked.id)).toBeGreaterThan(1);
    const tail = mod.WsCatalog.addChapter();
    const ids = mod.WsCatalog.get().map((c) => c.id);
    expect(ids[ids.length - 1]).toBe(tail.id);
    expect(tail.act).toBe("act2");
    // 三次新建都落到后端，且每一次提交的章序里定稿章都还在第 2 位（后端对挪动定稿章的章序一律 409）
    await vi.waitFor(() => expect(client.apiPost.mock.calls.filter(([url]) => url.endsWith("/chapter-order"))).toHaveLength(3), T);
    client.apiPost.mock.calls.filter(([url]) => url.endsWith("/chapter-order"))
      .forEach(([, body]) => expect(body.chapter_ids[1]).toBe("c2"));
    expect(window.alert).not.toHaveBeenCalled();
  });

  it("addChapter：书尾的位置式 id 删章后被重用时，新章建好之前的改名等它建好、发给它自己，不发给回收站里的旧章", async () => {
    let server = [DEFAULT_CHAP];
    const { mod, client } = await loadCatalog();
    const route = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === "/api/v2/projects/prj-main/catalog" ? Promise.resolve({ chapters: server }) : route(url)));
    let made = 0;
    let hold = null;
    client.apiPost.mockImplementation((url, body) => {
      if (url.endsWith("/catalog/chapters")) {
        made += 1;
        const chapterId = `c-new-${made}`;
        const done = () => {
          server = [...server, { ...DEFAULT_CHAP, slug: `ch0${server.length + 1}`, chapter_id: chapterId, title: body.title, current: false, scenes: [] }];
          return { chapter: { chapter_id: chapterId } };
        };
        if (hold) return new Promise((resolve) => { hold.release = () => resolve(done()); });
        return Promise.resolve(done());
      }
      if (url.endsWith("/chapters/trash")) {
        server = server.filter((c) => !body.chapter_ids.includes(c.chapter_id));
        return Promise.resolve({ processed: body.chapter_ids, blocked: [] });
      }
      return Promise.resolve({ scene: { scene_id: `s-${made}` } });
    });

    // 书尾新建 → 位置式 ch02 → 后端 c-new-1；再删掉它
    expect(mod.WsCatalog.addChapter().id).toBe("ch02");
    await vi.waitFor(() => expect((mod.WsCatalog.get().find((c) => c.id === "ch02") || {}).backendId).toBe("c-new-1"), T);
    await new Promise((resolve) => setTimeout(resolve, 60));       // 让这一次写入收尾（章序 + 重拉）
    mod.WsCatalog.set(mod.WsCatalog.get().filter((c) => c.id !== "ch02"));
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => url.endsWith("/chapters/trash"))).toBe(true), T);
    await vi.waitFor(() => expect(mod.WsCatalog.get()).toHaveLength(1), T);
    await new Promise((resolve) => setTimeout(resolve, 60));

    // 再在书尾新建：又是 ch02，这一次后端还没回话，作者就改了名
    hold = {};
    expect(mod.WsCatalog.addChapter().id).toBe("ch02");
    client.apiPatch.mockClear();
    client.apiPatch.mockResolvedValue({ chapter: {}, changed: true });
    mod.WsCatalog.set(mod.WsCatalog.get().map((c) => (c.id === "ch02" ? { ...c, title: "改过的名字" } : c)));
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(client.apiPatch).not.toHaveBeenCalled();                // 在等新章建好，不先发给旧 id
    await vi.waitFor(() => expect(typeof hold.release).toBe("function"), T);
    hold.release();
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/catalog/chapters/c-new-2", { title: "改过的名字" },
    ), T);
    expect(client.apiPatch.mock.calls.some(([url]) => url.includes("/chapters/c-new-1"))).toBe(false);
  });

  it("focusScene：当前章还没铺场时从当前章往后找没写完的场（找到书尾再绕回），不落回前面写完的章", async () => {
    const scene = (slug, id, state) => ({ ...DEFAULT_CHAP.scenes[0], slug, scene_id: id, state });
    const done = { ...DEFAULT_CHAP, current: false, scenes: [scene("A1", "a1", "done"), scene("A2", "a2", "done")] };
    const empty = { ...DEFAULT_CHAP, slug: "ch02", chapter_id: "c2", no: "02", current: true, scenes: [] };
    const open = { ...DEFAULT_CHAP, slug: "ch03", chapter_id: "c3", no: "03", current: false, scenes: [scene("C1", "c1s", "done"), scene("C2", "c2s", "todo")] };
    const { mod } = await loadCatalog({ catalog: [done, empty, open] });
    expect(mod.WsCatalog.focusScene().scene.sid).toBe("C2");
  });

  it("focusScene：当前章在书尾且没铺场时绕回书头找，仍然跳过写完的章", async () => {
    const scene = (slug, id, state) => ({ ...DEFAULT_CHAP.scenes[0], slug, scene_id: id, state });
    const first = { ...DEFAULT_CHAP, current: false, scenes: [scene("A1", "a1", "done")] };
    const second = { ...DEFAULT_CHAP, slug: "ch02", chapter_id: "c2", no: "02", current: false, scenes: [scene("B1", "b1", "done"), scene("B2", "b2", "writing")] };
    const third = { ...DEFAULT_CHAP, slug: "ch03", chapter_id: "c3", no: "03", current: false, scenes: [scene("C1", "c1s", "done")] };
    const emptyLast = { ...DEFAULT_CHAP, slug: "ch04", chapter_id: "c4", no: "04", current: true, scenes: [] };
    const { mod } = await loadCatalog({ catalog: [first, second, third, emptyLast] });
    expect(mod.WsCatalog.focusScene().scene.sid).toBe("B2");
  });

  it("focusScene：全书都写完时停在当前章之前最近的那一场上", async () => {
    const scene = (slug, id) => ({ ...DEFAULT_CHAP.scenes[0], slug, scene_id: id, state: "done" });
    const first = { ...DEFAULT_CHAP, current: false, scenes: [scene("A1", "a1")] };
    const second = { ...DEFAULT_CHAP, slug: "ch02", chapter_id: "c2", no: "02", current: false, scenes: [scene("B1", "b1"), scene("B2", "b2")] };
    const empty = { ...DEFAULT_CHAP, slug: "ch03", chapter_id: "c3", no: "03", current: true, scenes: [] };
    const { mod } = await loadCatalog({ catalog: [first, second, empty] });
    expect(mod.WsCatalog.focusScene().scene.sid).toBe("B2");
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

  it("读取状态：拉取失败是 error（不是「空」）；重试成功后是 ready，空列表才是真的空", async () => {
    const client = await import("./lib/client.js");
    installApiRouter(client, { trash: [] });
    const route = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (
      url.startsWith("/api/v2/trash") ? Promise.reject(new Error("无法连接后端。")) : route(url)));
    const mod = await import("./ws-catalog.jsx");
    await settleActive();
    await vi.waitFor(() => expect(mod.WsTrashStore.loadState().status).toBe("error"), T);
    expect(mod.WsTrashStore.loadState().message).toBe("无法连接后端。");
    expect(mod.WsTrashStore.list()).toEqual([]);

    client.apiGet.mockImplementation(route);
    const again = mod.WsTrashStore.refresh();
    expect(mod.WsTrashStore.loadState().status).toBe("loading");
    await again;
    expect(mod.WsTrashStore.loadState()).toEqual({ status: "ready", message: "" });
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

