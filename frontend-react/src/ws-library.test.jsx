// 资料库：store（ws-library-store.js）的读取 / 关系双向索引 / 写入（diff→PATCH、关系增删、失败回滚），
// 过渡期门面（ws-library-data.jsx / ws-library-edit.jsx）挂的 window 接缝，以及资料页视图。
//
// 断言取向（对齐 ws-catalog.test 范式）：断「可观测结果」+「仅失败路径触发的 alert」。
// 失败回滚断 alert + 以服务端为准重读（又发了一次 /library GET）；端点路由断精确 URL+body（可证伪）。
// installApiRouter 不识别 /library，故本 spec 自带 apiGet 路由。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

// 回收站已搬到 ws-trash.jsx（它的桩在 ws-trash.test.jsx）；资料库只用目录。
vi.mock("./ws-catalog.jsx", () => ({
  // 大事记的「所在章」按目录解析；资料库视图订阅目录
  WsCatalog: { get: () => [], subscribe: () => () => {} },
  // 目录适配层给的章号是补零的字符串（catalog_reader 的 "01"）
  useCatalogChapters: () => [
    { id: "ch01", backendId: "prj-main_CH01", n: "01", title: "雾港" },
    { id: "ch02", backendId: "prj-main_CH02", n: "02", title: "潮信" },
  ],
}));

const T = { timeout: 5000, interval: 25 };

// 默认资料库：林岑(people) —r1(conflict)→ 周岚(people)；档案馆(world)；第三潮汐(event)。
function libResponse() {
  return {
    characters: [
      { character_id: "lin", name: "林岑", role: "主角", summary: "修复师", ref: "character:lin", details: {} },
      { character_id: "zhou", name: "周岚", role: "对立", summary: "主任", ref: "character:zhou", details: {} },
    ],
    entities: [
      { entity_id: "arch", name: "档案馆", kind: "location", summary: "主场景", ref: "entity:arch", tags: [], details: {} },
    ],
    timeline: [
      { event_id: "e1", label: "第三潮汐事件", time_label: "2003", entity_refs: ["character:lin"], note: "事故" },
    ],
    relations: [
      { relation_id: "r1", from_ref: "character:lin", to_ref: "character:zhou", kind: "conflict", note: "宿敌" },
    ],
  };
}

function routeApiGet(client, lib) {
  client.apiGet.mockImplementation((url) => {
    if (url === "/api/v2/projects") return Promise.resolve({ items: [{ project_id: "prj-main", title: "北岸手记" }] });
    if (/\/api\/v2\/projects\/prj-main\/library$/.test(url)) return Promise.resolve(lib);
    return Promise.resolve({});
  });
  client.apiPost.mockResolvedValue({});
  client.apiPatch.mockResolvedValue({});
  client.apiDelete.mockResolvedValue({});
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function namedLibrary(id, name) {
  return {
    characters: [{ character_id: id, name, role: "主角", summary: "", ref: `character:${id}`, details: {} }],
    entities: [], timeline: [], relations: [],
  };
}

async function loadLib(lib = libResponse()) {
  const client = await import("./lib/client.js");
  routeApiGet(client, lib);
  // 先让 WsWorks 落到真实激活作品，再读资料库（store 只在有人要时才拉）
  const { WsWorks } = await import("./ws-works.jsx");
  await vi.waitFor(() => expect(WsWorks.activeId()).toBe("prj-main"), T);
  const data = await import("./ws-library-data.jsx");
  const edit = await import("./ws-library-edit.jsx");
  await data.libRefetch();
  return { client, data, edit };
}

const libraryGets = (client) => client.apiGet.mock.calls.filter(c => /\/library$/.test(c[0])).length;

describe("WsLibrary 数据层（libFetch 关系双向索引）", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("relation 双向挂载：lin 与 zhou 互为对方 links（带 relationId/type）", async () => {
    const { data } = await loadLib();
    const lin = data.LIB_BY_ID["lin"];
    const zhou = data.LIB_BY_ID["zhou"];
    expect(lin.links.find(l => l.id === "zhou")).toMatchObject({ id: "zhou", relationId: "r1", type: "conflict" });
    // 反向 backlink 必须存在（可证伪：libFetch 若只挂 from_ref 单向，则 zhou.links 找不到 lin）
    expect(zhou.links.find(l => l.id === "lin")).toMatchObject({ id: "lin", relationId: "r1" });
  });

  it("空 library 不抛、缓存清空", async () => {
    const { data } = await loadLib({ characters: [], entities: [], timeline: [], relations: [] });
    expect(data.LIB_ENTRIES.length).toBe(0);
    expect(data.libLive().entries).toEqual([]);
    expect(data.libLoadState().status).toBe("ready");
  });

  it("A→B 快速切换时立即隔离旧快照，且 A 的迟到响应不能覆盖 B", async () => {
    const client = await import("./lib/client.js");
    const projectA = deferred();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [
        { project_id: "project-a", title: "甲项目" },
        { project_id: "project-b", title: "乙项目" },
      ] });
      if (url === "/api/v2/projects/project-a/library") return projectA.promise;
      if (url === "/api/v2/projects/project-b/library") return Promise.resolve(namedLibrary("char-b", "乙角色"));
      return Promise.resolve({});
    });
    window.localStorage.setItem("ws_active_work_v1", "project-a");

    const { WsWorks } = await import("./ws-works.jsx");
    await vi.waitFor(() => expect(WsWorks.list().map(w => w.id)).toEqual(["project-a", "project-b"]), T);
    const data = await import("./ws-library-data.jsx");
    const off = data.libSubscribe(() => {});   // 像资料页一样挂上：开始读 A
    expect(data.LIB_ENTRIES).toHaveLength(0);
    expect(data.libLoadState()).toMatchObject({ pid: "project-a", status: "loading" });

    WsWorks.setActive("project-b");
    await vi.waitFor(() => expect(data.LIB_ENTRIES.map(e => e.name)).toEqual(["乙角色"]), T);

    projectA.resolve(namedLibrary("char-a", "甲角色"));
    await projectA.promise;
    await Promise.resolve();
    expect(data.LIB_ENTRIES.map(e => e.name)).toEqual(["乙角色"]);
    expect(data.LIB_BY_ID["char-a"]).toBeUndefined();
    expect(data.libLive().entries.map(e => e.name)).toEqual(["乙角色"]);
    off();
  });
});

/* store 本身：import 不拉数据，第一个用到的人才拉（写作台 / 章节编排直接 import 它，不必先开过「资料」页——审计 F05-01）；
   快照不可变，变了就换一份；门面只挂还有人读的几个窗口名 */
describe("资料库 store：按需拉取、不可变快照、窗口接缝", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  async function activeWork() {
    const client = await import("./lib/client.js");
    routeApiGet(client, libResponse());
    window.localStorage.setItem("ws_active_work_v1", "prj-main");
    const { WsWorks } = await import("./ws-works.jsx");
    await vi.waitFor(() => expect(WsWorks.activeId()).toBe("prj-main"), T);
    return client;
  }

  it("import 不拉；第一个订阅者才拉一次，读完快照换新、第二个订阅者不再拉", async () => {
    const client = await activeWork();
    const store = await import("./ws-library-store.js");
    // 基线取在 import 之后：上一个用例留下的旧模块实例在 import 新实例时才撤掉它的换作品监听
    const base = libraryGets(client);
    await act(async () => { await new Promise(r => setTimeout(r, 20)); });
    expect(libraryGets(client)).toBe(base);
    expect(store.libLive().entries).toEqual([]);
    const first = store.libLive();

    const seen = vi.fn();
    const off = store.libSubscribe(seen);
    await vi.waitFor(() => expect(store.libLoadState().status).toBe("ready"), T);
    expect(libraryGets(client)).toBe(base + 1);
    expect(seen).toHaveBeenCalled();
    const live = store.libLive();
    expect(live).not.toBe(first);
    expect(live.entries.map(e => e.name)).toEqual(["林岑", "周岚", "档案馆", "第三潮汐事件"]);
    expect(live.byId.zhou.name).toBe("周岚");
    expect(store.libLive()).toBe(live);        // 没变就是同一份，不每次复制

    const off2 = store.libSubscribe(() => {});
    await act(async () => { await Promise.resolve(); });
    expect(libraryGets(client)).toBe(base + 1);
    off(); off2();
  });

  it("useLibraryLive：冷启动挂上就拉，读到后组件拿到条目（写作台不必先开资料页）", async () => {
    await activeWork();
    const store = await import("./ws-library-store.js");
    function Names() {
      const live = store.useLibraryLive();
      return <span>{live.entries.map(e => e.name).join("、")}</span>;
    }
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    try {
      await act(async () => root.render(<Names />));
      await vi.waitFor(() => expect(host.textContent).toBe("林岑、周岚、档案馆、第三潮汐事件"), T);
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });

  it("没人用过就不因为换作品去拉；用过之后换作品跟着换", async () => {
    const client = await import("./lib/client.js");
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [
        { project_id: "project-a", title: "甲项目" }, { project_id: "project-b", title: "乙项目" },
      ] });
      if (url === "/api/v2/projects/project-a/library") return Promise.resolve(namedLibrary("char-a", "甲角色"));
      if (url === "/api/v2/projects/project-b/library") return Promise.resolve(namedLibrary("char-b", "乙角色"));
      return Promise.resolve({});
    });
    window.localStorage.setItem("ws_active_work_v1", "project-a");
    const { WsWorks } = await import("./ws-works.jsx");
    await vi.waitFor(() => expect(WsWorks.activeId()).toBe("project-a"), T);
    const store = await import("./ws-library-store.js");
    const base = libraryGets(client);

    WsWorks.setActive("project-b");
    await act(async () => { await new Promise(r => setTimeout(r, 20)); });
    expect(libraryGets(client)).toBe(base);

    await store.libEnsureLoaded();
    expect(store.libLive().entries.map(e => e.name)).toEqual(["乙角色"]);
    WsWorks.setActive("project-a");
    await vi.waitFor(() => expect(store.libLive().entries.map(e => e.name)).toEqual(["甲角色"]), T);
  });

  it("还没有能发请求的作品（书架还没读回来）：新建不发请求、说清楚为什么，不再拼出 /projects/null/…", async () => {
    const client = await import("./lib/client.js");
    const shelf = deferred();
    client.apiGet.mockImplementation((url) => (url === "/api/v2/projects" ? shelf.promise : Promise.resolve({})));
    client.apiPost.mockResolvedValue({ character_id: "c-new" });
    const { WsWorks } = await import("./ws-works.jsx");
    expect(WsWorks.readyId()).toBeNull();
    const store = await import("./ws-library-store.js");
    try {
      expect(await store.LIB_createEntry("people", "林岑")).toBeNull();
      expect(await store.LIB_createEntry("events", "第三潮汐事件")).toBeNull();
      expect(client.apiPost).not.toHaveBeenCalled();
      expect(window.alert).toHaveBeenCalledWith("作品还没打开好，稍后再试。");
    } finally {
      shelf.resolve({ items: [] });
    }
  });

  it("门面只挂还有人读的窗口名：LIB_ENTRIES / LIB_BY_ID / LIB_CATS（写作台、章节编排、冒烟）与 LIB_persist / LIB_live", async () => {
    await activeWork();
    const data = await import("./ws-library-data.jsx");
    await import("./ws-library-edit.jsx");
    await data.libRefetch();
    expect(window.LIB_ENTRIES).toBe(data.LIB_ENTRIES);
    expect(window.LIB_ENTRIES.map(e => e.name)).toContain("林岑");
    expect(window.LIB_BY_ID.lin.name).toBe("林岑");
    expect(window.LIB_CATS.map(c => c.id)).toEqual(["people", "world", "events"]);
    expect(window.LIB_live().byId.lin.name).toBe("林岑");
    expect(typeof window.LIB_persist).toBe("function");
    for (const gone of ["LIB_refetch", "LIB_relationsRaw", "LIB_subscribe", "LIB_snapshot", "LIB_loadEdits", "LIB_applyEdit",
      "LIB_loadAdds", "LIB_persistAdds", "LIB_newEntry", "LIB_seedOn", "DossierEdit", "DossierCreate"]) {
      expect(window[gone], gone).toBeUndefined();
    }
  });
});

describe("WsLibrary 视图与异步资料快照连通", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("组件先挂载、请求后完成时自动重算并显示服务端条目", async () => {
    const client = await import("./lib/client.js");
    const library = deferred();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [{ project_id: "prj-main", title: "北岸手记" }] });
      if (url === "/api/v2/projects/prj-main/library") return library.promise;
      return Promise.resolve({});
    });
    window.localStorage.setItem("ws_active_work_v1", "prj-main");

    const { WsWorks } = await import("./ws-works.jsx");
    await vi.waitFor(() => expect(WsWorks.activeId()).toBe("prj-main"), T);
    const { WsLibrary } = await import("./ws-library.jsx");
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    try {
      await act(async () => root.render(<WsLibrary go={vi.fn()} />));
      // 还在读：说在读，不先说「还是空的」、也不摆「新建第一份档案」
      expect(host.textContent).toContain("正在读取档案库");
      expect(host.textContent).not.toContain("这部作品的档案库还是空的");

      await act(async () => { library.resolve(libResponse()); await library.promise; });
      await vi.waitFor(() => expect(host.textContent).toContain("林岑"), T);
      expect(host.textContent).not.toContain("这部作品的档案库还是空的");
      expect(host.textContent).not.toContain("正在读取档案库");
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });

  it("读不到资料库：只有一处错误和重试，不冒充空档案库；重试成功后显示条目；真读到空才请作者新建", async () => {
    const client = await import("./lib/client.js");
    let mode = "fail";
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [{ project_id: "prj-main", title: "北岸手记" }] });
      if (url === "/api/v2/projects/prj-main/library") {
        if (mode === "fail") return Promise.reject(Object.assign(new Error("连接接口失败"), { code: "NETWORK_ERROR" }));
        return Promise.resolve(mode === "empty" ? { characters: [], entities: [], timeline: [], relations: [] } : libResponse());
      }
      return Promise.resolve({});
    });
    window.localStorage.setItem("ws_active_work_v1", "prj-main");

    const { WsWorks } = await import("./ws-works.jsx");
    await vi.waitFor(() => expect(WsWorks.activeId()).toBe("prj-main"), T);
    const data = await import("./ws-library-data.jsx");
    const { WsLibrary } = await import("./ws-library.jsx");
    vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(await data.libRefetch()).toBe(false);
    expect(data.libLoadState().status).toBe("error");
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    try {
      await act(async () => root.render(<WsLibrary go={vi.fn()} />));
      const notice = host.querySelector('[data-testid="library-load-error"]');
      expect(notice.getAttribute("role")).toBe("alert");
      expect(notice.textContent).toContain("连接接口失败");
      expect(notice.textContent).not.toContain("NETWORK_ERROR");
      expect(host.textContent).not.toContain("这部作品的档案库还是空的");
      expect(host.textContent).not.toContain("新建第一份档案");

      mode = "ok";
      const retry = [...notice.querySelectorAll("button")].find((b) => b.textContent === "重试");
      await act(async () => { retry.click(); });
      await vi.waitFor(() => expect(host.textContent).toContain("林岑"), T);
      expect(host.querySelector('[data-testid="library-load-error"]')).toBeNull();

      // 真读到了空库：这时才是「还是空的 · 新建第一份档案」
      mode = "empty";
      await act(async () => { await data.libRefetch(); });
      await vi.waitFor(() => expect(host.textContent).toContain("这部作品的档案库还是空的"), T);
      expect(host.textContent).toContain("新建第一份档案");
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });
});

describe("WsLibrary 编辑层（LIB_persist diff→PATCH + relations CRUD）", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("people 字段 patch 打到 characters 端点（kind→role + details.blurb）", async () => {
    const { client, edit } = await loadLib();
    client.apiPatch.mockClear();
    edit.LIB_persist({ lin: { name: "林岑·改", kind: "新角色", blurb: "新简述" } });
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/library/characters/lin",
      expect.objectContaining({
        name: "林岑·改",
        role: "新角色",
        details: expect.objectContaining({ blurb: "新简述" }),
      })), T);
  });

  it("events patch 走 timeline 端点（label/note，而非 name/role）", async () => {
    const { client, edit } = await loadLib();
    client.apiPatch.mockClear();
    edit.LIB_persist({ e1: { name: "事件改名", blurb: "新备注" } });
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/library/timeline/e1",
      { label: "事件改名", note: "新备注" }), T);
  });

  it("links 增边→POST relations；删旧边→DELETE relations/{relationId}", async () => {
    const { client, edit } = await loadLib();
    client.apiPost.mockClear();
    client.apiDelete.mockClear();
    // lin 原有 →zhou(r1)。新 links 仅含 →arch：应删 r1、增 lin→arch。
    edit.LIB_persist({ lin: { links: [{ id: "arch", type: "ally", rel: "工作于" }] } });
    await vi.waitFor(() => expect(client.apiDelete).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/library/relations/r1"), T);
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/library/relations",
      { from_ref: "character:lin", to_ref: "entity:arch", kind: "ally", note: "工作于" }), T);
  });

  it("event 作为 relation 终点被跳过（不产生 to_ref=event: 的 POST）", async () => {
    const { client, edit } = await loadLib();
    client.apiPost.mockClear();
    // 保留 →zhou(避免删边)，新增 →e1(事件终点应被守卫跳过)
    expect(await edit.LIB_persist({ lin: { links: [
      { id: "zhou", type: "conflict", rel: "宿敌", relationId: "r1" },
      { id: "e1", type: "related", rel: "卷入" },
    ] } })).toBe(true); // persist 全程跑完
    const postedTo = client.apiPost.mock.calls.map(c => c[1] && c[1].to_ref);
    // 可证伪：删掉 startsWith("event:") 守卫 → 出现 to_ref="event:e1" 的 POST
    expect(postedTo).not.toContain("event:e1");
  });

  it("PATCH 失败→alert 告警且以服务端为准重读回滚", async () => {
    const { client, edit } = await loadLib();
    const before = libraryGets(client);
    client.apiPatch.mockRejectedValueOnce(new Error("boom"));
    expect(await edit.LIB_persist({ lin: { name: "会失败" } })).toBe(false);
    expect(window.alert).toHaveBeenCalled();                                  // 仅失败路径调 alert
    await vi.waitFor(() => expect(libraryGets(client)).toBe(before + 1), T); // 回滚 = 重拉服务端
  });

  /* 审计 F05-02：同一份表单保存两次（中间服务端被别处改过，例如构思第 04 步给人物改了名），第二次必须照样发出去——
     以前按「这个会话上次发过的 patch」去重，第二次被静默跳过还报「已保存」，刷新后显示的是别处改的名字 */
  it("同一份 patch 再保存一次照样 PATCH（不按上次发过的内容去重）", async () => {
    const { client, edit } = await loadLib();
    client.apiPatch.mockClear();
    expect(await edit.LIB_persist({ lin: { name: "甲" } })).toBe(true);
    expect(await edit.LIB_persist({ lin: { name: "甲" } })).toBe(true);
    const patches = client.apiPatch.mock.calls.filter(c => /\/characters\/lin$/.test(c[0]));
    expect(patches).toHaveLength(2);
    expect(patches[1][1]).toEqual(expect.objectContaining({ name: "甲" }));
  });

  it("关系写入失败不会把整次编辑误标成功；相同 patch 可重试", async () => {
    const { client, edit } = await loadLib();
    client.apiPost.mockClear();
    client.apiPatch.mockClear();
    client.apiPost.mockRejectedValueOnce(new Error("relation unavailable"));
    const patch = { links: [
      { id: "zhou", type: "conflict", rel: "宿敌", relationId: "r1" },
      { id: "arch", type: "ally", rel: "工作于" },
    ] };

    expect(await edit.LIB_persist({ lin: patch })).toBe(false);
    expect(window.alert).toHaveBeenCalled();
    client.apiPost.mockResolvedValueOnce({});
    expect(await edit.LIB_persist({ lin: patch })).toBe(true);

    const relationPosts = client.apiPost.mock.calls.filter(c => /\/library\/relations$/.test(c[0]));
    expect(relationPosts).toHaveLength(2);
  });
});

describe("WsLibrary 编辑层：每个可编辑字段都真的写回后端", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("人物的标签与置顶写进 details（人物没有 tags 列）", async () => {
    const { client, edit } = await loadLib();
    client.apiPatch.mockClear();
    expect(await edit.LIB_persist({ lin: { tags: ["主线"], pinned: true } })).toBe(true);
    const call = client.apiPatch.mock.calls.find(c => /\/characters\/lin$/.test(c[0]));
    expect(call).toBeTruthy();
    expect(call[1].details).toEqual(expect.objectContaining({ tags: ["主线"], pinned: true }));
    expect(call[1]).not.toHaveProperty("tags");
  });

  it("世界的类型按中文名反查写回 entity.kind", async () => {
    const { client, edit } = await loadLib();
    client.apiPatch.mockClear();
    await edit.LIB_persist({ arch: { kind: "机构" } });
    expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/library/entities/arch",
      expect.objectContaining({ kind: "faction" }));
  });

  it("大事记的时间、所在章与相关档案写进 time_label / chapter_ref / entity_refs", async () => {
    const { client, edit } = await loadLib();
    client.apiPatch.mockClear();
    await edit.LIB_persist({ e1: { timeLabel: "开篇前三年", chapterRef: "prj-main_CH02", links: [{ id: "zhou" }, { id: "arch" }] } });
    expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/library/timeline/e1",
      { time_label: "开篇前三年", chapter_ref: "prj-main_CH02", entity_refs: ["character:zhou", "entity:arch"] });
  });

  it("改了已有关系的类型或标签：先删旧关系再建新关系（以前直接被跳过）", async () => {
    const { client, edit } = await loadLib();
    client.apiPost.mockClear();
    client.apiDelete.mockClear();
    expect(await edit.LIB_persist({ lin: { links: [{ id: "zhou", type: "ally", rel: "旧识", relationId: "r1" }] } })).toBe(true);
    expect(client.apiDelete).toHaveBeenCalledWith("/api/v2/projects/prj-main/library/relations/r1");
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/library/relations",
      { from_ref: "character:lin", to_ref: "character:zhou", kind: "ally", note: "旧识" });
  });

  it("关系没变时不发任何关系请求", async () => {
    const { client, edit } = await loadLib();
    client.apiPost.mockClear();
    client.apiDelete.mockClear();
    await edit.LIB_persist({ lin: { name: "林岑", links: [{ id: "zhou", type: "conflict", rel: "宿敌", relationId: "r1" }] } });
    expect(client.apiDelete).not.toHaveBeenCalled();
    expect(client.apiPost).not.toHaveBeenCalled();
  });

  it("没写标签的关系不把英文类型键当成标签显示", async () => {
    const lib = libResponse();
    lib.relations[0].note = "";
    const { data } = await loadLib(lib);
    expect(data.LIB_BY_ID.lin.links.find(l => l.id === "zhou")).toMatchObject({ rel: "", type: "conflict" });
  });

  it("新建先落后端，返回服务端 id 并刷新列表", async () => {
    const { client, edit } = await loadLib();
    client.apiPost.mockResolvedValueOnce({ entity_id: "ENT_NEW" });
    const id = await edit.LIB_createEntry("world", "钟楼", { kind: "location" });
    expect(id).toBe("ENT_NEW");
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/library/entities",
      expect.objectContaining({ name: "钟楼", kind: "location" }));
  });

  it("删除人物遇到「仍在使用」时说清原因并返回 false", async () => {
    const { client, edit, data } = await loadLib();
    client.apiDelete.mockRejectedValueOnce(Object.assign(new Error("character is still referenced"), {
      code: "LIBRARY_CHARACTER_IN_USE", details: { dependencies: { catalog_scenes: 2, snowflake_scenes: 1 } },
    }));
    expect(await edit.LIB_deleteEntry(data.LIB_BY_ID.lin)).toBe(false);
    expect(client.apiDelete).toHaveBeenCalledWith("/api/v2/projects/prj-main/library/characters/lin");
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("3 处"));
  });
});

function setField(el, value) {
  const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

async function mountLibrary(lib) {
  const client = await import("./lib/client.js");
  let current = lib;
  client.apiGet.mockImplementation((url) => {
    if (url === "/api/v2/projects") return Promise.resolve({ items: [{ project_id: "prj-main", title: "北岸手记" }] });
    if (url === "/api/v2/projects/prj-main/library") return Promise.resolve(current);
    return Promise.resolve({});
  });
  client.apiPost.mockResolvedValue({});
  client.apiPatch.mockResolvedValue({});
  client.apiDelete.mockResolvedValue({});
  window.localStorage.setItem("ws_active_work_v1", "prj-main");
  const { WsWorks } = await import("./ws-works.jsx");
  await vi.waitFor(() => expect(WsWorks.activeId()).toBe("prj-main"), T);
  const data = await import("./ws-library-data.jsx");
  await data.libRefetch();
  const { WsLibrary } = await import("./ws-library.jsx");
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => root.render(<WsLibrary go={vi.fn()} />));
  return {
    client, host,
    setLibrary: (next) => { current = next; },
    unmount: async () => { await act(async () => root.unmount()); host.remove(); },
  };
}

const click = async (el) => { await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); }); };
const buttonByText = (root, text) => Array.from(root.querySelectorAll("button")).find(b => b.textContent.trim().includes(text));

describe("WsLibrary 视图：新建、键盘与删除", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("新建一份档案：列表里只有一条，编辑器打开在服务端条目上", async () => {
    const view = await mountLibrary(libResponse());
    try {
      await click(buttonByText(view.host, "新建档案"));
      const nameInput = view.host.querySelector(".dcreate-name-input");
      expect(nameInput).toBeTruthy();
      await act(async () => setField(nameInput, "新来的邻居"));
      view.client.apiPost.mockResolvedValueOnce({ character_id: "CHAR_NEW" });
      const withNew = libResponse();
      withNew.characters.push({ character_id: "CHAR_NEW", name: "新来的邻居", role: "", summary: "", ref: "character:CHAR_NEW", details: {} });
      view.setLibrary(withNew);
      await click(buttonByText(view.host, "创建并编辑"));
      await vi.waitFor(() => expect(view.host.querySelector(".dform-name")).toBeTruthy(), T);
      expect(view.host.querySelector(".dform-name").value).toBe("新来的邻居");
      const rows = Array.from(view.host.querySelectorAll(".lib2-item")).filter(b => b.textContent.includes("新来的邻居"));
      expect(rows).toHaveLength(1);
      expect(rows[0].getAttribute("data-lib-id")).toBe("CHAR_NEW");
    } finally {
      await view.unmount();
    }
  });

  it("编辑表单里按 ↓ 不会跳到下一条、也不会丢掉正在写的内容", async () => {
    const view = await mountLibrary(libResponse());
    try {
      const first = view.host.querySelector('.lib2-item[data-lib-id="lin"]');
      await click(first);
      await click(buttonByText(view.host, "编辑档案"));
      const area = view.host.querySelector("textarea.dform-area");
      expect(area).toBeTruthy();
      await act(async () => setField(area, "写到一半"));
      await act(async () => {
        area.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
      });
      expect(view.host.querySelector("textarea.dform-area")).toBeTruthy();
      expect(view.host.querySelector("textarea.dform-area").value).toBe("写到一半");
      expect(view.host.querySelector(".dform-name").value).toBe("林岑");
    } finally {
      await view.unmount();
    }
  });

  it("焦点在列表条目上时 ↓ 翻到下一条", async () => {
    const view = await mountLibrary(libResponse());
    try {
      const first = view.host.querySelector(".lib2-item");
      await click(first);
      const firstId = first.getAttribute("data-lib-id");
      await act(async () => {
        first.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
      });
      const active = view.host.querySelector(".lib2-item.is-active");
      expect(active).toBeTruthy();
      expect(active.getAttribute("data-lib-id")).not.toBe(firstId);
    } finally {
      await view.unmount();
    }
  });

  it("搜索框里输入法组词时的 ↓ / Esc 属于输入法：不跳进列表、不清空搜索词", async () => {
    const view = await mountLibrary(libResponse());
    try {
      const search = view.host.querySelector(".lib2-search input");
      await act(async () => { search.focus(); setField(search, "林"); });
      const key = async (init) => {
        await act(async () => { search.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init })); });
        await act(async () => { await new Promise(r => setTimeout(r, 40)); });   // 等过跳焦点用的那一帧
      };
      await key({ key: "ArrowDown", isComposing: true, keyCode: 229 });
      expect(document.activeElement).toBe(search);
      await key({ key: "Escape", isComposing: true, keyCode: 229 });
      expect(search.value).toBe("林");
      // 组完词之后 ↓ 照常跳进列表（证明上面停在搜索框不是因为快捷键本身坏了）
      await key({ key: "ArrowDown" });
      expect(document.activeElement.classList.contains("lib2-item")).toBe(true);
    } finally {
      await view.unmount();
    }
  });

  /* 批准 #25（重评 R16）：7–8 月浏览器里的旧资料一次性上行删掉了——打开资料页不再读旧键、不往后端写，旧键原样留着 */
  it("打开资料库不再上行浏览器里的旧资料，旧键原样留着", async () => {
    window.localStorage.setItem("ws-lib-additions-v1::prj-main", JSON.stringify([
      { id: "u-old1", cat: "world", name: "旧本机地点", kind: "", summary: "", blurb: "", tags: [], facts: [], links: [] },
    ]));
    window.localStorage.setItem("ws-lib-edits-v1::prj-main", JSON.stringify({ lin: { blurb: "旧的本机简述" } }));
    const view = await mountLibrary(libResponse());
    try {
      await act(async () => { await new Promise(r => setTimeout(r, 30)); });
      expect(view.client.apiPost).not.toHaveBeenCalled();
      expect(view.client.apiPatch).not.toHaveBeenCalled();
      expect(window.localStorage.getItem("ws-lib-additions-v1::prj-main")).toContain("旧本机地点");
      expect(window.localStorage.getItem("ws-lib-edits-v1::prj-main")).toContain("旧的本机简述");
      expect(window.localStorage.getItem("ws-lib-migrated-v1::prj-main")).toBeNull();
    } finally {
      await view.unmount();
    }
  });

  it("从写作台点名字跳来（排队的 ws:lib-open 指令）：资料页一挂上就打开那份档案", async () => {
    const intents = await import("./ws-view-intents.js");
    intents.queueViewIntent("library", "ws:lib-open", "zhou");
    const view = await mountLibrary(libResponse());
    try {
      await vi.waitFor(() => expect(view.host.querySelector(".dossier-name")).toBeTruthy(), T);
      expect(view.host.querySelector(".dossier-name").textContent).toBe("周岚");
      // 挂着的时候再跳一次：换到另一份
      await act(async () => { window.dispatchEvent(new CustomEvent("ws:lib-open", { detail: "arch" })); });
      expect(view.host.querySelector(".dossier-name").textContent).toBe("档案馆");
    } finally {
      await view.unmount();
    }
  });

  it("删除真实条目：先确认，确认后调 DELETE 并回到总览", async () => {
    const view = await mountLibrary(libResponse());
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    try {
      await click(view.host.querySelector('.lib2-item[data-lib-id="arch"]'));
      await click(buttonByText(view.host, "删除"));
      await vi.waitFor(() => expect(view.client.apiDelete).toHaveBeenCalledWith("/api/v2/projects/prj-main/library/entities/arch"), T);
      expect(confirm).toHaveBeenCalled();
      await vi.waitFor(() => expect(view.host.querySelector('[data-screen-label="library-overview"]')).toBeTruthy(), T);
    } finally {
      await view.unmount();
    }
  });

  it("取消删除确认：不发 DELETE", async () => {
    const view = await mountLibrary(libResponse());
    vi.spyOn(window, "confirm").mockReturnValue(false);
    try {
      await click(view.host.querySelector('.lib2-item[data-lib-id="arch"]'));
      await click(buttonByText(view.host, "删除"));
      await act(async () => { await Promise.resolve(); });
      expect(view.client.apiDelete).not.toHaveBeenCalled();
    } finally {
      await view.unmount();
    }
  });

  it("总览不再显示假的就绪度 / 待你处理，而是还没写简述的档案", async () => {
    const lib = libResponse();
    lib.timeline[0].chapter_ref = "prj-main_CH02";
    const view = await mountLibrary(lib);
    try {
      const text = view.host.textContent;
      expect(text).not.toContain("就绪度");
      expect(text).not.toContain("待你处理");
      expect(text).toContain("还没写简述");
      // 大事记的所在章经目录解析成全站同一个叫法「第 N 章 · 标题」（不是「第 02 章」）
      await click(view.host.querySelector('.lib2-item[data-lib-id="e1"]'));
      expect(view.host.textContent).toContain("2003");
      expect(view.host.textContent).toContain("第 2 章 · 潮信");
      expect(view.host.textContent).not.toContain("第 02 章");
      expect(view.host.textContent).not.toContain("prj-main_CH02");
    } finally {
      await view.unmount();
    }
  });
});

describe("WsLibrary 视图：图谱与时间线", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  const radio = (root, text) => Array.from(root.querySelectorAll('[role="radio"]')).find(b => b.textContent.includes(text));
  const keydown = async (el, key) => {
    await act(async () => { el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })); });
  };

  it("没有任何关联时图谱给空态，不画一堆散点", async () => {
    const lib = libResponse();
    lib.relations = [];
    lib.timeline[0].entity_refs = [];
    const view = await mountLibrary(lib);
    try {
      await click(radio(view.host, "图谱"));
      expect(view.host.textContent).toContain("还没有关联");
      expect(view.host.querySelector(".graph-node")).toBeNull();
      await click(buttonByText(view.host, "去档案里添加关系"));
      expect(radio(view.host, "档案").getAttribute("aria-checked")).toBe("true");
    } finally {
      await view.unmount();
    }
  });

  it("图谱节点能用键盘走到：空格选中、回车打开档案", async () => {
    const view = await mountLibrary(libResponse());
    try {
      await click(radio(view.host, "图谱"));
      const nodes = Array.from(view.host.querySelectorAll(".graph-node"));
      expect(nodes).toHaveLength(4);
      nodes.forEach(n => expect(n.getAttribute("tabindex")).toBe("0"));
      const zhou = nodes.find(n => (n.getAttribute("aria-label") || "").startsWith("周岚"));
      await keydown(zhou, " ");
      expect(view.host.querySelector(".graph-panel").textContent).toContain("周岚");
      await keydown(zhou, "Enter");
      expect(radio(view.host, "档案").getAttribute("aria-checked")).toBe("true");
      expect(view.host.querySelector(".dossier-name").textContent).toBe("周岚");
    } finally {
      await view.unmount();
    }
  });

  it("选中节点的面板只数它自己的关联，键盘焦点移到别的节点时不跟着变", async () => {
    const lib = libResponse();
    lib.relations.push({ relation_id: "r2", from_ref: "character:lin", to_ref: "entity:arch", kind: "ally", note: "" });
    const view = await mountLibrary(lib);
    try {
      await click(radio(view.host, "图谱"));
      const nodes = Array.from(view.host.querySelectorAll(".graph-node"));
      const byName = (name) => nodes.find(n => (n.getAttribute("aria-label") || "").startsWith(name));
      await keydown(byName("周岚"), " ");
      const rel = () => view.host.querySelector(".graph-panel-rel").textContent.trim();
      expect(rel()).toBe("1 项关联");
      // 林岑有好几条关联；焦点移过去只影响高亮，面板说的仍是周岚
      await act(async () => { byName("林岑").dispatchEvent(new FocusEvent("focusin", { bubbles: true })); });
      expect(byName("林岑").classList.contains("is-lit")).toBe(true);
      expect(view.host.querySelector(".graph-panel").textContent).toContain("周岚");
      expect(rel()).toBe("1 项关联");
    } finally {
      await view.unmount();
    }
  });

  it("图例和筛选只列数据里出现过的类别与关系类型", async () => {
    const view = await mountLibrary(libResponse());
    try {
      await click(radio(view.host, "图谱"));
      const legend = view.host.querySelector(".graph-legend").textContent;
      expect(legend).toContain("人物");
      expect(legend).toContain("对立");
      expect(legend).not.toContain("同盟");
      await click(buttonByText(view.host, "筛选"));
      const pop = view.host.querySelector(".graph-pop");
      expect(pop).toBeTruthy();
      expect(pop.textContent).not.toContain("同盟");
      // 关掉「对立」这一类关系：那条边不画了，图例上划掉
      const conflict = Array.from(pop.querySelectorAll("label")).find(l => l.textContent.includes("对立"));
      await click(conflict.querySelector("input"));
      expect(view.host.querySelector(".graph-edge.rel-conflict")).toBeNull();
      const off = Array.from(view.host.querySelectorAll(".graph-legend-item.is-off")).map(li => li.textContent);
      expect(off).toContain("对立");
      // 弹层是共享的 Popover：Esc 收起并把焦点还给「筛选」按钮
      const filterBtn = buttonByText(view.host, "筛选");
      expect(filterBtn.getAttribute("aria-expanded")).toBe("true");
      await act(async () => {
        (document.activeElement || document.body).dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
      });
      expect(view.host.querySelector(".graph-pop")).toBeNull();
      expect(filterBtn.getAttribute("aria-expanded")).toBe("false");
      await act(async () => { await Promise.resolve(); });
      expect(document.activeElement).toBe(filterBtn);
    } finally {
      await view.unmount();
    }
  });

  it("时间线上的大事记按回车直接打开档案（不必双击）", async () => {
    const view = await mountLibrary(libResponse());
    try {
      await click(radio(view.host, "时间线"));
      const card = view.host.querySelector(".tl-event");
      expect(card.textContent).toContain("第三潮汐事件");
      await keydown(card, "Enter");
      expect(radio(view.host, "档案").getAttribute("aria-checked")).toBe("true");
      expect(view.host.querySelector(".dossier-name").textContent).toBe("第三潮汐事件");
    } finally {
      await view.unmount();
    }
  });
});
