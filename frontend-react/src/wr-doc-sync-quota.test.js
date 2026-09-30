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

/* 服务端：ensure 回 shared 眼下的样子（几份 store 实例共用 shared）。cas：PATCH 按修订号比对（不对就 409，带服务端眼下的修订号） */
async function loadDocs(shared, { cas = false } = {}) {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  client.apiPost.mockImplementation((url) => (/\/author-drafts\/scene\/.+\/ensure$/.test(url)
    ? Promise.resolve({ draft: { draft_id: "d1", revision_no: shared.revision, content: shared.content } })
    : Promise.resolve({})));
  client.apiPatch.mockImplementation((url, body) => {
    if (cas && Number(body.base_revision_no) !== shared.revision) {
      return Promise.reject(Object.assign(new Error("author draft has changed; refresh before saving"), {
        code: "AUTHOR_DRAFT_CONFLICT", status: 409, details: { current_revision_no: shared.revision },
      }));
    }
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

/* ==========================================================
   复核四：本机存储满了——按占用算容量（键 + 值的长度加起来不能超过这时的总量；F03-23：同步与恢复的记录没有上限，
   一个本机存储真的会被塞满）。断言的都是安全的结果：共用读缓存里不会有一份没同步上、又没有未同步标记的字
   （下次打开时它会被当成过时的读缓存、让服务端版本静默盖掉）；只留在本次会话里的字，作者当场被告知。
   ========================================================== */

function usage(store) {
  let total = 0;
  for (let i = 0; i < store.length; i += 1) {
    const key = store.key(i);
    total += key.length + String(store.getItem(key) ?? "").length;
  }
  return total;
}

/* 从这时起本机存储满了：容量 = 眼下已用的；让总量变大的写入都写不进去（写同样长的、变短的照常写） */
function armFullStorage() {
  const realSet = Storage.prototype.setItem;
  const cap = usage(window.localStorage);
  const refused = [];
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItem(key, value) {
    const k = String(key);
    const v = String(value);
    const before = this.getItem(k);
    const next = usage(this) - (before == null ? 0 : k.length + before.length) + k.length + v.length;
    if (next > cap) {
      refused.push(k);
      throw Object.assign(new Error("The quota has been exceeded."), { name: "QuotaExceededError" });
    }
    return realSet.call(this, key, value);
  });
  return { refused };
}

const OLD_RECORD = JSON.stringify({
  id: "old-1", version: 1, workId: "prj-main", sid: "ch09s9", type: "conflict", reason: "旧记录", label: "场景 ch09s9 · 冲突本地稿",
  source: "writer", createdAt: 1, html: `<p>${"很久以前的一段。".repeat(40)}</p>`,
});
const alertsSoFar = () => window.alert.mock.calls.map(([message]) => String(message));

describe("复核四 · 本机存储满了：没写进本机的字要么有未同步标记、要么当场告诉作者（W1-R4B-1 · W1-R4B-8）", () => {
  it("R4-S1 把一句改短了（读缓存写得进去）、未同步标记要新的一条键写不进去、又断网：读缓存里不留一份没有标记的新稿；作者被告知它只在本次会话里", async () => {
    const shared = { revision: 1, content: "<p>起点正文。第一句写好了。第二句写好了。第三句还要再改一改。</p>" };
    const tab = await loadDocs(shared);
    tab.mod.WrDocs.load("ch01s1");
    await tab.mod.WrDocs.hydrate("ch01s1");
    window.localStorage.setItem("wr-recovery:v1:old-1", OLD_RECORD);
    const quota = armFullStorage();
    tab.client.apiPatch.mockImplementation(() => Promise.reject(Object.assign(new Error("offline"), { code: "NETWORK_ERROR", status: 0, retryable: true })));
    const REWRITE = "<p>起点正文。第一句写好了。第二句写好了。新写的这一句。</p>";
    await expect(tab.mod.WrDocs.save("ch01s1", REWRITE)).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await tick();
    const slot = window.localStorage.getItem(CACHE);
    expect({ unmarkedUnsynced: slot === REWRITE && window.localStorage.getItem(PENDING) == null }).toEqual({ unmarkedUnsynced: false });
    expect(quota.refused).toContain(PENDING);
    expect(tab.mod.WrDocs.cachedHTML("ch01s1")).toBe(REWRITE);            // 编辑器这一页的会话内存里还是它
    expect(tab.mod.WrDocs.state("ch01s1")).toMatchObject({ localDurable: false, cacheError: expect.objectContaining({ code: "LOCAL_STORAGE_QUOTA" }) });
    expect(tab.mod.WrRecovery.list().filter((entry) => entry.html === REWRITE)).toEqual([expect.objectContaining({ durable: false })]);
    expect(alertsSoFar().filter((message) => message.includes("本次会话"))).toHaveLength(1);
    // 断网时接着自动保存：同一段失败不一遍遍提示
    await tab.mod.WrDocs.save("ch01s1", "<p>起点正文。第一句写好了。第二句写好了。新写的这一句，</p>").catch(() => {});
    await tick();
    expect(alertsSoFar().filter((message) => message.includes("本次会话"))).toHaveLength(1);
  });

  it("R4-S1b 把一句写长了（读缓存写不进去）、又断网：作者当场被告知这几句只留在本次会话的「同步与恢复」里", async () => {
    const shared = { revision: 1, content: "<p>起点正文。</p>" };
    const tab = await loadDocs(shared);
    tab.mod.WrDocs.load("ch01s1");
    await tab.mod.WrDocs.hydrate("ch01s1");
    await tab.mod.WrDocs.save("ch01s1", "<p>起点正文。第一句。</p>");       // rev 2，标记已清
    window.localStorage.setItem("wr-recovery:v1:old-1", OLD_RECORD);
    armFullStorage();
    tab.client.apiPatch.mockImplementation(() => Promise.reject(Object.assign(new Error("offline"), { code: "NETWORK_ERROR", status: 0, retryable: true })));
    const LONGER = "<p>起点正文。第一句。第二句是新写的。</p>";
    await expect(tab.mod.WrDocs.save("ch01s1", LONGER)).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await tick();
    expect(window.localStorage.getItem(CACHE)).toBe("<p>起点正文。第一句。</p>");
    expect(tab.mod.WrDocs.cachedHTML("ch01s1")).toBe(LONGER);
    expect(tab.mod.WrRecovery.list().filter((entry) => entry.html === LONGER)).toEqual([expect.objectContaining({ durable: false })]);
    const warned = alertsSoFar().filter((message) => message.includes("本次会话") && message.includes("同步与恢复"));
    expect(warned).toHaveLength(1);
  });
});

/* ==========================================================
   复核六：同一个标签页里的 NS-Q（W1-R6B-3）——这一页自己的冲突稿只放进了会话内存（同步与恢复放不下），共用读缓存里
   那一份标着未同步的本机稿就是它唯一的持久副本（openConflict 的承诺：「刷新后按跨会话的路径再留一次」）。
   ========================================================== */

describe("复核六 · 本机存储满了时这一页自己的冲突稿：作者再保存之前，后台复核不把本机缓存里那一份盖掉（W1-R6B-3）", () => {
  const MINE = "<p>起点，这一页刚写、没存上的一大段。</p>";

  it("NB6-Q 另一台设备又存了一版、作者只是重新打开这一场：本机缓存里那一份和未同步标记留着；刷新之后它按跨会话的路径再留一次", async () => {
    const shared = { revision: 1, content: "<p>起点</p>" };
    const tab = await loadDocs(shared, { cas: true });
    tab.mod.WrDocs.load("ch01s1");
    await tab.mod.WrDocs.hydrate("ch01s1");
    const realSet = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItem(key, value) {
      if (String(key).startsWith("wr-recovery:v1:")) throw Object.assign(new Error("full"), { name: "QuotaExceededError" });
      return realSet.call(this, key, value);
    });
    // 另一台设备存下了 rev 2；这一页的自动保存（带 rev 1）409 → 冲突稿只放得进会话内存
    shared.revision = 2;
    shared.content = "<p>另一台设备的正文</p>";
    await expect(tab.mod.WrDocs.save("ch01s1", MINE)).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(tab.events.some((event) => event.kind === "conflict-resolved")).toBe(true), T);
    expect(window.localStorage.getItem(CACHE)).toBe(MINE);
    expect(window.localStorage.getItem(PENDING)).not.toBeNull();
    expect(tab.mod.WrRecovery.list().filter((entry) => entry.html === MINE)).toEqual([expect.objectContaining({ durable: false })]);
    expect(window.alert.mock.calls.map(([message]) => String(message)).some((message) => message.includes("只留在本次会话"))).toBe(true);

    // 另一台设备又存了一版；作者只是重新打开这一场（没写字、没保存）
    shared.revision = 3;
    shared.content = "<p>另一台设备又改了一句</p>";
    tab.mod.WrDocs.load("ch01s1");
    await vi.waitFor(() => expect(tab.events.some((event) => event.kind === "loaded" && event.html === "<p>另一台设备又改了一句</p>")).toBe(true), T);
    expect(tab.mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>另一台设备又改了一句</p>"); // 编辑器是服务端版本
    expect(window.localStorage.getItem(CACHE)).toBe(MINE);                         // 那段本机稿还在本机存储里
    expect(window.localStorage.getItem(PENDING)).not.toBeNull();
    expect(tab.mod.WrRecovery.list().filter((entry) => entry.html === MINE)).toHaveLength(1); // 会话里只有一份，不重复放
    expect(tab.client.apiPatch).toHaveBeenCalledTimes(1);

    // 刷新（空间仍不足）：跨会话的路径再把它放进同步与恢复，本机存储里那一份照样留着
    vi.resetModules();
    const again = await loadDocs(shared, { cas: true });
    again.mod.WrDocs.load("ch01s1");
    await again.mod.WrDocs.hydrate("ch01s1");
    await tick();
    expect(again.mod.WrRecovery.list().filter((entry) => entry.html === MINE)).toHaveLength(1);
    expect(window.localStorage.getItem(CACHE)).toBe(MINE);
  });

  it("作者在服务端版本上接着写、存上了：本机缓存换成作者的新稿（提示说过它只在本次会话里），那段冲突稿还在本次会话的同步与恢复里", async () => {
    const shared = { revision: 1, content: "<p>起点</p>" };
    const tab = await loadDocs(shared, { cas: true });
    tab.mod.WrDocs.load("ch01s1");
    await tab.mod.WrDocs.hydrate("ch01s1");
    const realSet = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItem(key, value) {
      if (String(key).startsWith("wr-recovery:v1:")) throw Object.assign(new Error("full"), { name: "QuotaExceededError" });
      return realSet.call(this, key, value);
    });
    shared.revision = 2;
    shared.content = "<p>另一台设备的正文</p>";
    await tab.mod.WrDocs.save("ch01s1", MINE).catch(() => {});
    await vi.waitFor(() => expect(tab.events.some((event) => event.kind === "conflict-resolved")).toBe(true), T);
    const NEXT = "<p>另一台设备的正文，在它上面接着写</p>";
    await tab.mod.WrDocs.save("ch01s1", NEXT);
    expect(shared).toMatchObject({ revision: 3, content: NEXT });
    expect(window.localStorage.getItem(CACHE)).toBe(NEXT);
    expect(tab.mod.WrRecovery.list().filter((entry) => entry.html === MINE)).toEqual([expect.objectContaining({ durable: false })]);
  });
});

/* ==========================================================
   W1 复核七 · W1-R7A-4 · W1-R7B-3：冲突时同步与恢复放不下，这一页自己的冲突稿只进了会话（共用读缓存里那一份是唯一的持久副本）；
   之后作者腾出了空间（删掉几条旧记录）。那一稿持久地留下是对的，但它是这一页自己这一次的冲突稿——不说它是「另一个标签页
   （或上次打开时）」留下的，也不另起一条「未同步稿」。
   ========================================================== */

describe("复核七 · 冲突稿当时只放进了会话，之后腾出了空间（W1-R7A-4 · W1-R7B-3）", () => {
  const MINE = "<p>起点，这一页刚写、没存上的一大段。</p>";
  const alerts = () => window.alert.mock.calls.map(([message]) => String(message));

  async function conflictWithQuotaFull() {
    const shared = { revision: 1, content: "<p>起点</p>" };
    const tab = await loadDocs(shared, { cas: true });
    tab.mod.WrDocs.load("ch01s1");
    await tab.mod.WrDocs.hydrate("ch01s1");
    const realSet = Storage.prototype.setItem;
    const quota = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItem(key, value) {
      if (String(key).startsWith("wr-recovery:v1:")) throw Object.assign(new Error("full"), { name: "QuotaExceededError" });
      return realSet.call(this, key, value);
    });
    shared.revision = 2;                                                     // 另一台设备存了一版：这一页的自动保存 409
    shared.content = "<p>另一台设备的正文</p>";
    await tab.mod.WrDocs.save("ch01s1", MINE).catch(() => {});
    await vi.waitFor(() => expect(tab.events.some((event) => event.kind === "conflict-resolved")).toBe(true), T);
    expect(alerts().filter((message) => message.includes("只留在本次会话"))).toHaveLength(1);
    expect(tab.mod.WrRecovery.list().filter((entry) => entry.html === MINE)).toEqual([expect.objectContaining({ durable: false })]);
    return { shared, tab, quota };
  }

  const keptOnce = (tab) => tab.mod.WrRecovery.list().filter((entry) => entry.html === MINE);

  it("R7A-3 / NB7-3b 另一台设备又存了一版、作者只是重新打开：那一稿持久地留下一次（照它自己那一次冲突的原因），不说是「另一个标签页」的", async () => {
    const { shared, tab, quota } = await conflictWithQuotaFull();
    quota.mockRestore();                                                     // 作者清掉了几条旧记录，空间有了
    const before = alerts().length;
    shared.revision = 3;
    shared.content = "<p>另一台设备又改了一句</p>";
    tab.mod.WrDocs.load("ch01s1");
    await vi.waitFor(() => expect(tab.events.some((event) => event.kind === "loaded" && event.html === "<p>另一台设备又改了一句</p>")).toBe(true), T);
    expect(keptOnce(tab)).toEqual([expect.objectContaining({ durable: true, type: "conflict" })]);
    expect(keptOnce(tab)[0].reason).not.toMatch(/另一个标签页/);
    expect(alerts().slice(before).filter((message) => message.includes("另一个标签页"))).toEqual([]);
    expect(window.localStorage.getItem(CACHE)).toBe("<p>另一台设备又改了一句</p>");     // 持久地留下之后，本机缓存才换成服务端版本
    expect(window.localStorage.getItem(PENDING)).toBeNull();
  });

  it("NB7-3a 作者在服务端版本上接着写、存上了：同上——那一稿持久地留下一次，不说是「另一个标签页」的；接着写的存上了", async () => {
    const { shared, tab, quota } = await conflictWithQuotaFull();
    quota.mockRestore();
    const before = alerts().length;
    const NEXT = "<p>另一台设备的正文，在它上面接着写</p>";
    await tab.mod.WrDocs.save("ch01s1", NEXT);
    expect(shared).toMatchObject({ revision: 3, content: NEXT });
    expect(keptOnce(tab)).toEqual([expect.objectContaining({ durable: true, type: "conflict" })]);
    expect(alerts().slice(before).filter((message) => message.includes("另一个标签页"))).toEqual([]);
  });
});
