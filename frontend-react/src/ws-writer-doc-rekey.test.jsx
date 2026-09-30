// 写作台正文 · 乐观新建的场换了名字（W1 复核六 · W1-R6B-1 · W1-R6B-2，房间级：真的写作台 + 目录 + WrDocs）。
// 「创建第一章」「加一场」之后作者马上就能写：那一场先用临时 sid（tmp_…），后端建好、目录重拉之后换成稳定的 scene_id，
// 写作台随之换到新名字上。过去 WrDocs 按名字各起一台状态机：新名字下编辑器是空的、接着写的字撞上 409 说「在别处被修改过」，
// 临时 sid 那一台停着的一稿之后在较新的一稿后面补发；建章没成时写下的字只在一个谁也不读的本机键里，刷新之后也找不回来。
// 这里每一条断言的都是安全、照实的结果。
//
// 世界：一部空作品。「创建第一章」走真的 WsCatalog（乐观的一章、开场一场用临时 sid，写作台马上在它上面）；建章请求
// （POST …/catalog/chapters）按住，直到用例放行；之后 POST …/chapters/c9/scenes 回 scene_id s9，目录重拉时这一章带着稳定的 s9
// （真的 catTrackAliases 记下 tmp_… → s9，真的写作台换到 s9 上）。作者稿服务端（backend author_drafts.py）：第一次 ensure 建 d9
// （修订号 1、空稿）；请求到了服务端那一刻就读 / 改状态，net 毫秒后回包；PATCH 按修订号比对（409 带 current_revision_no），
// 字没变时修订号不动。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

const T = { timeout: 8000, interval: 25 };
const LONG = 60000;
const plain = (html) => String(html || "").replace(/<[^>]+>/g, "");
const wait = (ms) => act(async () => { await new Promise((resolve) => setTimeout(resolve, ms)); });
const later = (ms, fn) => new Promise((resolve, reject) => setTimeout(() => { try { resolve(fn()); } catch (e) { reject(e); } }, ms));
const conflict = (current) => Object.assign(new Error("author draft has changed; refresh before saving"), {
  code: "AUTHOR_DRAFT_CONFLICT", status: 409, details: { current_revision_no: current },
});
const offline = () => Object.assign(new Error("offline"), { code: "NETWORK_ERROR", status: 0, retryable: true });

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

const NEW_CHAPTER = {
  slug: "ch01", chapter_id: "c9", no: "01", title: "新建的一章", state: "writing", act: "act1", current: true,
  words: { cur: 0, target: 0 },
  scenes: [{ slug: "s9", scene_id: "s9", title: "新场景", kind: "proactive", state: "writing", words: 0, brief: {} }],
};

/* chapterAnswer：建章请求放行之后服务端回什么（默认建好了 c9）。conflicts：撞上 409 的 PATCH 数 */
function newWorld() {
  return {
    catalog: [], gate: deferred(), chapterAnswer: { chapter: { chapter_id: "c9", slug: "ch01" } },
    draft: null, ensures: [], patches: [], conflicts: 0, net: 15, patchFail: null,
  };
}

function installWorld(client, w) {
  installApiRouter(client, { catalog: [] });
  const baseGet = client.apiGet.getMockImplementation();
  client.apiGet.mockImplementation((url) => {
    if (/\/api\/v2\/projects\/[^/]+\/catalog(\?|$)/.test(url)) return Promise.resolve({ chapters: w.catalog });
    if (/deep-review/.test(url)) return Promise.resolve({ findings: [], text: "" });
    return baseGet(url);
  });
  const snapshot = () => ({ draft: { draft_id: w.draft.id, revision_no: w.draft.revision, content: w.draft.content } });
  client.apiPost.mockImplementation((url) => {
    if (/\/catalog\/chapters$/.test(url)) return w.gate.promise.then(() => w.chapterAnswer);
    if (/\/catalog\/chapters\/c9\/scenes$/.test(url)) {
      w.catalog = [NEW_CHAPTER];
      return Promise.resolve({ scene: { scene_id: "s9", slug: "s9" } });
    }
    const ensure = /\/author-drafts\/scene\/([^/]+)\/ensure$/.exec(url);
    if (ensure) {
      w.ensures.push(ensure[1]);
      if (!w.draft) w.draft = { id: "d9", revision: 1, content: "" };
      const snap = snapshot();
      return later(w.net, () => snap);
    }
    return Promise.resolve({});
  });
  client.apiPatch.mockImplementation((url, body) => {
    if (!/\/author-drafts\//.test(url)) return Promise.resolve({});
    w.patches.push([body.base_revision_no, plain(body.content)]);
    const fail = w.patchFail ? w.patchFail(body) : null;
    if (fail) return later(w.net, () => { throw fail; });
    if (Number(body.base_revision_no) !== w.draft.revision) {
      const current = w.draft.revision;
      w.conflicts += 1;
      return later(w.net, () => { throw conflict(current); });
    }
    if (body.content !== w.draft.content) {
      w.draft.revision += 1;
      w.draft.content = body.content;
    }
    const snap = snapshot();
    return later(w.net, () => snap);
  });
  return w;
}

async function loadWriter(w, { needScene = null } = {}) {
  const client = await import("./lib/client.js");
  installWorld(client, w);
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog.ready()).toBe(true), T);
  if (needScene) await vi.waitFor(() => expect(window.WsCatalog.sceneById(needScene)).toBeTruthy(), T);
  const store = await import("./wr-doc-store.jsx");
  const writer = await import("./ws-writer.jsx");
  return { client, w, ...writer, ...store };
}

const mounted = [];
async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return { host };
}
async function unmountAll() {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
}

function room(ctx, host) {
  const editor = () => host.querySelector(".wr-editor");
  const status = () => {
    const node = host.querySelector('[data-testid="draft-save-status"]');
    return node ? node.textContent : null;
  };
  const createButton = () => Array.from(host.querySelectorAll("button")).find((button) => button.textContent.includes("创建第一章"));
  return {
    editor, status, createButton,
    async type(html) {
      await act(async () => {
        editor().innerHTML = html;
        editor().dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: "" }));
      });
    },
    /* 一句话眼下在哪（这一场的编辑器 / 读缓存 / 同步与恢复 / 服务端） */
    where(needle, sid) {
      const inEditor = !!(editor() && editor().textContent.includes(needle));
      let inSceneCache = false;
      try { inSceneCache = String(ctx.WrDocs.cachedHTML(sid) || "").includes(needle); } catch (e) {}
      const recovery = ctx.WrRecovery.list().filter((entry) => String(entry.html || "").includes(needle)).map((entry) => [entry.sid, entry.type]);
      const onServer = !!(ctx.w.draft && plain(ctx.w.draft.content).includes(needle));
      return { inEditor, inSceneCache, recovery, onServer };
    },
  };
}

const alerts = () => window.alert.mock.calls.map(([message]) => String(message));
const elsewhere = () => alerts().filter((message) => message.includes("在别处"));

function matchMedia() {
  return { matches: false, media: "", addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() };
}
const innerTextDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");

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
  await unmountAll();
  if (!innerTextDescriptor) delete HTMLElement.prototype.innerText;
  vi.restoreAllMocks();
});

/* 空作品 →「创建第一章」→ 作者马上在临时 sid 上写 → 建章落地、目录带着 s9 回来、写作台换到 s9 上。
   → { tmp, typedNode }：typedNode = 作者写下的那一段在编辑器里的节点（编辑器整篇重装过就不再是它） */
async function createFirstChapterAndType(ctx, r, html, { beforeRelease } = {}) {
  await vi.waitFor(() => expect(r.createButton()).toBeTruthy(), T);
  await act(async () => { r.createButton().click(); });
  const writing = () => { const hit = window.WsCatalog.writingScene(); return hit && hit.scene ? hit.scene.sid : null; };
  await vi.waitFor(() => expect(String(writing() || "")).toMatch(/^tmp_/), T);
  const tmp = writing();
  await wait(100);
  await r.type(html);
  const typedNode = r.editor().firstChild;
  await wait(300);                                                          // 远在 900 ms 的自动保存之前
  if (beforeRelease) beforeRelease();
  await act(async () => { ctx.w.gate.resolve(); });
  await vi.waitFor(() => expect(window.WsCatalog.sceneById("s9")).toBeTruthy(), T);
  await vi.waitFor(() => expect(ctx.w.ensures.length).toBeGreaterThan(0), T);
  return { tmp, typedNode };
}

describe("复核六 · 创建第一章后马上动笔：临时 sid 换成稳定的 scene_id（W1-R6B-1）", () => {
  it("NB6-T1 在线：第一句一直在编辑器里（换名字不重装编辑器）；接着写的第二句和它一起存上；不说「在别处」", async () => {
    const ctx = await loadWriter(newWorld());
    const { host } = await render(<ctx.WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, host);
    const { tmp, typedNode } = await createFirstChapterAndType(ctx, r, "<p>第一句写在新场景里。</p>");
    await wait(80);
    expect(r.editor().textContent).toContain("第一句写在新场景里。");
    await wait(1500);                                                       // 那一台状态机把第一句存上（ensure、PATCH）
    expect(r.editor().textContent).toContain("第一句写在新场景里。");
    expect(r.editor().firstChild).toBe(typedNode);                          // 同一场只是换了名字：编辑器没有整篇重装
    expect(ctx.WrDocs.state(tmp)).toEqual(ctx.WrDocs.state("s9"));           // 一场一台状态机
    expect(plain(ctx.w.draft.content)).toBe("第一句写在新场景里。");
    await r.type(`${r.editor().innerHTML}<p>第二句接着写。</p>`);
    await wait(2000);
    expect(plain(ctx.w.draft.content)).toBe("第一句写在新场景里。第二句接着写。");
    expect(ctx.WrDocs.state("s9")).toMatchObject({ dirty: false, conflictPending: false, lastSaveError: null });
    expect(r.status()).toBe("草稿已保存");
    expect(ctx.w.conflicts).toBe(0);                                        // 没有在作者没见过的修订号上保存
    expect(elsewhere()).toEqual([]);
    expect(ctx.WrRecovery.list()).toEqual([]);
  }, LONG);

  it("NB6-T2 临时 sid 那一稿的第一次保存断网、之后刷新（网回来了）：刷新之后第一句在这一场的编辑器里，并且传上服务端", async () => {
    const w = newWorld();
    const ctx = await loadWriter(w);
    await render(<ctx.WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, mounted[0].host);
    await createFirstChapterAndType(ctx, r, "<p>第一句写在新场景里。</p>", { beforeRelease: () => { w.patchFail = () => offline(); } });
    await wait(1500);
    expect(r.editor().textContent).toContain("第一句写在新场景里。");
    expect(r.status()).toBe("草稿保存失败");
    // 刷新：页面没了，网回来了
    await unmountAll();
    w.patchFail = null;
    vi.resetModules();
    const again = await loadWriter(w, { needScene: "s9" });
    const second = await render(<again.WriterRoom t={{}} setTweak={() => {}} />);
    const r2 = room(again, second.host);
    await vi.waitFor(() => expect(r2.editor()).toBeTruthy(), T);
    await vi.waitFor(() => expect(r2.editor().textContent).toContain("第一句写在新场景里。"), T);
    await act(async () => { window.dispatchEvent(new Event("online")); window.dispatchEvent(new Event("focus")); });
    await vi.waitFor(() => expect(plain(w.draft.content)).toContain("第一句写在新场景里。"), T);
    await vi.waitFor(() => expect(r2.status()).toBe("草稿已保存"), T);
    expect(again.WrRecovery.list()).toEqual([]);
    expect(elsewhere()).toEqual([]);
    expect(Object.keys(window.localStorage).filter((key) => key.startsWith("wr-doc") && key.includes("tmp_"))).toEqual([]);
  }, LONG);

  it("NB6-T3 临时 sid 那一稿连同离场补发都断网，作者在新名字下接着写：两句一起存上，窗口重新聚焦不补发较旧的一稿，不说「在别处被修改过」", async () => {
    const w = newWorld();
    const ctx = await loadWriter(w);
    const { host } = await render(<ctx.WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, host);
    let failures = 2;                                                        // 临时 sid 那一次 PATCH 与离场冲刷补发的那一次
    await createFirstChapterAndType(ctx, r, "<p>第一句写在新场景里。</p>", {
      beforeRelease: () => { w.patchFail = () => (failures-- > 0 ? offline() : null); },
    });
    await wait(800);
    expect(r.editor().textContent).toContain("第一句写在新场景里。");
    await r.type(`${r.editor().innerHTML}<p>第二句另起一段。</p>`);
    await wait(1500);
    expect(plain(w.draft.content)).toBe("第一句写在新场景里。第二句另起一段。");
    const sent = w.patches.length;
    await act(async () => { window.dispatchEvent(new Event("focus")); });
    await wait(800);
    expect(w.patches).toHaveLength(sent);                                   // 停着的较旧一稿没有补发
    expect(plain(w.draft.content)).toBe("第一句写在新场景里。第二句另起一段。");
    expect(ctx.w.conflicts).toBe(0);
    expect(elsewhere()).toEqual([]);
    expect(ctx.WrDocs.state("s9")).toMatchObject({ dirty: false, conflictPending: false, lastSaveError: null });
  }, LONG);
});

describe("复核六 · 创建第一章后马上动笔、这一章没建成，目录退回服务端的版本（W1-R6B-2）", () => {
  /* 建章请求回来了、却没建出这一章（目录写入之后以服务端为准重拉：服务端没有它），乐观的一章随之消失——对写作台来说和建章请求
     失败一样：那一场不在目录里了。（请求被拒的那条路另有目录模块自己的问题：ws-catalog-diff 的 p.finally 没接失败，会留下一个
     没人处理的拒绝，这里不走它） */
  it("NB6-T4 写下的一段进了同步与恢复并提示（编辑器随那一场一起没了）；刷新之后还在", async () => {
    const w = newWorld();
    w.chapterAnswer = {};
    const ctx = await loadWriter(w);
    const { host } = await render(<ctx.WriterRoom t={{}} setTweak={() => {}} />);
    const r = room(ctx, host);
    await vi.waitFor(() => expect(r.createButton()).toBeTruthy(), T);
    await act(async () => { r.createButton().click(); });
    await vi.waitFor(() => expect(String((window.WsCatalog.writingScene() || { scene: {} }).scene.sid || "")).toMatch(/^tmp_/), T);
    await wait(100);
    await r.type("<p>建章还没回来，先写下的第一段。</p>");
    await wait(1400);                                                       // 900 ms 的自动保存把它交给了 WrDocs（它在等后端 id）
    await act(async () => { w.gate.resolve(); });
    await vi.waitFor(() => expect(window.WsCatalog.get().length).toBe(0), T);
    await vi.waitFor(() => expect(ctx.WrRecovery.list().some((entry) => plain(entry.html).includes("先写下的第一段"))).toBe(true), T);
    expect(alerts().filter((message) => message.includes("不在目录里了"))).toHaveLength(1);
    expect(ctx.WrRecovery.list().filter((entry) => plain(entry.html).includes("先写下的第一段"))).toEqual([expect.objectContaining({ durable: true })]);
    expect(Object.keys(window.localStorage).filter((key) => key.startsWith("wr-doc") && String(window.localStorage.getItem(key)).includes("先写下的第一段"))).toEqual([]);
    await unmountAll();
    vi.resetModules();
    const again = await loadWriter(w);
    const second = await render(<again.WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(room(again, second.host).createButton()).toBeTruthy(), T);
    expect(again.WrRecovery.list().filter((entry) => plain(entry.html).includes("先写下的第一段"))).toHaveLength(1);
  }, LONG);
});
