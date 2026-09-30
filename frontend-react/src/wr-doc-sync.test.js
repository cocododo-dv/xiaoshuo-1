// WrDocs 保存状态机的 store 层单测（W1）：一场一台状态机——本机缓存即时落地、一次只有一个 PATCH、
// 排队只留最新一稿、失败留在本机待重发、409 的冲突副本 + 读服务端版本 + 读不到时拒绝保存、水合共用一次、
// 已水合的场后台复核、两个标签页。房间级的保存顺序见 ws-writer-doc-saves.test.jsx。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_CHAP, DEFAULT_PROJECT, installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

const T = { timeout: 5000, interval: 20 };
const SERVER = "<p>另一台设备的正文</p>";
const conflictError = () => Object.assign(new Error("conflict"), { code: "AUTHOR_DRAFT_CONFLICT", status: 409 });
const offlineError = () => Object.assign(new Error("offline"), { code: "NETWORK_ERROR", retryable: true });

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

async function settleActive(id = "prj-main") {
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe(id), T);
}

/* 服务端 = 一份可变的草稿（server，起点 rev 1 空稿）：ensure 回它眼下的样子，「另一台设备存下一版」就是改它
   （otherDevice）；routeEnsure(fn) 换掉之后的回法（断网、卡住……），fn(current) 可以退回眼下的样子。
   按状态回包而不按第几次调用：前一个用例留下的旧实例偶尔会在下一个用例开头补一次 ensure（它的旧目录又装载了一次、
   触发了它自己的预热），按次序回包会被它吃掉一次。PATCH 默认按 base + 1 回。 */
async function loadDocs({ opts } = {}) {
  const client = await import("./lib/client.js");
  installApiRouter(client, opts);
  const server = { revision: 1, content: "" };
  const current = () => Promise.resolve({ draft: { draft_id: "d1", revision_no: server.revision, content: server.content } });
  let route = null;
  let ensures = 0;
  client.apiPost.mockImplementation((url) => {
    if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
      ensures += 1;
      return route ? route(current) : current();
    }
    if (/promote-canonical$/.test(url)) return Promise.resolve({ final_scene_row_id: "f1", draft_revision_no: server.revision, canonical_dirty: false });
    return Promise.resolve({});
  });
  client.apiPatch.mockImplementation((url, body) => Promise.resolve({
    draft: { draft_id: "d1", revision_no: Number(body.base_revision_no) + 1, content: body.content },
  }));
  await import("./ws-catalog.jsx");
  await settleActive("prj-main");
  await vi.waitFor(() => expect(window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const mod = await import("./wr-doc-store.jsx");
  const events = [];
  mod.WrDocs.subscribe((kind, detail) => events.push({ kind, ...(detail || {}) }));
  return { mod, client, events, server, routeEnsure: (fn) => { route = fn; }, ensures: () => ensures };
}

/* 另一台设备存下了 rev 3 */
function otherDevice(server) {
  server.revision = 3;
  server.content = SERVER;
}

/* 这一次 PATCH 撞上 409：服务端那一刻已经是另一台设备存下的版本 */
const conflictOnce = (server) => () => { otherDevice(server); return Promise.reject(conflictError()); };

const patches = (client) => client.apiPatch.mock.calls.map(([, body]) => body);
const cacheKey = () => window.wsKey("wr-doc:ch01s1");
const pendingKey = () => window.wsKey("wr-doc-pending:ch01s1");
const recoveryHtml = (mod) => mod.WrRecovery.list().map((entry) => entry.html);
/* 另一部作品的一章：场景 slug 就是 scene_id（阶段 X），和主作品的场不同名 */
const OTHER_WORK_CHAP = {
  ...DEFAULT_CHAP, slug: "zz01", chapter_id: "cz1", title: "旧港的另一章",
  scenes: [{ ...DEFAULT_CHAP.scenes[0], slug: "zz01s1", scene_id: "z1", title: "旧港" }],
};

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  vi.spyOn(window, "alert").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => vi.restoreAllMocks());

describe("保存：本机缓存即时落地，一次一个请求，排队只留最新一稿", () => {
  it("save 在调用之内就把正文写进本机缓存和未同步标记，PATCH 卡住也一样", async () => {
    const { mod, client } = await loadDocs();
    client.apiPatch.mockImplementation(() => new Promise(() => {}));
    void mod.WrDocs.save("ch01s1", "<p>第一稿</p>");
    expect(window.localStorage.getItem(cacheKey())).toBe("<p>第一稿</p>");
    expect(window.localStorage.getItem(pendingKey())).not.toBeNull();
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    void mod.WrDocs.save("ch01s1", "<p>第一稿，路上又写</p>");
    // 路上那一次还没回来：第二稿已经在本机，不另发请求
    expect(window.localStorage.getItem(cacheKey())).toBe("<p>第一稿，路上又写</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: true, saving: true });
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
  });

  it("排队只留最新一稿：中间那一稿被取代，不单独发；它的等待者随最新一稿一起兑现", async () => {
    const { mod, client } = await loadDocs();
    const first = deferred();
    client.apiPatch
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce((url, body) => Promise.resolve({ draft: { draft_id: "d1", revision_no: 3, content: body.content } }));
    const a = mod.WrDocs.save("ch01s1", "<p>一</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    const b = mod.WrDocs.save("ch01s1", "<p>一二</p>");
    const c = mod.WrDocs.save("ch01s1", "<p>一二三</p>");
    first.resolve({ draft: { draft_id: "d1", revision_no: 2, content: "<p>一</p>" } });
    await a;
    await Promise.all([b, c]);
    expect(patches(client)).toEqual([
      { content: "<p>一</p>", base_revision_no: 1 },
      { content: "<p>一二三</p>", base_revision_no: 2 },
    ]);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, revision: 3, saving: false });
    expect(window.localStorage.getItem(pendingKey())).toBeNull();
  });

  it("消毒只做一次：PATCH 发的就是读缓存里那一份干净正文", async () => {
    const { mod, client } = await loadDocs();
    await mod.WrDocs.save("ch01s1", '<p class="is-active" onclick="x()">正文<script>bad()</script></p><img src="x">');
    const cached = mod.WrDocs.cachedHTML("ch01s1");
    expect(cached).toBe("<p>正文</p>");
    expect(patches(client)[0].content).toBe(cached);
  });

  it("不再广播 ws:wr-doc-state / ws:wr-doc-loaded 窗口事件（写作台改用 WrDocs.subscribe）", async () => {
    const seen = [];
    const listener = (event) => seen.push(event.type);
    window.addEventListener("ws:wr-doc-state", listener);
    window.addEventListener("ws:wr-doc-loaded", listener);
    try {
      const { mod, events } = await loadDocs();
      await mod.WrDocs.save("ch01s1", "<p>正文</p>");
      mod.WrDocs.load("ch01s2");
      expect(events.some((event) => event.kind === "state")).toBe(true);
      expect(seen).toEqual([]);
    } finally {
      window.removeEventListener("ws:wr-doc-state", listener);
      window.removeEventListener("ws:wr-doc-loaded", listener);
    }
  });
});

describe("失败（断网 / 5xx）：留在本机，重发的永远是最新一稿", () => {
  it("失败后排队的那一稿停着，不自动发；下一次 save 发最新的；绝不把较旧的一稿补发到较新的后面", async () => {
    const { mod, client } = await loadDocs();
    const first = deferred();
    client.apiPatch.mockImplementationOnce(() => first.promise);
    const a = mod.WrDocs.save("ch01s1", "<p>旧的一稿</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    const b = mod.WrDocs.save("ch01s1", "<p>旧的一稿，新的一句</p>");
    first.reject(offlineError());
    await expect(a).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await expect(b).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: true, saving: false, lastSaveError: expect.objectContaining({ code: "NETWORK_ERROR" }) });
    expect(window.localStorage.getItem(cacheKey())).toBe("<p>旧的一稿，新的一句</p>");
    expect(window.localStorage.getItem(pendingKey())).not.toBeNull();

    await mod.WrDocs.save("ch01s1", "<p>旧的一稿，新的一句，再一句</p>");
    expect(patches(client).map((body) => body.content)).toEqual([
      "<p>旧的一稿</p>",
      "<p>旧的一稿，新的一句，再一句</p>",
    ]);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, lastSaveError: null });
  });

  it("显式 flush 把停着的最新一稿再发一次；flush({retry}) 在等的那一次失败时补发一次（只一次）", async () => {
    const { mod, client } = await loadDocs();
    client.apiPatch.mockRejectedValueOnce(offlineError());
    await expect(mod.WrDocs.save("ch01s1", "<p>断网时写的</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await expect(mod.WrDocs.flush("ch01s1")).resolves.toBe("saved");
    expect(patches(client).map((body) => body.content)).toEqual(["<p>断网时写的</p>", "<p>断网时写的</p>"]);

    const hung = deferred();
    client.apiPatch.mockImplementationOnce(() => hung.promise).mockRejectedValueOnce(offlineError());
    void mod.WrDocs.save("ch01s1", "<p>断网时写的，又一句</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(3), T);
    const leaving = mod.WrDocs.flush("ch01s1", { retry: true });
    hung.reject(offlineError());
    await expect(leaving).resolves.toBe("failed");
    // 补发了一次最新的一稿，就停：不再第三次
    expect(patches(client).slice(2).map((body) => body.content)).toEqual([
      "<p>断网时写的，又一句</p>",
      "<p>断网时写的，又一句</p>",
    ]);
    expect(mod.WrDocs.state("ch01s1").dirty).toBe(true);
  });

  it("提升只提升已经存上的：最近一次保存失败时拒绝，也不替作者重发", async () => {
    const { mod, client } = await loadDocs();
    const failure = offlineError();
    client.apiPatch.mockRejectedValueOnce(failure);
    await expect(mod.WrDocs.save("ch01s1", "<p>没存上</p>")).rejects.toBe(failure);
    await expect(mod.WrDocs.promote("ch01s1")).rejects.toBe(failure);
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
    expect(client.apiPost.mock.calls.some(([url]) => /promote-canonical$/.test(url))).toBe(false);
  });
});

describe("冲突（409）：本机稿先进同步与恢复，读到服务端版本之前不保存", () => {
  it("路上那一稿和排队那一稿（去重）进同步与恢复；读到之后读缓存 = 服务端版本，conflict-resolved 带正文，错误清掉，可以提升", async () => {
    const { mod, client, events, server } = await loadDocs();
    const first = deferred();
    client.apiPatch.mockImplementationOnce(() => first.promise);
    const a = mod.WrDocs.save("ch01s1", "<p>本机一稿</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    const b = mod.WrDocs.save("ch01s1", "<p>本机一稿，排队的一句</p>");
    const c = mod.WrDocs.save("ch01s1", "<p>本机一稿，排队的一句</p>");
    const conflict = conflictError();
    otherDevice(server);
    first.reject(conflict);
    await expect(a).rejects.toBe(conflict);
    await expect(b).rejects.toBe(conflict);
    await expect(c).rejects.toBe(conflict);
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved")).toBe(true), T);

    expect(events.find((event) => event.kind === "conflict-resolved")).toMatchObject({ sid: "ch01s1", workId: "prj-main", html: SERVER });
    expect(recoveryHtml(mod).sort()).toEqual(["<p>本机一稿</p>", "<p>本机一稿，排队的一句</p>"].sort());
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(SERVER);
    expect(window.localStorage.getItem(cacheKey())).toBe(SERVER);
    expect(window.localStorage.getItem(pendingKey())).toBeNull();
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, conflictPending: false, lastSaveError: null, revision: 3 });
    expect(client.apiPatch).toHaveBeenCalledTimes(1);

    await mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged" });
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/author-drafts/d1/promote-canonical", expect.objectContaining({ base_revision_no: 3 }));
  });

  it("读服务端版本失败：保持冲突，拒绝保存（不发请求），只提示一次；下一次保存触发重读；这期间存的字换掉读缓存前进同步与恢复", async () => {
    const slow = deferred();
    const { mod, client, events, server, routeEnsure } = await loadDocs();
    let reads = 0;
    client.apiPatch.mockImplementationOnce(() => {
      otherDevice(server);
      routeEnsure(() => { reads += 1; return reads === 1 ? Promise.reject(offlineError()) : slow.promise; });
      return Promise.reject(conflictError());
    });
    await expect(mod.WrDocs.save("ch01s1", "<p>撞上 409 的一稿</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("暂时读不到")), T);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({
      conflictPending: true,
      dirty: true,
      lastSaveError: expect.objectContaining({ code: "AUTHOR_DRAFT_CONFLICT" }),
    });
    await expect(mod.WrDocs.promote("ch01s1")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });

    // 这期间作者接着写：本机缓存照样落地，请求不发，重读被再次触发
    const held = mod.WrDocs.save("ch01s1", "<p>撞上 409 的一稿，读不到时接着写</p>");
    await expect(held).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await expect(mod.WrDocs.flush("ch01s1")).resolves.toBe("conflict");
    expect(window.localStorage.getItem(cacheKey())).toBe("<p>撞上 409 的一稿，读不到时接着写</p>");
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
    expect(window.alert.mock.calls.filter(([message]) => String(message).includes("暂时读不到"))).toHaveLength(1);

    slow.resolve({ draft: { draft_id: "d1", revision_no: 3, content: SERVER } });
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved")).toBe(true), T);
    expect(recoveryHtml(mod)).toEqual(expect.arrayContaining(["<p>撞上 409 的一稿</p>", "<p>撞上 409 的一稿，读不到时接着写</p>"]));
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(SERVER);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: false, lastSaveError: null });
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
  });

  it("读服务端版本失败之后，窗口重新聚焦（或重新联网）马上再读", async () => {
    let down = false;
    const { mod, client, events, server, routeEnsure } = await loadDocs();
    routeEnsure((current) => (down ? Promise.reject(offlineError()) : current()));
    client.apiPatch.mockImplementationOnce(() => { otherDevice(server); down = true; return Promise.reject(conflictError()); });
    await expect(mod.WrDocs.save("ch01s1", "<p>本机一稿</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("暂时读不到")), T);
    down = false;
    window.dispatchEvent(new Event("focus"));
    // 退避计时要 2 秒；聚焦后不等它
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved")).toBe(true), { timeout: 1000, interval: 20 });
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(SERVER);
  });

  it("读服务端版本失败之后，退避计时到了也会再读（没有聚焦、没有保存）", async () => {
    let down = false;
    const { mod, client, events, server, routeEnsure, ensures } = await loadDocs();
    routeEnsure((current) => (down ? Promise.reject(offlineError()) : current()));
    client.apiPatch.mockImplementationOnce(() => { otherDevice(server); down = true; return Promise.reject(conflictError()); });
    await expect(mod.WrDocs.save("ch01s1", "<p>本机一稿</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("暂时读不到")), T);
    const afterFailure = ensures();
    down = false;
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved")).toBe(true), { timeout: 4000, interval: 50 });
    expect(ensures()).toBe(afterFailure + 1);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(SERVER);
  }, 10000);

  it("断网失败之后再 409：停着的最新一稿随下一次保存发出，撞上 409 就进同步与恢复，服务端版本上读缓存", async () => {
    const { mod, client, events, server } = await loadDocs();
    client.apiPatch.mockRejectedValueOnce(offlineError()).mockImplementationOnce(conflictOnce(server));
    await expect(mod.WrDocs.save("ch01s1", "<p>断网那一稿</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await expect(mod.WrDocs.save("ch01s1", "<p>断网那一稿，联网后又写</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved")).toBe(true), T);
    expect(patches(client).map((body) => [body.base_revision_no, body.content])).toEqual([
      [1, "<p>断网那一稿</p>"],
      [1, "<p>断网那一稿，联网后又写</p>"],
    ]);
    expect(recoveryHtml(mod)).toContain("<p>断网那一稿，联网后又写</p>");
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(SERVER);
  });

  it("写作台补交编辑器里没交出去的字：与这次冲突已经留过的、与服务端版本一样的都不再留", async () => {
    const { mod, client, server } = await loadDocs();
    const kept = [];
    mod.WrDocs.subscribe((kind, detail) => {
      if (kind !== "conflict-resolved") return;
      kept.push(mod.WrDocs.keepLocalCopy("ch01s1", "<p>本机一稿</p>"));           // 已经留过
      kept.push(mod.WrDocs.keepLocalCopy("ch01s1", detail.html));                 // 就是服务端版本
      kept.push(mod.WrDocs.keepLocalCopy("ch01s1", "<p>本机一稿，编辑器里刚敲的</p>"));
    });
    client.apiPatch.mockImplementationOnce(conflictOnce(server));
    await expect(mod.WrDocs.save("ch01s1", "<p>本机一稿</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(kept).toHaveLength(3), T);
    expect(kept[0]).toBeNull();
    expect(kept[1]).toBeNull();
    expect(kept[2]).toMatchObject({ type: "conflict", html: "<p>本机一稿，编辑器里刚敲的</p>" });
    expect(recoveryHtml(mod).sort()).toEqual(["<p>本机一稿</p>", "<p>本机一稿，编辑器里刚敲的</p>"].sort());
    // 编辑器那几句并进冲突的那一条提示，不另弹一条
    await vi.waitFor(() => expect(window.alert.mock.calls.filter(([message]) => String(message).includes("别处被修改"))).toHaveLength(1), T);
  });
});

describe("水合（F03-05）与复核（F03-24）", () => {
  it("并发的水合共用一次 ensure；显式 hydrate 读不到服务器时抛错，load 吞掉", async () => {
    const { mod, routeEnsure, ensures } = await loadDocs();
    const ensure = deferred();
    routeEnsure(() => ensure.promise);
    const before = ensures();
    expect(mod.WrDocs.load("ch01s1")).toBeNull();
    const waiting = mod.WrDocs.hydrate("ch01s1");
    ensure.resolve({ draft: { draft_id: "d1", revision_no: 2, content: "<p>作者的正文</p>" } });
    await expect(waiting).resolves.toBe("<p>作者的正文</p>");
    expect(ensures() - before).toBe(1);

    vi.resetModules();
    window.localStorage.clear();
    const again = await loadDocs();
    again.routeEnsure(() => Promise.reject(offlineError()));
    expect(() => again.mod.WrDocs.load("ch01s1")).not.toThrow();
    await expect(again.mod.WrDocs.hydrate("ch01s1")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    // 连上了：同一个调用再来一次就水合上（也让这个用例收尾时没有半截的水合留给下一个用例）
    again.routeEnsure(null);
    await expect(again.mod.WrDocs.hydrate("ch01s1")).resolves.toBeNull();
  });

  it("已水合、没有本机改动的一场：load 时后台复核，修订号往前走了就换读缓存并通知 loaded；更旧的修订号不理", async () => {
    const { mod, events, server, routeEnsure } = await loadDocs();
    await mod.WrDocs.hydrate("ch01s1");
    server.revision = 4;
    server.content = "<p>别的设备改过的一版</p>";
    expect(mod.WrDocs.load("ch01s1")).toBeNull();
    await vi.waitFor(() => expect(events.some((event) => event.kind === "loaded" && event.html === "<p>别的设备改过的一版</p>")).toBe(true), T);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>别的设备改过的一版</p>");
    expect(mod.WrDocs.state("ch01s1").revision).toBe(4);

    routeEnsure(() => Promise.resolve({ draft: { draft_id: "d1", revision_no: 2, content: "<p>过时的回包</p>" } }));
    mod.WrDocs.load("ch01s1");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>别的设备改过的一版</p>");
    expect(mod.WrDocs.state("ch01s1").revision).toBe(4);
  });

  it("复核途中作者开始写了：回包整个不吸收（保存仍带旧修订号，服务端动过就 409 走冲突）", async () => {
    const revalidation = deferred();
    const { mod, client, routeEnsure } = await loadDocs();
    await mod.WrDocs.hydrate("ch01s1");
    await mod.WrDocs.save("ch01s1", "<p>第一稿</p>"); // rev 2
    routeEnsure(() => revalidation.promise);
    mod.WrDocs.load("ch01s1");                        // 后台复核发出去，还没回来
    const hung = deferred();
    client.apiPatch.mockImplementationOnce(() => hung.promise);
    void mod.WrDocs.save("ch01s1", "<p>第一稿，接着写</p>");
    revalidation.resolve({ draft: { draft_id: "d1", revision_no: 5, content: SERVER } });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(mod.WrDocs.state("ch01s1").revision).toBe(2);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>第一稿，接着写</p>");
    expect(patches(client).map((body) => body.base_revision_no)).toEqual([1, 2]);
  });

  it("水合之前就有保存排着：服务端和作者写时看到的读缓存一样 → 照常保存；不一样 → 冲突（本机稿进同步与恢复，服务端版本上屏）", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>这台电脑上的旧缓存</p>");
    const ensure = deferred();
    const { mod, client, events, routeEnsure } = await loadDocs();
    routeEnsure(() => ensure.promise);
    const save = mod.WrDocs.save("ch01s1", "<p>这台电脑上的旧缓存，打开就写</p>");
    ensure.resolve({ draft: { draft_id: "d1", revision_no: 7, content: SERVER } });
    await expect(save).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === SERVER)).toBe(true), T);
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(recoveryHtml(mod)).toContain("<p>这台电脑上的旧缓存，打开就写</p>");
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(SERVER);

    vi.resetModules();
    window.localStorage.clear();
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>和服务端一样的正文</p>");
    const same = deferred();
    const again = await loadDocs();
    again.routeEnsure(() => same.promise);
    const ok = again.mod.WrDocs.save("ch01s1", "<p>和服务端一样的正文，接着写</p>");
    same.resolve({ draft: { draft_id: "d1", revision_no: 7, content: "<p>和服务端一样的正文</p>" } });
    await ok;
    expect(patches(again.client)).toEqual([{ content: "<p>和服务端一样的正文，接着写</p>", base_revision_no: 7 }]);
  });
});

describe("采纳归档（acceptCanonical）", () => {
  it("第一次保存还在等水合时就采纳了：那一稿不再发，状态干净，等它的调用方收到冲突", async () => {
    const { mod, client, routeEnsure } = await loadDocs();
    const ensure = deferred();
    routeEnsure(() => ensure.promise);
    const save = mod.WrDocs.save("ch01s1", "<p>还没发出去的一稿</p>");
    const saveRejected = expect(save).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    mod.WrDocs.acceptCanonical("ch01s1", "<p>采纳的 AI 稿</p>", { author_draft: { draft_id: "d1", revision_no: 2 } });
    ensure.resolve({ draft: { draft_id: "d1", revision_no: 2, content: "<p>采纳的 AI 稿</p>" } });
    await saveRejected;
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, saving: false, revision: 2 });
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>采纳的 AI 稿</p>");
    expect(recoveryHtml(mod)).toContain("<p>还没发出去的一稿</p>");
    expect(window.localStorage.getItem(pendingKey())).toBeNull();
  });

  it("失败后停着的本机稿不再发；采纳的正文上读缓存；冲突与错误都清掉", async () => {
    const { mod, client } = await loadDocs();
    client.apiPatch.mockRejectedValueOnce(offlineError());
    await expect(mod.WrDocs.save("ch01s1", "<p>断网时写的</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    const snapshot = mod.WrDocs.acceptCanonical("ch01s1", "<p>采纳的 AI 稿</p>", {
      author_draft: { draft_id: "d1", revision_no: 2 },
      final_scene_row_id: "f9",
    });
    expect(snapshot).toMatchObject({ dirty: false, lastSaveError: null, revision: 2, currentFinalSceneRowId: "f9" });
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>采纳的 AI 稿</p>");
    expect(recoveryHtml(mod)).toContain("<p>断网时写的</p>");
    await expect(mod.WrDocs.flush("ch01s1")).resolves.toBe("saved");
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
  });
});

describe("恢复（同步与恢复中心的「恢复」）", () => {
  it("这一场这次还没打开过：先和服务端对齐，自动备份的是服务端眼下那一版，再把恢复稿存上去", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>这台电脑上过时的缓存</p>");
    const { mod, client, server } = await loadDocs();
    server.revision = 6;
    server.content = "<p>服务端眼下的正文</p>";
    const entry = mod.WrRecovery.createCandidate("ch01s1", "<p>要恢复的那一稿</p>");
    const result = await mod.WrRecovery.restore(entry.id);
    expect(result.replacedBackup).toMatchObject({ type: "backup", html: "<p>服务端眼下的正文</p>", durable: true });
    expect(patches(client)).toEqual([{ content: "<p>要恢复的那一稿</p>", base_revision_no: 6 }]);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>要恢复的那一稿</p>");
  });

  it("读不到服务器：停下来，不覆盖", async () => {
    const { mod, client, routeEnsure } = await loadDocs();
    routeEnsure(() => Promise.reject(offlineError()));
    const entry = mod.WrRecovery.createCandidate("ch01s1", "<p>要恢复的那一稿</p>");
    await expect(mod.WrRecovery.restore(entry.id)).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(client.apiPatch).not.toHaveBeenCalled();
  });
});

describe("两份正文是不是同一段文字（写作台据此决定接着写还是按冲突换稿）", () => {
  it("只看字和分段：写法不同、套了一层、<br> 分行、纯文本与 <p>、开头的旧占位句都算同一段；多一个字就不是", async () => {
    await loadDocs();
    const { sameManuscriptText } = await import("./wr-doc-cache.js");
    expect(sameManuscriptText("<p>一</p><p>二</p>", "<div><p>一</p><p>二</p></div>")).toBe(true);
    expect(sameManuscriptText("<p>一<br>二</p>", "<p>一</p><p>二</p>")).toBe(true);
    expect(sameManuscriptText("<p><span>起点</span></p>", '<p class="is-active">起点</p>')).toBe(true);
    expect(sameManuscriptText("一\n二", "<p>一</p><p>二</p>")).toBe(true);
    expect(sameManuscriptText("<p>在这里开始写这一场……</p>", "")).toBe(true);
    expect(sameManuscriptText(null, "<p><br></p>")).toBe(true);
    expect(sameManuscriptText("<p>一</p>", "<p>一二</p>")).toBe(false);
    expect(sameManuscriptText("<p>一</p><p>二</p>", "<p>一二</p>")).toBe(false);
  });

  /* 复核 W1-R1B-7：只比字的话，别的设备只加了斜体的那一版被当成「同一段」，在它上面一存斜体就没了 */
  it("格式也算正文：斜体、粗体、引文不同就不是同一段；同一种格式的几种写法、拆成几个节点、嵌套先后、空白挪位都算一样", async () => {
    await loadDocs();
    const { sameManuscriptText } = await import("./wr-doc-cache.js");
    expect(sameManuscriptText("<p>他<em>终于</em>回来了。</p>", "<p>他终于回来了。</p>")).toBe(false);
    expect(sameManuscriptText("<p><b>甲</b>乙</p>", "<p><b>甲乙</b></p>")).toBe(false);
    expect(sameManuscriptText("<blockquote>甲</blockquote>", "<p>甲</p>")).toBe(false);
    expect(sameManuscriptText("<p><b>甲</b></p>", "<p><strong>甲</strong></p>")).toBe(true);
    expect(sameManuscriptText("<p><i>甲</i></p>", "<p><em>甲</em></p>")).toBe(true);
    expect(sameManuscriptText("<p><b>甲</b><b>乙</b></p>", "<p><b>甲乙</b></p>")).toBe(true);
    expect(sameManuscriptText("<p><b><i>甲</i></b></p>", "<p><i><b>甲</b></i></p>")).toBe(true);
    expect(sameManuscriptText("<p>甲 <b>乙</b></p>", "<p>甲<b> 乙</b></p>")).toBe(true);
    expect(sameManuscriptText("<p><span>甲</span></p>", "<p><mark>甲</mark></p>")).toBe(true);
    expect(sameManuscriptText("<div><p><em>甲</em></p></div>", "<p><em>甲</em></p>")).toBe(true);
  });
});

describe("跨作品与两个标签页", () => {
  it("路上那一次回来时作者已切到另一部作品：未同步标记和冲突副本都记在原作品名下", async () => {
    const second = { ...DEFAULT_PROJECT, project_id: "prj-second", title: "旧港" };
    const { mod, client, server } = await loadDocs({ opts: { projects: [DEFAULT_PROJECT, second] } });
    // 另一部作品有自己的场（阶段 X 起目录的场景 slug 就是 scene_id，两部作品不会有同名的场）：切过去时它预热的是它自己的场，
    // 下面断言的 prj-second 名下的键只可能是原作品这一场的收尾写错了地方
    const get = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (/\/projects\/prj-second\/catalog/.test(url)
      ? Promise.resolve({ chapters: [OTHER_WORK_CHAP] })
      : get(url)));
    const first = deferred();
    client.apiPatch.mockImplementationOnce(() => first.promise);
    const save = mod.WrDocs.save("ch01s1", "<p>主作品的一稿</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    window.WsWorks.setActive("prj-second");
    await settleActive("prj-second");
    otherDevice(server);
    first.reject(conflictError());
    await expect(save).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(mod.WrRecovery.list()).toHaveLength(1), T);
    expect(mod.WrRecovery.list()[0]).toMatchObject({ workId: "prj-main", sid: "ch01s1", html: "<p>主作品的一稿</p>" });
    expect(window.localStorage.getItem("wr-doc:ch01s1::prj-second")).toBeNull();
    expect(window.localStorage.getItem("wr-doc-pending:ch01s1::prj-second")).toBeNull();
    await vi.waitFor(() => expect(window.localStorage.getItem("wr-doc:ch01s1::prj-main")).toBe(SERVER), T);
  });

  /* 同一浏览器两个标签页 = 两份 store、同一个 localStorage。保证：服务端按修订号拒绝后到的那一次，那一页走冲突副本、
     换成服务端版本，谁也盖不掉谁；恢复记录两边都看得到。不保证：读缓存键只有一个，后写的一页覆盖先写的（见 wr-doc-sync.js 文件头）。 */
  it("两个标签页同时改同一场：后到的那一次 409，走冲突副本；恢复记录两边都看得到；读缓存键后写的覆盖先写的", async () => {
    let serverRevision = 1;
    let serverContent = "<p>起点</p>";
    const route = (client) => {
      client.apiPost.mockImplementation((url) => (/\/ensure$/.test(url)
        ? Promise.resolve({ draft: { draft_id: "d1", revision_no: serverRevision, content: serverContent } })
        : Promise.resolve({})));
      client.apiPatch.mockImplementation((url, body) => {
        if (Number(body.base_revision_no) !== serverRevision) return Promise.reject(conflictError());
        serverRevision += 1;
        serverContent = body.content;
        return Promise.resolve({ draft: { draft_id: "d1", revision_no: serverRevision, content: body.content } });
      });
    };
    const tabA = await loadDocs();
    route(tabA.client);
    await tabA.mod.WrDocs.hydrate("ch01s1");
    vi.resetModules();
    const tabB = await loadDocs();
    route(tabB.client);
    await tabB.mod.WrDocs.hydrate("ch01s1");

    await tabA.mod.WrDocs.save("ch01s1", "<p>起点，A 页写的</p>");
    await expect(tabB.mod.WrDocs.save("ch01s1", "<p>起点，B 页写的</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(tabB.events.some((event) => event.kind === "conflict-resolved" && event.html === "<p>起点，A 页写的</p>")).toBe(true), T);

    expect(serverContent).toBe("<p>起点，A 页写的</p>");           // A 的没被 B 盖掉
    expect(recoveryHtml(tabA.mod)).toContain("<p>起点，B 页写的</p>"); // B 的进了同步与恢复，A 页也看得到
    expect(recoveryHtml(tabB.mod)).toContain("<p>起点，B 页写的</p>");
    expect(window.localStorage.getItem(cacheKey())).toBe("<p>起点，A 页写的</p>");
  });
});

/* ==========================================================
   W1 复核一：两路复核（lens A / lens B）在 b75f2d5 上复现的不安全顺序，改成断言安全结果的永久用例。
   用例名带复核编号。服务端按修订号比对（像 author_drafts.save）：base 不是眼下的修订号就 409。
   ========================================================== */

/* 按修订号比对的 PATCH；server.applied 按先后记下每一次存上的正文 */
function casPatch(server) {
  server.applied = [];
  return (url, body) => {
    if (Number(body.base_revision_no) !== server.revision) return Promise.reject(conflictError());
    server.revision += 1;
    server.content = body.content;
    server.applied.push(body.content);
    return Promise.resolve({ draft: { draft_id: "d1", revision_no: server.revision, content: body.content } });
  };
}

/* 起草台「采用」：服务端在一个事务里存下并提升了 AI 稿（adopt-current），写作台这一层吸收回包 */
function adopt(mod, server, html) {
  server.revision += 1;
  server.content = html;
  if (server.applied) server.applied.push(html);
  return mod.WrDocs.acceptCanonical("ch01s1", html, {
    author_draft: { draft_id: "d1", revision_no: server.revision, last_promoted_revision_no: server.revision, canonical_dirty: false },
    final_scene_row_id: "f-adopt",
  });
}

const serverError = () => Object.assign(new Error("database operation failed"), { code: "DATABASE_ERROR", status: 500, retryable: true });
const ADOPTED = "<p>采纳的 AI 稿</p>";
const tick = (ms = 50) => new Promise((resolve) => setTimeout(resolve, ms));
const conflictAlerts = () => window.alert.mock.calls.map(([message]) => String(message)).filter((message) => message.includes("别处被修改"));

describe("复核一 · 采纳落地时路上那一次保存作废（W1-A2 · W1-R1B-1 · W1-A5）", () => {
  async function adoptWhileSaving(failWith) {
    const ctx = await loadDocs();
    const { mod, client, server } = ctx;
    server.content = "<p>起点</p>";
    await mod.WrDocs.hydrate("ch01s1");
    const cas = casPatch(server);
    const hung = deferred();
    client.apiPatch.mockImplementationOnce(() => hung.promise).mockImplementation(cas);
    const save = mod.WrDocs.save("ch01s1", "<p>起点，采纳前作者写的</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    return { ...ctx, save, fail: () => hung.reject(failWith()) };
  }

  it("A1a / S-A' 离场冲刷在等时采纳落地、路上那一次随后断网失败：不补发采纳前的字，采纳的正文不被盖掉", async () => {
    const { mod, client, server, save, fail } = await adoptWhileSaving(offlineError);
    const leaving = mod.WrDocs.flush("ch01s1", { retry: true });   // 写作台卸载：离场冲刷在等这一次
    expect(adopt(mod, server, ADOPTED)).toMatchObject({ dirty: false, saving: false, revision: 2 });
    await expect(save).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    fail();                                                         // 服务端其实按修订号拒绝了它，回包丢了
    expect(await leaving).not.toBe("saved");
    await tick();
    expect(patches(client).map((body) => body.content)).toEqual(["<p>起点，采纳前作者写的</p>"]);
    expect(server.content).toBe(ADOPTED);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(ADOPTED);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, saving: false, lastSaveError: null, conflictPending: false });
    expect(window.localStorage.getItem(pendingKey())).toBeNull();
    expect(recoveryHtml(mod)).toContain("<p>起点，采纳前作者写的</p>");   // 采纳前的字留在同步与恢复
  });

  it("A1b / S-A 路上那一次回 500：之后的冲刷（离场 / 深改 / 提升前）什么都不发，状态干净，采纳的正文不被盖掉", async () => {
    const { mod, client, server, save, fail } = await adoptWhileSaving(serverError);
    adopt(mod, server, ADOPTED);
    fail();
    await expect(save).rejects.toBeTruthy();
    await tick();
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, saving: false, lastSaveError: null });
    await expect(mod.WrDocs.flush("ch01s1")).resolves.toBe("saved");
    await expect(mod.WrDocs.flush("ch01s1", { retry: true })).resolves.toBe("saved");
    await tick();
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
    expect(server.content).toBe(ADOPTED);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(ADOPTED);
  });

  it("R-A1c / W1-A5 路上那一次在采纳之后撞上 409（本来就会）：不开冲突、不提示「别处被修改」、不再读服务端版本、不多一份恢复记录", async () => {
    const { mod, server, events, ensures, save, fail } = await adoptWhileSaving(conflictError);
    // 起草台确认覆盖前先备份了作者稿（scnAdoptToDoc）：就是路上那一稿
    const backup = mod.WrRecovery.createBackup("ch01s1", mod.WrDocs.cachedHTML("ch01s1"), "AI 稿确认覆盖前自动备份作者正文");
    const before = ensures();
    adopt(mod, server, ADOPTED);
    fail();
    await expect(save).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await tick(100);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(conflictAlerts()).toEqual([]);
    expect(ensures()).toBe(before);
    expect(mod.WrRecovery.list().map((entry) => entry.id)).toEqual([backup.id]);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: false, dirty: false, lastSaveError: null });
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(ADOPTED);
  });

  it("采纳之后在采纳的正文上接着写：等作废的那一次回来再发（一次只一个在路上），带的是采纳的修订号", async () => {
    const { mod, client, server, fail } = await adoptWhileSaving(conflictError);
    adopt(mod, server, ADOPTED);
    const next = mod.WrDocs.save("ch01s1", "<p>采纳的 AI 稿，接着写</p>");
    await tick(30);
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
    fail();
    await next;
    expect(patches(client).slice(1)).toEqual([{ content: "<p>采纳的 AI 稿，接着写</p>", base_revision_no: 2 }]);
    expect(server.content).toBe("<p>采纳的 AI 稿，接着写</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, revision: 3, conflictPending: false });
    expect(conflictAlerts()).toEqual([]);
  });
});

describe("复核一 · 恢复 / 重试同步：编辑器与本机缓存同一刻换成恢复稿（W1-A1 · W1-R1B-2）", () => {
  const loadedRestores = (events) => events.filter((event) => event.kind === "loaded" && event.reason === "restore").map((event) => event.html);

  it("A2a 恢复稿没同步上（断网）：PATCH 还没回来就通知写作台换稿；恢复说「还没同步」，恢复稿停在本机，之后冲刷发的正是编辑器里的它", async () => {
    const { mod, client, server, events } = await loadDocs();
    await mod.WrDocs.hydrate("ch01s1");
    const cas = casPatch(server);
    client.apiPatch.mockImplementation(cas);
    await mod.WrDocs.save("ch01s1", "<p>编辑器里的正文 X</p>");
    const entry = mod.WrRecovery.createCandidate("ch01s1", "<p>要恢复的那一稿 R</p>");
    const hung = deferred();
    client.apiPatch.mockImplementationOnce(() => hung.promise);
    const restoring = mod.WrRecovery.restore(entry.id);
    const failed = expect(restoring).rejects.toMatchObject({ code: "RECOVERY_NOT_SYNCED" });
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(2), T);
    expect(loadedRestores(events)).toEqual(["<p>要恢复的那一稿 R</p>"]);
    hung.reject(offlineError());
    await failed;
    await restoring.catch((error) => expect(error.message).toContain("还没同步到服务端"));
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>要恢复的那一稿 R</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: true, lastSaveError: expect.objectContaining({ code: "NETWORK_ERROR" }) });
    expect(mod.WrRecovery.list().some((item) => item.id === entry.id)).toBe(true);
    // 恢复前的正文 X 自动备份过
    expect(mod.WrRecovery.list().some((item) => item.type === "backup" && item.html === "<p>编辑器里的正文 X</p>")).toBe(true);
    client.apiPatch.mockImplementation(cas);
    await expect(mod.WrDocs.flush("ch01s1")).resolves.toBe("saved");
    expect(server.content).toBe("<p>要恢复的那一稿 R</p>");
  });

  it("S-B 重试同步时路上还有一次保存：写作台当场换成恢复稿；恢复稿被随后的一稿取代时不报成功（RECOVERY_SUPERSEDED），记录留着", async () => {
    const { mod, client, server, events } = await loadDocs();
    server.content = "<p>起点</p>";
    await mod.WrDocs.hydrate("ch01s1");
    const cas = casPatch(server);
    const hung = deferred();
    client.apiPatch.mockImplementationOnce((url, body) => hung.promise.then(() => cas(url, body))).mockImplementation(cas);
    void mod.WrDocs.save("ch01s1", "<p>起点，作者在写</p>").catch(() => {});
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    const entry = mod.WrRecovery.create({ sid: "ch01s1", html: "<p>上次没同步上的那一稿</p>", type: "unsynced", reason: "断网", label: "场景 ch01s1 · 未同步稿" });
    let result = null;
    let failure = null;
    const retrying = mod.WrRecovery.retry(entry.id).then((value) => { result = value; }, (error) => { failure = error; });
    await vi.waitFor(() => expect(loadedRestores(events)).toEqual(["<p>上次没同步上的那一稿</p>"]), T);
    expect(result).toBeNull();
    expect(failure).toBeNull();
    // 作者在编辑器里（已经是恢复稿）接着写
    void mod.WrDocs.save("ch01s1", "<p>上次没同步上的那一稿，接着写</p>").catch(() => {});
    hung.resolve();
    await retrying;
    expect(server.applied).toEqual(["<p>起点，作者在写</p>", "<p>上次没同步上的那一稿，接着写</p>"]);
    expect(result).toBeNull();
    expect(failure).toMatchObject({ code: "RECOVERY_SUPERSEDED" });
    expect(mod.WrRecovery.list().some((item) => item.id === entry.id)).toBe(true);
  });

  it("S-B2 重试同步时没有别的在路上：PATCH 回来之前写作台就换了稿；恢复稿自己那一次存上了才移出列表（removed=true）", async () => {
    const { mod, client, server, events } = await loadDocs();
    server.content = "<p>起点</p>";
    await mod.WrDocs.hydrate("ch01s1");
    const cas = casPatch(server);
    client.apiPatch.mockImplementation(cas);
    await mod.WrDocs.save("ch01s1", "<p>起点，作者在写</p>");
    const hung = deferred();
    client.apiPatch.mockImplementationOnce((url, body) => hung.promise.then(() => cas(url, body)));
    const entry = mod.WrRecovery.create({ sid: "ch01s1", html: "<p>上次没同步上的那一稿</p>", type: "unsynced", reason: "断网", label: "场景 ch01s1 · 未同步稿" });
    const retrying = mod.WrRecovery.retry(entry.id);
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(2), T);
    expect(loadedRestores(events)).toEqual(["<p>上次没同步上的那一稿</p>"]);
    hung.resolve();
    await expect(retrying).resolves.toMatchObject({ removed: true, carried: true });
    expect(server.content).toBe("<p>上次没同步上的那一稿</p>");
    expect(mod.WrRecovery.list().some((item) => item.id === entry.id)).toBe(false);
  });
});

describe("复核一 · 409 之后只认冲突之后读到的服务端版本（W1-A3 · W1-R1B-4）", () => {
  it("A4 / S-C 冲突之前发出的后台复核还没回来：读服务端版本不拿它的回包，等它落地后另发一次，编辑器换成另一台设备的版本", async () => {
    const { mod, client, server, events, routeEnsure } = await loadDocs();
    server.content = "<p>起点</p>";
    await mod.WrDocs.hydrate("ch01s1");
    // 另一台设备保存之前发出的 ensure：服务端当时回的是 rev 1 的快照，只是回包很慢（按服务端状态而不按第几次调用回包）
    const slow = deferred();
    routeEnsure((current) => (server.revision === 1 ? current().then((snapshot) => slow.promise.then(() => snapshot)) : current()));
    mod.WrDocs.load("ch01s1");                        // 回到这一场：后台复核发出去
    await tick(20);
    client.apiPatch.mockImplementationOnce(conflictOnce(server));
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，本机一句</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    slow.resolve();
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved")).toBe(true), T);
    expect(events.filter((event) => event.kind === "conflict-resolved").map((event) => event.html)).toEqual([SERVER]);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(SERVER);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ revision: 3, conflictPending: false });
    expect(recoveryHtml(mod)).toContain("<p>起点，本机一句</p>");
  });

  /* 复核二 W1-R2A-1：读回来的是冲突之前的旧快照（服务端按回包丢了的那一次的幂等键重放）——当没读到、马上再读，
     不提示「暂时读不到」（服务端明明连得上）。每一次 ensure 带自己的键之后，这种重放本来就不该再出现。 */
  it("冲突之后读回来的修订号比撞上的那一次还旧（重放的旧快照）：不当成服务端版本，马上再读，不提示「暂时读不到」", async () => {
    const { mod, client, server, events, routeEnsure } = await loadDocs();
    server.content = "<p>起点</p>";
    await mod.WrDocs.hydrate("ch01s1");
    let replay = true;
    client.apiPatch.mockImplementationOnce(() => {
      otherDevice(server);
      routeEnsure((current) => {
        if (!replay) return current();
        replay = false;
        return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "<p>起点</p>" } });
      });
      return Promise.reject(conflictError());
    });
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，本机一句</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved")).toBe(true), T);
    expect(events.filter((event) => event.kind === "conflict-resolved").map((event) => event.html)).toEqual([SERVER]);
    expect(window.alert.mock.calls.map(([message]) => String(message)).filter((message) => message.includes("暂时读不到"))).toEqual([]);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: false, revision: 3 });
    expect(recoveryHtml(mod)).toContain("<p>起点，本机一句</p>");
  });

  it("读回来的一直是冲突之前的旧快照：几次之后才算读不到（保持冲突、提示一次），之后读到了照常换上", async () => {
    const { mod, client, server, events, routeEnsure } = await loadDocs();
    server.content = "<p>起点</p>";
    await mod.WrDocs.hydrate("ch01s1");
    let stale = true;
    client.apiPatch.mockImplementationOnce(() => {
      otherDevice(server);
      routeEnsure((current) => (stale ? Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "<p>起点</p>" } }) : current()));
      return Promise.reject(conflictError());
    });
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，本机一句</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("暂时读不到")), T);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: true, revision: 1 });
    stale = false;
    window.dispatchEvent(new Event("focus"));
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === SERVER)).toBe(true), { timeout: 1000, interval: 20 });
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: false, revision: 3 });
  });
});

describe("复核一 · 回包丢了的那一稿其实存上了（W1-A6）", () => {
  it("A3 上一稿存上了、回包丢了，下一稿撞上 409：认出是自己那一稿，接上修订号接着发；不开冲突、不提示、不进同步与恢复", async () => {
    const { mod, client, server, events } = await loadDocs();
    await mod.WrDocs.hydrate("ch01s1");
    const cas = casPatch(server);
    client.apiPatch
      .mockImplementationOnce((url, body) => { void cas(url, body); return Promise.reject(offlineError()); })
      .mockImplementation(cas);
    await expect(mod.WrDocs.save("ch01s1", "<p>第一句</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await expect(mod.WrDocs.save("ch01s1", "<p>第一句，第二句</p>")).resolves.toBeTruthy();
    expect(patches(client).map((body) => [body.base_revision_no, body.content])).toEqual([
      [1, "<p>第一句</p>"],
      [1, "<p>第一句，第二句</p>"],
      [2, "<p>第一句，第二句</p>"],
    ]);
    expect(server.content).toBe("<p>第一句，第二句</p>");
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(mod.WrRecovery.list()).toEqual([]);
    expect(window.alert).not.toHaveBeenCalled();
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, revision: 3, conflictPending: false, lastSaveError: null });
  });

  it("回包丢了、那一次其实没存上，服务端是另一台设备的版本：照常走冲突（本机稿进同步与恢复，服务端版本上读缓存）", async () => {
    const { mod, client, server, events } = await loadDocs();
    await mod.WrDocs.hydrate("ch01s1");
    client.apiPatch
      .mockImplementationOnce(() => Promise.reject(offlineError()))
      .mockImplementationOnce(() => { server.revision = 2; server.content = SERVER; return Promise.reject(conflictError()); });
    await expect(mod.WrDocs.save("ch01s1", "<p>第一句</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await expect(mod.WrDocs.save("ch01s1", "<p>第一句，第二句</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === SERVER)).toBe(true), T);
    expect(recoveryHtml(mod)).toContain("<p>第一句，第二句</p>");
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(SERVER);
    expect(client.apiPatch).toHaveBeenCalledTimes(2);
  });

  /* 复核二 W1-R2A-1 · W1-R2B-4：核对读不到服务端不等于别处改过——保持核对（不发保存、状态是保存失败），不开冲突、
     不提示「别处被修改」；读到之后按结果走：这里服务端真是另一台设备的版本，那时才走冲突（本机稿进同步与恢复）。 */
  it("核对时读不到服务端：不开冲突、不提示，保持核对、不发保存；读到之后才按结果走（这里是别处的版本 → 冲突）", async () => {
    const { mod, client, server, events, routeEnsure } = await loadDocs();
    await mod.WrDocs.hydrate("ch01s1");
    client.apiPatch
      .mockImplementationOnce(() => Promise.reject(offlineError()))
      .mockImplementationOnce(() => {
        server.revision = 2;
        server.content = SERVER;
        routeEnsure(() => Promise.reject(offlineError()));
        return Promise.reject(conflictError());
      });
    await expect(mod.WrDocs.save("ch01s1", "<p>第一句</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await expect(mod.WrDocs.save("ch01s1", "<p>第一句，第二句</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({
      conflictPending: false, dirty: true, saving: false, lastSaveError: expect.objectContaining({ code: "NETWORK_ERROR" }),
    });
    expect(window.alert).not.toHaveBeenCalled();
    expect(mod.WrRecovery.list()).toEqual([]);
    expect(window.localStorage.getItem(cacheKey())).toBe("<p>第一句，第二句</p>");
    expect(window.localStorage.getItem(pendingKey())).not.toBeNull();
    // 核对期间接着写：只进本机缓存，不发（同时再读一次服务端，还是读不到）
    await expect(mod.WrDocs.save("ch01s1", "<p>第一句，第二句，第三句</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(client.apiPatch).toHaveBeenCalledTimes(2);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    // 连上了：读到的是另一台设备的 rev 2 → 冲突，最新的本机稿进同步与恢复，编辑器换成服务端版本
    routeEnsure(null);
    window.dispatchEvent(new Event("focus"));
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === SERVER)).toBe(true), { timeout: 1000, interval: 20 });
    expect(recoveryHtml(mod)).toContain("<p>第一句，第二句，第三句</p>");
    expect(client.apiPatch).toHaveBeenCalledTimes(2);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: false, revision: 2, lastSaveError: null });
  });
});

describe("复核一 · 提升在路上时又存了一稿（W1-R1B-5）", () => {
  it("S-D 提升回来时草稿已经往前走了一版：仍是「待提升」，不说权威正文已是最新", async () => {
    const { mod, client, server } = await loadDocs();
    await mod.WrDocs.hydrate("ch01s1");
    client.apiPatch.mockImplementation(casPatch(server));
    await mod.WrDocs.save("ch01s1", "<p>一</p>");      // rev 2
    const promoting = deferred();
    const base = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body) => (/promote-canonical$/.test(url) ? promoting.promise : base(url, body)));
    const promote = mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged" });
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => /promote-canonical$/.test(url))).toBe(true), T);
    await mod.WrDocs.save("ch01s1", "<p>一二</p>");    // rev 3：提升还在服务端重建场景记忆
    promoting.resolve({ final_scene_row_id: "f1", draft_revision_no: 2, canonical_dirty: false });
    await promote;
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ revision: 3, lastPromotedRevisionNo: 2, canonicalDirty: true });
  });
});

describe("复核一 · 两个标签页：比的是这一页编辑器里的那一份，不是共用的读缓存（W1-R1B-3）", () => {
  /* 两页共用的服务端（按修订号比对）；gate 在时，这一页的 ensure 等它放行再回眼下的样子 */
  function routeShared(client, shared, gate = null) {
    const snapshot = () => ({ draft: { draft_id: "d1", revision_no: shared.revision, content: shared.content } });
    client.apiPost.mockImplementation((url) => {
      if (!/\/ensure$/.test(url)) return Promise.resolve({});
      return gate ? gate.then(snapshot) : Promise.resolve(snapshot());
    });
    client.apiPatch.mockImplementation((url, body) => {
      if (Number(body.base_revision_no) !== shared.revision) return Promise.reject(conflictError());
      shared.revision += 1;
      shared.content = body.content;
      return Promise.resolve(snapshot());
    });
  }
  const B_TEXT = "<p>起点，B 页刚存的一句</p>";

  async function tabBSavesWhileTabAHydrates() {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>起点</p>");
    const shared = { revision: 1, content: "<p>起点</p>" };
    const tabA = await loadDocs();
    const ensureA = deferred();
    routeShared(tabA.client, shared, ensureA.promise);
    expect(tabA.mod.WrDocs.load("ch01s1")).toBe("<p>起点</p>");   // A 页编辑器装进「起点」，水合在路上
    vi.resetModules();
    const tabB = await loadDocs();
    routeShared(tabB.client, shared);
    await tabB.mod.WrDocs.hydrate("ch01s1");
    await tabB.mod.WrDocs.save("ch01s1", B_TEXT);                  // 服务端 rev 2，共用的读缓存也成了 B 的字
    return { tabA, tabB, shared, release: () => ensureA.resolve() };
  }

  it("S-E A 页在旧版上打开就写、水合回来之前 B 页已经存了一句：A 页不在 B 的修订号上保存，走冲突，B 的字上屏", async () => {
    const { tabA, shared, release } = await tabBSavesWhileTabAHydrates();
    const saveA = tabA.mod.WrDocs.save("ch01s1", "<p>起点，A 页打开就写</p>");
    release();
    await expect(saveA).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(tabA.events.some((event) => event.kind === "conflict-resolved" && event.html === B_TEXT)).toBe(true), T);
    // 两页的 client 替身是同一个模块（resetModules 不重建 vi.mock 的模块）：按正文认出 A 页发的
    expect(patches(tabA.client).filter((body) => body.content.includes("A 页"))).toEqual([]);
    expect(shared.content).toBe(B_TEXT);
    expect(recoveryHtml(tabA.mod)).toContain("<p>起点，A 页打开就写</p>");
  });

  it("S-E2 A 页水合在路上时 B 页存了一句，A 页还没写字：A 页水合回来收到 loaded，编辑器换成 B 的字", async () => {
    const { tabA, release } = await tabBSavesWhileTabAHydrates();
    release();
    await tabA.mod.WrDocs.hydrate("ch01s1");
    expect(tabA.events.filter((event) => event.kind === "loaded").map((event) => [event.reason, event.html])).toEqual([["server", B_TEXT]]);
  });
});

describe("复核一 · 服务端版本是空稿（W1-R1B-6）", () => {
  it("S-F 另一台设备把这一场清空了（rev 5 的空稿），这台电脑在旧缓存上打开就写：按冲突处理，不在 rev 5 上把旧稿存回去", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>这台电脑上的旧稿</p>");
    const { mod, client, server, events, routeEnsure } = await loadDocs();
    server.revision = 5;
    server.content = "";
    const ensure = deferred();
    routeEnsure((current) => ensure.promise.then(() => current()));
    mod.WrDocs.load("ch01s1");
    const save = mod.WrDocs.save("ch01s1", "<p>这台电脑上的旧稿，打开就写</p>");
    ensure.resolve();
    await expect(save).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === "")).toBe(true), T);
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(recoveryHtml(mod)).toContain("<p>这台电脑上的旧稿，打开就写</p>");
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("");
  });

  it("对照：ensure 刚建的空稿（rev 1）——本机那份就是工作稿，照常保存", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>这台电脑上的稿</p>");
    const { mod, client, routeEnsure } = await loadDocs();
    const ensure = deferred();
    routeEnsure((current) => ensure.promise.then(() => current()));
    mod.WrDocs.load("ch01s1");
    const save = mod.WrDocs.save("ch01s1", "<p>这台电脑上的稿，接着写</p>");
    ensure.resolve();
    await save;
    expect(patches(client)).toEqual([{ content: "<p>这台电脑上的稿，接着写</p>", base_revision_no: 1 }]);
    expect(mod.WrRecovery.list()).toEqual([]);
  });
});

describe("复核一 · 格式也是正文（W1-R1B-7）", () => {
  it("S-I 另一台设备只加了斜体（rev 5），这台电脑在旧缓存上打开就写：按冲突处理，斜体不被静默去掉", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>他终于回来了。</p>");
    const { mod, client, server, events, routeEnsure } = await loadDocs();
    server.revision = 5;
    server.content = "<p>他<em>终于</em>回来了。</p>";
    const ensure = deferred();
    routeEnsure((current) => ensure.promise.then(() => current()));
    mod.WrDocs.load("ch01s1");
    const save = mod.WrDocs.save("ch01s1", "<p>他终于回来了。门没关。</p>");
    ensure.resolve();
    await expect(save).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === "<p>他<em>终于</em>回来了。</p>")).toBe(true), T);
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(recoveryHtml(mod)).toContain("<p>他终于回来了。门没关。</p>");
  });
});

/* ==========================================================
   W1 复核二：两路复核（lens A / lens B）在 bd6f02d 上复现的顺序，改成断言安全结果的永久用例（用例名带复核编号）。
   服务端更像真的（像 author_drafts.save + 幂等层）：PATCH 按修订号比对，409 带 details.current_revision_no；内容没变
   修订号不动；「回包丢了」= 服务端存上了、浏览器只看到断网，同一份请求再来时按幂等键重放当时的回包。
   多个标签页 = 多份 store 实例（vi.resetModules）共用一份 shared 与同一个 localStorage。
   ========================================================== */

function sharedServer(content = "<p>起点</p>", revision = 1) {
  return { draftId: "d1", revision, content, applied: [], lost: new Map(), promoted: [], adopted: [], hooks: {}, locked: false };
}

const casConflict = (current) => Object.assign(new Error("author draft has changed; refresh before saving"), {
  code: "AUTHOR_DRAFT_CONFLICT", status: 409, details: { current_revision_no: current },
});
/* 章在别处批准了：服务端拒绝改它的正文（chapter_approval.require_chapter_mutation_allowed） */
const lockedRefusal = () => Object.assign(new Error("approved chapter must be explicitly reopened before it or its scenes can change"), {
  code: "CHAPTER_APPROVED_LOCKED", status: 409, retryable: false, details: { reopen_required: true },
});

/* 装上 shared 这台服务端（每个标签页 loadDocs 之后都要装一次：client 替身是同一个模块，loadDocs 会把它换回默认）。
   shared.locked：章已批准锁定，改正文的保存被拒；shared.draftId 换了：旧草稿 id 上的保存被拒（AUTHOR_DRAFT_NOT_CURRENT）；
   adopt-current 在一个事务里存下并提升采纳的那一稿（shared.hooks.adoptAnswer：回包先按住，服务端这边已经存下了） */
function routeServer(client, shared) {
  const snapshot = () => ({ draft: { draft_id: shared.draftId, revision_no: shared.revision, content: shared.content } });
  const keyOf = (body) => JSON.stringify([Number(body.base_revision_no), body.content]);
  const apply = (body) => {
    const key = keyOf(body);
    if (shared.lost.has(key)) { // 回包丢了的那一次：同一份请求再来，按幂等键重放当时的回包
      const stored = shared.lost.get(key);
      shared.lost.delete(key);
      return Promise.resolve(stored);
    }
    if (Number(body.base_revision_no) !== shared.revision) return Promise.reject(casConflict(shared.revision));
    if (body.content !== shared.content) {
      if (shared.locked) return Promise.reject(lockedRefusal());
      shared.revision += 1;
      shared.content = body.content;
      shared.applied.push(body.content);
    }
    return Promise.resolve(snapshot());
  };
  /* 服务端存上了，回包在回来的路上丢了 */
  const applyLost = (body) => apply(body).then((stored) => {
    shared.lost.set(keyOf(body), stored);
    return Promise.reject(offlineError());
  });
  shared.api = { apply, applyLost, snapshot };
  client.apiPost.mockImplementation((url, body) => {
    if (/\/author-drafts\/scene\/.+\/ensure$/.test(url)) {
      const current = () => Promise.resolve(snapshot());
      return shared.hooks.ensure ? shared.hooks.ensure(current) : current();
    }
    if (/promote-canonical$/.test(url)) {
      const promote = () => {
        if (Number(body.base_revision_no) !== shared.revision) return Promise.reject(casConflict(shared.revision));
        shared.promoted.push({ revision: shared.revision, content: shared.content });
        return Promise.resolve({ final_scene_row_id: `f-${shared.revision}`, draft_revision_no: shared.revision, canonical_dirty: false });
      };
      return shared.hooks.promote ? shared.hooks.promote(promote) : promote();
    }
    if (/\/api\/v1\/scenes\/[^/]+\/adopt-current$/.test(url)) {
      const exact = body.exact_author_draft;
      if (Number(exact.base_revision_no) !== shared.revision) return Promise.reject(casConflict(shared.revision));
      if (exact.content !== shared.content) {
        shared.revision += 1;
        shared.content = exact.content;
        shared.applied.push(exact.content);
      }
      shared.adopted.push({ revision: shared.revision, content: exact.content });
      const answer = {
        scene_id: "s1", scene_status: "archived", final_scene_row_id: `f-${shared.revision}`, content_hash: "h",
        author_draft: { draft_id: shared.draftId, revision_no: shared.revision, content: exact.content, last_promoted_revision_no: shared.revision, canonical_dirty: false },
      };
      return shared.hooks.adoptAnswer ? shared.hooks.adoptAnswer.then(() => answer) : Promise.resolve(answer);
    }
    return Promise.resolve({});
  });
  client.apiPatch.mockImplementation((url, body) => {
    if (!/\/author-drafts\//.test(url)) return Promise.resolve({});
    if (!url.endsWith(`/author-drafts/${shared.draftId}`)) {
      return Promise.reject(Object.assign(new Error("author draft is not current"), { code: "AUTHOR_DRAFT_NOT_CURRENT", status: 409, retryable: false, details: {} }));
    }
    return shared.hooks.patch ? shared.hooks.patch(body, shared.api) : apply(body);
  });
  return shared;
}

/* 一个标签页：一份新的 store 实例，装上共用的服务端 */
async function openTab(shared, options) {
  const tab = await loadDocs(options);
  routeServer(tab.client, shared);
  return tab;
}

const alertTexts = () => window.alert.mock.calls.map(([message]) => String(message));
const elsewhereAlerts = () => alertTexts().filter((message) => message.includes("别处被修改") || message.includes("在别处有更新"));

describe("复核二 · 409 之后核对自己那一稿读不到 / 读到旧快照（W1-R2A-1 · W1-R2B-4）", () => {
  async function lostThenConflict(shared, mod) {
    shared.hooks.patch = (body, api) => { shared.hooks.patch = null; return api.applyLost(body); };
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，第一句</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(shared).toMatchObject({ revision: 2, content: "<p>起点，第一句</p>" }); // 其实存上了
  }

  it("R2-D / NS-1a 核对的那一次读取断网：不开冲突、不提示「别处被修改」，编辑器不退回到自己更旧的一稿；连上之后接着发最新的一稿", async () => {
    const shared = sharedServer();
    const { mod, client, events } = await openTab(shared);
    await mod.WrDocs.hydrate("ch01s1");
    await lostThenConflict(shared, mod);
    let blip = true;
    shared.hooks.ensure = (current) => {
      if (!blip) return current();
      blip = false;
      return Promise.reject(offlineError());
    };
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，第一句，第二句</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await tick();
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(alertTexts()).toEqual([]);
    expect(mod.WrRecovery.list()).toEqual([]);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，第一句，第二句</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: false, dirty: true, saving: false, lastSaveError: expect.objectContaining({ code: "NETWORK_ERROR" }) });
    expect(patches(client)).toHaveLength(2);
    // 重新联网：再核对一次，认出 rev 2 是自己那一稿，最新的一稿接着发
    window.dispatchEvent(new Event("online"));
    await vi.waitFor(() => expect(shared.content).toBe("<p>起点，第一句，第二句</p>"), { timeout: 1000, interval: 20 });
    expect(shared.applied).toEqual(["<p>起点，第一句</p>", "<p>起点，第一句，第二句</p>"]);
    expect(patches(client).map((body) => body.base_revision_no)).toEqual([1, 1, 2]);
    await vi.waitFor(() => expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, revision: 3, lastSaveError: null }), T);
    expect(alertTexts()).toEqual([]);
    expect(mod.WrRecovery.list()).toEqual([]);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
  });

  it("NS-1b 核对读回来的是冲突之前的旧快照（服务端重放了回包丢了的那一次 ensure）：当没读到、马上再读，认出自己那一稿", async () => {
    const shared = sharedServer();
    const { mod, events } = await openTab(shared);
    await mod.WrDocs.hydrate("ch01s1");
    await lostThenConflict(shared, mod);
    let replay = { draft: { draft_id: "d1", revision_no: 1, content: "<p>起点</p>" } };
    shared.hooks.ensure = (current) => {
      if (!replay) return current();
      const stored = replay;
      replay = null;
      return Promise.resolve(stored);
    };
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，第一句，第二句</p>")).resolves.toBeTruthy();
    expect(shared.applied).toEqual(["<p>起点，第一句</p>", "<p>起点，第一句，第二句</p>"]);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(alertTexts()).toEqual([]);
    expect(mod.WrRecovery.list()).toEqual([]);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, revision: 3, conflictPending: false });
  });

  it("每一次 ensure 都带自己的幂等键：回包丢了的那一次不会被服务端按同一个键重放给之后的读取", async () => {
    const { mod, client, server } = await loadDocs();
    await mod.WrDocs.hydrate("ch01s1");
    server.revision = 2;
    server.content = "<p>别处的一版</p>";
    mod.WrDocs.load("ch01s1");
    const ensureCalls = () => client.apiPost.mock.calls.filter(([url]) => /\/ensure$/.test(url));
    await vi.waitFor(() => expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>别处的一版</p>"), T);
    const keys = ensureCalls().map(([, , options]) => options && options.idempotencyKey);
    expect(keys.length).toBeGreaterThanOrEqual(2);
    expect(keys.every((key) => typeof key === "string" && key.length > 0)).toBe(true);
    expect(new Set(keys).size).toBe(keys.length);
  });
});

describe("复核二 · 两个标签页共用读缓存（W1-R2B-1 · W1-R2B-3）", () => {
  it("NS-17 A 页重新打开这一场时，共用读缓存里是 B 页没同步上的字：A 页读到的仍是自己的那一份；提升的就是 A 页显示的那一版", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>起点</p>");
    const shared = sharedServer("<p>起点</p>");
    const tabA = await openTab(shared);
    expect(tabA.mod.WrDocs.load("ch01s1")).toBe("<p>起点</p>");
    await tabA.mod.WrDocs.hydrate("ch01s1");
    vi.resetModules();
    const tabB = await openTab(shared);
    await tabB.mod.WrDocs.hydrate("ch01s1");
    shared.hooks.patch = () => { shared.hooks.patch = null; return Promise.reject(serverError()); };
    await expect(tabB.mod.WrDocs.save("ch01s1", "<p>起点，B 页没存上的一段</p>")).rejects.toMatchObject({ code: "DATABASE_ERROR" });
    expect(window.localStorage.getItem(cacheKey())).toBe("<p>起点，B 页没存上的一段</p>");

    // 回到 A 页、重新打开这一场
    expect(tabA.mod.WrDocs.load("ch01s1")).toBe("<p>起点</p>");
    expect(tabA.mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点</p>");
    await tick(80);
    expect(tabA.mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, revision: 1 });
    await tabA.mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged" });
    expect(shared.promoted).toEqual([{ revision: 1, content: "<p>起点</p>" }]);
    // B 页没同步上的字没丢：还在共用读缓存里、带着未同步标记（B 页下一次保存照常再发），或已经进了同步与恢复
    const inSlot = window.localStorage.getItem(cacheKey()) === "<p>起点，B 页没存上的一段</p>"
      && window.localStorage.getItem(pendingKey()) != null;
    expect({ kept: inSlot || recoveryHtml(tabA.mod).includes("<p>起点，B 页没存上的一段</p>") }).toEqual({ kept: true });
  });

  it("NS-2 B 页一次 5xx 停着，A 页随后在同一场存了一稿：B 页没同步上的字先进同步与恢复（提示一次），B 页关掉之后还在", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>起点</p>");
    const shared = sharedServer("<p>起点</p>");
    const tabA = await openTab(shared);
    await tabA.mod.WrDocs.hydrate("ch01s1");
    vi.resetModules();
    const tabB = await openTab(shared);
    await tabB.mod.WrDocs.hydrate("ch01s1");
    shared.hooks.patch = () => { shared.hooks.patch = null; return Promise.reject(serverError()); };
    await expect(tabB.mod.WrDocs.save("ch01s1", "<p>起点，B 页写的一段</p>")).rejects.toMatchObject({ code: "DATABASE_ERROR" });

    await tabA.mod.WrDocs.save("ch01s1", "<p>起点，A 页写的一段</p>");
    expect(shared.content).toBe("<p>起点，A 页写的一段</p>");
    expect(recoveryHtml(tabA.mod)).toContain("<p>起点，B 页写的一段</p>");
    expect(alertTexts().filter((message) => message.includes("另一个标签页"))).toHaveLength(1);

    // B 页关掉（它的内存没了），这一场在新的一页里打开：B 页的字在同步与恢复里，编辑器是服务端上 A 页的那一稿
    vi.resetModules();
    const tabC = await openTab(shared);
    tabC.mod.WrDocs.load("ch01s1");
    await tabC.mod.WrDocs.hydrate("ch01s1");
    expect(recoveryHtml(tabC.mod)).toContain("<p>起点，B 页写的一段</p>");
    expect(tabC.mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，A 页写的一段</p>");
  });

  it("A 页的保存在路上时 B 页写进了没同步上的字：A 页存上了不清未同步标记，B 页关掉以后它照样进同步与恢复", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>起点</p>");
    const shared = sharedServer("<p>起点</p>");
    const tabA = await openTab(shared);
    await tabA.mod.WrDocs.hydrate("ch01s1");
    vi.resetModules();
    const tabB = await openTab(shared);
    await tabB.mod.WrDocs.hydrate("ch01s1");
    const hung = deferred();
    shared.hooks.patch = (body, api) => {
      shared.hooks.patch = () => Promise.reject(serverError()); // B 页那一次：服务端出错
      return hung.promise.then(() => api.apply(body));
    };
    const saveA = tabA.mod.WrDocs.save("ch01s1", "<p>起点，A 页写的</p>");
    await vi.waitFor(() => expect(tabA.client.apiPatch).toHaveBeenCalledTimes(1), T);
    await expect(tabB.mod.WrDocs.save("ch01s1", "<p>起点，B 页写的</p>")).rejects.toMatchObject({ code: "DATABASE_ERROR" });
    hung.resolve();
    await saveA;
    expect(shared.content).toBe("<p>起点，A 页写的</p>");
    expect(window.localStorage.getItem(cacheKey())).toBe("<p>起点，B 页写的</p>");
    expect(window.localStorage.getItem(pendingKey())).not.toBeNull();

    vi.resetModules();
    const tabC = await openTab(shared);
    tabC.mod.WrDocs.load("ch01s1");
    await tabC.mod.WrDocs.hydrate("ch01s1");
    expect(recoveryHtml(tabC.mod)).toContain("<p>起点，B 页写的</p>");
    expect(tabC.mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，A 页写的</p>");
  });
});

describe("复核二 · 空白与空行也是正文（W1-R2B-5）", () => {
  it("全角空格的段首缩进、两段之间的空行不同就不是同一段；排版空白、&nbsp;、开头结尾的空段、空行的几种写法都算一样", async () => {
    await loadDocs();
    const { sameManuscriptText: same } = await import("./wr-doc-cache.js");
    expect(same("<p>　　他终于回来了。</p>", "<p>他终于回来了。</p>")).toBe(false);
    expect(same("<p>他终于回来了。　</p>", "<p>他终于回来了。</p>")).toBe(false);
    expect(same("<p>A</p><p><br></p><p>B</p>", "<p>A</p><p>B</p>")).toBe(false);
    expect(same("<p>A<br><br>B</p>", "<p>A</p><p>B</p>")).toBe(false);
    expect(same("<p>A<br><br>B</p>", "<p>A</p><p><br></p><p>B</p>")).toBe(true);
    expect(same("<p>A</p><p>&nbsp;</p><p>B</p>", "<p>A</p><p><br></p><p>B</p>")).toBe(true);
    expect(same("<p>A</p><p><br></p><p><br></p><p>B</p>", "<p>A</p><p><br></p><p>B</p>")).toBe(true);
    expect(same("<div><p>A</p><p><br></p></div><p>B</p>", "<p>A</p><p><br></p><p>B</p>")).toBe(true);
    expect(same("<p>A</p><p><br></p>", "<p>A</p>")).toBe(true);
    expect(same("<p><br></p><p>A</p>", "<p>A</p>")).toBe(true);
    expect(same("<p>A<br></p><p>B</p>", "<p>A</p><p>B</p>")).toBe(true);
    expect(same("<p>A</p>\n<p>B</p>", "<p>A</p><p>B</p>")).toBe(true);
    expect(same("<p>A&nbsp; B </p>", "<p>A B</p>")).toBe(true);
  });

  async function staleCacheThenType(stale, serverHTML, typed) {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", stale);
    const ctx = await loadDocs();
    ctx.server.revision = 5;
    ctx.server.content = serverHTML;
    const ensure = deferred();
    ctx.routeEnsure((current) => ensure.promise.then(() => current()));
    ctx.mod.WrDocs.load("ch01s1");
    const save = ctx.mod.WrDocs.save("ch01s1", typed);
    ensure.resolve();
    await expect(save).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(ctx.events.some((event) => event.kind === "conflict-resolved" && event.html === serverHTML)).toBe(true), T);
    expect(ctx.client.apiPatch).not.toHaveBeenCalled();
    expect(recoveryHtml(ctx.mod)).toContain(typed);
    expect(ctx.mod.WrDocs.cachedHTML("ch01s1")).toBe(serverHTML);
  }

  it("NS-3a 另一台设备只加了全角空格的段首缩进（rev 5），这台电脑在旧缓存上打开就写：按冲突处理，缩进不被静默去掉", async () => {
    await staleCacheThenType(
      "<p>他终于回来了。</p><p>门外的雨停了。</p>",
      "<p>　　他终于回来了。</p><p>　　门外的雨停了。</p>",
      "<p>他终于回来了。</p><p>门外的雨停了。他没进门。</p>",
    );
  });

  it("NS-3b 另一台设备只在两段之间加了一个空行（rev 5）：同样按冲突处理", async () => {
    await staleCacheThenType(
      "<p>他终于回来了。</p><p>第二天清早，码头空了。</p>",
      "<p>他终于回来了。</p><p><br></p><p>第二天清早，码头空了。</p>",
      "<p>他终于回来了。</p><p>第二天清早，码头空了。潮水退了。</p>",
    );
  });
});

describe("复核二 · 同步与恢复的「恢复」说的是真话（W1-R2A-2 · W1-R2A-3 · W1-R2B-6）", () => {
  it("R2-F 恢复一份目录里已经没有这一场的记录：说清恢复不了；不写本机缓存、不标未同步、不发请求，记录还在", async () => {
    const { mod, client, events } = await loadDocs();
    const entry = mod.WrRecovery.create({ sid: "ch09s9", html: "<p>一场已经删掉的戏</p>", type: "conflict", reason: "409", label: "场景 ch09s9 · 冲突本地稿" });
    const failure = await mod.WrRecovery.restore(entry.id).catch((error) => error);
    expect(failure).toMatchObject({ code: "RECOVERY_SCENE_UNAVAILABLE" });
    expect(failure.message).not.toMatch(/已恢复到编辑器|网络或服务端出错|会再同步/);
    expect(window.localStorage.getItem(window.wsKey("wr-doc:ch09s9"))).toBeNull();
    expect(window.localStorage.getItem(window.wsKey("wr-doc-pending:ch09s9"))).toBeNull();
    expect(events.some((event) => event.kind === "loaded" && event.sid === "ch09s9")).toBe(false);
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(mod.WrRecovery.list().some((item) => item.id === entry.id)).toBe(true);
  });

  it("R2-A 恢复进一章已批准锁定的场：先停下、说要先重新打开本章；不换稿、不写本机缓存、不发请求，记录还在", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>起点正文</p>");
    const { mod, client, events } = await loadDocs({ opts: { catalog: [{ ...DEFAULT_CHAP, state: "approved" }] } });
    const entry = mod.WrRecovery.create({ sid: "ch01s1", html: "<p>以前冲突时留下的本机稿</p>", type: "conflict", reason: "409", label: "场景 ch01s1 · 冲突本地稿" });
    const failure = await mod.WrRecovery.restore(entry.id).catch((error) => error);
    expect(failure).toMatchObject({ code: "CHAPTER_APPROVED_LOCKED" });
    expect(failure.message).toContain("重新打开本章");
    expect(failure.message).not.toMatch(/网络或服务端出错|会再同步/);
    await expect(mod.WrDocs.replace("ch01s1", "<p>以前冲突时留下的本机稿</p>")).rejects.toMatchObject({ code: "CHAPTER_APPROVED_LOCKED" });
    expect(events.some((event) => event.kind === "loaded" && event.reason === "restore")).toBe(false);
    expect(window.localStorage.getItem(cacheKey())).toBe("<p>起点正文</p>");
    expect(window.localStorage.getItem(pendingKey())).toBeNull();
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(mod.WrRecovery.list().some((item) => item.id === entry.id)).toBe(true);
  });

  it("恢复稿的 PATCH 被服务端明确拒绝（这期间章在别处批准了）：说服务端拒绝了什么，不说「之后会再同步」；编辑器和本机缓存换回服务端的正文，记录还在", async () => {
    const shared = sharedServer();
    const { mod, client, events } = await openTab(shared);
    await mod.WrDocs.hydrate("ch01s1");
    await mod.WrDocs.save("ch01s1", "<p>起点，恢复前的正文</p>");                 // rev 2
    shared.hooks.patch = () => Promise.reject(Object.assign(new Error("approved chapter must be explicitly reopened before it or its scenes can change"), {
      code: "CHAPTER_APPROVED_LOCKED", status: 409, retryable: false, details: {},
    }));
    const entry = mod.WrRecovery.createCandidate("ch01s1", "<p>要恢复的那一稿</p>");
    const before = events.length;
    const failure = await mod.WrRecovery.restore(entry.id).catch((error) => error);
    expect(failure).toMatchObject({ code: "RECOVERY_REFUSED" });
    expect(failure.message).toContain("已批准锁定");
    expect(failure.message).toContain("编辑器换回了服务端上的正文");
    expect(failure.message).not.toMatch(/网络或服务端出错|会再同步/);
    // 编辑器先换成恢复稿，服务端拒绝之后换回服务端上的正文（force：作者正在写也换；复核三 W1-R3B-2 起由 WrDocs 自己换回）
    expect(events.slice(before).filter((event) => event.kind === "loaded").map((event) => [event.reason, event.html, event.force])).toEqual([
      ["restore", "<p>要恢复的那一稿</p>", false],
      ["refused", "<p>起点，恢复前的正文</p>", true],
    ]);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，恢复前的正文</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, lastSaveError: null, revision: 2 });
    expect(window.localStorage.getItem(pendingKey())).toBeNull();
    expect(mod.WrRecovery.list().some((item) => item.id === entry.id)).toBe(true);
    // 不再重发被拒的那一稿
    await expect(mod.WrDocs.flush("ch01s1")).resolves.toBe("saved");
    expect(patches(client).map((body) => body.content)).toEqual(["<p>起点，恢复前的正文</p>", "<p>要恢复的那一稿</p>"]);
  });

  it("NS-4 「恢复」撞上 409、随后读不到服务端版本：失败说明照实说「还没读下来」，不说编辑器已换成服务端版本；什么都没被盖掉", async () => {
    const shared = sharedServer();
    const { mod, events } = await openTab(shared);
    await mod.WrDocs.hydrate("ch01s1");
    await mod.WrDocs.save("ch01s1", "<p>起点，作者的正文 X</p>");          // rev 2
    shared.revision = 3;                                                     // 另一台设备 rev 3
    shared.content = "<p>另一台设备的正文</p>";
    shared.hooks.ensure = () => Promise.reject(offlineError());              // 冲突之后读不到服务端版本
    const entry = mod.WrRecovery.createCandidate("ch01s1", "<p>要恢复的那一稿 R</p>");
    const failure = await mod.WrRecovery.restore(entry.id).catch((error) => error);
    await tick(80);
    expect(failure).toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    expect(failure.message).toContain("还没读下来");
    expect(failure.message).not.toContain("编辑器已换成服务端的最新版本");
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: true });
    expect(shared.content).toBe("<p>另一台设备的正文</p>");
    expect(recoveryHtml(mod)).toEqual(expect.arrayContaining(["<p>起点，作者的正文 X</p>", "<p>要恢复的那一稿 R</p>"]));
  });

  it("NS-8b 核对进行中「恢复」、核对读到的是另一台设备的版本：恢复照实报冲突（编辑器已换成它），本机的两稿都在同步与恢复", async () => {
    const shared = sharedServer();
    const { mod, events } = await openTab(shared);
    await mod.WrDocs.hydrate("ch01s1");
    shared.hooks.patch = (body) => {
      shared.hooks.patch = null;
      shared.revision = 2;
      shared.content = "<p>另一台设备的正文</p>";
      void body;
      return Promise.reject(offlineError());
    };
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，第一句</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    const hold = deferred();
    let checking = false;
    shared.hooks.ensure = (current) => { checking = true; return hold.promise.then(() => { shared.hooks.ensure = null; return current(); }); };
    void mod.WrDocs.save("ch01s1", "<p>起点，第一句，第二句</p>").catch(() => {});
    await vi.waitFor(() => expect(checking).toBe(true), T);
    const entry = mod.WrRecovery.create({ sid: "ch01s1", html: "<p>要恢复的那一稿</p>", type: "unsynced", reason: "断网", label: "场景 ch01s1 · 未同步稿" });
    const restoring = mod.WrRecovery.restore(entry.id).catch((error) => error);
    await tick(40);
    hold.resolve();
    const failure = await restoring;
    expect(failure).toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    expect(failure.message).toContain("编辑器已换成服务端的最新版本");
    expect(events.filter((event) => event.kind === "conflict-resolved").map((event) => event.html)).toEqual(["<p>另一台设备的正文</p>"]);
    expect(recoveryHtml(mod)).toEqual(expect.arrayContaining(["<p>起点，第一句，第二句</p>", "<p>要恢复的那一稿</p>"]));
    expect(shared.content).toBe("<p>另一台设备的正文</p>");
  });
});

describe("复核二 · 提升只提升作者眼前的那一稿（W1-R2A-4）", () => {
  it("R2-B2 打开时水合没成、编辑器是这台电脑的旧缓存，点提升：先水合，读到别处更新的版本就不提升，编辑器换成它；看过之后再提升的就是它", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>这台电脑上的旧稿</p>");
    const { mod, client, events, server, routeEnsure } = await loadDocs();
    server.revision = 4;
    server.content = "<p>另一台设备上改过的一版</p>";
    let first = true;
    routeEnsure((current) => {
      if (!first) return current();
      first = false;
      return Promise.reject(offlineError());
    });
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>这台电脑上的旧稿</p>");
    await tick();
    expect(mod.WrDocs.state("ch01s1").revision).toBe(0);                  // 水合没成
    await expect(mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged" })).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    expect(client.apiPost.mock.calls.some(([url]) => /promote-canonical$/.test(url))).toBe(false);
    expect(events.filter((event) => event.kind === "loaded").map((event) => event.html)).toEqual(["<p>另一台设备上改过的一版</p>"]);
    await mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged" });
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/author-drafts/d1/promote-canonical", expect.objectContaining({ base_revision_no: 4 }));
  });

  it("服务端是 ensure 刚建的空稿、本机缓存里有这一场上次没同步上的字：它是还没同步上的工作稿（不提升那份空稿）；冲刷时传上去，提升的就是它", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>这台电脑上没同步上的一段</p>");
    window.localStorage.setItem("wr-doc-pending:ch01s1::prj-main", String(Date.now()));
    const { mod, client, server } = await loadDocs();
    client.apiPatch.mockImplementation(casPatch(server));
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>这台电脑上没同步上的一段</p>");
    await vi.waitFor(() => expect(mod.WrDocs.state("ch01s1")).toMatchObject({
      dirty: true, lastSaveError: expect.objectContaining({ code: "AUTHOR_DRAFT_NOT_SYNCED" }),
    }), T);
    expect(client.apiPatch).not.toHaveBeenCalled();                 // 水合本身不发请求
    await expect(mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged" })).rejects.toMatchObject({ code: "AUTHOR_DRAFT_NOT_SYNCED" });
    expect(client.apiPost.mock.calls.some(([url]) => /promote-canonical$/.test(url))).toBe(false);
    // 写作台提升之前先冲刷：工作稿传上去，提升的就是它
    await expect(mod.WrDocs.flush("ch01s1")).resolves.toBe("saved");
    expect(patches(client)).toEqual([{ content: "<p>这台电脑上没同步上的一段</p>", base_revision_no: 1 }]);
    expect(window.localStorage.getItem(pendingKey())).toBeNull();
    await mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged" });
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/author-drafts/d1/promote-canonical", expect.objectContaining({ base_revision_no: 2 }));
    expect(mod.WrRecovery.list()).toEqual([]);
  });
});

describe("复核二 · 保存失败后停着的一稿", () => {
  it("重新联网 / 窗口重新聚焦时再发一次：发的是停着的最新一稿，不是更早的", async () => {
    const { mod, client } = await loadDocs();
    const first = deferred();
    client.apiPatch.mockImplementationOnce(() => first.promise);
    const a = mod.WrDocs.save("ch01s1", "<p>一</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    const b = mod.WrDocs.save("ch01s1", "<p>一二</p>");
    first.reject(offlineError());
    await expect(a).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await expect(b).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await tick();
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new Event("online"));
    await vi.waitFor(() => expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, lastSaveError: null }), T);
    expect(patches(client).map((body) => body.content)).toEqual(["<p>一</p>", "<p>一二</p>"]);
    expect(window.localStorage.getItem(pendingKey())).toBeNull();
  });
});

/* ==========================================================
   复核三（W1 第三轮复核）：两路复核报出来的顺序改成的永久用例，每一条断言的都是安全 / 照实的结果。
   ========================================================== */

const AI_DRAFT = [{ id: "p1", parts: [{ text: "雨城的夜里，林昭把旧信收进了案卷。" }] }];
const AI_DRAFT_HTML = "<p>雨城的夜里，林昭把旧信收进了案卷。</p>";
const draftPatches = (client) => client.apiPatch.mock.calls.filter(([url]) => /\/author-drafts\//.test(url)).map(([, body]) => body);
const otherTabAlerts = () => alertTexts().filter((message) => message.includes("另一个标签页"));
const chapterCopy = () => ({ ...DEFAULT_CHAP, scenes: DEFAULT_CHAP.scenes.map((scene) => ({ ...scene })) });

describe("复核三 · 断网写了几分钟：回包丢了的自己那一稿不被挤掉（W1-R3A-2）", () => {
  it("R3A-1 自己那一稿存上了、回包丢了，之后又断网失败了四次，连上之后撞上 409：认出是自己那一稿，不开冲突、不提示「别处被修改」，最新的一稿存上", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>起点</p>");
    const shared = sharedServer("<p>起点</p>");
    const { mod, events } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    shared.hooks.patch = (body, api) => { shared.hooks.patch = null; return api.applyLost(body); };
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，一</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(shared).toMatchObject({ revision: 2, content: "<p>起点，一</p>" });
    // 连接一直断着：又写了四稿，一稿也没到服务端
    for (const html of ["<p>起点，一，二</p>", "<p>起点，一，二，三</p>", "<p>起点，一，二，三，四</p>", "<p>起点，一，二，三，四，五</p>"]) {
      shared.hooks.patch = () => { shared.hooks.patch = null; return Promise.reject(offlineError()); };
      // eslint-disable-next-line no-await-in-loop
      await expect(mod.WrDocs.save("ch01s1", html)).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    }
    // 连上了：下一稿带着 rev 1 撞上 409 {2}——服务端上的是作者自己那第一稿
    const outcome = await mod.WrDocs.save("ch01s1", "<p>起点，一，二，三，四，五，六</p>").then(() => "saved", (e) => e && e.code);
    await tick(100);
    expect(outcome).toBe("saved");
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(elsewhereAlerts()).toEqual([]);
    expect(shared.content).toBe("<p>起点，一，二，三，四，五，六</p>");
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，一，二，三，四，五，六</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, conflictPending: false, lastSaveError: null });
  });
});

describe("复核三 · 保存失败重新标未同步时，共用读缓存里已经是别的字（W1-R3A-5 · W1-R3B-7）", () => {
  it("R3A-2 B 页 5xx 停着，A 页随后存上了；B 页联网重发又 5xx、接着写：不把 A 页存上的字当成另一个标签页没同步上的", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>起点</p>");
    const shared = sharedServer("<p>起点</p>");
    const tabA = await openTab(shared);
    tabA.mod.WrDocs.load("ch01s1");
    await tabA.mod.WrDocs.hydrate("ch01s1");
    vi.resetModules();
    const tabB = await openTab(shared);
    tabB.mod.WrDocs.load("ch01s1");
    await tabB.mod.WrDocs.hydrate("ch01s1");
    shared.hooks.patch = () => { shared.hooks.patch = null; return Promise.reject(serverError()); };
    await expect(tabB.mod.WrDocs.save("ch01s1", "<p>起点，B 页写的</p>")).rejects.toMatchObject({ code: "DATABASE_ERROR" });
    // A 页存上：B 页没同步上的那一句先留进同步与恢复（这一句提示是真的）
    await tabA.mod.WrDocs.save("ch01s1", "<p>起点，A 页写的</p>");
    expect(shared).toMatchObject({ revision: 2, content: "<p>起点，A 页写的</p>" });
    expect(otherTabAlerts()).toHaveLength(1);
    // B 页联网：停着的那一稿重发，又是 5xx（等它真的失败了）；B 页接着写
    shared.hooks.patch = () => { shared.hooks.patch = null; return Promise.reject(serverError()); };
    window.dispatchEvent(new Event("online"));
    // （两个标签页共用同一个 client 替身：数 B 页那一稿发了几次）
    await vi.waitFor(() => expect(draftPatches(tabB.client).filter((body) => body.content === "<p>起点，B 页写的</p>")).toHaveLength(2), T);
    await vi.waitFor(() => expect(tabB.mod.WrDocs.state("ch01s1")).toMatchObject({
      saving: false, lastSaveError: expect.objectContaining({ code: "DATABASE_ERROR" }),
    }), T);
    await tabB.mod.WrDocs.save("ch01s1", "<p>起点，B 页写的，接着写</p>").catch(() => {});
    await tick(200);
    const unsynced = tabB.mod.WrRecovery.list().filter((entry) => entry.type === "unsynced").map((entry) => entry.html);
    expect(unsynced).not.toContain("<p>起点，A 页写的</p>");     // A 页的字在服务端上，不是没同步上的
    expect(otherTabAlerts()).toHaveLength(1);                        // 没有第二句（假的）「另一个标签页」
    const kept = (needle) => Object.keys(window.localStorage).some((key) => String(window.localStorage.getItem(key)).includes(needle))
      || tabB.mod.WrRecovery.list().some((entry) => entry.html.includes(needle));
    expect(kept("B 页写的，接着写")).toBe(true);
    expect(shared.content).toBe("<p>起点，A 页写的</p>");
  });

  it("NS3-2 B 页的保存还在路上时 A 页打开这一场（水合把服务端版本写进共用读缓存）、B 页随后 5xx：B 页接着写时不把服务端版本当成另一个标签页的字", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>起点</p>");
    const shared = sharedServer("<p>起点</p>");
    const tabB = await openTab(shared);
    tabB.mod.WrDocs.load("ch01s1");
    await tabB.mod.WrDocs.hydrate("ch01s1");
    const gate = deferred();
    shared.hooks.patch = () => gate.promise;
    const bFirst = tabB.mod.WrDocs.save("ch01s1", "<p>起点，B 页写的一句</p>").then(() => "saved", (e) => e && e.code);
    await tick(30);
    vi.resetModules();
    const tabA = await openTab(shared);
    tabA.mod.WrDocs.load("ch01s1");
    await tabA.mod.WrDocs.hydrate("ch01s1");
    await tick(30);
    const alertsBefore = alertTexts().length;
    shared.hooks.patch = null;
    gate.reject(serverError());
    expect(await bFirst).toBe("DATABASE_ERROR");
    await tabB.mod.WrDocs.save("ch01s1", "<p>起点，B 页写的一句，接着写</p>").catch(() => {});
    await tick(60);
    expect(alertTexts().slice(alertsBefore).filter((message) => message.includes("另一个标签页"))).toEqual([]);
    expect(tabB.mod.WrRecovery.list().filter((entry) => entry.type === "unsynced" && entry.html === "<p>起点</p>")).toEqual([]);
    const newest = shared.content === "<p>起点，B 页写的一句，接着写</p>"
      || Object.keys(window.localStorage).some((key) => String(window.localStorage.getItem(key)).includes("B 页写的一句，接着写"));
    expect(newest).toBe(true);
  });
});

describe("复核三 · 在上次会话没同步上的本机稿上接着写（W1-R3A-3）", () => {
  const LOCAL = "<p>起点，上次会话没存上的一句</p>";
  const TYPED = "<p>起点，上次会话没存上的一句，今天接着写</p>";

  it("P-4 旧版未同步标记（没说写在哪一版上）、水合回来之前就在它上面接着写、服务端版本和它对不上：提示照实说是上次会话的本机稿，不说「在别处被修改过」；两份本机稿都在同步与恢复，不发 PATCH", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", LOCAL);
    window.localStorage.setItem("wr-doc-pending:ch01s1::prj-main", String(Date.now()));
    const { mod, client, events, server, routeEnsure } = await loadDocs();
    server.content = "<p>起点</p>";
    const gate = deferred();
    routeEnsure((current) => gate.promise.then(current));
    expect(mod.WrDocs.load("ch01s1")).toBe(LOCAL);
    const saving = mod.WrDocs.save("ch01s1", TYPED).then(() => "saved", (e) => e && e.code);
    routeEnsure(null);
    gate.resolve();
    expect(await saving).toBe("AUTHOR_DRAFT_CONFLICT");
    await tick();
    expect(elsewhereAlerts()).toEqual([]);
    expect(alertTexts().some((message) => message.includes("上次会话"))).toBe(true);
    expect(events.filter((event) => event.kind === "conflict-resolved").map((event) => event.html)).toEqual(["<p>起点</p>"]);
    expect(recoveryHtml(mod)).toEqual(expect.arrayContaining([LOCAL, TYPED]));
    expect(draftPatches(client)).toEqual([]);
  });

  it("上次会话断网没存上的本机稿、未同步标记说它就写在服务端眼下这一版上：水合回来之前接着写的字就是这一版的下一稿，照常存上；不开冲突、不提示、不进同步与恢复", async () => {
    const shared = sharedServer("<p>起点</p>");
    const first = await openTab(shared);
    await first.mod.WrDocs.hydrate("ch01s1");
    shared.hooks.patch = () => Promise.reject(offlineError());
    await expect(first.mod.WrDocs.save("ch01s1", LOCAL)).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    shared.hooks.patch = null;
    // 刷新（还是断网时的样子）：这一页的内存没了，本机缓存 + 未同步标记还在；服务端还停在 rev 1
    vi.resetModules();
    const gate = deferred();
    shared.hooks.ensure = (current) => gate.promise.then(current);
    const second = await openTab(shared);
    expect(second.mod.WrDocs.load("ch01s1")).toBe(LOCAL);
    const saving = second.mod.WrDocs.save("ch01s1", TYPED).then(() => "saved", (e) => e && e.code);
    shared.hooks.ensure = null;
    gate.resolve();
    expect(await saving).toBe("saved");
    expect(shared).toMatchObject({ revision: 2, content: TYPED });
    expect(second.events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(alertTexts()).toEqual([]);
    expect(second.mod.WrRecovery.list()).toEqual([]);
    expect(window.localStorage.getItem(pendingKey())).toBeNull();
  });

  it("未同步标记说它写在 rev 1、服务端已经到了别处存下的 rev 2：按冲突处理（本机的都进同步与恢复、不发 PATCH），提示照实说是上次会话的本机稿", async () => {
    const shared = sharedServer("<p>起点</p>");
    const first = await openTab(shared);
    await first.mod.WrDocs.hydrate("ch01s1");
    shared.hooks.patch = () => Promise.reject(offlineError());
    await expect(first.mod.WrDocs.save("ch01s1", LOCAL)).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    shared.hooks.patch = null;
    shared.revision = 2;
    shared.content = SERVER;
    vi.resetModules();
    const gate = deferred();
    shared.hooks.ensure = (current) => gate.promise.then(current);
    const second = await openTab(shared);
    second.mod.WrDocs.load("ch01s1");
    const saving = second.mod.WrDocs.save("ch01s1", TYPED).then(() => "saved", (e) => e && e.code);
    shared.hooks.ensure = null;
    gate.resolve();
    expect(await saving).toBe("AUTHOR_DRAFT_CONFLICT");
    await tick();
    expect(shared).toMatchObject({ revision: 2, content: SERVER });
    expect(second.events.filter((event) => event.kind === "conflict-resolved").map((event) => event.html)).toEqual([SERVER]);
    expect(recoveryHtml(second.mod)).toEqual(expect.arrayContaining([LOCAL, TYPED]));
    expect(elsewhereAlerts()).toEqual([]);
    expect(alertTexts().some((message) => message.includes("上次会话"))).toBe(true);
  });
});

describe("复核三 · 服务端的历史往回走了（库从备份恢复，这一页没刷新）（W1-R3B-1）", () => {
  async function restoredBackup() {
    const shared = sharedServer("<p>起点</p>");
    const tab = await openTab(shared);
    tab.mod.WrDocs.load("ch01s1");
    await tab.mod.WrDocs.hydrate("ch01s1");
    await tab.mod.WrDocs.save("ch01s1", "<p>起点，一</p>");            // rev 2
    await tab.mod.WrDocs.save("ch01s1", "<p>起点，一，二</p>");        // rev 3
    shared.revision = 2;                                                 // 库从备份恢复：rev 2 又是当前的那一版
    shared.content = "<p>起点，一</p>";
    return { ...tab, shared };
  }

  it("NB-1 409 说服务端眼下是 rev 2（比撞上的那一次还旧）：读到 rev 2 就换上，本机稿进同步与恢复，不一直「读不到」；之后接着写照常存上", async () => {
    const { mod, events, shared } = await restoredBackup();
    mod.WrDocs.load("ch01s1");                                           // 回到这一场：后台复核读到更旧的 rev 2，不理
    await tick(100);
    const first = await mod.WrDocs.save("ch01s1", "<p>起点，一，二，三</p>").then(() => "saved", (e) => e && e.code);
    expect(first).toBe("AUTHOR_DRAFT_CONFLICT");
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === "<p>起点，一</p>")).toBe(true), T);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: false, revision: 2, lastSaveError: null });
    expect(alertTexts().some((message) => message.includes("暂时读不到"))).toBe(false);
    expect(recoveryHtml(mod)).toContain("<p>起点，一，二，三</p>");
    await mod.WrDocs.save("ch01s1", "<p>起点，一，接着写</p>");
    expect(shared).toMatchObject({ revision: 3, content: "<p>起点，一，接着写</p>" });
  });

  it("409 没说服务端眼下是哪一版、读回来的一轮一轮都是同一个更旧的版本：几轮之后信它、换上它，保存恢复", async () => {
    const { mod, events, shared } = await restoredBackup();
    shared.hooks.patch = () => { shared.hooks.patch = null; return Promise.reject(conflictError()); }; // 没带 current_revision_no
    // 每一轮读不到，WrDocs 记一条警告：等上一轮真的结束了再触发下一轮
    const rounds = () => console.warn.mock.calls.filter(([message]) => String(message).includes("冲突之后读取服务端版本失败")).length;
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，一，二，三</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(rounds()).toBe(1), T);
    expect(alertTexts().some((message) => message.includes("暂时读不到"))).toBe(true);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: true });
    window.dispatchEvent(new Event("focus"));                            // 第二轮：还是 rev 2
    await vi.waitFor(() => expect(rounds()).toBe(2), T);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: true });
    window.dispatchEvent(new Event("focus"));                            // 第三轮：还是 rev 2——信它
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === "<p>起点，一</p>")).toBe(true), T);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: false, revision: 2, lastSaveError: null });
    expect(recoveryHtml(mod)).toContain("<p>起点，一，二，三</p>");
    await mod.WrDocs.save("ch01s1", "<p>起点，一，接着写</p>");
    expect(shared).toMatchObject({ revision: 3, content: "<p>起点，一，接着写</p>" });
  });
});

describe("复核三 · 服务端明确拒绝的保存：不停着、不重发，拒掉的字进同步与恢复，换回服务端上的正文（W1-R3B-2）", () => {
  it("NS3-3 章在别处批准锁定、自动保存被拒（CHAPTER_APPROVED_LOCKED）：换回服务端上的正文（force），拒掉的字进同步与恢复，状态不再是保存失败；目录知道之后 load 读到的是终稿正文，flush / 聚焦都不再发", async () => {
    const chap = chapterCopy();
    const shared = sharedServer("<p>起点</p>");
    const { mod, client, events } = await openTab(shared, { opts: { catalog: [chap] } });
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    await mod.WrDocs.save("ch01s1", "<p>起点，一</p>");                // rev 2：之后被批准的就是它
    shared.locked = true;                                                // 在另一台设备上批准了
    const refused = await mod.WrDocs.save("ch01s1", "<p>起点，一，批准之后才到的一句</p>").then(() => "saved", (e) => e && e.code);
    expect(refused).toBe("CHAPTER_APPROVED_LOCKED");
    expect(events.filter((event) => event.kind === "loaded" && event.force).map((event) => event.html)).toEqual(["<p>起点，一</p>"]);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，一</p>");
    expect(recoveryHtml(mod)).toContain("<p>起点，一，批准之后才到的一句</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, lastSaveError: null, conflictPending: false });
    expect(alertTexts().some((message) => message.includes("已批准锁定") && message.includes("同步与恢复"))).toBe(true);
    chap.state = "approved";
    await window.WsCatalog.__refresh();
    await vi.waitFor(() => expect(mod.WrDocs.locked("ch01s1")).toBe(true), T);
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>起点，一</p>");
    window.dispatchEvent(new Event("focus"));
    await tick(100);
    await expect(mod.WrDocs.flush("ch01s1")).resolves.toBe("saved");
    expect(draftPatches(client)).toHaveLength(2);
    expect(shared.content).toBe("<p>起点，一</p>");
  });

  it("这份作者稿已不是当前的一份（AUTHOR_DRAFT_NOT_CURRENT）：拒掉的字进同步与恢复，重新水合读到眼下那一份，之后的保存存到它上面", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod, events } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    shared.draftId = "d2";                                               // 服务端换了一份当前作者稿
    shared.revision = 1;
    shared.content = "<p>服务端眼下的那一份</p>";
    const refused = await mod.WrDocs.save("ch01s1", "<p>起点，旧草稿上写的</p>").then(() => "saved", (e) => e && e.code);
    expect(refused).toBe("AUTHOR_DRAFT_NOT_CURRENT");
    await vi.waitFor(() => expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>服务端眼下的那一份</p>"), T);
    expect(events.some((event) => event.kind === "loaded" && event.html === "<p>服务端眼下的那一份</p>")).toBe(true);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ draftId: "d2", dirty: false, lastSaveError: null });
    expect(recoveryHtml(mod)).toContain("<p>起点，旧草稿上写的</p>");
    await mod.WrDocs.save("ch01s1", "<p>服务端眼下的那一份，接着写</p>");
    expect(shared).toMatchObject({ revision: 2, content: "<p>服务端眼下的那一份，接着写</p>" });
  });

  it("被拒的那一次带的是旧修订号（请求体太大，在比对修订号之前就被拒了），之前回包丢了的那一稿其实存上了：下一次保存撞上 409 时认得出是自己那一稿，不提示「别处被修改」（模型检查 seed 7 run 4）", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod, events } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    shared.hooks.patch = (body, api) => { shared.hooks.patch = null; return api.applyLost(body); };
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，一</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(shared.revision).toBe(2);                                     // 存上了，回包丢了
    // 之后的读取先按住：被拒之后 WrDocs 在后台再问服务端，它回来之前作者已经接着写了
    const held = deferred();
    shared.hooks.ensure = (current) => held.promise.then(current);
    shared.hooks.patch = () => {
      shared.hooks.patch = null;
      return Promise.reject(Object.assign(new Error("request body exceeds the configured size limit"), {
        code: "REQUEST_BODY_TOO_LARGE", status: 413, retryable: false, details: { max_bytes: 16 },
      }));
    };
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，一，一大段贴进来的字</p>")).rejects.toMatchObject({ code: "REQUEST_BODY_TOO_LARGE" });
    expect(recoveryHtml(mod)).toContain("<p>起点，一，一大段贴进来的字</p>");
    const saving = mod.WrDocs.save("ch01s1", "<p>起点，一，接着写</p>").then(() => "saved", (e) => e && e.code);
    await tick(50);                                                      // PATCH 带着 rev 1 出去，撞上 409 {2}，核对在等读取
    shared.hooks.ensure = null;
    held.resolve();
    expect(await saving).toBe("saved");
    await tick(50);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(elsewhereAlerts()).toEqual([]);
    expect(shared).toMatchObject({ revision: 3, content: "<p>起点，一，接着写</p>" });
  });

  it("保存 5xx 停着、之后才知道章已批准锁定：重新打开这一场时停着的那一稿进同步与恢复、读到的是终稿正文，聚焦也不再重发", async () => {
    const chap = chapterCopy();
    const shared = sharedServer("<p>起点</p>");
    const { mod, client } = await openTab(shared, { opts: { catalog: [chap] } });
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    await mod.WrDocs.save("ch01s1", "<p>起点，一</p>");
    shared.hooks.patch = () => { shared.hooks.patch = null; return Promise.reject(serverError()); };
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，一，5xx 时写的</p>")).rejects.toMatchObject({ code: "DATABASE_ERROR" });
    chap.state = "approved";
    shared.locked = true;
    await window.WsCatalog.__refresh();
    await vi.waitFor(() => expect(mod.WrDocs.locked("ch01s1")).toBe(true), T);
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>起点，一</p>");
    expect(recoveryHtml(mod)).toContain("<p>起点，一，5xx 时写的</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, lastSaveError: null });
    window.dispatchEvent(new Event("focus"));
    await tick(100);
    expect(draftPatches(client)).toHaveLength(2);
  });
});

describe("复核三 · 章已批准锁定时交进来的字（W1-R3B-3）", () => {
  it("目录说章已批准锁定：WrDocs.save 不写读缓存、不发请求，这一稿进同步与恢复并提示；写作台收到 force 的 loaded，换回终稿正文；没有新字时什么都不做", async () => {
    const chap = chapterCopy();
    const shared = sharedServer("<p>起点</p>");
    const { mod, client, events } = await openTab(shared, { opts: { catalog: [chap] } });
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    await mod.WrDocs.save("ch01s1", "<p>起点，一</p>");
    chap.state = "approved";
    await window.WsCatalog.__refresh();
    await vi.waitFor(() => expect(mod.WrDocs.locked("ch01s1")).toBe(true), T);
    const outcome = await mod.WrDocs.save("ch01s1", "<p>起点，一，最后敲下的半句</p>").then(() => "saved", (e) => e && e.code);
    expect(outcome).toBe("CHAPTER_APPROVED_LOCKED");
    expect(window.localStorage.getItem(cacheKey())).toBe("<p>起点，一</p>");
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，一</p>");
    expect(window.localStorage.getItem(pendingKey())).toBeNull();
    expect(recoveryHtml(mod)).toContain("<p>起点，一，最后敲下的半句</p>");
    expect(events.filter((event) => event.kind === "loaded" && event.force).map((event) => [event.reason, event.html])).toEqual([["locked", "<p>起点，一</p>"]]);
    expect(alertTexts().filter((message) => message.includes("已批准锁定"))).toHaveLength(1);
    expect(draftPatches(client)).toHaveLength(1);
    // 交进来的就是已存上的正文（没有新写的字）：不留、不提示、不换稿
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，一</p>")).rejects.toMatchObject({ code: "CHAPTER_APPROVED_LOCKED" });
    expect(alertTexts().filter((message) => message.includes("已批准锁定"))).toHaveLength(1);
    expect(events.filter((event) => event.kind === "loaded" && event.force)).toHaveLength(1);
    expect(draftPatches(client)).toHaveLength(1);
  });
});

describe("复核三 · 提升只提升作者确认的那一稿（W1-R3A-1 · W1-R3B-5 · W1-R3B-8）", () => {
  it("P-5b 作者确认时编辑器里是 X，确认框开着时后台复核读到了另一台设备的版本、换掉了编辑器：带着 X 去提升就拒绝（不发 promote-canonical）；看过新版本再确认它，提升的就是它", async () => {
    const shared = sharedServer("<p>起点正文</p>");
    const { mod, client, events } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    const asked = mod.WrDocs.cachedHTML("ch01s1");
    shared.revision = 2;
    shared.content = "<p>起点正文。另一台设备：他其实没有回来。</p>";
    mod.WrDocs.load("ch01s1");
    await vi.waitFor(() => expect(events.some((event) => event.kind === "loaded" && event.html === shared.content)).toBe(true), T);
    await expect(mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged", expectedText: asked })).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    expect(client.apiPost.mock.calls.some(([url]) => /promote-canonical$/.test(url))).toBe(false);
    expect(shared.promoted).toEqual([]);
    await mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged", expectedText: shared.content });
    expect(shared.promoted).toEqual([{ revision: 2, content: "<p>起点正文。另一台设备：他其实没有回来。</p>" }]);
  });

  it("P-5a 打开时第一次水合还没回来、编辑器是这台电脑的旧稿 X；确认框开着时水合落地换成了别处的版本：带着 X 去提升就拒绝", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>这台电脑上的旧稿 X</p>");
    const shared = sharedServer("<p>另一台设备上改过的一版</p>", 4);
    const gate = deferred();
    shared.hooks.ensure = (current) => gate.promise.then(current);
    const { mod, events } = await openTab(shared);
    const asked = mod.WrDocs.load("ch01s1");
    expect(asked).toBe("<p>这台电脑上的旧稿 X</p>");
    shared.hooks.ensure = null;
    gate.resolve();
    await vi.waitFor(() => expect(events.some((event) => event.kind === "loaded" && event.html === "<p>另一台设备上改过的一版</p>")).toBe(true), T);
    await expect(mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged", expectedText: asked })).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    expect(shared.promoted).toEqual([]);
  });

  it("NB-5 提升在路上时作者接着写、自动保存先到了服务端：提升被拒说的是 AUTHOR_DRAFT_MOVED_BY_SELF（不是在别处更新）；再提升一次提升的就是新的一版", async () => {
    const shared = sharedServer("<p>起点正文</p>");
    const { mod } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    await mod.WrDocs.save("ch01s1", "<p>起点正文，一</p>");                 // rev 2
    const gate = deferred();
    shared.hooks.promote = (promote) => gate.promise.then(promote);
    const promoting = mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged" }).then(() => "promoted", (e) => e && e.code);
    await tick(20);
    await mod.WrDocs.save("ch01s1", "<p>起点正文，一，二</p>");             // rev 3 先到了服务端
    expect(shared.revision).toBe(3);
    shared.hooks.promote = null;
    gate.resolve();
    expect(await promoting).toBe("AUTHOR_DRAFT_MOVED_BY_SELF");
    expect(shared.promoted).toEqual([]);
    await mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged" });
    expect(shared.promoted).toEqual([{ revision: 3, content: "<p>起点正文，一，二</p>" }]);
  });

  it("对照：提升在路上时是另一台设备存下了新的一版：照旧 AUTHOR_DRAFT_CONFLICT（在别处更新）", async () => {
    const shared = sharedServer("<p>起点正文</p>");
    const { mod } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    await mod.WrDocs.save("ch01s1", "<p>起点正文，一</p>");
    const gate = deferred();
    shared.hooks.promote = (promote) => gate.promise.then(promote);
    const promoting = mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged" }).then(() => "promoted", (e) => e && e.code);
    await tick(20);
    shared.revision = 3;
    shared.content = SERVER;
    shared.hooks.promote = null;
    gate.resolve();
    expect(await promoting).toBe("AUTHOR_DRAFT_CONFLICT");
    expect(shared.promoted).toEqual([]);
  });
});

describe("复核三 · 采纳：路上那一次保存撞上的 409 比采纳的回包先到（W1-R3A-4 · W1-R3B-6）", () => {
  it("NS3-1 服务端先存下了采纳的那一稿，慢的那一次 PATCH 随后 409、先回到浏览器：不开冲突、不提示「别处被修改」；采纳的回包到了照常换上，采纳前的字在同步与恢复", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod, client, events } = await openTab(shared);
    await mod.WrDocs.hydrate("ch01s1");
    const patchGate = deferred();
    shared.hooks.patch = (body, api) => patchGate.promise.then(() => { shared.hooks.patch = null; return api.apply(body); });
    const writerSave = mod.WrDocs.save("ch01s1", "<p>起点，采纳前作者写的</p>").then(() => "saved", (e) => e && e.code);
    await vi.waitFor(() => expect(draftPatches(client)).toHaveLength(1), T);
    const adoptAnswer = deferred();
    shared.hooks.adoptAnswer = adoptAnswer.promise;
    const api = await import("./ws-scene-api.js");
    const adopting = api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true });
    await vi.waitFor(() => expect(shared.adopted).toHaveLength(1), T);   // 服务端已经存下了，回包还在路上
    patchGate.resolve();                                                   // 慢的那一次 PATCH 这才到：409 {2}
    await tick(150);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(elsewhereAlerts()).toEqual([]);
    adoptAnswer.resolve();
    expect(await adopting).toMatchObject({ ok: true, archived: true });
    await tick(100);
    expect(await writerSave).not.toBe("saved");
    expect(shared.content).toBe(AI_DRAFT_HTML);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(AI_DRAFT_HTML);
    expect(draftPatches(client)).toHaveLength(1);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: false, dirty: false, lastSaveError: null });
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(elsewhereAlerts()).toEqual([]);
    expect(recoveryHtml(mod)).toContain("<p>起点，采纳前作者写的</p>");
  });

  it("采纳没成（服务端是另一台设备的新版本，采纳被 409 拒绝）：采纳期间按住的那一次 409 照常走冲突——本机稿进同步与恢复，编辑器换成服务端版本", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod, client, events } = await openTab(shared);
    await mod.WrDocs.hydrate("ch01s1");
    shared.revision = 2;                                                   // 另一台设备先存下了一版
    shared.content = SERVER;
    const patchGate = deferred();
    shared.hooks.patch = (body, api) => patchGate.promise.then(() => { shared.hooks.patch = null; return api.apply(body); });
    void mod.WrDocs.save("ch01s1", "<p>起点，这台电脑写的</p>").catch(() => {});
    await vi.waitFor(() => expect(draftPatches(client)).toHaveLength(1), T);
    const refuse = deferred();
    const post = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body) => (/adopt-current$/.test(url) ? refuse.promise : post(url, body)));
    const api = await import("./ws-scene-api.js");
    const adopting = api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true });
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => /adopt-current$/.test(url))).toBe(true), T);
    patchGate.resolve();                                                   // 写作台那一次 409，采纳还没有结果：先按住
    await tick(100);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    refuse.reject(casConflict(2));                                         // 采纳被拒
    expect(await adopting).toMatchObject({ ok: false });
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === SERVER)).toBe(true), T);
    expect(recoveryHtml(mod)).toContain("<p>起点，这台电脑写的</p>");
    expect(shared.content).toBe(SERVER);
    expect(draftPatches(client)).toHaveLength(1);
  });
});

describe("复核三 · 采纳前的预检把停着的一稿再发一次（W1-R3B-4）", () => {
  it("NS3-7 写作台那一稿其实存上了（回包丢了）、离场补发也断网失败，之后在起草台采纳：预检把停着的一稿再发一次，采纳一次就成", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod } = await openTab(shared);
    await mod.WrDocs.hydrate("ch01s1");
    shared.hooks.patch = (body, api) => {
      shared.hooks.patch = () => Promise.reject(offlineError());          // 后端在重启：之后的重发都失败
      return api.applyLost(body);                                          // 存上了，回包丢了
    };
    await mod.WrDocs.save("ch01s1", "<p>起点，作者写的 T1</p>").catch(() => {});
    expect(await mod.WrDocs.flush("ch01s1", { retry: true })).toBe("failed");
    expect(shared.revision).toBe(2);
    shared.hooks.patch = null;                                             // 后端好了；没有聚焦、没有联网事件
    const api = await import("./ws-scene-api.js");
    const result = await api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true });
    expect(result).toMatchObject({ ok: true, archived: true });
    expect(shared.content).toBe(AI_DRAFT_HTML);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, conflictPending: false, lastSaveError: null });
  });

  it("采纳没成、作者再试一次：作者稿一模一样，覆盖前的备份沿用上一份，不每试一次多一份", async () => {
    const shared = sharedServer("<p>起点，作者的正文</p>");
    const { mod, client } = await openTab(shared);
    await mod.WrDocs.hydrate("ch01s1");
    const post = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body) => (/adopt-current$/.test(url) ? Promise.reject(serverError()) : post(url, body)));
    const api = await import("./ws-scene-api.js");
    for (let i = 0; i < 3; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      expect(await api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true })).toMatchObject({ ok: false });
    }
    expect(mod.WrRecovery.list().filter((entry) => entry.type === "backup")).toEqual([
      expect.objectContaining({ html: "<p>起点，作者的正文</p>", source: "author", durable: true }),
    ]);
  });
});

/* ==========================================================
   W1 复核四：两路复核（lens A / lens B）在 3909a93 上复现的顺序，改成断言安全结果的永久用例（用例名带复核编号）。
   服务端同上（routeServer：PATCH / 提升 / 采纳按修订号比对，409 带 current_revision_no；回包丢了的 PATCH 同样的请求再来按键重放）。
   ========================================================== */

/* 采纳的回包丢了：服务端照常存下并提升了采纳的那一稿，浏览器只看到断网 */
function loseAdoptAnswer(client) {
  const post = client.apiPost.getMockImplementation();
  client.apiPost.mockImplementation((url, body) => (/adopt-current$/.test(url)
    ? post(url, body).then(() => Promise.reject(offlineError()))
    : post(url, body)));
}

describe("复核四 · 采纳的记号带着开始时的那一场（W1-R4A-1）", () => {
  it("R4A-1 采纳在路上时作者换到另一部作品、回包到了再换回来：采纳的那一场照常保存（PATCH 发出去、flush 有结果），另一部作品名下不多一份", async () => {
    const second = { ...DEFAULT_PROJECT, project_id: "prj-second", title: "旧港" };
    const shared = sharedServer("<p>起点，作者的正文</p>");
    const { mod, client } = await openTab(shared, { opts: { projects: [DEFAULT_PROJECT, second] } });
    const get = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (/\/projects\/prj-second\/catalog/.test(url)
      ? Promise.resolve({ chapters: [OTHER_WORK_CHAP] })
      : get(url)));
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    const answer = deferred();
    shared.hooks.adoptAnswer = answer.promise;
    const api = await import("./ws-scene-api.js");
    const adopting = api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true });
    await vi.waitFor(() => expect(shared.adopted).toHaveLength(1), T);   // 服务端存下并提升了，回包还在路上
    window.WsWorks.setActive("prj-second");                                // 作者这时换到另一部作品
    await settleActive("prj-second");
    await vi.waitFor(() => expect(window.WsCatalog.sceneById("zz01s1")).toBeTruthy(), T);
    answer.resolve();
    expect(await adopting).toMatchObject({ ok: true, archived: true });
    window.WsWorks.setActive("prj-main");                                  // ……又换回来，在采纳的那一场里接着写
    await settleActive("prj-main");
    await vi.waitFor(() => expect(window.WsCatalog.sceneById("ch01s1")).toBeTruthy(), T);
    expect(mod.WrDocs.load("ch01s1")).toBe(AI_DRAFT_HTML);
    const before = draftPatches(client).length;
    const TYPED = "<p>雨城的夜里，林昭把旧信收进了案卷。他又添了一句。</p>";
    const saving = mod.WrDocs.save("ch01s1", TYPED).then(() => "saved", (e) => (e && e.code) || "rejected");
    expect(await Promise.race([saving, tick(2000).then(() => "STILL-PENDING")])).toBe("saved");
    expect(await Promise.race([mod.WrDocs.flush("ch01s1"), tick(1000).then(() => "STILL-PENDING")])).toBe("saved");
    expect(draftPatches(client).slice(before)).toEqual([{ content: TYPED, base_revision_no: 2 }]);
    expect(shared.content).toBe(TYPED);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, saving: false, lastSaveError: null });
    expect(window.localStorage.getItem("wr-doc:ch01s1::prj-second")).toBeNull();
  });

  it("R4A-1b 同一场同时有两次采纳（起草台重新挂载后）、两次都被拒：采纳期间按住的 409 照常走冲突——写作台那一稿有结果，flush 不停在路上", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod, client, events } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    const patchGate = deferred();
    shared.hooks.patch = (body, api) => patchGate.promise.then(() => { shared.hooks.patch = null; return api.apply(body); });
    const WRITER = "<p>起点，写作台最后一次自动保存</p>";
    const writerSave = mod.WrDocs.save("ch01s1", WRITER).then(() => "saved", (e) => (e && e.code) || "rejected");
    await vi.waitFor(() => expect(draftPatches(client)).toHaveLength(1), T);
    const refusals = [deferred(), deferred()];
    const post = client.apiPost.getMockImplementation();
    let adoptCalls = 0;
    client.apiPost.mockImplementation((url, body) => {
      if (!/adopt-current$/.test(url)) return post(url, body);
      adoptCalls += 1;
      return refusals[adoptCalls - 1].promise;
    });
    const api = await import("./ws-scene-api.js");
    const first = api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true });
    await vi.waitFor(() => expect(adoptCalls).toBe(1), T);
    shared.revision = 2;                                                   // 另一台设备这时存了一版
    shared.content = SERVER;
    patchGate.resolve();                                                   // 写作台那一次 409 {2}：采纳在路上，先按住
    await tick(80);
    const secondAdoption = api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true });
    await vi.waitFor(() => expect(adoptCalls).toBe(2), T);
    refusals[0].reject(casConflict(2));                                    // 两次采纳都被拒（带的都是 rev 1）
    expect(await first).toMatchObject({ ok: false });
    await tick(50);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false); // 还有一次采纳在路上：按住的还按着
    refusals[1].reject(casConflict(2));
    expect(await secondAdoption).toMatchObject({ ok: false });
    expect(await Promise.race([writerSave, tick(2000).then(() => "STILL-PENDING")])).toBe("AUTHOR_DRAFT_CONFLICT");
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === SERVER)).toBe(true), T);
    expect(await Promise.race([mod.WrDocs.flush("ch01s1"), tick(1000).then(() => "STILL-PENDING")])).toBe("saved");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, conflictPending: false });
    expect(shared.content).toBe(SERVER);
    expect(recoveryHtml(mod)).toContain(WRITER);
  });
});

describe("复核四 · 未同步标记说的是它自己那一段字（W1-R4A-2）", () => {
  /* 浏览器存储满了：读缓存（wr-doc:）和恢复记录写不进去，短的未同步标记还写得进去 */
  function fillableStorage() {
    const state = { full: false };
    const realSet = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItem(key, value) {
      const k = String(key);
      if (state.full && (k.startsWith("wr-doc:") || k.startsWith("wr-recovery:v1:"))) {
        throw Object.assign(new Error("full"), { name: "QuotaExceededError" });
      }
      return realSet.call(this, key, value);
    });
    return state;
  }

  it("R4A-2 读缓存写不进去（本机存储满了）、之后又断网：下次打开时在旧读缓存上接着写，不在作者没见过的 rev 2 上保存——rev 2 上屏，刚写的进同步与恢复", async () => {
    const storage = fillableStorage();
    const shared = sharedServer("<p>起点</p>");
    const first = await openTab(shared);
    first.mod.WrDocs.load("ch01s1");
    await first.mod.WrDocs.hydrate("ch01s1");                             // 共用读缓存 = rev 1「起点」
    storage.full = true;
    await first.mod.WrDocs.save("ch01s1", "<p>起点，第一段</p>");         // 存上了（rev 2），读缓存没写进去
    expect(shared).toMatchObject({ revision: 2, content: "<p>起点，第一段</p>" });
    shared.hooks.patch = () => Promise.reject(offlineError());
    await first.mod.WrDocs.save("ch01s1", "<p>起点，第一段，第二段</p>").catch(() => {});
    expect(window.localStorage.getItem("wr-doc:ch01s1::prj-main")).toBe("<p>起点</p>");
    // 刷新（空间腾出来了、网络好了、这一次的读取慢）：编辑器装的是读缓存里的「起点」，作者在它上面接着写
    storage.full = false;
    shared.hooks.patch = null;
    vi.resetModules();
    const gate = deferred();
    shared.hooks.ensure = (current) => gate.promise.then(current);
    const second = await openTab(shared);
    expect(second.mod.WrDocs.load("ch01s1")).toBe("<p>起点</p>");
    const saving = second.mod.WrDocs.save("ch01s1", "<p>起点，打开就写</p>").then(() => "saved", (e) => e && e.code);
    shared.hooks.ensure = null;
    gate.resolve();
    expect(await saving).toBe("AUTHOR_DRAFT_CONFLICT");
    await vi.waitFor(() => expect(second.events.some((event) => event.kind === "conflict-resolved" && event.html === "<p>起点，第一段</p>")).toBe(true), T);
    expect(shared).toMatchObject({ revision: 2, content: "<p>起点，第一段</p>" });
    expect(draftPatches(second.client).filter((body) => body.content === "<p>起点，打开就写</p>")).toEqual([]);
    expect(recoveryHtml(second.mod)).toContain("<p>起点，打开就写</p>");
  });

  it("对照：存储有空间时读缓存里就是没同步上的最新一稿，标记说它写在 rev 2 上——在它上面接着写就是 rev 2 的下一稿，照常存上", async () => {
    const shared = sharedServer("<p>起点</p>");
    const first = await openTab(shared);
    first.mod.WrDocs.load("ch01s1");
    await first.mod.WrDocs.hydrate("ch01s1");
    await first.mod.WrDocs.save("ch01s1", "<p>起点，第一段</p>");
    shared.hooks.patch = () => Promise.reject(offlineError());
    await first.mod.WrDocs.save("ch01s1", "<p>起点，第一段，第二段</p>").catch(() => {});
    shared.hooks.patch = null;
    vi.resetModules();
    const gate = deferred();
    shared.hooks.ensure = (current) => gate.promise.then(current);
    const second = await openTab(shared);
    expect(second.mod.WrDocs.load("ch01s1")).toBe("<p>起点，第一段，第二段</p>");
    const saving = second.mod.WrDocs.save("ch01s1", "<p>起点，第一段，第二段，打开就写</p>").then(() => "saved", (e) => e && e.code);
    shared.hooks.ensure = null;
    gate.resolve();
    expect(await saving).toBe("saved");
    expect(shared).toMatchObject({ revision: 3, content: "<p>起点，第一段，第二段，打开就写</p>" });
    expect(second.mod.WrRecovery.list()).toEqual([]);
  });

  it("R4A-2b 两个标签页：共用读缓存上的未同步标记是另一页的（它存上了 rev 2、下一稿停着），这一页第一次水合没成、在自己那份 rev 1 上接着写：不在 rev 2 上保存，rev 2 上屏", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>起点</p>");
    const shared = sharedServer("<p>起点</p>");
    const busy = () => Promise.reject(Object.assign(new Error("database is busy"), { code: "DATABASE_BUSY", status: 503, retryable: true }));
    shared.hooks.ensure = busy;                                            // A 页打开这一场时库正忙：第一次水合没成
    const tabA = await openTab(shared);
    expect(tabA.mod.WrDocs.load("ch01s1")).toBe("<p>起点</p>");
    await tick(100);
    expect(tabA.mod.WrDocs.state("ch01s1")).toMatchObject({ draftId: null });
    shared.hooks.ensure = null;
    vi.resetModules();
    const tabB = await openTab(shared);                                    // 同一浏览器的 B 页
    tabB.mod.WrDocs.load("ch01s1");
    await tabB.mod.WrDocs.hydrate("ch01s1");
    await tabB.mod.WrDocs.save("ch01s1", "<p>起点，B 存上的一句</p>");     // rev 2
    shared.hooks.patch = () => Promise.reject(Object.assign(new Error("db busy"), { code: "DATABASE_BUSY", status: 503, retryable: true }));
    await tabB.mod.WrDocs.save("ch01s1", "<p>起点，B 改写了这一句</p>").catch(() => {}); // 停在 B 页：标记说它写在 rev 2 上
    shared.hooks.patch = null;
    const gate = deferred();
    shared.hooks.ensure = (current) => gate.promise.then(current);
    const saving = tabA.mod.WrDocs.save("ch01s1", "<p>起点，A 写的</p>").then(() => "saved", (e) => e && e.code);
    shared.hooks.ensure = null;
    gate.resolve();
    expect(await saving).toBe("AUTHOR_DRAFT_CONFLICT");
    await vi.waitFor(() => expect(tabA.events.some((event) => event.kind === "conflict-resolved" && event.html === "<p>起点，B 存上的一句</p>")).toBe(true), T);
    expect(shared).toMatchObject({ revision: 2, content: "<p>起点，B 存上的一句</p>" });
    expect(recoveryHtml(tabA.mod)).toEqual(expect.arrayContaining(["<p>起点，A 写的</p>", "<p>起点，B 改写了这一句</p>"]));
  });
});

describe("复核四 · 采纳的回包丢了（W1-R4A-4 · W1-R4B-5）", () => {
  it("R4A-3 离场冲刷的那一次排在采纳后面撞上 409、采纳的回包随后丢了：读一次服务端认出采纳已经存上——报归档成功，不提示「别处被修改」，采纳前的字在备份里", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod, client, events } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    const patchGate = deferred();
    shared.hooks.patch = (body, api) => patchGate.promise.then(() => { shared.hooks.patch = null; return api.apply(body); });
    void mod.WrDocs.save("ch01s1", "<p>起点，采纳前作者写的</p>").catch(() => {});
    await vi.waitFor(() => expect(draftPatches(client)).toHaveLength(1), T);
    const answer = deferred();
    const post = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body) => (/adopt-current$/.test(url)
      ? post(url, body).then(() => answer.promise)                           // 服务端存下并提升了，回包在路上
      : post(url, body)));
    const api = await import("./ws-scene-api.js");
    const adopting = api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true });
    await vi.waitFor(() => expect(shared.adopted).toHaveLength(1), T);
    patchGate.resolve();                                                    // 离场冲刷的那一次 409 {2}：按住
    await tick(80);
    answer.reject(offlineError());                                          // 采纳的回包丢了
    expect(await adopting).toMatchObject({ ok: true, archived: true });
    await tick(150);
    expect(elsewhereAlerts()).toEqual([]);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(shared.content).toBe(AI_DRAFT_HTML);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(AI_DRAFT_HTML);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, conflictPending: false, lastSaveError: null, revision: 2 });
    expect(recoveryHtml(mod)).toContain("<p>起点，采纳前作者写的</p>");
    await mod.WrDocs.save("ch01s1", "<p>雨城的夜里，林昭把旧信收进了案卷。接着写。</p>");
    expect(shared).toMatchObject({ revision: 3, content: "<p>雨城的夜里，林昭把旧信收进了案卷。接着写。</p>" });
  });

  it("R4-S8 写作台开着这一场、采纳的回包丢了：报归档成功，再采纳一次照常成；回到写作台接着写不提示「别处被修改」", async () => {
    const shared = sharedServer("<p>起点正文</p>");
    const { mod, client } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    const post = client.apiPost.getMockImplementation();
    let lose = true;
    client.apiPost.mockImplementation((url, body) => {
      if (!/adopt-current$/.test(url) || !lose) return post(url, body);
      lose = false;
      return post(url, body).then(() => Promise.reject(offlineError()));
    });
    const api = await import("./ws-scene-api.js");
    expect(await api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true })).toMatchObject({ ok: true, archived: true });
    const retry = await api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true });
    expect(retry).toMatchObject({ ok: true, archived: true });
    await mod.WrDocs.save("ch01s1", "<p>雨城的夜里，林昭把旧信收进了案卷。作者回来写的一句。</p>");
    await tick(100);
    expect(alertTexts().filter((message) => message.includes("别处被修改") || message.includes("别处有更新"))).toEqual([]);
    expect(shared.content).toBe("<p>雨城的夜里，林昭把旧信收进了案卷。作者回来写的一句。</p>");
  });

  it("对照：回包丢了、服务端也没存下采纳的那一稿（别处先存了一版）：照旧报没成，按住的那一次 409 照常走冲突", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod, client, events } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    const patchGate = deferred();
    shared.hooks.patch = (body, api) => patchGate.promise.then(() => { shared.hooks.patch = null; return api.apply(body); });
    void mod.WrDocs.save("ch01s1", "<p>起点，这台电脑写的</p>").catch(() => {});
    await vi.waitFor(() => expect(draftPatches(client)).toHaveLength(1), T);
    const lost = deferred();
    const post = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body) => (/adopt-current$/.test(url) ? lost.promise : post(url, body)));
    const api = await import("./ws-scene-api.js");
    const adopting = api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true });
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => /adopt-current$/.test(url))).toBe(true), T);
    shared.revision = 2;                                                    // 另一台设备存了一版，采纳根本没到服务端
    shared.content = SERVER;
    patchGate.resolve();
    await tick(80);
    lost.reject(offlineError());
    expect(await adopting).toMatchObject({ ok: false });
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved" && event.html === SERVER)).toBe(true), T);
    expect(shared.content).toBe(SERVER);
    expect(recoveryHtml(mod)).toContain("<p>起点，这台电脑写的</p>");
  });
});

describe("复核四 · 章锁定那一刻还有一次保存在路上（W1-R4A-5 · W1-R4B-3）", () => {
  it("R4A-5 那一次随后存上了：交进来的半句进同步与恢复，提示不说「已经换回」；那一次有了结果，编辑器换回已存上的正文（loaded force），之后什么都不再发", async () => {
    const chap = chapterCopy();
    const shared = sharedServer("<p>起点</p>");
    const { mod, client, events } = await openTab(shared, { opts: { catalog: [chap] } });
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    const patchGate = deferred();
    shared.hooks.patch = (body, api) => patchGate.promise.then(() => { shared.hooks.patch = null; return api.apply(body); });
    void mod.WrDocs.save("ch01s1", "<p>起点，一</p>").catch(() => {});  // 在路上（在批准之前就会存上）
    await vi.waitFor(() => expect(draftPatches(client)).toHaveLength(1), T);
    chap.state = "approved";                                                // 目录知道了章在别处批准
    await window.WsCatalog.__refresh();
    await vi.waitFor(() => expect(mod.WrDocs.locked("ch01s1")).toBe(true), T);
    const half = await mod.WrDocs.save("ch01s1", "<p>起点，一，批准前敲的半句</p>").then(() => "saved", (e) => e && e.code);
    expect(half).toBe("CHAPTER_APPROVED_LOCKED");
    const atLock = alertTexts().filter((message) => message.includes("已批准锁定"));
    expect(atLock).toHaveLength(1);
    expect(atLock[0]).toContain("同步与恢复");
    expect(atLock[0]).not.toContain("编辑器换回了");                         // 这时还没换：不说已经换了
    expect(events.filter((event) => event.kind === "loaded" && event.force)).toEqual([]);
    patchGate.resolve();                                                    // 路上那一次存上了
    await vi.waitFor(() => expect(events.filter((event) => event.kind === "loaded" && event.force)
      .map((event) => [event.reason, event.html])).toEqual([["locked", "<p>起点，一</p>"]]), T);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，一</p>");
    expect(recoveryHtml(mod)).toContain("<p>起点，一，批准前敲的半句</p>");
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, lastSaveError: null });
    expect(alertTexts().filter((message) => message.includes("已批准锁定"))).toHaveLength(1); // 换稿时不再提示一遍
    window.dispatchEvent(new Event("focus"));
    await expect(mod.WrDocs.flush("ch01s1")).resolves.toBe("saved");
    await tick(80);
    expect(draftPatches(client)).toHaveLength(1);
    expect(shared.content).toBe("<p>起点，一</p>");
  });

  it("排队的较旧一稿不在半句被拒之后补发：路上那一次失败（断网）时它留进同步与恢复、编辑器换回已存上的正文；flush / 聚焦都不再发", async () => {
    const chap = chapterCopy();
    const shared = sharedServer("<p>起点</p>");
    const { mod, client, events } = await openTab(shared, { opts: { catalog: [chap] } });
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    const patchGate = deferred();
    shared.hooks.patch = () => patchGate.promise;
    void mod.WrDocs.save("ch01s1", "<p>起点，一</p>").catch(() => {});
    await vi.waitFor(() => expect(draftPatches(client)).toHaveLength(1), T);
    void mod.WrDocs.save("ch01s1", "<p>起点，一，二</p>").catch(() => {});  // 排着
    chap.state = "approved";
    await window.WsCatalog.__refresh();
    await vi.waitFor(() => expect(mod.WrDocs.locked("ch01s1")).toBe(true), T);
    await mod.WrDocs.save("ch01s1", "<p>起点，一，二，锁定后敲的半句</p>").catch(() => {});
    shared.hooks.patch = null;
    patchGate.reject(offlineError());                                       // 路上那一次断网
    await vi.waitFor(() => expect(events.filter((event) => event.kind === "loaded" && event.force)
      .map((event) => [event.reason, event.html])).toEqual([["locked", "<p>起点</p>"]]), T);
    expect(recoveryHtml(mod)).toEqual(expect.arrayContaining(["<p>起点，一，二</p>", "<p>起点，一，二，锁定后敲的半句</p>"]));
    await expect(mod.WrDocs.flush("ch01s1", { retry: true })).resolves.toBe("saved");
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("online"));
    await tick(100);
    expect(draftPatches(client).map((body) => body.content)).toEqual(["<p>起点，一</p>"]);
    expect(shared).toMatchObject({ revision: 1, content: "<p>起点</p>" });
  });
});

describe("复核四 · 同一个修订号上换了一版（库从备份恢复、别处又存到了这个修订号）（W1-R4B-2）", () => {
  it("R4-S2 回到这一场时后台复核读到同一个修订号上另一台设备的字：换稿（loaded），提升照实拒绝；在它上面接着写的存上，另一台设备的字还在", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod, events } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    await mod.WrDocs.save("ch01s1", "<p>起点，A 一</p>");                  // rev 2
    await mod.WrDocs.save("ch01s1", "<p>起点，A 一，A 二</p>");            // rev 3
    const OTHER = "<p>起点，B 一，B 二：他其实没有回来。</p>";
    shared.revision = 3;                                                    // 库从 rev 1 的备份恢复，另一台设备随后又存了两版
    shared.content = OTHER;
    const before = events.length;
    mod.WrDocs.load("ch01s1");                                              // 回到这一场
    await vi.waitFor(() => expect(events.slice(before).filter((event) => event.kind === "loaded")
      .map((event) => [event.reason, event.html])).toEqual([["server", OTHER]]), T);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe(OTHER);
    await expect(mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged", expectedText: "<p>起点，A 一，A 二</p>" }))
      .rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    expect(shared.promoted).toEqual([]);
    await mod.WrDocs.save("ch01s1", "<p>起点，B 一，B 二：他其实没有回来。A 三</p>");
    expect(shared).toMatchObject({ revision: 4, content: "<p>起点，B 一，B 二：他其实没有回来。A 三</p>" });
  });
});

describe("复核四 · 采纳前的预检把已水合的干净一场再读一次（W1-R4B-4）", () => {
  it("R4-S4 写作台打开过这一场、另一台设备随后存了一版，之后在起草台采纳：第一次就成，覆盖前备份的是服务端眼下的那一版", async () => {
    const shared = sharedServer("<p>起点正文</p>");
    const { mod } = await openTab(shared);
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    shared.revision = 2;                                                    // 另一台设备
    shared.content = "<p>起点正文，另一台设备写的一句。</p>";
    const api = await import("./ws-scene-api.js");
    const result = await api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true });
    expect(result).toMatchObject({ ok: true, archived: true });
    expect(result.authorBackup).toMatchObject({ html: "<p>起点正文，另一台设备写的一句。</p>", durable: true });
    expect(shared.adopted).toEqual([{ revision: 3, content: AI_DRAFT_HTML }]);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ revision: 3, dirty: false });
  });
});

describe("复核四 · 水合总是读一次服务端（W1-R4B-6）", () => {
  it("R4-S3 成稿中心先看过这一场的版本对比、另一台设备随后存了 rev 2：第一次在写作台打开这一场就换成 rev 2，不用那份旧快照", async () => {
    window.localStorage.setItem("wr-doc:ch01s1::prj-main", "<p>起点</p>");
    const shared = sharedServer("<p>起点</p>");
    const { mod, events } = await openTab(shared);
    await mod.WrDocVersions.list("ch01s1");                                  // 吸收了 rev 1 的快照，没有水合
    shared.revision = 2;
    shared.content = "<p>起点，另一台设备：码头的名字改了。</p>";
    expect(mod.WrDocs.load("ch01s1")).toBe("<p>起点</p>");
    await vi.waitFor(() => expect(events.filter((event) => event.kind === "loaded").map((event) => event.html))
      .toEqual(["<p>起点，另一台设备：码头的名字改了。</p>"]), T);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ revision: 2 });
    await mod.WrDocs.save("ch01s1", "<p>起点，另一台设备：码头的名字改了。作者接着写。</p>");
    expect(shared).toMatchObject({ revision: 3, content: "<p>起点，另一台设备：码头的名字改了。作者接着写。</p>" });
    expect(elsewhereAlerts()).toEqual([]);
  });

  it("R4-S3b 两个标签页：X 页看过版本对比、Y 页随后存了 rev 2，X 页打开这一场：编辑器和共用读缓存都不退回 rev 1", async () => {
    const shared = sharedServer("<p>起点</p>");
    const tabX = await openTab(shared);
    await tabX.mod.WrDocVersions.list("ch01s1");
    vi.resetModules();
    const tabY = await openTab(shared);
    tabY.mod.WrDocs.load("ch01s1");
    await tabY.mod.WrDocs.hydrate("ch01s1");
    await tabY.mod.WrDocs.save("ch01s1", "<p>起点，Y 页刚写好的一句</p>");
    const before = tabX.events.length;
    expect(tabX.mod.WrDocs.load("ch01s1")).toBe("<p>起点，Y 页刚写好的一句</p>");
    await tabX.mod.WrDocs.hydrate("ch01s1");
    await tick(100);
    expect(tabX.events.slice(before).filter((event) => event.kind === "loaded")).toEqual([]);
    expect(tabX.mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，Y 页刚写好的一句</p>");
    expect(tabX.mod.WrDocs.state("ch01s1")).toMatchObject({ revision: 2 });
    expect(window.localStorage.getItem("wr-doc:ch01s1::prj-main")).toBe("<p>起点，Y 页刚写好的一句</p>");
  });
});

describe("复核四 · 提升途中自己的自动保存先到了服务端、回包却丢了（W1-R4B-7）", () => {
  async function promoteRacingLostSave(shared, mod, { stillDown = false } = {}) {
    mod.WrDocs.load("ch01s1");
    await mod.WrDocs.hydrate("ch01s1");
    await mod.WrDocs.save("ch01s1", "<p>起点，一</p>");                     // rev 2
    const gate = deferred();
    shared.hooks.promote = (promote) => gate.promise.then(promote);
    const promoting = mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged", expectedText: "<p>起点，一</p>" })
      .then(() => "promoted", (e) => e && e.code);
    await tick(30);
    shared.hooks.patch = (body, api) => {
      shared.hooks.patch = stillDown ? () => Promise.reject(offlineError()) : null;
      return api.applyLost(body);                                          // 存上了（rev 3），回包丢了
    };
    await mod.WrDocs.save("ch01s1", "<p>起点，一，二</p>").catch(() => {});
    expect(shared.revision).toBe(3);
    shared.hooks.promote = null;
    gate.resolve();
    return promoting;
  }

  it("R4-S9 提升被拒不说「在别处更新」：把停着的那一稿再发一次（服务端按键重放），认出是自己存上的——AUTHOR_DRAFT_MOVED_BY_SELF；再提升一次提升的是新的那一版", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod } = await openTab(shared);
    expect(await promoteRacingLostSave(shared, mod)).toBe("AUTHOR_DRAFT_MOVED_BY_SELF");
    expect(shared.promoted).toEqual([]);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, revision: 3, lastSaveError: null });
    await mod.WrDocs.promote("ch01s1", { narrativeEffect: "facts_unchanged", expectedText: "<p>起点，一，二</p>" });
    expect(shared.promoted).toEqual([{ revision: 3, content: "<p>起点，一，二</p>" }]);
  });

  it("再发也没成（还断着）：说草稿还没确认存上（AUTHOR_DRAFT_UNSAVED），不说在别处更新", async () => {
    const shared = sharedServer("<p>起点</p>");
    const { mod } = await openTab(shared);
    expect(await promoteRacingLostSave(shared, mod, { stillDown: true })).toBe("AUTHOR_DRAFT_UNSAVED");
    expect(shared.promoted).toEqual([]);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，一，二</p>");
  });
});
