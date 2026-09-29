// WrDocs 保存状态机的 store 层单测（W1）：一场一台状态机——本机缓存即时落地、一次只有一个 PATCH、
// 排队只留最新一稿、失败留在本机待重发、409 的冲突副本 + 读服务端版本 + 读不到时拒绝保存、水合共用一次、
// 已水合的场后台复核、两个标签页。房间级的保存顺序见 ws-writer-doc-saves.test.jsx。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_PROJECT, installApiRouter } from "./test-helpers.js";

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

  it("冲突之后读回来的修订号比撞上的那一次还旧（幂等重放的旧回包）：不当成服务端版本，保持冲突，稍后再读", async () => {
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
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("暂时读不到")), T);
    expect(events.some((event) => event.kind === "conflict-resolved")).toBe(false);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: true, revision: 1 });
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

  it("核对时读不到服务端：按冲突处理——本机稿先进同步与恢复，读到之前不再发保存", async () => {
    const { mod, client, server, routeEnsure } = await loadDocs();
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
    await expect(mod.WrDocs.save("ch01s1", "<p>第一句，第二句</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(mod.WrDocs.state("ch01s1").conflictPending).toBe(true), T);
    expect(recoveryHtml(mod)).toContain("<p>第一句，第二句</p>");
    await expect(mod.WrDocs.save("ch01s1", "<p>第一句，第二句，第三句</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    expect(client.apiPatch).toHaveBeenCalledTimes(2);
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
