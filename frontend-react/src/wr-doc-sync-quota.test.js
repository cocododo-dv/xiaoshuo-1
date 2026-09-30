// W1 复核二 · W1-R2B-2（NS-Q）：同步与恢复的本机存储满了时，本机缓存里那一份没同步上的本机稿是它唯一的持久副本——
// 提示说它「本机缓存里也还留着一份，直到这一场再保存」，那么在作者再保存之前，后台的复核 / 水合不得把它盖掉。
// 单独一个文件：同一文件里前一个用例留下的旧 store 实例可能在下一个用例开头水合这一场、改写预先放好的读缓存。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

const T = { timeout: 6000, interval: 20 };
const CACHE = "wr-doc:ch01s1::prj-main";
const PENDING = "wr-doc-pending:ch01s1::prj-main";
const LOCAL = "<p>起点，上次会话没存上的一段</p>";
const tick = (ms = 50) => new Promise((resolve) => setTimeout(resolve, ms));

/* 服务端：ensure 回 shared 眼下的样子（几份 store 实例共用 shared） */
async function loadDocs(shared) {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  client.apiPost.mockImplementation((url) => (/\/author-drafts\/scene\/.+\/ensure$/.test(url)
    ? Promise.resolve({ draft: { draft_id: "d1", revision_no: shared.revision, content: shared.content } })
    : Promise.resolve({})));
  client.apiPatch.mockImplementation((url, body) => {
    shared.revision += 1;
    shared.content = body.content;
    return Promise.resolve({ draft: { draft_id: "d1", revision_no: shared.revision, content: body.content } });
  });
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const mod = await import("./wr-doc-store.jsx");
  const events = [];
  mod.WrDocs.subscribe((kind, detail) => events.push({ kind, ...(detail || {}) }));
  return { mod, client, events };
}

const inStorage = (needle) => Object.keys(window.localStorage).filter((key) => String(window.localStorage.getItem(key) || "").includes(needle));

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  vi.spyOn(window, "alert").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => vi.restoreAllMocks());

describe("复核二 · 同步与恢复放不下时，本机缓存里那一份本机稿留到作者再保存（W1-R2B-2 · NS-Q）", () => {
  it("NS-Q 另一台设备又存了一版、作者只是重新打开这一场：后台复核不把本机缓存里那一份盖掉；刷新以后它还在", async () => {
    window.localStorage.setItem(CACHE, LOCAL);
    window.localStorage.setItem(PENDING, String(Date.now()));
    const shared = { revision: 3, content: "<p>另一台设备的正文</p>" };
    const realSet = Storage.prototype.setItem;
    const quota = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItem(key, value) {
      if (String(key).startsWith("wr-recovery:v1:")) throw Object.assign(new Error("full"), { name: "QuotaExceededError" });
      return realSet.call(this, key, value);
    });
    const tab = await loadDocs(shared);
    tab.mod.WrDocs.load("ch01s1");
    await tab.mod.WrDocs.hydrate("ch01s1");
    await tick();
    expect(window.alert.mock.calls.map(([message]) => String(message)).some((message) => message.includes("本机缓存里也还留着一份"))).toBe(true);
    expect(tab.mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>另一台设备的正文</p>");   // 编辑器是服务端版本
    expect(window.localStorage.getItem(CACHE)).toBe(LOCAL);
    expect(window.localStorage.getItem(PENDING)).not.toBeNull();

    // 另一台设备又存了一版；作者去了别的场、又回到这一场（没写字、没保存）
    shared.revision = 4;
    shared.content = "<p>另一台设备又改了一句</p>";
    tab.mod.WrDocs.load("ch01s1");
    await vi.waitFor(() => expect(tab.events.some((event) => event.kind === "loaded" && event.html === "<p>另一台设备又改了一句</p>")).toBe(true), T);
    expect(tab.mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>另一台设备又改了一句</p>");
    expect(window.localStorage.getItem(CACHE)).toBe(LOCAL);                      // 本机稿还在本机存储里
    expect(window.localStorage.getItem(PENDING)).not.toBeNull();
    expect(tab.mod.WrRecovery.list().filter((entry) => entry.html === LOCAL)).toHaveLength(1); // 会话里只有一份，不重复放
    expect(tab.client.apiPatch).not.toHaveBeenCalled();

    // 刷新（空间仍不足）：跨会话的路径再把它放进同步与恢复，本机存储里那一份照样留着
    vi.resetModules();
    const again = await loadDocs(shared);
    again.mod.WrDocs.load("ch01s1");
    await again.mod.WrDocs.hydrate("ch01s1");
    await tick();
    expect(inStorage("上次会话没存上的一段").length > 0 || again.mod.WrRecovery.list().some((entry) => entry.html === LOCAL)).toBe(true);
    expect(window.localStorage.getItem(CACHE)).toBe(LOCAL);

    // 腾出了空间，再刷新：它持久地进了同步与恢复，服务端版本上屏
    quota.mockRestore();
    vi.resetModules();
    const roomy = await loadDocs(shared);
    roomy.mod.WrDocs.load("ch01s1");
    await roomy.mod.WrDocs.hydrate("ch01s1");
    expect(roomy.mod.WrRecovery.list()).toEqual([expect.objectContaining({ html: LOCAL, durable: true })]);
    expect(window.localStorage.getItem(CACHE)).toBe("<p>另一台设备又改了一句</p>");
    expect(window.localStorage.getItem(PENDING)).toBeNull();
  });

  it("作者在服务端版本上接着写、保存：本机缓存换成作者的新稿（提示说过「直到这一场再保存」），那段本机稿只在本次会话的同步与恢复里", async () => {
    window.localStorage.setItem(CACHE, LOCAL);
    window.localStorage.setItem(PENDING, String(Date.now()));
    const shared = { revision: 3, content: "<p>另一台设备的正文</p>" };
    const realSet = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItem(key, value) {
      if (String(key).startsWith("wr-recovery:v1:")) throw Object.assign(new Error("full"), { name: "QuotaExceededError" });
      return realSet.call(this, key, value);
    });
    const tab = await loadDocs(shared);
    tab.mod.WrDocs.load("ch01s1");
    await tab.mod.WrDocs.hydrate("ch01s1");
    await tab.mod.WrDocs.save("ch01s1", "<p>另一台设备的正文，在它上面接着写</p>");
    expect(shared.content).toBe("<p>另一台设备的正文，在它上面接着写</p>");
    expect(window.localStorage.getItem(CACHE)).toBe("<p>另一台设备的正文，在它上面接着写</p>");
    expect(tab.mod.WrRecovery.list().filter((entry) => entry.html === LOCAL)).toEqual([expect.objectContaining({ durable: false })]);
  });
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

describe("复核三 · 本机存储满了：共用读缓存里停着的是这一页自己写进去的那一份（W1-R3A-6）", () => {
  it("R3A-3 一个标签页，保存在路上时本机存储满了、作者接着写：不把这一页自己写进去的服务端版本当成另一个标签页没同步上的（不提示、不多一份未同步稿）", async () => {
    const shared = { revision: 1, content: "<p>起点</p>" };
    const tab = await loadDocs(shared);
    const hung = deferred();
    tab.client.apiPatch.mockImplementation(() => hung.promise);
    tab.mod.WrDocs.load("ch01s1");
    await tab.mod.WrDocs.hydrate("ch01s1");
    expect(window.localStorage.getItem(CACHE)).toBe("<p>起点</p>");
    // 从这时起本机存储满了：让 wr-doc: 的值变长、多放一条恢复记录都写不进去；不变长的小写入（未同步标记）还写得进去
    const realSet = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItem(key, value) {
      const k = String(key);
      const before = this.getItem(k);
      const grows = before == null ? String(value).length > 20 : String(value).length > before.length;
      if (k.startsWith("wr-recovery:v1:") || (k.startsWith("wr-doc:") && grows)) {
        throw Object.assign(new Error("full"), { name: "QuotaExceededError" });
      }
      return realSet.call(this, key, value);
    });
    void tab.mod.WrDocs.save("ch01s1", "<p>起点，第一句写得长一些。</p>").catch(() => {});
    await vi.waitFor(() => expect(tab.client.apiPatch).toHaveBeenCalledTimes(1), T);
    void tab.mod.WrDocs.save("ch01s1", "<p>起点，第一句写得长一些。第二句。</p>").catch(() => {});
    await tick(50);
    const alerts = window.alert.mock.calls.map(([message]) => String(message));
    expect(alerts.filter((message) => message.includes("另一个标签页"))).toEqual([]);
    expect(tab.mod.WrRecovery.list().filter((entry) => entry.html === "<p>起点</p>")).toEqual([]);
    // 作者的字没丢：在这一页的会话内存里（编辑器读到的就是它）
    expect(tab.mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，第一句写得长一些。第二句。</p>");
    hung.resolve({ draft: { draft_id: "d1", revision_no: 2, content: "<p>起点，第一句写得长一些。</p>" } });
    await tick(50);
  });
});
