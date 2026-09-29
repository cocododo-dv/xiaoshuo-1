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
