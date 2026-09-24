// 风格参考 store（ws-styleref-store.js）：每个写操作都「先改界面、再等服务端，失败回滚并把错误抛给调用方」；
// 活动表只收作业表条目（key 以 job: 开头，别的不收）、到终态刷新对应缓存；上传经 lib/client 的 FormData。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({ apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn() }));

const API = "/api/v2/style-reference";

const BOOK_A = {
  book_id: "bk-a", title: "甲书", author_label: "某甲", total_chars: 120000, status: "ready", cloud_policy: "allow_full_cloud",
  classification_provenance: { source: "llm" }, profile: { profile_id: "pf-a", needs_relearn: false },
  applied_projects: [{ project_id: "w1", binding_id: "bd-1", config: { sample_windows: 12 } }],
};
const BOOK_B = {
  book_id: "bk-b", title: "乙书", total_chars: 80000, status: "ready", cloud_policy: "segments_only",
  profile: null, applied_projects: [],
};

const PROFILE = {
  profile_id: "pf-a",
  card_line_states: {},
  dimensions: [
    { dimension: "scene.dialogue", lines: [{ line_id: "L1", text: "对白短，常一问一答", state: null }, { line_id: "L2", text: "少用引号外的说明", state: null }] },
    { dimension: "theme.values", lines: [{ line_id: "L3", text: "不替人物下判断", state: null }] },
  ],
};

const BINDING = {
  binding_id: "bd-1", profile_id: "pf-a", scope: "project", scope_ref_id: "w1", status: "active",
  config: { reference_mode: "full", sample_windows: 12, draft_mode: "style_first", dimension_states: {} },
};

let client;
let store;

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function routeGets(overrides = {}) {
  const routes = {
    [`${API}/books`]: () => ({ books: [BOOK_A, BOOK_B] }),
    [`${API}/profiles/pf-a`]: () => ({ profile: JSON.parse(JSON.stringify(PROFILE)) }),
    [`${API}/projects/w1/style-binding`]: () => ({ project_id: "w1", binding: JSON.parse(JSON.stringify(BINDING)), profile: BOOK_A.profile }),
    [`${API}/activity`]: () => ({ items: [] }),
    [`${API}/runtime`]: () => ({ default_cloud_policy: "segments_only", llm_enabled: true, llm_is_local: false }),
    ...overrides,
  };
  client.apiGet.mockImplementation((url) => {
    const hit = routes[url];
    return hit ? Promise.resolve(hit()) : Promise.resolve({});
  });
}

beforeEach(async () => {
  vi.resetModules();
  client = await import("./lib/client.js");
  client.apiGet.mockReset();
  client.apiPost.mockReset();
  client.apiPatch.mockReset();
  client.apiDelete.mockReset();
  routeGets();
  store = await import("./ws-styleref-store.js");
  store.srResetForTests();
});

afterEach(() => {
  store.srResetForTests();
  vi.useRealTimers();
});

describe("书库", () => {
  it("读书库摘要：映射成界面用的形状；读失败时记下错误码而不是清空", async () => {
    const books = await store.srSyncBooks();
    expect(books.map((b) => b.id)).toEqual(["bk-a", "bk-b"]);
    expect(books[0]).toMatchObject({ title: "甲书", author: "某甲", chars: 120000, cloudPolicy: "allow_full_cloud" });
    expect(books[0].appliedProjects).toEqual(BOOK_A.applied_projects);
    expect(store.srBooksState().phase).toBe("ready");

    client.apiGet.mockRejectedValueOnce(Object.assign(new Error("连不上"), { code: "NETWORK_ERROR" }));
    await store.srSyncBooks();
    expect(store.srBooksState()).toMatchObject({ phase: "error", code: "NETWORK_ERROR" });
    expect(store.srBooks().map((b) => b.id)).toEqual(["bk-a", "bk-b"]);
  });

  it("导入对话框的默认值来自 GET /runtime，读过一次就不再读", async () => {
    const data = await store.srLoadRuntime();
    expect(data.default_cloud_policy).toBe("segments_only");
    await store.srLoadRuntime();
    expect(client.apiGet.mock.calls.filter(([url]) => url === `${API}/runtime`)).toHaveLength(1);
  });

  it("页面重新挂载（从「去设置模型」回来）时重读 /runtime：接好模型之后不再锁着（复核 #2）", async () => {
    let enabled = false;
    routeGets({ [`${API}/runtime`]: () => ({ llm_enabled: enabled, llm_is_local: false, default_cloud_policy: "local_only" }) });
    store.srSetViewMounted(true);
    expect((await store.srLoadRuntime()).llm_enabled).toBe(false);
    store.srSetViewMounted(false);
    enabled = true; // 作者去设置里接好了模型
    store.srSetViewMounted(true);
    expect((await store.srLoadRuntime()).llm_enabled).toBe(true);
    expect(store.srRuntime().data.llm_enabled).toBe(true);
    expect(client.apiGet.mock.calls.filter(([url]) => url === `${API}/runtime`)).toHaveLength(2);
    // 同一时间只发一个请求
    store.srSetViewMounted(true);
    await Promise.all([store.srLoadRuntime(), store.srLoadRuntime({ force: true })]);
    expect(client.apiGet.mock.calls.filter(([url]) => url === `${API}/runtime`)).toHaveLength(3);
  });
});

describe("文风卡一句的 ✓ / ✗", () => {
  it("先在界面上标上，服务端的结果回来后以它为准", async () => {
    await store.srLoadProfile("pf-a");
    const gate = deferred();
    client.apiPost.mockReturnValueOnce(gate.promise);
    const pending = store.srSetCardLineState("pf-a", "L1", "pinned");
    const optimistic = store.srProfileDetail("pf-a").data;
    expect(optimistic.card_line_states).toEqual({ L1: "pinned" });
    expect(optimistic.dimensions[0].lines[0].state).toBe("pinned");
    gate.resolve({ card_line_states: { L1: "pinned", L3: "excluded" } });
    await pending;
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/card-lines/L1`, { state: "pinned" });
    const settled = store.srProfileDetail("pf-a").data;
    expect(settled.dimensions[1].lines[0].state).toBe("excluded");
  });

  it("服务端拒绝时回滚并把错误抛给调用方", async () => {
    await store.srLoadProfile("pf-a");
    client.apiPost.mockRejectedValueOnce(Object.assign(new Error("gone"), { code: "STYLE_REFERENCE_CARD_LINE_NOT_FOUND" }));
    await expect(store.srSetCardLineState("pf-a", "L2", "excluded")).rejects.toMatchObject({ code: "STYLE_REFERENCE_CARD_LINE_NOT_FOUND" });
    const after = store.srProfileDetail("pf-a").data;
    expect(after.card_line_states).toEqual({});
    expect(after.dimensions[0].lines[1].state).toBeNull();
  });

  it("取消标记（state=null）也走同一个端点", async () => {
    await store.srLoadProfile("pf-a");
    client.apiPost.mockResolvedValueOnce({ card_line_states: {} });
    await store.srSetCardLineState("pf-a", "L1", null);
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/card-lines/L1`, { state: null });
  });
});

describe("用于作品：直接绑定 / 改配置 / 解除", () => {
  it("用于作品：先把作品标成「在用这份」（pending），成功后换成服务端的绑定并刷新", async () => {
    await store.srSyncBooks();
    const gate = deferred();
    client.apiPost.mockReturnValueOnce(gate.promise);
    const pending = store.srApplyProfile("pf-a", { projectId: "w2", config: { sample_windows: 8 } });
    const optimistic = store.srProjectBinding("w2").data;
    expect(optimistic.binding).toMatchObject({ profile_id: "pf-a", pending: true });
    expect(optimistic.binding.config.sample_windows).toBe(8);
    expect(store.srBookById("bk-a").appliedProjects.map((p) => p.project_id)).toEqual(["w1", "w2"]);
    gate.resolve({ binding: { ...BINDING, binding_id: "bd-2", scope_ref_id: "w2" }, created: true, changed: true, replaced: [] });
    const result = await pending;
    expect(result.binding.binding_id).toBe("bd-2");
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/apply`, { scope: "project", scope_ref_id: "w2", config: { sample_windows: 8 } });
    expect(client.apiGet).toHaveBeenCalledWith(`${API}/projects/w2/style-binding`);
  });

  it("用于作品失败：作品缓存与书库都回到原样，错误抛给调用方", async () => {
    await store.srSyncBooks();
    await store.srLoadProjectBinding("w1");
    const before = store.srProjectBinding("w1").data;
    client.apiPost.mockRejectedValueOnce(Object.assign(new Error("stale"), { code: "STYLE_REFERENCE_PROFILE_STALE" }));
    await expect(store.srApplyProfile("pf-a", { projectId: "w1", config: { reference_mode: "card_only" } }))
      .rejects.toMatchObject({ code: "STYLE_REFERENCE_PROFILE_STALE" });
    expect(store.srProjectBinding("w1").data).toEqual(before);
    expect(store.srBookById("bk-a").appliedProjects).toEqual(BOOK_A.applied_projects);
  });

  it("没有打开作品时不发请求", async () => {
    await expect(store.srApplyProfile("pf-a", { projectId: null })).rejects.toMatchObject({ code: "SR_NO_WORK" });
    expect(client.apiPost).not.toHaveBeenCalled();
  });

  it("改配置：维度状态按维合并、先改界面；失败回滚", async () => {
    await store.srSyncBooks();
    await store.srLoadProjectBinding("w1");
    const gate = deferred();
    client.apiPatch.mockReturnValueOnce(gate.promise);
    const pending = store.srSetDimensionState("w1", "bd-1", "scene.dialogue", "emphasize");
    const optimistic = store.srProjectBinding("w1").data.binding.config;
    expect(optimistic.dimension_states["scene.dialogue"]).toBe("emphasize");
    expect(optimistic.dimension_states["theme.values"]).toBe("normal");
    expect(client.apiPatch).toHaveBeenCalledWith(`${API}/bindings/bd-1`, { config: { dimension_states: { "scene.dialogue": "emphasize" } } });
    gate.reject(Object.assign(new Error("gone"), { code: "STYLE_REFERENCE_BINDING_NOT_FOUND" }));
    await expect(pending).rejects.toMatchObject({ code: "STYLE_REFERENCE_BINDING_NOT_FOUND" });
    expect(store.srProjectBinding("w1").data.binding.config.dimension_states["scene.dialogue"] || "normal").toBe("normal");
    expect(store.srBookById("bk-a").appliedProjects[0].config).toEqual(BOOK_A.applied_projects[0].config);
  });

  it("改配置成功：以服务端返回的绑定为准", async () => {
    await store.srLoadProjectBinding("w1");
    client.apiPatch.mockResolvedValueOnce({ binding: { ...BINDING, config: { ...BINDING.config, sample_windows: 4 } }, changed: true });
    await store.srUpdateBinding("bd-1", { sample_windows: 4 }, { projectId: "w1" });
    expect(store.srProjectBinding("w1").data.binding.config.sample_windows).toBe(4);
  });

  it("解除：先从作品与书库里去掉，成功后重读；失败放回去", async () => {
    await store.srSyncBooks();
    await store.srLoadProjectBinding("w1");
    const gate = deferred();
    client.apiDelete.mockReturnValueOnce(gate.promise);
    const pending = store.srUnbind("bd-1", { projectId: "w1", profileId: "pf-a" });
    expect(store.srProjectBinding("w1").data.binding).toBeNull();
    expect(store.srBookById("bk-a").appliedProjects).toEqual([]);
    gate.reject(Object.assign(new Error("net"), { code: "NETWORK_ERROR" }));
    await expect(pending).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(store.srProjectBinding("w1").data.binding.binding_id).toBe("bd-1");
    expect(store.srBookById("bk-a").appliedProjects.map((p) => p.binding_id)).toEqual(["bd-1"]);

    client.apiDelete.mockResolvedValueOnce({ binding_id: "bd-1", deleted: true });
    await store.srUnbind("bd-1", { projectId: "w1", profileId: "pf-a" });
    expect(client.apiDelete).toHaveBeenLastCalledWith(`${API}/bindings/bd-1`);
  });

  it("用于 / 解除之后「这份文风还用在」的清单重读，不是删掉了事（复核 #9）", async () => {
    await store.srSyncBooks();
    await store.srLoadProfileBindings("pf-a");
    const bindingsCalls = () => client.apiGet.mock.calls.filter(([url]) => url === `${API}/profiles/pf-a/bindings`).length;
    expect(bindingsCalls()).toBe(1);
    client.apiPost.mockResolvedValueOnce({ binding: { ...BINDING, binding_id: "bd-2", scope_ref_id: "w2" }, created: true, changed: true, replaced: [] });
    await store.srApplyProfile("pf-a", { projectId: "w2", config: {} });
    await vi.waitFor(() => expect(bindingsCalls()).toBe(2));
    expect(store.srProfileBindings("pf-a")).not.toBeNull();
    expect(store.srProfileBindings("pf-a").phase).toBe("ready");

    client.apiDelete.mockResolvedValueOnce({ binding_id: "bd-2", deleted: true });
    await store.srUnbind("bd-2", { projectId: "w2", profileId: "pf-a" });
    await vi.waitFor(() => expect(bindingsCalls()).toBe(3));
    expect(store.srProfileBindings("pf-a").phase).toBe("ready");
  });

  it("解除旧版全局绑定：缓存里用着它的每一部作品都先改成「没有在用」，成功后全部重读（复核 #3）", async () => {
    const GLOBAL = { ...BINDING, binding_id: "bd-g", scope: "global", scope_ref_id: null };
    let serverBinding = GLOBAL;
    routeGets({
      [`${API}/projects/w1/style-binding`]: () => ({ project_id: "w1", binding: serverBinding }),
      [`${API}/projects/w2/style-binding`]: () => ({ project_id: "w2", binding: serverBinding }),
    });
    await store.srLoadProjectBinding("w1");
    await store.srLoadProjectBinding("w2");
    const gate = deferred();
    client.apiDelete.mockReturnValueOnce(gate.promise);
    // 从「这份文风还用在」解除（不是哪一部作品自己的，projectId 为空）
    const pending = store.srUnbind("bd-g", { projectId: null, profileId: "pf-a" });
    expect(store.srProjectBinding("w1").data.binding).toBeNull();
    expect(store.srProjectBinding("w2").data.binding).toBeNull();
    serverBinding = null;
    gate.resolve({ binding_id: "bd-g", deleted: true });
    await pending;
    const reloaded = (id) => client.apiGet.mock.calls.filter(([url]) => url === `${API}/projects/${id}/style-binding`).length;
    await vi.waitFor(() => { expect(reloaded("w1")).toBe(2); expect(reloaded("w2")).toBe(2); });
    expect(store.srProjectBinding("w1").data.binding).toBeNull();

    // 失败：两部作品都回到原样
    serverBinding = GLOBAL;
    await store.srLoadProjectBinding("w1", { force: true });
    await store.srLoadProjectBinding("w2", { force: true });
    client.apiDelete.mockRejectedValueOnce(Object.assign(new Error("net"), { code: "NETWORK_ERROR" }));
    await expect(store.srUnbind("bd-g", { profileId: "pf-a" })).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(store.srProjectBinding("w1").data.binding.binding_id).toBe("bd-g");
    expect(store.srProjectBinding("w2").data.binding.binding_id).toBe("bd-g");
  });

  it("合并口径与后端一致：顶层覆盖，维度状态按维合并", () => {
    const merged = store.srMergeConfig(
      { reference_mode: "full", sample_windows: 12, dimension_states: { "scene.dialogue": "emphasize" } },
      { sample_windows: 3, dimension_states: { "theme.values": "exclude" } },
    );
    expect(merged.sample_windows).toBe(3);
    expect(merged.reference_mode).toBe("full");
    expect(merged.dimension_states["scene.dialogue"]).toBe("emphasize");
    expect(merged.dimension_states["theme.values"]).toBe("exclude");
  });
});

describe("删书（批量）", () => {
  it("先从书库拿掉；服务端没删成的放回来（已经不在的不放）", async () => {
    await store.srSyncBooks();
    const gate = deferred();
    client.apiPost.mockReturnValueOnce(gate.promise);
    const pending = store.srDeleteBooks(["bk-a", "bk-b", "bk-a"]);
    expect(store.srBooks()).toEqual([]);
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bulk-delete`, { book_ids: ["bk-a", "bk-b"] });
    // 删完之后的重读先挂着：这里看的是本地把没删成的放回来，不是重读的结果
    const resync = deferred();
    client.apiGet.mockImplementation((url) => (url === `${API}/books` ? resync.promise : Promise.resolve({})));
    gate.resolve({
      results: [
        { book_id: "bk-a", deleted: true },
        { book_id: "bk-b", deleted: false, error: { code: "INTERNAL_ERROR", message: "boom" } },
      ],
      deleted_count: 1, failed_count: 1,
    });
    const result = await pending;
    expect(result.failed_count).toBe(1);
    expect(store.srBooks().map((b) => b.id)).toEqual(["bk-b"]);
    resync.resolve({ books: [BOOK_B] });
  });

  it("整个请求失败：书库原样放回，错误抛给调用方", async () => {
    await store.srSyncBooks();
    client.apiPost.mockRejectedValueOnce(Object.assign(new Error("net"), { code: "NETWORK_ERROR" }));
    await expect(store.srDeleteBooks(["bk-a"])).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(store.srBooks().map((b) => b.id)).toEqual(["bk-a", "bk-b"]);
  });

  it("空列表不发请求", async () => {
    await store.srDeleteBooks([]);
    expect(client.apiPost).not.toHaveBeenCalled();
  });

  it("删掉的书在跑的作业：活动条目一并拿掉，删书之前发出的活动清单晚到也不复活，轮询随之停下（复核 #1）", async () => {
    vi.useFakeTimers();
    await store.srSyncBooks();
    let activityCalls = 0;
    let serverItems = [{ key: "job:jl", job_id: "jl", kind: "learn", status: "running", book_id: "bk-a", percent: 20 }];
    client.apiGet.mockImplementation((url) => {
      if (url === `${API}/activity`) { activityCalls += 1; return Promise.resolve({ items: serverItems }); }
      if (url === `${API}/books`) return Promise.resolve({ books: [BOOK_B] });
      return Promise.resolve({});
    });
    store.srActivityStart();
    await vi.advanceTimersByTimeAsync(0);
    expect(store.srActivityFor("bk-a", "learn")).not.toBeNull();
    client.apiPost.mockResolvedValueOnce({ results: [{ book_id: "bk-a", deleted: true }], deleted_count: 1, failed_count: 0 });
    await store.srDeleteBooks(["bk-a"]);
    expect(store.srActivityFor("bk-a", "learn")).toBeNull();
    expect(store.srActivityEntries()).toEqual([]);
    // 删书之前发出的一轮清单晚到：还列着那本书的作业，不能让它复活
    store.srActivityApply([{ key: "job:jl", job_id: "jl", kind: "learn", status: "running", book_id: "bk-a" }], { complete: true, sentAt: Date.now() });
    expect(store.srActivityEntries()).toEqual([]);
    // 服务端已经不再列它（作业行随书删了）：轮询停下
    serverItems = [];
    const before = activityCalls;
    await vi.advanceTimersByTimeAsync(10000);
    expect(activityCalls - before).toBeLessThanOrEqual(1);
    const settled = activityCalls;
    await vi.advanceTimersByTimeAsync(10000);
    expect(activityCalls).toBe(settled);
  });

  it("删书之后：用着它的作品先改成「没有在用」再重读，这份画像的缓存丢掉，别的绑定清单重读（复核 #5）", async () => {
    await store.srSyncBooks();
    await store.srLoadProjectBinding("w1");
    await store.srLoadProfile("pf-a");
    await store.srLoadProfileBindings("pf-a");
    await store.srLoadProfileBindings("pf-x");
    const resync = deferred();
    client.apiGet.mockImplementation((url) => {
      if (url === `${API}/projects/w1/style-binding`) return resync.promise;
      if (url === `${API}/books`) return Promise.resolve({ books: [BOOK_B] });
      return Promise.resolve({});
    });
    client.apiPost.mockResolvedValueOnce({ results: [{ book_id: "bk-a", deleted: true }], deleted_count: 1, failed_count: 0 });
    const mark = client.apiGet.mock.calls.length;
    await store.srDeleteBooks(["bk-a"]);
    // 重读还没回来：作品页已经不说它在用了
    expect(store.srProjectBinding("w1").data.binding).toBeNull();
    expect(store.srProfileDetail("pf-a")).toBeNull();
    expect(store.srProfileBindings("pf-a")).toBeNull();
    const urls = client.apiGet.mock.calls.slice(mark).map(([url]) => url);
    expect(urls).toContain(`${API}/projects/w1/style-binding`);
    expect(urls).toContain(`${API}/profiles/pf-x/bindings`);
    expect(urls).not.toContain(`${API}/profiles/pf-a/bindings`);
    resync.resolve({ project_id: "w1", binding: null });
    await vi.waitFor(() => expect(store.srProjectBinding("w1").phase).toBe("ready"));
  });
});

describe("导入、重新分类、学习", () => {
  it("导入经 lib/client 发 FormData（带幂等键），分类作业登记进活动表，广播 sr:book-imported", async () => {
    client.apiPost.mockResolvedValueOnce({ book: { book_id: "bk-new", title: "新书" }, job_id: "job-1" });
    const seen = [];
    const onImported = (e) => seen.push(e.detail);
    window.addEventListener("sr:book-imported", onImported);
    const file = new File(["第一章\n正文一段。"], "新书.txt", { type: "text/plain" });
    await store.srRunImport({
      file, title: "新书", authorLabel: " 某人 ", cloudPolicy: "segments_only",
      rightsDeclaration: { analysis_rights: true, send_rights: true }, importKey: "imp-1",
    });
    window.removeEventListener("sr:book-imported", onImported);
    const [url, body, opts] = client.apiPost.mock.calls[0];
    expect(url).toBe(`${API}/books/import-upload`);
    expect(body).toBeInstanceOf(FormData);
    expect(body.get("title")).toBe("新书");
    expect(body.get("author_label")).toBe("某人");
    expect(body.get("cloud_policy")).toBe("segments_only");
    expect(JSON.parse(body.get("rights_declaration"))).toEqual({ analysis_rights: true, send_rights: true });
    expect(body.get("file").name).toBe("新书.txt");
    expect(opts).toEqual({ idempotencyKey: "imp-1" });
    expect(seen).toEqual([{ bookId: "bk-new", jobId: "job-1" }]);
    expect(store.srActivityFor("bk-new", "classify")).toMatchObject({ key: "job:job-1", kind: "classify", mode: "import" });
  });

  it("导入失败原样抛给对话框，不登记活动", async () => {
    client.apiPost.mockRejectedValueOnce(Object.assign(new Error("dup"), { code: "STYLE_REFERENCE_BOOK_DUPLICATE", details: { book_id: "bk-a" } }));
    const file = new File(["x"], "a.txt", { type: "text/plain" });
    await expect(store.srRunImport({ file, title: "a", cloudPolicy: "local_only" })).rejects.toMatchObject({ code: "STYLE_REFERENCE_BOOK_DUPLICATE" });
    expect(store.srActivityEntries()).toEqual([]);
  });

  it("用模型重新分类（保留画像）发 mode=retype；学习文风 / 继续 / 取消走各自的端点", async () => {
    client.apiPost.mockResolvedValueOnce({ job_id: "job-r", mode: "retype" });
    await store.srRetype("bk-a");
    expect(client.apiPost).toHaveBeenLastCalledWith(`${API}/books/bk-a/reclassify`, { mode: "retype" });
    expect(store.srActivityFor("bk-a", "classify")).toMatchObject({ mode: "retype" });

    client.apiPost.mockResolvedValueOnce({ job_id: "job-l" });
    await store.srStartLearn("bk-b");
    expect(client.apiPost).toHaveBeenLastCalledWith(`${API}/books/bk-b/learn`, {});
    expect(store.srActivityFor("bk-b", "learn")).toMatchObject({ key: "job:job-l" });

    client.apiPost.mockResolvedValueOnce({ job_id: "job-l2" });
    await store.srStartLearn("bk-b", { resume: true });
    expect(client.apiPost).toHaveBeenLastCalledWith(`${API}/books/bk-b/learn`, { resume: true });

    // 正文很短也学（清理 C2）；全书窗口重打标签（O1）
    client.apiPost.mockResolvedValueOnce({ job_id: "job-l3" });
    await store.srStartLearn("bk-b", { force: true });
    expect(client.apiPost).toHaveBeenLastCalledWith(`${API}/books/bk-b/learn`, { force: true });
    client.apiPost.mockResolvedValueOnce({ job_id: "job-l4" });
    await store.srStartLearn("bk-b", { retag: true });
    expect(client.apiPost).toHaveBeenLastCalledWith(`${API}/books/bk-b/learn`, { retag: true });
    expect(typeof store.srActivityPoke).toBe("function");

    client.apiPost.mockResolvedValueOnce({ cancelled: true });
    await store.srCancelLearn("bk-b");
    expect(client.apiPost).toHaveBeenLastCalledWith(`${API}/books/bk-b/learn/cancel`, {});

    client.apiPost.mockResolvedValueOnce({ job_id: "job-c", mode: "reclassify" });
    await store.srResumeClassification("bk-a");
    expect(client.apiPost).toHaveBeenLastCalledWith(`${API}/books/bk-a/reclassify`, { resume: true });

    client.apiPost.mockResolvedValueOnce({ cancelled: true });
    await store.srCancelClassification("bk-a");
    expect(client.apiPost).toHaveBeenLastCalledWith(`${API}/books/bk-a/classification/cancel`, {});
  });

  it("本场预览发当前（没保存的）设置与场景", async () => {
    client.apiPost.mockResolvedValueOnce({ windows: [] });
    await store.srScenePreview("pf-a", { sceneId: "sc-1", projectId: "w1", config: { reference_mode: "samples_only", sample_windows: 5 } });
    const [url, body] = client.apiPost.mock.calls[0];
    expect(url).toBe(`${API}/profiles/pf-a/injection-preview`);
    expect(body).toMatchObject({ reference_mode: "samples_only", sample_windows: 5, draft_mode: "style_first", scene_id: "sc-1", project_id: "w1" });
    expect(Object.keys(body.dimension_states)).toHaveLength(16);
  });
});

describe("参考书活动", () => {
  const jobEntry = (over = {}) => ({
    key: "job:j1", job_id: "j1", kind: "learn", status: "running", book_id: "bk-a", percent: 40,
    phase_label: "学习文风 · 分层读原文", started_at: "2026-09-23T10:00:00Z", ...over,
  });

  it("只收作业表条目（key 以 job: 开头），别的条目不收", () => {
    store.srActivityApply([
      jobEntry({ key: "job:j9", kind: "classify", mode: "import" }),
      { key: "imp-legacy-key", kind: "import", status: "running" },
    ]);
    expect(store.srActivityEntries().map((e) => e.key)).toEqual(["job:j9"]);
  });

  it("从进行中走到终态：广播 sr:activity-finished，并按 kind 重读书库与学习信息", async () => {
    store.srActivityApply([jobEntry()]);
    expect(store.srActivityFor("bk-a", "learn")).not.toBeNull();
    const finished = [];
    const onFinished = (e) => finished.push(e.detail.key);
    window.addEventListener("sr:activity-finished", onFinished);
    client.apiGet.mockClear();
    store.srActivityApply([jobEntry({ status: "succeeded", percent: 100, result: { profile_id: "pf-a" } })]);
    await vi.waitFor(() => expect(finished).toEqual(["job:j1"]));
    window.removeEventListener("sr:activity-finished", onFinished);
    const urls = client.apiGet.mock.calls.map(([url]) => url);
    expect(urls).toContain(`${API}/books`);
    expect(urls).toContain(`${API}/books/bk-a/learn`);
    expect(urls).toContain(`${API}/profiles/pf-a`);
    expect(store.srActivityFor("bk-a", "learn")).toBeNull();
  });

  it("作者关掉的终态条目不被下一轮服务端清单复活；「清除已结束」只清终态", () => {
    store.srActivityApply([jobEntry({ key: "job:done", status: "failed" }), jobEntry({ key: "job:run" })]);
    store.srActivityDismiss("job:done");
    store.srActivityApply([jobEntry({ key: "job:done", status: "failed" })]);
    expect(store.srActivityEntries().map((e) => e.key)).toEqual(["job:run"]);
    store.srActivityApply([jobEntry({ key: "job:old", status: "cancelled" })]);
    store.srActivityClearFinished();
    expect(store.srActivityEntries().map((e) => e.key)).toEqual(["job:run"]);
  });

  it("全量清单里不再有的在跑条目：登记超过几秒就收尾拿掉，轮询停下；刚登记的不动（复核 #1）", async () => {
    vi.useFakeTimers();
    let calls = 0;
    client.apiGet.mockImplementation((url) => {
      if (url === `${API}/activity`) { calls += 1; return Promise.resolve({ items: [] }); }
      return Promise.resolve({});
    });
    store.srActivityApply([jobEntry({ key: "job:ghost", job_id: "ghost", book_id: "bk-z" })]);
    // 刚登记（在宽限内）：清单里还没有它也不拿掉（发起作业与轮询可能错开）
    store.srActivityApply([], { complete: true, sentAt: Date.now() });
    expect(store.srActivityEntries().map((e) => e.key)).toEqual(["job:ghost"]);
    // 不是全量清单（例如测试 / 部分数据）不判消失
    vi.advanceTimersByTime(store.SR_ACTIVITY_VANISH_GRACE_MS + 1000);
    store.srActivityApply([]);
    expect(store.srActivityEntries().map((e) => e.key)).toEqual(["job:ghost"]);
    // 一次成功的 /activity 不再列它：拿掉，轮询随之停下
    store.srActivityStart();
    await vi.advanceTimersByTimeAsync(0);
    expect(store.srActivityEntries()).toEqual([]);
    expect(store.srActivityFor("bk-z")).toBeNull();
    const settled = calls;
    await vi.advanceTimersByTimeAsync(10000);
    expect(calls).toBe(settled);
  });

  it("书库摘要里在排队 / 在跑的作业补登进活动表并开始轮询；第一次 /activity 读失败也会退避重试（复核 #10）", async () => {
    vi.useFakeTimers();
    let activityCalls = 0;
    let failActivity = true;
    const running = { key: "job:jl9", job_id: "jl9", kind: "learn", status: "running", book_id: "bk-b", percent: 50, phase_label: "学习文风 · 打标签" };
    client.apiGet.mockImplementation((url) => {
      if (url === `${API}/activity`) {
        activityCalls += 1;
        return failActivity ? Promise.reject(Object.assign(new Error("down"), { code: "NETWORK_ERROR" })) : Promise.resolve({ items: [running] });
      }
      if (url === `${API}/books`) {
        return Promise.resolve({ books: [BOOK_A, { ...BOOK_B, learn: { job_id: "jl9", state: "running", done: 2, total: 7, phase_label: "学习文风 · 打标签" } }] });
      }
      return Promise.resolve({});
    });
    // 页面挂着，第一次 /activity 就失败（后端在重启）：不就此停下
    store.srSetViewMounted(true);
    store.srActivityStart();
    await vi.advanceTimersByTimeAsync(0);
    expect(activityCalls).toBe(1);
    await vi.advanceTimersByTimeAsync(3000);
    expect(activityCalls).toBe(2);
    // 书库读到了：摘要说乙书在学 → 补登，界面上就是「学习中」
    await store.srSyncBooks();
    const seeded = store.srActivityFor("bk-b", "learn");
    expect(seeded).toMatchObject({ key: "job:jl9", status: "running", steps: { done: 2, total: 7 } });
    // 后端回来了：轮询接上，条目换成服务端的快照
    failActivity = false;
    await vi.advanceTimersByTimeAsync(15000);
    expect(store.srActivityFor("bk-b", "learn")).toMatchObject({ percent: 50 });
    const n = activityCalls;
    await vi.advanceTimersByTimeAsync(1500);
    expect(activityCalls).toBe(n + 1);
    // 页面不挂着、也没有在跑的：读失败就不再重试
    store.srResetForTests();
    failActivity = true;
    activityCalls = 0;
    store.srActivityStart();
    await vi.advanceTimersByTimeAsync(20000);
    expect(activityCalls).toBe(1);
  });

  it("续跑沿用同一个作业 id：排队盖过旧的终态、「关掉过」的记号清掉；跑完照常收尾（复核 #15）", async () => {
    store.srActivityApply([jobEntry({ key: "job:j5", job_id: "j5", status: "failed", resumable: true, error: { code: "STYLE_REFERENCE_JOB_FAILED" } })]);
    const sentBefore = Date.now() - 1;
    // 「继续学习」：旧条目（失败）还在活动表里时登记同一个作业
    store.srActivityTrack("j5", { kind: "learn", book_id: "bk-a" });
    const entry = store.srActivityFor("bk-a", "learn");
    expect(entry).toMatchObject({ key: "job:j5", status: "queued", phase_label: "排队中", seenTerminal: false });
    expect(entry.error).toBeUndefined();
    expect(store.srActivityEntries()).toHaveLength(1);
    // 续跑之前就发出的那一轮清单晚到：里面还是旧的「失败」，不算数
    store.srActivityApply([jobEntry({ key: "job:j5", job_id: "j5", status: "failed" })], { complete: true, sentAt: sentBefore });
    expect(store.srActivityFor("bk-a", "learn")).toMatchObject({ status: "queued" });
    // 之后的清单：在跑 → 成功，照常广播结束（没有被当成「关掉过」丢掉）
    const finished = [];
    const onFinished = (e) => finished.push(e.detail.key);
    window.addEventListener("sr:activity-finished", onFinished);
    store.srActivityApply([jobEntry({ key: "job:j5", job_id: "j5", status: "running", percent: 60 })], { complete: true, sentAt: Date.now() + 1 });
    store.srActivityApply([jobEntry({ key: "job:j5", job_id: "j5", status: "succeeded", percent: 100 })], { complete: true, sentAt: Date.now() + 2 });
    await vi.waitFor(() => expect(finished).toEqual(["job:j5"]));
    window.removeEventListener("sr:activity-finished", onFinished);
    expect(store.srActivityEntries().map((e) => [e.key, e.status])).toEqual([["job:j5", "succeeded"]]);
  });

  it("轮询：有在跑的每 1.5 秒拉一次，都结束了就停", async () => {
    vi.useFakeTimers();
    let calls = 0;
    client.apiGet.mockImplementation((url) => {
      if (url !== `${API}/activity`) return Promise.resolve({});
      calls += 1;
      return Promise.resolve({ items: [jobEntry({ status: calls >= 2 ? "succeeded" : "running" })] });
    });
    store.srActivityStart();
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1);
    await vi.advanceTimersByTimeAsync(1500);
    expect(calls).toBe(2);
    await vi.advanceTimersByTimeAsync(6000);
    expect(calls).toBe(2);
  });
});
