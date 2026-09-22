// WsLibrary store 层单测：libFetch 关系双向索引 + LIB_persist diff→PATCH +
// relations CRUD（含 event 终点跳过）+ 失败 refetch/告警 + LIB_persistAdds 去重。
//
// 「store 实质」不在 ws-library.jsx（那是视图组件），而在：
//   · ws-library-data.jsx —— libFetch / LIB_ENTRIES / LIB_BY_ID / window.LIB_refetch
//   · ws-library-edit.jsx —— LIB_persist / libSyncLinks / LIB_persistAdds / LIB_newEntry
// 这两个模块级纯函数族就是被测契约面。
//
// 断言取向（对齐 ws-catalog.test 范式）：断「可观测结果」+「仅失败路径触发的 alert」。
// 写动词去重不可靠，故失败回滚断 alert + refetch；端点路由断精确 URL+body（可证伪）。
// installApiRouter 不识别 /library，故本 spec 自带 apiGet 路由。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

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
  useCatalogChapters: () => [
    { id: "ch01", backendId: "prj-main_CH01", n: 1, title: "雾港" },
    { id: "ch02", backendId: "prj-main_CH02", n: 2, title: "潮信" },
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
  // 先让 WsWorks 落到真实激活作品，再 import 数据层（其 import 期 libFetch 才会真正拉取）
  await import("./ws-works.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  const data = await import("./ws-library-data.jsx");
  const edit = await import("./ws-library-edit.jsx");
  await window.LIB_refetch();   // 等内联 IIFE 拉取 settle（resolve 在 LIB_ENTRIES 赋值之后）
  return { client, data, edit };
}

describe("WsLibrary 数据层（libFetch 关系双向索引）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
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
    expect(window.LIB_relationsRaw()).toEqual([]);
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
    expect(data.LIB_ENTRIES).toHaveLength(0);

    WsWorks.setActive("project-b");
    await vi.waitFor(() => expect(data.LIB_ENTRIES.map(e => e.name)).toEqual(["乙角色"]), T);

    projectA.resolve(namedLibrary("char-a", "甲角色"));
    await projectA.promise;
    await Promise.resolve();
    expect(data.LIB_ENTRIES.map(e => e.name)).toEqual(["乙角色"]);
    expect(data.LIB_BY_ID["char-a"]).toBeUndefined();
  });
});

describe("WsLibrary 视图与异步资料快照连通", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
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
    await vi.waitFor(() => expect(data.libLoadState().status).toBe("error"), T);
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
    window.localStorage.clear();
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
    const refetch = vi.spyOn(window, "LIB_refetch");
    // 保留 →zhou(避免删边)，新增 →e1(事件终点应被守卫跳过)
    edit.LIB_persist({ lin: { links: [
      { id: "zhou", type: "conflict", rel: "宿敌", relationId: "r1" },
      { id: "e1", type: "related", rel: "卷入" },
    ] } });
    await vi.waitFor(() => expect(refetch).toHaveBeenCalled(), T); // persist 全程跑完
    const postedTo = client.apiPost.mock.calls.map(c => c[1] && c[1].to_ref);
    // 可证伪：删掉 startsWith("event:") 守卫 → 出现 to_ref="event:e1" 的 POST
    expect(postedTo).not.toContain("event:e1");
  });

  it("PATCH 失败→alert 告警且 refetch 以服务端为准回滚", async () => {
    const { client, edit } = await loadLib();
    const refetch = vi.spyOn(window, "LIB_refetch");
    client.apiPatch.mockRejectedValueOnce(new Error("boom"));
    edit.LIB_persist({ lin: { name: "会失败" } });
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalled(), T); // 仅失败路径调 alert
    await vi.waitFor(() => expect(refetch).toHaveBeenCalled(), T);      // 回滚 = 重拉服务端
  });

  it("LIB_persistAdds 新建 people→POST characters，且同 id 不重复发", async () => {
    const { client, edit } = await loadLib();
    client.apiPost.mockClear();
    const ne = edit.LIB_newEntry("people", "新人物");
    edit.LIB_persistAdds([ne]);
    edit.LIB_persistAdds([ne]); // 第二次应被 libSentAdds 去重
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/library/characters",
      expect.objectContaining({ name: "新人物" })), T);
    const charPosts = client.apiPost.mock.calls.filter(c => /\/library\/characters$/.test(c[0]));
    expect(charPosts.length).toBe(1); // 去重可证伪
  });

  it("新建请求失败不会污染去重集合；同一条目可重试", async () => {
    const { client, edit } = await loadLib();
    client.apiPost.mockClear();
    client.apiPost.mockRejectedValueOnce(new Error("network down"));
    const ne = edit.LIB_newEntry("people", "可重试人物");

    expect(await edit.LIB_persistAdds([ne])).toBe(false);
    expect(window.alert).toHaveBeenCalled();
    client.apiPost.mockResolvedValueOnce({});
    expect(await edit.LIB_persistAdds([ne])).toBe(true);

    const posts = client.apiPost.mock.calls.filter(c => /\/library\/characters$/.test(c[0]));
    expect(posts).toHaveLength(2);
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
    window.localStorage.clear();
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

  it("旧本机覆盖层迁移：资料库没读到时不上行也不写完成标记，读到后才 PATCH 并记完成", async () => {
    const { client, edit } = await loadLib();
    window.localStorage.setItem("ws-lib-edits-v1::prj-main", JSON.stringify({ lin: { blurb: "旧的本机简述" } }));
    client.apiPatch.mockClear();
    client.apiGet.mockImplementation((url) => {
      if (/\/library$/.test(url)) return Promise.reject(new Error("offline"));
      return Promise.resolve({ items: [{ project_id: "prj-main", title: "北岸手记" }] });
    });
    vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(await edit.LIB_migrateLegacy()).toBe(false);
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(window.localStorage.getItem("ws-lib-migrated-v1::prj-main")).toBeNull();

    routeApiGet(client, libResponse());
    expect(await edit.LIB_migrateLegacy()).toBe(true);
    expect(client.apiPatch).toHaveBeenCalledWith(
      "/api/v2/projects/prj-main/library/characters/lin",
      expect.objectContaining({ details: expect.objectContaining({ blurb: "旧的本机简述" }) }),
    );
    expect(window.localStorage.getItem("ws-lib-migrated-v1::prj-main")).not.toBeNull();
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
    window.localStorage.clear();
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

  it("打开资料库时把旧版只存在本机的新建档案上行一次", async () => {
    window.localStorage.setItem("ws-lib-additions-v1::prj-main", JSON.stringify([
      { id: "u-old1", cat: "world", name: "旧本机地点", kind: "", summary: "", blurb: "", tags: [], facts: [], links: [] },
    ]));
    const view = await mountLibrary(libResponse());
    try {
      await vi.waitFor(() => expect(view.client.apiPost).toHaveBeenCalledWith(
        "/api/v2/projects/prj-main/library/entities",
        expect.objectContaining({ name: "旧本机地点" }),
      ), T);
      await vi.waitFor(() => expect(window.localStorage.getItem("ws-lib-migrated-v1::prj-main")).not.toBeNull(), T);
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
      // 大事记的所在章经目录解析成「第 N 章 · 标题」
      await click(view.host.querySelector('.lib2-item[data-lib-id="e1"]'));
      expect(view.host.textContent).toContain("2003");
      expect(view.host.textContent).toContain("第 2 章 · 潮信");
      expect(view.host.textContent).not.toContain("prj-main_CH02");
    } finally {
      await view.unmount();
    }
  });
});

describe("WsLibrary 视图：图谱与时间线", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
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
