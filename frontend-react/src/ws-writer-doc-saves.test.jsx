// 写作台正文保存的房间级回归（W1 · 正文保存状态机）。
// 这里是 Q3 复核与再复核里那些「不安全的保存顺序」（lens A 的 P1…P7、lens B 的 N1…N7 与 INV-R1…R5）
// 改成的永久用例：每一条断言的都是安全的结果，不是当时观察到的坏结果。四条安全契约：
//   1. 交给 WrDocs 的正文不会被更旧的一稿盖回去：每一次 PATCH 带的，不比它发出之前已经交给 WrDocs 的任何一稿旧；
//   2. 409 之后不在作者没见过的修订号上保存：base_revision_no ≥ 3（另一台设备存下的那一版）的 PATCH
//      一定是作者在那一版上接着写的（正文里有那一版的字）；
//   3. 作者敲下的每一段字，任何时刻都至少在一处：编辑器、本机缓存、同步与恢复；
//   4. 另一台设备的版本不会没给作者看就被盖掉：冲突之后编辑器换成它。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_CHAP, installApiRouter } from "./test-helpers.js";
import { wrSerializeManuscript } from "./ws-writer-manuscript.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

const T = { timeout: 5000, interval: 25 };
const LONG = 40000;
const SERVER = "另一台设备的正文";
const CACHE_KEY = "wr-doc:ch01s1::prj-main";
const PENDING_KEY = "wr-doc-pending:ch01s1::prj-main";
const mounted = [];
const innerTextDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");
const TWO_SCENE_CHAP = {
  ...DEFAULT_CHAP,
  scenes: [...DEFAULT_CHAP.scenes, { ...DEFAULT_CHAP.scenes[0], slug: "ch01s2", scene_id: "s2", title: "回潮" }],
};

const conflict = () => Object.assign(new Error("conflict"), { code: "AUTHOR_DRAFT_CONFLICT", status: 409 });
const offline = () => Object.assign(new Error("offline"), { code: "NETWORK_ERROR", retryable: true });
const serverError = () => Object.assign(new Error("database operation failed"), { code: "DATABASE_ERROR", status: 500, retryable: true });

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

const wait = (ms) => act(async () => { await new Promise((resolve) => setTimeout(resolve, ms)); });

function matchMedia() {
  return {
    matches: false, media: "",
    addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(),
  };
}

async function loadWriter(opts) {
  const client = await import("./lib/client.js");
  installApiRouter(client, opts);
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const store = await import("./wr-doc-store.jsx");
  const writer = await import("./ws-writer.jsx");
  return { client, ...writer, ...store };
}

async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const entry = { root, host };
  mounted.push(entry);
  await act(async () => root.render(node));
  return { host, entry };
}

async function unmount(entry) {
  const i = mounted.indexOf(entry);
  if (i >= 0) mounted.splice(i, 1);
  await act(async () => entry.root.unmount());
  entry.host.remove();
}

const openScene = (sid) => act(async () => { window.dispatchEvent(new CustomEvent("ws:writer-scene", { detail: sid })); });
const patchesTo = (client, draftId) => client.apiPatch.mock.calls.filter(([url]) => url.endsWith(`/author-drafts/${draftId}`));
const okPatch = (url, body) => Promise.resolve({
  draft: { draft_id: url.split("/").pop(), revision_no: Number(body.base_revision_no) + 1, content: body.content },
});

/* s1：作者第一次保存之前，服务端是 rev 1「起点正文」；第一次 PATCH 发出之后（另一台设备同时存下了 rev 3），
   第 n 次 ensure → later(n)（默认 rev 3 另一台设备的正文）。返回的计数是那之后的 ensure 次数。s2 → d2「第二场」。
   按「有没有发过 PATCH」而不按 ensure 的次序回包：前一个用例的旧实例偶尔在下一个用例开头补一次 ensure。 */
function routeEnsures(client, later) {
  let reloads = 0;
  client.apiPost.mockImplementation((url) => {
    if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
      if (patchesTo(client, "d1").length === 0) return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "<p>起点正文</p>" } });
      reloads += 1;
      if (typeof later === "function") return later(reloads);
      return Promise.resolve({ draft: { draft_id: "d1", revision_no: 3, content: `<p>${SERVER}</p>` } });
    }
    if (/\/author-drafts\/scene\/s2\/ensure$/.test(url)) {
      return Promise.resolve({ draft: { draft_id: "d2", revision_no: 1, content: "<p>第二场</p>" } });
    }
    if (/promote-canonical$/.test(url)) return Promise.resolve({ final_scene_row_id: "f1", draft_revision_no: 3, canonical_dirty: false });
    return Promise.resolve({});
  });
  return () => reloads;
}
const rev1 = () => Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "<p>起点正文</p>" } });

/* 一间写作台 + 四条安全契约的检查。typed 按作者敲字的先后记下每一稿（编辑器序列化后的样子），用来判断「新 / 旧」。 */
function room({ client, WrDocs, WrRecovery }, host) {
  const saveSpy = vi.spyOn(WrDocs, "save");
  const typed = [];
  const editor = () => host().querySelector(".wr-editor");
  const status = () => host().querySelector('[data-testid="draft-save-status"]').textContent;
  const record = () => { typed.push(wrSerializeManuscript(editor())); };
  const versionOf = (html) => typed.indexOf(html);
  return {
    editor, status, typed,
    async type(html) {
      await act(async () => {
        editor().innerHTML = html;
        editor().dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: "" }));
      });
      record();
    },
    /* 作者在编辑器眼下显示的正文后面接着写 */
    async append(text) {
      const html = editor().innerHTML.replace(/<\/p>\s*$/, `${text}</p>`);
      await this.type(html);
    },
    /* 契约 1 */
    expectNoOlderPatch(sid = "ch01s1", draftId = "d1") {
      const events = [
        ...saveSpy.mock.calls.map((args, i) => ({ kind: "hand", sid: args[0], v: versionOf(args[1]), order: saveSpy.mock.invocationCallOrder[i] })),
        ...client.apiPatch.mock.calls.map(([url, body], i) => ({ kind: "patch", url, body, v: versionOf(body.content), order: client.apiPatch.mock.invocationCallOrder[i] })),
      ].sort((a, b) => a.order - b.order);
      let newestHanded = -1;
      events.forEach((event) => {
        if (event.kind === "hand" && event.sid === sid) newestHanded = Math.max(newestHanded, event.v);
        if (event.kind === "patch" && event.url.endsWith(`/author-drafts/${draftId}`)) {
          expect({ content: event.body.content, olderThanHanded: event.v < newestHanded })
            .toEqual({ content: event.body.content, olderThanHanded: false });
        }
      });
    },
    /* 契约 2 */
    expectServerNeverOverwritten(draftId = "d1") {
      patchesTo(client, draftId).forEach(([, body]) => {
        if (Number(body.base_revision_no) >= 3) expect(body.content).toContain(SERVER);
      });
    },
    /* 契约 3：编辑器、本机缓存（含会话内存）、同步与恢复，至少一处有这段字 */
    expectKept(needle, sid = "ch01s1") {
      const inEditor = !!(editor() && editor().textContent.includes(needle));
      let inCache = false;
      try { inCache = String(WrDocs.cachedHTML(sid) || "").includes(needle); } catch (e) {}
      const inStorage = Object.keys(window.localStorage).some((key) => String(window.localStorage.getItem(key) || "").includes(needle));
      const inRecovery = WrRecovery.list().some((entry) => String(entry.html || "").includes(needle));
      expect({ needle, kept: inEditor || inCache || inStorage || inRecovery }).toEqual({ needle, kept: true });
    },
    /* 刷新页面也丢不了：本机存储（读缓存 / 持久的恢复记录）里有 */
    expectDurable(needle) {
      const inStorage = Object.keys(window.localStorage).some((key) => String(window.localStorage.getItem(key) || "").includes(needle));
      expect({ needle, durable: inStorage }).toEqual({ needle, durable: true });
    },
    recoveryHas(needle) {
      return WrRecovery.list().some((entry) => String(entry.html || "").includes(needle));
    },
  };
}

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMedia });
  if (!innerTextDescriptor) {
    Object.defineProperty(HTMLElement.prototype, "innerText", {
      configurable: true,
      get() { return this.textContent || ""; },
      set(value) { this.textContent = value; },
    });
  }
  vi.spyOn(window, "alert").mockImplementation(() => {});
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  if (!innerTextDescriptor) delete HTMLElement.prototype.innerText;
  vi.restoreAllMocks();
});

describe("换场 / 离场时保存还在路上（P1 · P1b · P1c）", () => {
  /* P1：A（自动保存在路上）→ B → A 又写 → 定时器没到就离开（离场冲刷带着较新的一稿）→ 路上那一次断网失败，后端一直连不上 */
  it("P1 路上那一次断网失败：不会把较旧的一稿补发到较新的后面；较新的一稿留在本机缓存，回来看到的就是它", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    routeEnsures(client, rev1);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => {
      if (url.endsWith("/author-drafts/d1")) return patchesTo(client, "d1").length === 1 ? first.promise : Promise.reject(offline());
      return okPatch(url, body);
    });
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    r.expectKept("本机改了一句");
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    r.expectKept("本机改了一句");
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("本机改了一句"), T);
    await r.type("<p>起点正文，本机改了一句，回来又写</p>");
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    r.expectKept("回来又写");
    await act(async () => { first.reject(offline()); });
    await wait(400);

    r.expectNoOlderPatch();
    r.expectKept("回来又写");
    r.expectDurable("回来又写");
    expect(window.localStorage.getItem(CACHE_KEY)).toContain("回来又写");
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("回来又写"), T);
  }, LONG);

  /* P1b：同 P1，路上那一次 500，之后后端恢复 */
  it("P1b 路上那一次 500、随后后端恢复：服务端最后存下的是较新的一稿", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    routeEnsures(client, rev1);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => {
      if (url.endsWith("/author-drafts/d1") && patchesTo(client, "d1").length === 1) return first.promise;
      return okPatch(url, body);
    });
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("本机改了一句"), T);
    await r.type("<p>起点正文，本机改了一句，回来又写</p>");
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    await act(async () => { first.reject(serverError()); });
    await vi.waitFor(() => expect(patchesTo(client, "d1").length).toBeGreaterThanOrEqual(2), T);
    await wait(400);

    r.expectNoOlderPatch();
    const sent = patchesTo(client, "d1");
    expect(sent[sent.length - 1][1].content).toContain("回来又写");
    expect(window.localStorage.getItem(CACHE_KEY)).toContain("回来又写");
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("回来又写"), T);
  }, LONG);

  /* P1c：写作台卸载再挂上（切视图再回来），新的写作台写了较新的一稿，旧的那一次 500 */
  it("P1c 写作台重新挂载后写了较新的一稿，旧的那一次 500：不会把较旧的一稿盖上去", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    routeEnsures(client, rev1);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (patchesTo(client, "d1").length === 1 ? first.promise : okPatch(url, body)));
    const one = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    let current = one.host;
    const r = room(ctx, () => current);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await unmount(one.entry);
    const two = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    current = two.host;
    await vi.waitFor(() => expect(r.editor().textContent).toContain("本机改了一句"), T);
    await r.type("<p>起点正文，本机改了一句，回来又写</p>");
    await wait(1300);                                  // 新写作台的自动保存到点：排在旧的那一次后面
    r.expectDurable("回来又写");
    await act(async () => { first.reject(serverError()); });
    await vi.waitFor(() => expect(patchesTo(client, "d1").length).toBeGreaterThanOrEqual(2), T);
    await wait(400);

    r.expectNoOlderPatch();
    const sent = patchesTo(client, "d1");
    expect(sent[sent.length - 1][1].content).toContain("回来又写");
    expect(window.localStorage.getItem(CACHE_KEY)).toContain("回来又写");
    expect(r.editor().textContent).toContain("回来又写");
  }, LONG);
});

describe("保存还在路上时敲的字（P2）", () => {
  it("P2 等着的时候就已经进了本机缓存和未同步标记：这时刷新页面也丢不了", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const first = deferred();                            // 一直不回（后端卡住）
    client.apiPatch.mockImplementation(() => first.promise);
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>第一处改动</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    await r.type("<p>第一处改动，第二处改动</p>");
    await wait(1500);                                    // 过了 900 ms 的自动保存

    r.expectDurable("第二处改动");
    expect(window.localStorage.getItem(CACHE_KEY)).toContain("第二处改动");
    expect(window.localStorage.getItem(PENDING_KEY)).not.toBeNull();
    expect(client.apiPatch).toHaveBeenCalledTimes(1);   // 同一场同一时刻只有一次在路上
    expect(r.status()).toContain("正在保存");
  }, LONG);
});

describe("离场冲刷还在路上、回来又写，然后 409（P3 · P3b · P6 · P7 · N2b）", () => {
  /* P3：A 写完没等定时器就去 B（离场冲刷在路上）→ 回 A 接着写，自动保存到点 → 离场冲刷那一次 409 */
  it("P3 回来后的自动保存不会叠上去盖掉另一台设备的正文；编辑器换成服务端版本，两份本机稿都在同步与恢复", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => {
      if (url.endsWith("/author-drafts/d1") && patchesTo(client, "d1").length === 1) return first.promise;
      return okPatch(url, body);
    });
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await openScene("ch01s2");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("本机改了一句"), T);
    await r.type("<p>起点正文，本机改了一句，回来又写</p>");
    await wait(1300);
    r.expectKept("回来又写");
    await act(async () => { first.reject(conflict()); });

    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    await wait(300);
    r.expectServerNeverOverwritten();
    r.expectNoOlderPatch();
    expect(patchesTo(client, "d1")).toHaveLength(1);
    expect(r.recoveryHas("本机改了一句")).toBe(true);
    expect(r.recoveryHas("回来又写")).toBe(true);
    // 作者在服务端版本上接着写：带着新的修订号，也带着服务端那一版的字
    await r.append("，接着写");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(2), T);
    expect(patchesTo(client, "d1")[1][1]).toMatchObject({ base_revision_no: 3 });
    r.expectServerNeverOverwritten();
  }, LONG);

  /* P3b：同 P3，但 409 在回来后的自动保存到点之前就回来了（作者正在打字） */
  it("P3b 409 回来时作者正在打字：编辑器照样换成服务端版本，正在打的字先进同步与恢复", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => {
      if (url.endsWith("/author-drafts/d1") && patchesTo(client, "d1").length === 1) return first.promise;
      return okPatch(url, body);
    });
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await openScene("ch01s2");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("本机改了一句"), T);
    await r.type("<p>起点正文，本机改了一句，回来又写</p>");
    await act(async () => { first.reject(conflict()); });  // 900 ms 的自动保存还没到点

    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    await wait(1500);
    r.expectServerNeverOverwritten();
    r.expectNoOlderPatch();
    expect(patchesTo(client, "d1")).toHaveLength(1);
    expect(r.recoveryHas("本机改了一句")).toBe(true);
    expect(r.recoveryHas("回来又写")).toBe(true);
    r.expectDurable("回来又写");
  }, LONG);

  /* P6：保存在路上时又写了一句，定时器没到就离开（离场冲刷带着较新的一稿排在后面）→ 路上那一次 409 */
  it("P6 排在后面的较新一稿不会在 409 之后赢过另一台设备的正文；两份本机稿都在同步与恢复", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => {
      if (url.endsWith("/author-drafts/d1") && patchesTo(client, "d1").length === 1) return first.promise;
      return okPatch(url, body);
    });
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await r.type("<p>起点正文，本机改了一句，又加一句</p>");
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    r.expectKept("又加一句");
    await act(async () => { first.reject(conflict()); });
    await wait(1200);

    r.expectServerNeverOverwritten();
    r.expectNoOlderPatch();
    expect(patchesTo(client, "d1")).toHaveLength(1);
    expect(r.recoveryHas("本机改了一句")).toBe(true);
    expect(r.recoveryHas("又加一句")).toBe(true);
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
  }, LONG);

  /* P7：自动保存在路上 → 又写一句 → 离开（离场冲刷排队）→ 回来再写，自动保存到点 → 路上那一次 409 */
  it("P7 回来再写的字不会只剩编辑器里一份：状态说「已保存」时正文真的存上了", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => {
      if (url.endsWith("/author-drafts/d1") && patchesTo(client, "d1").length === 1) return first.promise;
      return okPatch(url, body);
    });
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await r.type("<p>起点正文，本机改了一句，又加一句</p>");
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("又加一句"), T);
    await r.type("<p>起点正文，本机改了一句，又加一句，回来再写</p>");
    await wait(1300);
    r.expectDurable("回来再写");
    await act(async () => { first.reject(conflict()); });
    await wait(2500);

    r.expectServerNeverOverwritten();
    r.expectNoOlderPatch();
    r.expectKept("回来再写");
    r.expectDurable("回来再写");
    // 「已保存」只在编辑器里的字真的存上了时出现
    const saved = patchesTo(client, "d1").map(([, body]) => body.content);
    if (r.status().includes("已保存")) expect(saved).toContain(wrSerializeManuscript(r.editor()));
    expect(r.editor().textContent).toContain(SERVER);
  }, LONG);

  /* N2b：保存在路上时又写了一句 → 409 → WrDocs 读服务端版本还没回来 → 作者离开这一场 */
  it("N2b 读服务端版本的途中离开：路上又写的那一句进同步与恢复，另一台设备的正文不被盖掉", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    const rehydrate = deferred();
    const reloads = routeEnsures(client, () => rehydrate.promise);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (url.endsWith("/d1") && patchesTo(client, "d1").length === 1 ? first.promise : okPatch(url, body)));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await r.append("，路上又写一句");
    await act(async () => { first.reject(conflict()); });
    await vi.waitFor(() => expect(reloads()).toBe(1), T);
    r.expectKept("路上又写一句");
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    r.expectKept("路上又写一句");
    await act(async () => { rehydrate.resolve({ draft: { draft_id: "d1", revision_no: 3, content: `<p>${SERVER}</p>` } }); });
    await wait(1500);
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);

    r.expectServerNeverOverwritten();
    r.expectNoOlderPatch();
    expect(r.recoveryHas("路上又写一句")).toBe(true);
    expect(r.recoveryHas("本机改了一句")).toBe(true);
  }, LONG);
});

describe("写作台卸载再挂上时保存还在路上（P4 = N4 · P4b）", () => {
  /* P4 / N4：自动保存在路上 → 切到别的视图再回来 → 新写作台接着写，自动保存到点 → 旧的那一次 409 */
  it("P4 新写作台的保存不会排在旧的后面、409 之后盖掉另一台设备；编辑器换成服务端版本", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (patchesTo(client, "d1").length === 1 ? first.promise : okPatch(url, body)));
    const one = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    let current = one.host;
    const r = room(ctx, () => current);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await unmount(one.entry);
    const two = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    current = two.host;
    await vi.waitFor(() => expect(r.editor().textContent).toContain("本机改了一句"), T);
    await r.append("，回来又写");
    await wait(1300);
    r.expectKept("回来又写");
    await act(async () => { first.reject(conflict()); });

    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    await wait(300);
    r.expectServerNeverOverwritten();
    r.expectNoOlderPatch();
    expect(patchesTo(client, "d1")).toHaveLength(1);
    expect(r.recoveryHas("本机改了一句")).toBe(true);
    expect(r.recoveryHas("回来又写")).toBe(true);
  }, LONG);

  /* P4b：同 P4，但 409 回来时新写作台里的字还没到自动保存 */
  it("P4b 409 回来时新写作台里的字还没交出去：先进同步与恢复，再换成服务端版本", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (patchesTo(client, "d1").length === 1 ? first.promise : okPatch(url, body)));
    const one = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    let current = one.host;
    const r = room(ctx, () => current);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await unmount(one.entry);
    const two = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    current = two.host;
    await vi.waitFor(() => expect(r.editor().textContent).toContain("本机改了一句"), T);
    await r.append("，回来又写");
    await act(async () => { first.reject(conflict()); });

    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    await wait(1500);
    r.expectServerNeverOverwritten();
    expect(patchesTo(client, "d1")).toHaveLength(1);
    expect(r.recoveryHas("回来又写")).toBe(true);
    expect(r.recoveryHas("本机改了一句")).toBe(true);
  }, LONG);
});

describe("409 之后读不到服务端版本（P5 · N6 · INV-R5）", () => {
  /* P5：409 之后第一次读服务端版本断网失败，作者接着写；再下一次读很慢，读回来时作者已经写了几句 */
  it("P5 读不到服务端版本期间不保存（不拿刷新后的修订号盖掉另一台设备）；状态是保存失败；读到之后编辑器换成服务端版本", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const slow = deferred();
    routeEnsures(client, (n) => (n === 1 ? Promise.reject(offline()) : slow.promise));
    client.apiPatch.mockImplementation((url, body) => (patchesTo(client, "d1").length === 1 ? Promise.reject(conflict()) : okPatch(url, body)));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await wait(300);
    // 服务端版本还没读到：编辑器里仍是本机的字，状态如实说保存失败（不说「已加载」）
    expect(r.editor().textContent).toContain("本机改了一句");
    await vi.waitFor(() => expect(r.status()).toContain("保存失败"), T);
    await r.type("<p>起点正文，本机改了一句，接着写</p>");
    await wait(1300);
    r.expectKept("接着写");
    expect(patchesTo(client, "d1")).toHaveLength(1);
    await act(async () => { slow.resolve({ draft: { draft_id: "d1", revision_no: 3, content: `<p>${SERVER}</p>` } }); });

    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    await wait(300);
    r.expectServerNeverOverwritten();
    expect(patchesTo(client, "d1")).toHaveLength(1);
    expect(r.recoveryHas("本机改了一句")).toBe(true);
    expect(r.recoveryHas("接着写")).toBe(true);
  }, LONG);

  /* N6：409 之后连着两次读不到服务端版本；作者打字时网络已经回来了 */
  it("N6 网络回来后的第一次敲字不拿刷新后的修订号保存本机稿；先换成服务端版本、本机稿进同步与恢复", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    let down = false;
    const reloads = routeEnsures(client, () => (down
      ? Promise.reject(offline())
      : Promise.resolve({ draft: { draft_id: "d1", revision_no: 3, content: `<p>${SERVER}</p>` } })));
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1 ? first.promise : okPatch(url, body)));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    down = true;
    await act(async () => { first.reject(conflict()); });
    await vi.waitFor(() => expect(reloads()).toBeGreaterThanOrEqual(1), T);
    await wait(300);
    expect(r.status()).toContain("保存失败");
    r.expectKept("本机改了一句");
    down = false;
    await r.append("，再来一句");

    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    await wait(500);
    r.expectServerNeverOverwritten();
    expect(r.recoveryHas("本机改了一句")).toBe(true);
    expect(r.recoveryHas("再来一句")).toBe(true);
  }, LONG);

  /* INV-R5：409 之后读服务端版本失败一次，之后自己恢复 */
  it("INV-R5 读服务端版本失败一次后恢复：编辑器最终是服务端版本，本机稿在同步与恢复，接着写不盖掉服务端", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    routeEnsures(client, (n) => (n === 1 ? Promise.reject(offline()) : Promise.resolve({ draft: { draft_id: "d1", revision_no: 3, content: `<p>${SERVER}</p>` } })));
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1 ? Promise.reject(conflict()) : okPatch(url, body)));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    await wait(300);
    await r.append("，再来一句");
    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    await wait(300);
    r.expectServerNeverOverwritten();
    expect(r.recoveryHas("本机改了一句")).toBe(true);
    r.expectKept("再来一句");
    await r.append("，续写");
    await vi.waitFor(() => expect(patchesTo(client, "d1").length).toBeGreaterThanOrEqual(2), T);
    r.expectServerNeverOverwritten();
  }, LONG);
});

describe("409 之后 WrDocs 重新读取期间（N1 · N2a）", () => {
  /* N1：409 之后读服务端版本很慢，这期间自动保存到点两次 */
  it("N1 两次到点都不保存；编辑器最后是服务端版本；敲过的三段都在同步与恢复", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const rehydrate = deferred();
    const reloads = routeEnsures(client, () => rehydrate.promise);
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1 ? Promise.reject(conflict()) : okPatch(url, body)));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，第一句</p>");
    await vi.waitFor(() => expect(reloads()).toBe(1), T);
    await r.append("，第二句");
    await wait(1200);
    r.expectKept("第二句");
    await r.append("，第三句");
    await wait(1200);
    r.expectKept("第三句");
    await act(async () => { rehydrate.resolve({ draft: { draft_id: "d1", revision_no: 3, content: `<p>${SERVER}</p>` } }); });
    await wait(1500);

    r.expectServerNeverOverwritten();
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
    expect(r.editor().textContent).toContain(SERVER);
    ["第一句", "第二句", "第三句"].forEach((piece) => expect(r.recoveryHas(piece)).toBe(true));
    await r.append("，续写");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(2), T);
    expect(client.apiPatch.mock.calls[1][1]).toMatchObject({ base_revision_no: 3 });
    r.expectServerNeverOverwritten();
  }, LONG);

  /* N2a：409 之后读服务端版本途中离开，之后没再写 */
  it("N2a 读取途中离开、之后没再写：不多存一次；回来看到服务端版本", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    const rehydrate = deferred();
    const reloads = routeEnsures(client, () => rehydrate.promise);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (url.endsWith("/d1") && patchesTo(client, "d1").length === 1 ? first.promise : okPatch(url, body)));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await act(async () => { first.reject(conflict()); });
    await vi.waitFor(() => expect(reloads()).toBe(1), T);
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    await act(async () => { rehydrate.resolve({ draft: { draft_id: "d1", revision_no: 3, content: `<p>${SERVER}</p>` } }); });
    await wait(1200);
    r.expectServerNeverOverwritten();
    expect(patchesTo(client, "d1")).toHaveLength(1);
    expect(r.recoveryHas("本机改了一句")).toBe(true);
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
  }, LONG);
});

describe("离场之后路上那一次断网，再 409（N3 · N3b = 网络错误之后 409）", () => {
  /* N3：离开（之后没再写）→ 很快回来接着写 → 路上那一次断网 → 补发时服务端回 409 */
  it("N3 补发和回来后的保存不会叠成一次覆盖；回来写的字在同步与恢复，编辑器换成服务端版本", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const first = deferred();
    let d1Calls = 0;
    client.apiPatch.mockImplementation((url, body) => {
      if (!url.endsWith("/d1")) return okPatch(url, body);
      d1Calls += 1;
      if (d1Calls === 1) return first.promise;
      if (d1Calls === 2) return Promise.reject(conflict());
      return okPatch(url, body);
    });
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("本机改了一句"), T);
    await r.append("，回来又写");
    await wait(1200);
    r.expectKept("回来又写");
    await act(async () => { first.reject(offline()); });
    await wait(2000);

    r.expectServerNeverOverwritten();
    r.expectNoOlderPatch();
    r.expectKept("回来又写");
    // 冲突走完：编辑器是服务端版本，回来写的字在同步与恢复
    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    expect(r.recoveryHas("回来又写")).toBe(true);
  }, LONG);

  /* N3b（对照）：离开之后路上那一次断网，补发回 409，作者没回来 */
  it("N3b 作者没回来：补发走冲突副本，回来时看到服务端版本", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const first = deferred();
    let d1Calls = 0;
    client.apiPatch.mockImplementation((url, body) => {
      if (!url.endsWith("/d1")) return okPatch(url, body);
      d1Calls += 1;
      if (d1Calls === 1) return first.promise;
      if (d1Calls === 2) return Promise.reject(conflict());
      return okPatch(url, body);
    });
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    await act(async () => { first.reject(offline()); });
    await wait(1500);
    r.expectServerNeverOverwritten();
    expect(r.recoveryHas("本机改了一句")).toBe(true);
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
  }, LONG);
});

describe("提升为权威正文前后的 409（N5a · N5b · INV-R4）", () => {
  /* N5a：保存在路上时点「提升」，那一次 409 */
  it("N5a 什么都不提升、什么都不盖掉；编辑器换成服务端版本，两段字都在；作者得到一句冲突提示", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1 ? first.promise : okPatch(url, body)));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，第一处</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    await r.append("，第二处");
    const button = host.querySelector(".wr-canonical-promote");
    expect(button && !button.disabled).toBe(true);
    await act(async () => { button.click(); });
    r.expectKept("第二处");
    await act(async () => { first.reject(conflict()); });
    await wait(1500);

    expect(client.apiPost.mock.calls.filter(([url]) => /promote-canonical$/.test(url))).toHaveLength(0);
    r.expectServerNeverOverwritten();
    expect(r.editor().textContent).toContain(SERVER);
    expect(r.recoveryHas("第一处")).toBe(true);
    expect(r.recoveryHas("第二处")).toBe(true);
    expect(window.alert.mock.calls.map(([message]) => String(message)).join(" | ")).toMatch(/已在别处更新|别处被修改/);
  }, LONG);

  /* N5b：提升接口自己回 409（草稿在服务端动过了），作者接着写 */
  it("N5b 提升被拒时说「已在别处更新」；接着写撞上 409 时照样走冲突副本", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const base = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body) => (/promote-canonical$/.test(url)
      ? Promise.reject(Object.assign(new Error("draft moved"), { code: "AUTHOR_DRAFT_CONFLICT", status: 409 }))
      : base(url, body)));
    let d1 = 0;
    client.apiPatch.mockImplementation((url, body) => {
      d1 += 1;
      if (d1 === 2) return Promise.reject(conflict());
      return okPatch(url, body);
    });
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，第一处</p>");
    await vi.waitFor(() => expect(r.status()).toContain("已保存"), T);
    await act(async () => { host.querySelector(".wr-canonical-promote").click(); });
    await wait(600);
    expect(window.alert.mock.calls.map(([message]) => String(message)).join(" | ")).toMatch(/已在别处更新/);
    await r.append("，第二处");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(2), T);
    await wait(1500);
    r.expectServerNeverOverwritten();
    r.expectKept("第二处");
    expect(r.recoveryHas("第二处")).toBe(true);
  }, LONG);

  /* INV-R4：409 之后编辑器已换成服务端版本，作者没再写就点「提升」 */
  it("INV-R4 冲突处理完之后提升服务端那一版：不再被上一次的冲突挡住", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom, WrDocs } = ctx;
    routeEnsures(client);
    client.apiPatch.mockImplementation(() => Promise.reject(conflict()));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    await wait(300);
    expect(WrDocs.state("ch01s1")).toMatchObject({ dirty: false, lastSaveError: null });
    const patchesBefore = client.apiPatch.mock.calls.length;
    await act(async () => { host.querySelector(".wr-canonical-promote").click(); });
    await vi.waitFor(() => expect(client.apiPost.mock.calls.filter(([url]) => /promote-canonical$/.test(url))).toHaveLength(1), T);
    expect(client.apiPost.mock.calls.find(([url]) => /promote-canonical$/.test(url))[1]).toMatchObject({ base_revision_no: 3 });
    expect(client.apiPatch.mock.calls.length).toBe(patchesBefore);
  }, LONG);
});

describe("深改姿态进场冲刷（N7）", () => {
  it("N7 保存在路上时进深改（又写过）、那一次 409：只发一次；编辑器是服务端版本，两段本机稿都在同步与恢复", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    routeEnsures(client);
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (/deep-review/.test(url) ? Promise.resolve({ findings: [], text: "" }) : baseGet(url)));
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1 ? first.promise : okPatch(url, body)));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，第一处</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    await r.append("，第二处");
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:writer-posture", { detail: "deep" })); });
    await wait(200);
    await act(async () => { first.reject(conflict()); });
    await wait(1500);
    r.expectServerNeverOverwritten();
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
    expect(r.editor().textContent).toContain(SERVER);
    expect(r.recoveryHas("第一处")).toBe(true);
    expect(r.recoveryHas("第二处")).toBe(true);
  }, LONG);
});

describe("冲突副本碰上浏览器存储满了", () => {
  /* WrDocs 自己那份冲突副本就放不进本机存储 */
  it("本机稿只能留在本次会话的同步与恢复：编辑器照样换成服务端版本，提示说清并给出入口；之后不在服务端版本上盖本机稿", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom, WrRecovery } = ctx;
    routeEnsures(client);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1 ? first.promise : okPatch(url, body)));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    const realSet = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItem(key, value) {
      if (String(key).startsWith("wr-recovery:v1:")) throw Object.assign(new Error("full"), { name: "QuotaExceededError" });
      return realSet.call(this, key, value);
    });
    await act(async () => { first.reject(conflict()); });

    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    const kept = WrRecovery.list().find((entry) => entry.html.includes("本机改了一句"));
    expect(kept && kept.durable).toBe(false);
    expect(window.alert.mock.calls.map(([message]) => String(message)).join(" | ")).toContain("本次会话");
    // 刷新前本机存储里仍留着那一稿（读缓存 + 未同步标记），刷新后还会再走一次冲突副本
    r.expectDurable("本机改了一句");
    await r.append("，第三处");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(2), T);
    r.expectServerNeverOverwritten();
  }, LONG);
});

describe("读缓存换成了别的版本（水合 / 复核），作者正在写", () => {
  function routeS1(client, respond) {
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) return respond();
      if (/\/author-drafts\/scene\/s2\/ensure$/.test(url)) return Promise.resolve({ draft: { draft_id: "d2", revision_no: 1, content: "<p>第二场</p>" } });
      return Promise.resolve({});
    });
  }

  it("打开时本机缓存是旧的、服务端版本回来之前就敲了字：换成服务端版本，敲的字进同步与恢复并提示；不在服务端的修订号上盖它", async () => {
    window.localStorage.setItem(CACHE_KEY, "<p>这台电脑上的旧稿</p>");
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const ensure = deferred();
    routeS1(client, () => ensure.promise);
    client.apiPatch.mockImplementation(okPatch);
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("这台电脑上的旧稿"), T);
    await r.type("<p>这台电脑上的旧稿，打开就写</p>");
    await act(async () => { ensure.resolve({ draft: { draft_id: "d1", revision_no: 5, content: `<p>${SERVER}</p>` } }); });

    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    await wait(1200);
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(r.recoveryHas("打开就写")).toBe(true);
    expect(window.alert.mock.calls.map(([message]) => String(message)).join(" | ")).toContain("别处有更新");
    await r.append("，在新版本上写");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    expect(client.apiPatch.mock.calls[0][1]).toMatchObject({ base_revision_no: 5 });
    expect(client.apiPatch.mock.calls[0][1].content).toContain(SERVER);
  }, LONG);

  it("旧缓存上敲的字已经到了自动保存、服务端版本才回来：同样按冲突处理（本机稿进同步与恢复，编辑器换成服务端版本，不发 PATCH）", async () => {
    window.localStorage.setItem(CACHE_KEY, "<p>这台电脑上的旧稿</p>");
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const ensure = deferred();
    routeS1(client, () => ensure.promise);
    client.apiPatch.mockImplementation(okPatch);
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("这台电脑上的旧稿"), T);
    await r.type("<p>这台电脑上的旧稿，打开就写</p>");
    await wait(1200);                                   // 自动保存到点：WrDocs 还在等水合
    r.expectDurable("打开就写");
    await act(async () => { ensure.resolve({ draft: { draft_id: "d1", revision_no: 5, content: `<p>${SERVER}</p>` } }); });

    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    await wait(300);
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(r.recoveryHas("打开就写")).toBe(true);
  }, LONG);

  it("服务端就是作者正在写的那一版（写法不同、字一样）：接着写，不换稿，也不进同步与恢复", async () => {
    window.localStorage.setItem(CACHE_KEY, "<p>起点正文</p>");
    const ctx = await loadWriter();
    const { client, WriterRoom, WrRecovery } = ctx;
    const ensure = deferred();
    routeS1(client, () => ensure.promise);
    client.apiPatch.mockImplementation(okPatch);
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，接着写</p>");
    await act(async () => { ensure.resolve({ draft: { draft_id: "d1", revision_no: 5, content: "<p><span>起点正文</span></p>" } }); });
    await wait(300);
    expect(r.editor().textContent).toContain("接着写");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    expect(client.apiPatch.mock.calls[0][1]).toEqual({ content: "<p>起点正文，接着写</p>", base_revision_no: 5 });
    expect(WrRecovery.list()).toEqual([]);
  }, LONG);

  it("回到一场时它在别处被改过（F03-24）：没有本机改动，编辑器换成服务端的新版本", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    let s1 = { draft: { draft_id: "d1", revision_no: 1, content: "<p>起点正文</p>" } };
    routeS1(client, () => Promise.resolve(s1));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    s1 = { draft: { draft_id: "d1", revision_no: 4, content: "<p>别处改过的一版</p>" } };
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("别处改过的一版"), T);
    expect(ctx.WrDocs.state("ch01s1").revision).toBe(4);
  }, LONG);
});

/* ==========================================================
   W1 复核一：两路复核（lens A / lens B）在 b75f2d5 上复现的不安全顺序，房间级（真写作台 + 真 WrDocs + 起草台的
   scnAdoptToDoc + 同步与恢复的 WrRecovery）。服务端按修订号比对：PATCH / adopt-current / promote-canonical 的
   base 不是眼下的修订号就 409。每条断言的都是安全的结果。
   ========================================================== */

/* s1 → d1「起点正文」、s2 → d2「第二场」，都是 rev 1。hooks.ensure(sceneId, current) / hooks.patch(url, body, apply) /
   hooks.promote(url, body) 换掉那一路的回法；history 记下每一次存上的正文，promoted 记下每一次提升（采纳的带 via）。 */
function casServer(client) {
  const drafts = {
    s1: { id: "d1", revision: 1, content: "<p>起点正文</p>", history: [] },
    s2: { id: "d2", revision: 1, content: "<p>第二场</p>", history: [] },
  };
  const hooks = { ensure: null, patch: null, promote: null };
  const promoted = [];
  const byId = (id) => Object.values(drafts).find((draft) => draft.id === id);
  const snapshot = (draft) => ({ draft: { draft_id: draft.id, revision_no: draft.revision, content: draft.content } });
  const apply = (url, body) => {
    const draft = byId(url.split("/").pop());
    if (Number(body.base_revision_no) !== draft.revision) return Promise.reject(conflict());
    draft.revision += 1;
    draft.content = body.content;
    draft.history.push({ revision: draft.revision, content: body.content, via: "patch" });
    return Promise.resolve(snapshot(draft));
  };
  const baseGet = client.apiGet.getMockImplementation();
  client.apiGet.mockImplementation((url) => (/deep-review/.test(url) ? Promise.resolve({ findings: [], text: "" }) : baseGet(url)));
  client.apiPost.mockImplementation((url, body) => {
    const ensure = /\/author-drafts\/scene\/([^/]+)\/ensure$/.exec(url);
    if (ensure) {
      const draft = drafts[ensure[1]];
      const current = () => Promise.resolve(snapshot(draft));
      return hooks.ensure ? hooks.ensure(ensure[1], current) : current();
    }
    const adoption = /\/api\/v1\/scenes\/([^/]+)\/adopt-current$/.exec(url);
    if (adoption) {
      const draft = drafts[adoption[1]];
      const exact = body.exact_author_draft;
      if (Number(exact.base_revision_no) !== draft.revision) return Promise.reject(conflict());
      draft.revision += 1;
      draft.content = exact.content;
      draft.history.push({ revision: draft.revision, content: exact.content, via: "adopt" });
      promoted.push({ draft: draft.id, revision: draft.revision, content: draft.content, via: "adopt" });
      return Promise.resolve({
        scene_id: adoption[1], scene_status: "archived", final_scene_row_id: `f-${draft.revision}`, content_hash: "h",
        author_draft: { draft_id: draft.id, revision_no: draft.revision, content: draft.content, last_promoted_revision_no: draft.revision, canonical_dirty: false },
      });
    }
    const promote = /\/author-drafts\/([^/]+)\/promote-canonical$/.exec(url);
    if (promote) {
      if (hooks.promote) return hooks.promote(url, body);
      const draft = byId(promote[1]);
      if (Number(body.base_revision_no) !== draft.revision) return Promise.reject(conflict());
      promoted.push({ draft: draft.id, revision: draft.revision, content: draft.content });
      return Promise.resolve({ final_scene_row_id: `f-${draft.revision}`, draft_revision_no: draft.revision, canonical_dirty: false });
    }
    return Promise.resolve({});
  });
  client.apiPatch.mockImplementation((url, body) => {
    if (!/\/author-drafts\//.test(url)) return Promise.resolve({});
    return hooks.patch ? hooks.patch(url, body, apply) : apply(url, body);
  });
  return { drafts, hooks, promoted, apply };
}

/* d1 的第一次 PATCH 卡住（hung），之后照常按修订号比对 */
function holdFirstPatch(srv, { thenApply = false } = {}) {
  const hung = deferred();
  let first = true;
  srv.hooks.patch = (url, body, apply) => {
    if (url.endsWith("/d1") && first) {
      first = false;
      return thenApply ? hung.promise.then(() => apply(url, body)) : hung.promise;
    }
    return apply(url, body);
  };
  return hung;
}

const AI_DRAFT = [{ id: "p1", parts: [{ text: "潮水退去，她看清了闸门上的名字。" }] }];
const ADOPTED_TEXT = "潮水退去，她看清了闸门上的名字。";
const adoptOnDesk = async () => {
  const api = await import("./ws-scene-api.js");
  let adopted = null;
  await act(async () => { adopted = await api.scnAdoptToDoc("ch01s1", AI_DRAFT, null, { mode: "overwrite", confirmed: true }); });
  expect(adopted).toMatchObject({ ok: true, archived: true });
  return adopted;
};
const d1Texts = (client) => patchesTo(client, "d1").map(([, body]) => body.content);
const conflictNotices = () => window.alert.mock.calls.map(([message]) => String(message)).filter((message) => message.includes("别处被修改"));

describe("复核一 · 起草台采用时写作台的保存还在路上（R-A1 · R-A1c · R-A1d · V-A2 · V-A3）", () => {
  it("R-A1 / R-A1d 最后一次自动保存还在路上、随后断网失败：离场冲刷不补发采纳前的字；回到写作台看到的就是服务端上的采纳稿，它已是权威正文", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const srv = casServer(client);
    const hung = holdFirstPatch(srv);
    const one = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    let current = one.host;
    const r = room(ctx, () => current);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await unmount(one.entry);                            // 切到 AI 起草台：离场冲刷在等路上那一次
    await adoptOnDesk();
    await act(async () => { hung.reject(offline()); });  // 服务端其实按修订号拒绝了它，回包丢了
    await wait(300);
    expect(d1Texts(client)).toEqual(["<p>起点正文，本机改了一句</p>"]);
    expect(srv.drafts.s1.content).toBe(`<p>${ADOPTED_TEXT}</p>`);

    const two = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    current = two.host;
    await vi.waitFor(() => expect(r.editor().textContent).toBe(ADOPTED_TEXT), T);
    await wait(300);
    expect(d1Texts(client)).toHaveLength(1);
    expect(current.querySelector('[data-testid="canonical-status"]').textContent).toContain("权威正文已更新");
    expect(current.querySelector(".wr-canonical-promote").disabled).toBe(true);
    expect(srv.promoted.map((entry) => entry.content)).toEqual([`<p>${ADOPTED_TEXT}</p>`]);
    expect(r.recoveryHas("本机改了一句")).toBe(true);
  }, LONG);

  it("R-A1c 路上那一次在采用之后撞上 409（本来就会）：不提示「别处被修改」，采纳前的字只在同步与恢复里留一份", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom, WrRecovery } = ctx;
    const srv = casServer(client);
    const hung = holdFirstPatch(srv);
    const one = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => one.host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await unmount(one.entry);
    await adoptOnDesk();
    await act(async () => { hung.reject(conflict()); });
    await wait(500);
    expect(conflictNotices()).toEqual([]);
    expect(WrRecovery.list().filter((entry) => entry.html.includes("本机改了一句"))).toHaveLength(1);
    expect(srv.drafts.s1.content).toBe(`<p>${ADOPTED_TEXT}</p>`);
    expect(ctx.WrDocs.state("ch01s1")).toMatchObject({ conflictPending: false, dirty: false });
  }, LONG);

  it("V-A2 换场的离场冲刷还在路上时采用、它随后回 500：回到这一场看到采纳稿，接着写是在采纳的修订号上写，采纳前的字不再发", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    const srv = casServer(client);
    const hung = holdFirstPatch(srv);
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，采纳前作者写的</p>");
    await openScene("ch01s2");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    await adoptOnDesk();
    await act(async () => { hung.reject(serverError()); });
    await wait(300);
    expect(srv.drafts.s1.content).toBe(`<p>${ADOPTED_TEXT}</p>`);
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toBe(ADOPTED_TEXT), T);
    await wait(300);
    expect(r.status()).not.toContain("保存失败");
    await r.append("，续写");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(2), T);
    expect(patchesTo(client, "d1")[1][1]).toEqual({ content: `<p>${ADOPTED_TEXT}，续写</p>`, base_revision_no: 2 });
    expect(srv.drafts.s1.history.map((entry) => entry.via)).toEqual(["adopt", "patch"]);
  }, LONG);

  it("V-A3 路上是较旧的一稿、离场时又交了较新的一稿，采用之后路上那一次回 500：回来进深改不发任何一稿，状态不说失败", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const srv = casServer(client);
    const hung = holdFirstPatch(srv);
    const one = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    let current = one.host;
    const r = room(ctx, () => current);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，第一稿</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await r.append("，第二稿");
    await unmount(one.entry);                            // 离场冲刷把第二稿交出去（排在路上那一次后面）
    await adoptOnDesk();
    await act(async () => { hung.reject(serverError()); });
    await wait(300);
    const two = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    current = two.host;
    await vi.waitFor(() => expect(r.editor().textContent).toBe(ADOPTED_TEXT), T);
    expect(r.status()).not.toContain("保存失败");
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:writer-posture", { detail: "deep" })); });
    await wait(800);
    expect(patchesTo(client, "d1")).toHaveLength(1);
    expect(srv.drafts.s1.content).toBe(`<p>${ADOPTED_TEXT}</p>`);
    expect(r.status()).not.toContain("保存失败");
    expect(r.recoveryHas("第二稿")).toBe(true);
  }, LONG);
});

describe("复核一 · 同步与恢复的「恢复」「重试同步」（R-A2 · V-B · S-B2）", () => {
  it("R-A2 写作台开着这一场时「恢复」没同步上（断网）：编辑器当场换成恢复稿，状态是保存失败；联网后点提升，存上并提升的就是编辑器里的这一稿", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom, WrRecovery, WrDocs } = ctx;
    const srv = casServer(client);
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，作者眼下的正文 X</p>");
    await vi.waitFor(() => expect(srv.drafts.s1.content).toContain("正文 X"), T);
    await vi.waitFor(() => expect(r.status()).toContain("已保存"), T);
    const entry = WrRecovery.createCandidate("ch01s1", "<p>要恢复的那一稿 R</p>");
    srv.hooks.patch = () => Promise.reject(offline());
    let restoreError = null;
    await act(async () => { try { await WrRecovery.restore(entry.id); } catch (e) { restoreError = e; } });
    expect(r.editor().textContent).toBe("要恢复的那一稿 R");        // 编辑器与本机缓存同一刻换成恢复稿
    expect(restoreError).toMatchObject({ code: "RECOVERY_NOT_SYNCED" });
    await vi.waitFor(() => expect(r.status()).toBe("草稿保存失败"), T);
    expect(WrDocs.cachedHTML("ch01s1")).toBe("<p>要恢复的那一稿 R</p>");
    expect(r.recoveryHas("作者眼下的正文 X")).toBe(true);   // 恢复前的正文自动备份过
    srv.hooks.patch = null;                                // 联网了；作者没再敲字，点「提升」
    await act(async () => { host.querySelector(".wr-canonical-promote").click(); });
    await vi.waitFor(() => expect(srv.promoted).toHaveLength(1), T);
    expect(srv.promoted[0].content).toBe("<p>要恢复的那一稿 R</p>");
    expect(r.editor().textContent).toBe("要恢复的那一稿 R");
    expect(r.status()).toBe("草稿已保存");
  }, LONG);

  it("V-B 自动保存还在路上时「重试同步」、作者接着写：写作台当场换成恢复稿，作者是在它上面接着写的；恢复前的字不在它后面再发一次", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom, WrRecovery } = ctx;
    const srv = casServer(client);
    const hung = holdFirstPatch(srv, { thenApply: true });
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，作者在写</p>");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    const entry = WrRecovery.create({ sid: "ch01s1", html: "<p>上次没同步上的那一稿</p>", type: "unsynced", reason: "断网", label: "场景 ch01s1 · 未同步稿" });
    let result = null;
    let failure = null;
    let retrying = null;
    await act(async () => { retrying = WrRecovery.retry(entry.id).then((value) => { result = value; }, (error) => { failure = error; }); });
    expect(r.editor().textContent).toBe("上次没同步上的那一稿");   // 同步中：编辑器已经是恢复稿
    expect(result).toBeNull();
    expect(failure).toBeNull();
    await r.append("，接着写");
    await wait(1300);
    await act(async () => { hung.resolve(); });
    await act(async () => { await retrying; });
    await wait(300);
    expect(d1Texts(client)).toEqual(["<p>起点正文，作者在写</p>", "<p>上次没同步上的那一稿，接着写</p>"]);
    expect(srv.drafts.s1.content).toBe("<p>上次没同步上的那一稿，接着写</p>");
    expect(result).toBeNull();                                   // 存下的是接着写的那一稿：不报「已同步」，记录先留着
    expect(failure).toMatchObject({ code: "RECOVERY_SUPERSEDED" });
    expect(WrRecovery.list().some((item) => item.id === entry.id)).toBe(true);
    expect(r.recoveryHas("作者在写")).toBe(true);                 // 恢复前的正文自动备份过
  }, LONG);

  it("S-B2 「重试同步」时编辑器里还有没交出去的几句、恢复稿的 PATCH 很慢：那几句先进同步与恢复，编辑器换成恢复稿；恢复稿存上后不会被旧编辑器的自动保存盖掉", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom, WrRecovery } = ctx;
    const srv = casServer(client);
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，作者在写</p>");
    await vi.waitFor(() => expect(r.status()).toContain("已保存"), T);
    await r.append("，还没存的一句");                           // 900 ms 的自动保存还没到点
    const hung = deferred();
    srv.hooks.patch = (url, body, apply) => {
      if (url.endsWith("/d1") && body.content.includes("上次没同步上")) { srv.hooks.patch = null; return hung.promise.then(() => apply(url, body)); }
      return apply(url, body);
    };
    const entry = WrRecovery.create({ sid: "ch01s1", html: "<p>上次没同步上的那一稿</p>", type: "unsynced", reason: "断网", label: "场景 ch01s1 · 未同步稿" });
    let result = null;
    let retrying = null;
    await act(async () => { retrying = WrRecovery.retry(entry.id).then((value) => { result = value; }); });
    expect(r.editor().textContent).toBe("上次没同步上的那一稿");
    await wait(1300);                                            // 旧编辑器的自动保存到点的时候
    await act(async () => { hung.resolve(); });
    await act(async () => { await retrying; });
    await wait(300);
    expect(srv.drafts.s1.content).toBe("<p>上次没同步上的那一稿</p>");
    expect(d1Texts(client).filter((html) => html.includes("还没存的一句"))).toEqual([]);
    expect(result).toMatchObject({ removed: true });
    expect(WrRecovery.list().some((item) => item.id === entry.id)).toBe(false);
    expect(r.recoveryHas("还没存的一句")).toBe(true);
  }, LONG);
});

describe("复核一 · 409 之后换上的是服务端眼下的版本（V-C）", () => {
  it("V-C 回到一场时后台复核发出、回得很慢，另一台设备随后存下 rev 3，作者的第一次保存 409：编辑器换成另一台设备的版本，不是复核那份旧快照", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom, WrDocs } = ctx;
    const srv = casServer(client);
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    const slow = deferred();
    srv.hooks.ensure = (sceneId, current) => (sceneId === "s1" && srv.drafts.s1.revision === 1
      ? current().then((snapshot) => slow.promise.then(() => snapshot))
      : current());
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await wait(50);
    srv.drafts.s1.revision = 3;                          // 另一台设备存下 rev 3
    srv.drafts.s1.content = `<p>${SERVER}</p>`;
    await r.append("，本机接着写");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(1), T);
    await act(async () => { slow.resolve(); });
    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    expect(WrDocs.state("ch01s1")).toMatchObject({ revision: 3, conflictPending: false });
    expect(r.recoveryHas("本机接着写")).toBe(true);
  }, LONG);
});

describe("复核一 · 状态照实说（V-D · R-V13）", () => {
  it("V-D 提升在路上时作者接着写、存上了新的一版：提升回来后不说「权威正文已更新」，提升按钮仍可点", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const srv = casServer(client);
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    const canonical = () => host.querySelector('[data-testid="canonical-status"]').textContent;
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，一</p>");
    await vi.waitFor(() => expect(r.status()).toContain("已保存"), T);
    const promoting = deferred();
    srv.hooks.promote = () => promoting.promise;
    await act(async () => { host.querySelector(".wr-canonical-promote").click(); });
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => /promote-canonical$/.test(url))).toBe(true), T);
    await r.append("，二");
    await wait(1300);
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(2), T);
    await act(async () => { promoting.resolve({ final_scene_row_id: "f1", draft_revision_no: 2, canonical_dirty: false }); });
    await wait(300);
    expect(srv.drafts.s1.revision).toBe(3);
    expect(canonical()).not.toContain("权威正文已更新");
    expect(host.querySelector(".wr-canonical-promote").disabled).toBe(false);
  }, LONG);

  it("R-V13 409 之后读不到服务端版本期间接着敲字：状态一直是「草稿保存失败」，不闪「正在保存」", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const srv = casServer(client);
    let down = false;
    srv.hooks.ensure = (sceneId, current) => (down && sceneId === "s1" ? Promise.reject(offline()) : current());
    srv.hooks.patch = (url, body, apply) => {
      if (url.endsWith("/d1") && !down) {
        down = true;
        srv.drafts.s1.revision = 3;
        srv.drafts.s1.content = `<p>${SERVER}</p>`;
        return Promise.reject(conflict());
      }
      return apply(url, body);
    };
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(r.status()).toBe("草稿保存失败"), T);
    await r.type("<p>起点正文，本机改了一句，接着写</p>");
    expect(r.status()).toBe("草稿保存失败");
    await wait(400);
    expect(r.status()).toBe("草稿保存失败");
    await wait(1000);
    expect(r.status()).toBe("草稿保存失败");
    r.expectKept("接着写");
    expect(patchesTo(client, "d1")).toHaveLength(1);
  }, LONG);
});

describe("复核一 · 刷新 / 关掉 / 切到后台前没到自动保存的几句（R-C1）", () => {
  it("R-C1 visibilitychange（切到后台）与 pagehide（刷新 / 关掉）时，编辑器里还没交出去的字已经在本机缓存和未同步标记里", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const srv = casServer(client);
    srv.hooks.patch = () => new Promise(() => {});      // 后端卡住：只看本机
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，切到后台前敲的一句</p>");
    await wait(300);
    const visibility = Object.getOwnPropertyDescriptor(Document.prototype, "visibilityState");
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    try {
      await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    } finally {
      delete document.visibilityState;
      if (visibility) expect(document.visibilityState).toBe(visibility.get.call(document));
    }
    r.expectDurable("切到后台前敲的一句");
    expect(window.localStorage.getItem(PENDING_KEY)).not.toBeNull();
    await r.append("，刷新前又敲的一句");
    await wait(300);
    await act(async () => { window.dispatchEvent(new Event("pagehide")); });
    r.expectDurable("刷新前又敲的一句");
    expect(client.apiPatch).toHaveBeenCalled();
  }, LONG);
});

/* ==========================================================
   W1 复核二：两路复核在 bd6f02d 上复现的顺序（房间级：真写作台 + 真 WrDocs / WrRecovery），断言安全的结果。
   ========================================================== */

const alertTexts = () => window.alert.mock.calls.map(([message]) => String(message));

describe("复核二 · 409 之后核对自己那一稿时读不到服务端（NV-2 · W1-R2A-1 · W1-R2B-4）", () => {
  it("NV-2 自己那一稿存上了但回包丢了，接着写的下一稿 409、核对的读取断网一次：不提示「别处被修改」，编辑器不退回到自己更旧的一稿；联网后最新的一稿存上", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const srv = casServer(client);
    let first = true;
    srv.hooks.patch = (url, body, apply) => {
      if (url.endsWith("/d1") && first) {
        first = false;
        return apply(url, body).then(() => Promise.reject(offline())); // 存上了，回包丢了
      }
      return apply(url, body);
    };
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await r.type("<p>起点正文，第一稿</p>");
    await vi.waitFor(() => expect(r.status()).toBe("草稿保存失败"), T);
    expect(srv.drafts.s1.content).toBe("<p>起点正文，第一稿</p>");
    let blip = true;
    srv.hooks.ensure = (sceneId, current) => {
      if (sceneId !== "s1" || !blip) return current();
      blip = false;
      return Promise.reject(offline());
    };
    await r.append("，第二稿");
    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(2), T);
    await wait(400);
    expect(conflictNotices()).toEqual([]);
    expect(r.editor().textContent).toContain("第二稿");
    expect(r.status()).toBe("草稿保存失败");
    r.expectDurable("第二稿");
    await act(async () => { window.dispatchEvent(new Event("online")); });
    await vi.waitFor(() => expect(srv.drafts.s1.content).toBe("<p>起点正文，第一稿，第二稿</p>"), T);
    await vi.waitFor(() => expect(r.status()).toBe("草稿已保存"), T);
    expect(conflictNotices()).toEqual([]);
    expect(ctx.WrRecovery.list()).toEqual([]);
    r.expectNoOlderPatch();
  }, LONG);
});

describe("复核二 · 同步与恢复的「恢复」（R2-A · NV-4 / R2-C · W1-R2A-2 · W1-R2A-3 · W1-R2B-6）", () => {
  it("R2-A 「恢复」进一章已批准锁定的场：先拒绝、说要先重新打开本章；只读的编辑器仍是终稿正文，离开再回来也是，不发请求", async () => {
    const ctx = await loadWriter({ catalog: [{ ...TWO_SCENE_CHAP, state: "approved" }] });
    const { client, WriterRoom, WrRecovery } = ctx;
    const srv = casServer(client);
    srv.hooks.patch = () => Promise.reject(Object.assign(new Error("approved chapter must be explicitly reopened"), {
      code: "CHAPTER_APPROVED_LOCKED", status: 409, retryable: false, details: {},
    }));
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    expect(host.querySelector(".wr-final-lock")).not.toBeNull();
    const entry = WrRecovery.create({ sid: "ch01s1", html: "<p>以前冲突时留下的本机稿</p>", type: "conflict", reason: "409", label: "场景 ch01s1 · 冲突本地稿" });
    let failure = null;
    await act(async () => { try { await WrRecovery.restore(entry.id); } catch (error) { failure = error; } });
    await wait(300);
    expect(failure).toMatchObject({ code: "CHAPTER_APPROVED_LOCKED" });
    expect(failure.message).toContain("重新打开本章");
    expect(failure.message).not.toMatch(/网络或服务端出错|会再同步/);
    expect(r.editor().textContent).toContain("起点正文");
    expect(r.editor().textContent).not.toContain("以前冲突时留下的本机稿");
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    await openScene("ch01s1");
    await wait(300);
    expect(r.editor().textContent).toContain("起点正文");
    expect(patchesTo(client, "d1")).toHaveLength(0);
    expect(WrRecovery.list().some((item) => item.id === entry.id)).toBe(true);
  }, LONG);

  it("NV-4 / R2-C 写作台开着这一场时「恢复」撞上 409、随后读不到服务端版本：同步与恢复收到的说明和编辑器一致；连上之后编辑器换成服务端版本，两份本机稿都在", async () => {
    const ctx = await loadWriter();
    const { client, WriterRoom, WrRecovery } = ctx;
    const srv = casServer(client);
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    // 另一台设备存了 rev 2；这一页一直开着这一场（开着时不复核），还在 rev 1 上
    srv.drafts.s1.revision = 2;
    srv.drafts.s1.content = `<p>${SERVER}</p>`;
    srv.hooks.ensure = (sceneId, current) => (sceneId === "s1" ? Promise.reject(offline()) : current());
    const entry = WrRecovery.create({ sid: "ch01s1", html: "<p>要恢复的那一稿</p>", type: "unsynced", reason: "断网", label: "场景 ch01s1 · 未同步稿" });
    let failure = null;
    await act(async () => { try { await WrRecovery.restore(entry.id); } catch (error) { failure = error; } });
    await wait(400);
    expect(failure).toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    expect(failure.message).toContain("还没读下来");
    expect(failure.message).not.toContain("编辑器已换成服务端的最新版本");
    expect(r.editor().textContent).toContain("要恢复的那一稿");
    expect(r.status()).toBe("草稿保存失败");
    expect(srv.drafts.s1.content).toBe(`<p>${SERVER}</p>`);
    srv.hooks.ensure = null;
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    await vi.waitFor(() => expect(r.editor().textContent).toContain(SERVER), T);
    expect(r.recoveryHas("要恢复的那一稿")).toBe(true);
    expect(r.recoveryHas("起点正文")).toBe(true);
    r.expectServerNeverOverwritten();
  }, LONG);
});

describe("复核二 · 提升只提升作者眼前的那一稿（R2-B2 · NV-5 · W1-R2A-4 · W1-R2B-1）", () => {
  it("R2-B2 打开时第一次水合失败、编辑器是这台电脑的旧缓存，点「提升」：不提升编辑器没显示过的服务端正文；编辑器换成它，提示已在别处更新", async () => {
    window.localStorage.setItem(CACHE_KEY, "<p>这台电脑上的旧稿</p>");
    const ctx = await loadWriter();
    const { client, WriterRoom } = ctx;
    const srv = casServer(client);
    srv.drafts.s1.revision = 4;
    srv.drafts.s1.content = "<p>另一台设备上改过的一版</p>";
    let ensures = 0;
    srv.hooks.ensure = (sceneId, current) => {
      if (sceneId !== "s1") return current();
      ensures += 1;
      return ensures === 1 ? Promise.reject(offline()) : current();
    };
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("这台电脑上的旧稿"), T);
    await wait(400);
    expect(ensures).toBe(1);
    await act(async () => { host.querySelector(".wr-canonical-promote").click(); });
    await wait(800);
    expect(srv.promoted).toEqual([]);
    expect(r.editor().textContent).toContain("另一台设备上改过的一版");
    expect(alertTexts().some((message) => message.includes("已在别处更新"))).toBe(true);
    expect(client.apiPatch).not.toHaveBeenCalled();
  }, LONG);

  it("NV-5 两个标签页：写作台回到一场时，共用读缓存里是另一个标签页没同步上的字——编辑器仍是这一页的那一份，提升的就是编辑器里的那一版", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { WriterRoom } = ctx;
    const srv = casServer(ctx.client);
    // 本机缓存打开时就和服务端一样：这一页水合时不必往读缓存里写（过去于是也没有这一页自己的那一份）。
    // 在 store 装好之后才放：前一个用例留下的旧实例这时已经退役，不会再改写它
    window.localStorage.setItem(CACHE_KEY, "<p>起点正文</p>");
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("起点正文"), T);
    await wait(200);
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    // 另一个标签页在这一场写了一段，服务端回 5xx：它没同步上的字在共用读缓存里、带着未同步标记
    window.localStorage.setItem(CACHE_KEY, "<p>起点正文，B 页没存上的一段</p>");
    window.localStorage.setItem(PENDING_KEY, String(Date.now()));
    await openScene("ch01s1");
    await wait(400);
    expect(r.editor().textContent).toContain("起点正文");
    expect(r.editor().textContent).not.toContain("B 页没存上的一段");
    await act(async () => { host.querySelector(".wr-canonical-promote").click(); });
    await wait(600);
    expect(srv.promoted.map((entry) => entry.content)).toEqual(["<p>起点正文</p>"]);
    // 另一页没同步上的字没丢：还在共用读缓存里（带着未同步标记），或已经进了同步与恢复
    const inSlot = window.localStorage.getItem(CACHE_KEY) === "<p>起点正文，B 页没存上的一段</p>"
      && window.localStorage.getItem(PENDING_KEY) != null;
    expect({ kept: inSlot || r.recoveryHas("B 页没存上的一段") }).toEqual({ kept: true });
  }, LONG);
});

describe("复核二 · 空白也是正文（NV-7 · W1-R2B-5）", () => {
  it("NV-7 另一台设备只加了段首缩进（全角空格），回到这一场时作者正在写、后台复核才回来：缩进不被静默去掉——编辑器换成它，刚写的进同步与恢复", async () => {
    const ctx = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const { client, WriterRoom } = ctx;
    const srv = casServer(client);
    srv.drafts.s1.content = "<p>他终于回来了。</p>";
    const { host } = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, () => host);
    await vi.waitFor(() => expect(r.editor().textContent).toContain("他终于回来了"), T);
    await openScene("ch01s2");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("第二场"), T);
    const indented = "<p>　　他终于回来了。</p>";
    srv.drafts.s1.revision = 3;
    srv.drafts.s1.content = indented;
    const slow = deferred();
    let held = 0;
    srv.hooks.ensure = (sceneId, current) => {
      if (sceneId !== "s1" || held > 0) return current();
      held += 1;
      return current().then((snapshot) => slow.promise.then(() => snapshot));
    };
    await openScene("ch01s1");
    await vi.waitFor(() => expect(r.editor().textContent).toContain("他终于回来了"), T);
    await r.type("<p>他终于回来了。门没关。</p>");
    await act(async () => { slow.resolve(); });
    await wait(1400);
    expect(r.editor().innerHTML).toContain("　　他终于回来了");
    expect(r.recoveryHas("门没关")).toBe(true);
    patchesTo(client, "d1").forEach(([, body]) => {
      if (Number(body.base_revision_no) >= 3) expect(body.content).toContain("　");
    });
    expect(srv.drafts.s1.content).toContain("　");
  }, LONG);
});
