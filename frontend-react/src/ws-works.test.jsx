// React 主线首个 store 层单测：WsWorks 乐观写 + 失败回滚。
// mock client 模块（比 mock fetch 干净——store 只消费 verb 返回的 promise）。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// vi.mock 被提升到 import 之上；factory 不得引用外部变量。
vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

// 在 mock 就位后再动态载入 store，拿到「全新」模块实例（module 级 WS_WORKS 复位）。
async function loadStore(items = [{ project_id: "p1", title: "Test Project", stats: {} }]) {
  const client = await import("./lib/client.js");
  client.apiGet.mockImplementation((url) => (
    url === "/api/v2/projects" ? Promise.resolve({ items }) : Promise.resolve({})
  ));
  client.apiPatch.mockResolvedValue({});
  client.apiPost.mockResolvedValue({});
  client.apiDelete.mockResolvedValue({});
  const mod = await import("./ws-works.jsx");
  await vi.waitFor(() => expect(mod.WsWorks.status().projects.phase).toBe("ready"));
  return { mod, client };
}

describe("WsWorks.update (optimistic write + rollback)", () => {
  beforeEach(() => {
    vi.resetModules(); // 每个用例拿到全新 module 级 WS_WORKS
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {}); // wsToastError -> alert
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("applies the optimistic profile change immediately", async () => {
    const { mod } = await loadStore();
    const { WsWorks } = mod;

    const before = WsWorks.list()[0];
    expect(before).toBeTruthy();

    WsWorks.update(before.id, { title: "新书名" });

    expect(WsWorks.list().find((w) => w.id === before.id).title).toBe("新书名");
  });

  it("rolls back to the prior value when the PATCH rejects", async () => {
    const { mod, client } = await loadStore();
    const { WsWorks } = mod;

    const before = WsWorks.list()[0];
    const originalTitle = before.title;

    client.apiPatch.mockRejectedValueOnce(new Error("boom"));

    WsWorks.update(before.id, { title: "会被回滚的标题" });

    // 乐观值同步可见
    expect(WsWorks.list().find((w) => w.id === before.id).title).toBe("会被回滚的标题");

    // 等待被拒的 apiPatch microtask + .catch 回滚
    await vi.waitFor(() => {
      expect(WsWorks.list().find((w) => w.id === before.id).title).toBe(originalTitle);
    });

    expect(client.apiPatch).toHaveBeenCalledWith(
      `/api/v2/projects/${before.id}/profile`,
      { title: "会被回滚的标题" }
    );
    expect(window.alert).toHaveBeenCalled(); // 失败时 wsToastError 触发
  });
});

describe("WsWorks 远端状态（内联失败与重试契约）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  it("后端确认空书架后清除加载占位，且不请求伪 dashboard", async () => {
    window.localStorage.setItem("ws_active_work_v1", "stale-project");
    const client = await import("./lib/client.js");
    client.apiGet.mockResolvedValue({ items: [] });

    const { WsWorks } = await import("./ws-works.jsx");

    await vi.waitFor(() => expect(WsWorks.status().projects.phase).toBe("ready"));
    expect(WsWorks.list()).toEqual([]);
    expect(WsWorks.activeId()).toBe("");
    expect(WsWorks.active()).toMatchObject({ id: "", title: "还没有作品" });
    expect(window.localStorage.getItem("ws_active_work_v1")).toBeNull();
    expect(client.apiGet).toHaveBeenCalledTimes(1);
    expect(client.apiGet).toHaveBeenCalledWith("/api/v2/projects");
  });

  it("清除可丢弃的作品缓存后仍恢复用户选中的作品", async () => {
    window.localStorage.setItem("ws_active_work_v1", "p2");
    const client = await import("./lib/client.js");
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") {
        return Promise.resolve({
          items: [
            { project_id: "p1", title: "First", stats: {} },
            { project_id: "p2", title: "Selected", stats: {} },
          ],
        });
      }
      return Promise.resolve({});
    });

    const { WsWorks } = await import("./ws-works.jsx");

    await vi.waitFor(() => expect(WsWorks.status().projects.phase).toBe("ready"));
    expect(WsWorks.activeId()).toBe("p2");
    expect(WsWorks.active().title).toBe("Selected");
  });

  it("作品列表断网时保留缓存并暴露可重试 error，成功后收敛 ready", async () => {
    const client = await import("./lib/client.js");
    const offline = Object.assign(new Error("无法连接作品服务"), { code: "NETWORK_ERROR" });
    client.apiGet.mockRejectedValueOnce(offline);
    const { WsWorks } = await import("./ws-works.jsx");

    await vi.waitFor(() => expect(WsWorks.status().projects.phase).toBe("error"));
    expect(WsWorks.status().projects.error).toMatchObject({ code: "NETWORK_ERROR", message: "无法连接作品服务" });
    expect(WsWorks.list().length).toBeGreaterThan(0); // 本地缓存 / 加载影子仍可渲染

    client.apiGet.mockResolvedValue({ items: [] });
    await WsWorks.retry("projects");
    expect(WsWorks.status().projects).toMatchObject({ phase: "ready", error: null });
  });

  it("dashboard 失败单独标记，不把项目列表误报失败；retry 只重拉当前主页", async () => {
    const client = await import("./lib/client.js");
    const project = {
      project_id: "p1", title: "离线书稿", genre: "悬疑", mark: "离", accent: "sage",
      stats: { words_total: 12, words_today: 3, streak_days: 1 },
    };
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [project] });
      if (url === "/api/v2/projects/p1/dashboard") return Promise.reject(new Error("dashboard timeout"));
      return Promise.resolve({});
    });
    const { WsWorks } = await import("./ws-works.jsx");
    await vi.waitFor(() => expect(WsWorks.status("p1").dashboard.phase).toBe("error"));
    expect(WsWorks.status("p1").projects.phase).toBe("ready");

    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects/p1/dashboard") return Promise.resolve({ stats: { words_total: 99 }, chapters_recent: [] });
      return Promise.resolve({ items: [project] });
    });
    await WsWorks.retry("dashboard", "p1");
    expect(WsWorks.status("p1").dashboard.phase).toBe("ready");
    expect(WsWorks.active().wordsTotal).toBe(99);
  });

  it("同一部作品的 dashboard 在途时，再来的装载并进这一次（启动时列表装载 + 主页挂载不再各打一个 GET）", async () => {
    const client = await import("./lib/client.js");
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v2/projects") return Promise.resolve({ items: [{ project_id: "p1", title: "书", stats: {} }] });
      if (url === "/api/v2/projects/p1/dashboard") return gate.then(() => ({ stats: { words_total: 7 }, chapters_recent: [] }));
      return Promise.resolve({});
    });
    const { WsWorks } = await import("./ws-works.jsx");
    await vi.waitFor(() => expect(WsWorks.status("p1").dashboard.phase).toBe("loading"));
    const dashboardGets = () => client.apiGet.mock.calls.filter(([url]) => url === "/api/v2/projects/p1/dashboard").length;
    expect(dashboardGets()).toBe(1);

    const again = WsWorks.retry("dashboard", "p1");
    const third = WsWorks.retry("dashboard", "p1");
    expect(dashboardGets()).toBe(1);
    release();
    await Promise.all([again, third]);
    expect(WsWorks.status("p1").dashboard.phase).toBe("ready");
    expect(WsWorks.active().wordsTotal).toBe(7);

    // 落定之后再要就是新的一次
    await WsWorks.retry("dashboard", "p1");
    expect(dashboardGets()).toBe(2);
  });

  it("离线缓存不会复活已退役的演示作品", async () => {
    window.localStorage.setItem("ws_active_work_v1", "tide");
    window.localStorage.setItem("ws_works_cache_v1", JSON.stringify([
      { id: "tide", title: "退役演示一" },
      { id: "salt", title: "退役演示二" },
      { id: "project-real", title: "作者作品", home: { blank: true } },
    ]));
    const client = await import("./lib/client.js");
    client.apiGet.mockRejectedValue(new Error("offline"));

    const { WsWorks } = await import("./ws-works.jsx");

    await vi.waitFor(() => expect(WsWorks.status().projects.phase).toBe("error"));
    expect(WsWorks.list().map((work) => work.id)).toEqual(["project-real"]);
    expect(WsWorks.activeId()).toBe("project-real");
  });
});

describe("ws:work-changed 只表示「换了作品 / 书架成员变了」（统计回写不再广播它）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  function recordEvents() {
    const seen = [];
    const onChanged = (event) => seen.push(["ws:work-changed", event.detail]);
    const onStats = (event) => seen.push(["ws:work-stats-changed", event.detail]);
    window.addEventListener("ws:work-changed", onChanged);
    window.addEventListener("ws:work-stats-changed", onStats);
    return { seen, stop: () => { window.removeEventListener("ws:work-changed", onChanged); window.removeEventListener("ws:work-stats-changed", onStats); } };
  }

  it("作品 id 第一次从加载占位落定时照旧广播 ws:work-changed", async () => {
    const rec = recordEvents();
    try {
      await loadStore();
      expect(rec.seen).toContainEqual(["ws:work-changed", "p1"]);
    } finally { rec.stop(); }
  });

  it("字数统计回写与档案修改只发 ws:work-stats-changed；切换作品才发 ws:work-changed", async () => {
    const { mod } = await loadStore([
      { project_id: "p1", title: "First", stats: {} },
      { project_id: "p2", title: "Second", stats: {} },
    ]);
    const { WsWorks } = mod;
    // 等启动时的 dashboard 装载也落定，再开始记
    await vi.waitFor(() => expect(WsWorks.status("p1").dashboard.phase).toBe("ready"));
    const rec = recordEvents();
    try {
      WsWorks.__applyDerived("p1", { wordsToday: 4321 });
      WsWorks.update("p1", { title: "改过的书名" });
      expect(rec.seen.map(([type]) => type)).toEqual(["ws:work-stats-changed", "ws:work-stats-changed"]);
      expect(WsWorks.active().wordsToday).toBe(4321);

      WsWorks.setActive("p2");
      expect(rec.seen).toContainEqual(["ws:work-changed", "p2"]);
    } finally { rec.stop(); }
  });

  it("删掉另一部作品（书架成员变化）也广播 ws:work-changed", async () => {
    const { mod } = await loadStore([
      { project_id: "p1", title: "First", stats: {} },
      { project_id: "p2", title: "Second", stats: {} },
    ]);
    await vi.waitFor(() => expect(mod.WsWorks.status("p1").dashboard.phase).toBe("ready"));
    const rec = recordEvents();
    try {
      mod.WsWorks.remove("p2");
      expect(rec.seen).toEqual([["ws:work-changed", "p1"]]);
    } finally { rec.stop(); }
  });

  it("useActiveWorkIdentity 的快照只在身份字段变化时换引用", async () => {
    const { mod } = await loadStore();
    const React = (await import("react")).default;
    const { act } = await import("react");
    const { createRoot } = await import("react-dom/client");
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const renders = [];
    function Probe() {
      const identity = mod.useActiveWorkIdentity();
      renders.push(identity);
      return <span>{identity.title}</span>;
    }
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    try {
      await act(async () => root.render(<Probe />));
      const before = renders.length;
      await act(async () => { mod.WsWorks.__applyDerived("p1", { wordsTotal: 999, wordsToday: 12 }); });
      expect(renders.length).toBe(before);
      await act(async () => { mod.WsWorks.update("p1", { title: "新名字" }); });
      expect(renders.length).toBe(before + 1);
      expect(host.textContent).toBe("新名字");
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });
});
