// useDocBinding 的房间级回归：409 冲突之后编辑器必须换成服务端版本（F03-01），
// 否则下一次敲字会带着新的 base_revision_no 把另一台设备的正文静默盖掉。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_CHAP, installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

const T = { timeout: 4000, interval: 25 };
const LONG = 20000;
const mounted = [];
const innerTextDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");
const TWO_SCENE_CHAP = {
  ...DEFAULT_CHAP,
  scenes: [...DEFAULT_CHAP.scenes, { ...DEFAULT_CHAP.scenes[0], slug: "ch01s2", scene_id: "s2", title: "回潮" }],
};

const conflict = () => Object.assign(new Error("conflict"), { code: "AUTHOR_DRAFT_CONFLICT", status: 409 });

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
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return host;
}

async function typeInto(editor, html) {
  await act(async () => {
    editor.innerHTML = html;
    editor.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: "" }));
  });
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

describe("useDocBinding · 409 冲突之后", () => {
  it("编辑器换成服务端版本；下一次敲字在服务端版本上续写，不盖掉另一台设备的正文", async () => {
    const { client, WriterRoom, WrRecovery } = await loadWriter();
    let ensures = 0;
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        ensures += 1;
        return Promise.resolve(ensures === 1
          ? { draft: { draft_id: "d1", revision_no: 1, content: "<p>起点正文</p>" } }
          : { draft: { draft_id: "d1", revision_no: 3, content: "<p>另一台设备的正文</p>" } });
      }
      return Promise.resolve({});
    });
    const conflict = Object.assign(new Error("conflict"), { code: "AUTHOR_DRAFT_CONFLICT", status: 409 });
    client.apiPatch.mockImplementation((url, body) => {
      if (/\/author-drafts\/d1$/.test(url)) {
        if (client.apiPatch.mock.calls.length === 1) return Promise.reject(conflict);
        return Promise.resolve({ draft: { draft_id: "d1", revision_no: Number(body.base_revision_no) + 1, content: body.content } });
      }
      return Promise.resolve({});
    });

    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().textContent).toContain("起点正文"), T);

    await typeInto(editor(), "<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    // 本机那一稿进了同步与恢复，编辑器换成服务端版本（toast 说的就是这件事）
    await vi.waitFor(() => expect(editor().textContent).toContain("另一台设备的正文"), T);
    expect(editor().textContent).not.toContain("本机改了一句");
    expect(WrRecovery.list().some((entry) => entry.type === "conflict" && entry.html.includes("本机改了一句"))).toBe(true);

    // 下一次敲字：在服务端版本上续写
    await typeInto(editor(), "<p>另一台设备的正文，再添一句</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(2), T);
    const [, body] = client.apiPatch.mock.calls[1];
    expect(body.base_revision_no).toBe(3);
    expect(body.content).toContain("另一台设备的正文");
  }, LONG);

  it("保存进行中又敲了字：新敲的那一稿也放进同步与恢复，再换成服务端版本", async () => {
    const { client, WriterRoom, WrRecovery } = await loadWriter();
    let ensures = 0;
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        ensures += 1;
        return Promise.resolve(ensures === 1
          ? { draft: { draft_id: "d1", revision_no: 1, content: "<p>起点正文</p>" } }
          : { draft: { draft_id: "d1", revision_no: 3, content: "<p>另一台设备的正文</p>" } });
      }
      return Promise.resolve({});
    });
    let rejectFirst;
    const conflict = Object.assign(new Error("conflict"), { code: "AUTHOR_DRAFT_CONFLICT", status: 409 });
    client.apiPatch.mockImplementation(() => new Promise((_, reject) => { rejectFirst = reject; }));

    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().textContent).toContain("起点正文"), T);
    await typeInto(editor(), "<p>第一处改动</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    // 请求还在路上，作者又敲了一句（下一次自动保存还没到点）
    await typeInto(editor(), "<p>第一处改动，第二处改动</p>");
    await act(async () => { rejectFirst(conflict); });

    await vi.waitFor(() => expect(editor().textContent).toContain("另一台设备的正文"), T);
    const htmls = WrRecovery.list().map((entry) => entry.html);
    expect(htmls.some((html) => html.includes("第二处改动"))).toBe(true);
  }, LONG);

  it("WrDocs 在 409 之后重新水合时自动保存到点：不叠一次保存，编辑器照样换成服务端版本", async () => {
    const { client, WriterRoom, WrRecovery } = await loadWriter();
    let ensures = 0;
    const rehydrate = deferred();
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        ensures += 1;
        if (ensures === 1) return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "<p>起点正文</p>" } });
        return rehydrate.promise; // 409 之后的重新水合很慢
      }
      return Promise.resolve({});
    });
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1
      ? Promise.reject(conflict())
      : Promise.resolve({ draft: { draft_id: "d1", revision_no: Number(body.base_revision_no) + 1, content: body.content } })));

    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().textContent).toContain("起点正文"), T);
    await typeInto(editor(), "<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(ensures).toBe(2), T); // 409 已经回来，WrDocs 正在重新水合
    // 作者接着敲字；900 ms 的自动保存在重新水合还没回来时到点
    await typeInto(editor(), "<p>起点正文，本机改了一句，又添一句</p>");
    await wait(1200);
    await act(async () => { rehydrate.resolve({ draft: { draft_id: "d1", revision_no: 3, content: "<p>另一台设备的正文</p>" } }); });

    await vi.waitFor(() => expect(editor().textContent).toContain("另一台设备的正文"), T);
    await wait(300);
    // 没有带着刷新后的修订号把本机稿存上去（那会静默盖掉另一台设备的正文）
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
    expect(WrRecovery.list().some((entry) => entry.html.includes("又添一句"))).toBe(true);

    await typeInto(editor(), "<p>另一台设备的正文，再添一句</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(2), T);
    expect(client.apiPatch.mock.calls[1][1]).toMatchObject({ base_revision_no: 3 });
    expect(client.apiPatch.mock.calls[1][1].content).toContain("另一台设备的正文");
  }, LONG);

  it("保存还在路上时自动保存又到点：等这一次落地；它 409 时编辑器换成服务端版本，两份本机稿都在同步与恢复", async () => {
    const { client, WriterRoom, WrRecovery } = await loadWriter();
    let ensures = 0;
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        ensures += 1;
        return Promise.resolve(ensures === 1
          ? { draft: { draft_id: "d1", revision_no: 1, content: "<p>起点正文</p>" } }
          : { draft: { draft_id: "d1", revision_no: 3, content: "<p>另一台设备的正文</p>" } });
      }
      return Promise.resolve({});
    });
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1
      ? first.promise
      : Promise.resolve({ draft: { draft_id: "d1", revision_no: Number(body.base_revision_no) + 1, content: body.content } })));

    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().textContent).toContain("起点正文"), T);
    await typeInto(editor(), "<p>第一处改动</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    await typeInto(editor(), "<p>第一处改动，第二处改动</p>");
    await wait(1200); // 下一次自动保存到点时，第一次还在路上
    await act(async () => { first.reject(conflict()); });

    await vi.waitFor(() => expect(editor().textContent).toContain("另一台设备的正文"), T);
    await wait(300);
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
    const htmls = WrRecovery.list().map((entry) => entry.html);
    expect(htmls.some((html) => html.includes("第一处改动") && !html.includes("第二处改动"))).toBe(true);
    expect(htmls.some((html) => html.includes("第二处改动"))).toBe(true);
  }, LONG);

  it("409 时保存路上新敲的字存不进本机（空间不足）：编辑器照样换成服务端版本，并说清那段字只在本次会话里", async () => {
    const { client, WriterRoom, WrRecovery } = await loadWriter();
    let ensures = 0;
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        ensures += 1;
        return Promise.resolve(ensures === 1
          ? { draft: { draft_id: "d1", revision_no: 1, content: "<p>起点正文</p>" } }
          : { draft: { draft_id: "d1", revision_no: 3, content: "<p>另一台设备的正文</p>" } });
      }
      return Promise.resolve({});
    });
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1
      ? first.promise
      : Promise.resolve({ draft: { draft_id: "d1", revision_no: Number(body.base_revision_no) + 1, content: body.content } })));

    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().textContent).toContain("起点正文"), T);
    await typeInto(editor(), "<p>第一处改动</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    await typeInto(editor(), "<p>第一处改动，第二处改动</p>");
    // 配额：WrDocs 的冲突副本还放得下，编辑器里新敲的那一份放不下了
    const realSet = Storage.prototype.setItem;
    let recoveryWrites = 0;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function setItem(key, value) {
      if (String(key).startsWith("wr-recovery:v1:")) {
        recoveryWrites += 1;
        if (recoveryWrites > 1) throw Object.assign(new Error("full"), { name: "QuotaExceededError" });
      }
      return realSet.call(this, key, value);
    });
    await act(async () => { first.reject(conflict()); });

    // 编辑器不留在本机稿上：WrDocs 的修订号已经刷新，留着的话下一次敲字会把另一台设备的正文静默盖掉
    await vi.waitFor(() => expect(editor().textContent).toContain("另一台设备的正文"), T);
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("本次会话"));
    const pending = WrRecovery.list().find((entry) => entry.html.includes("第二处改动"));
    expect(pending && pending.durable).toBe(false);
    await wait(1200);
    expect(client.apiPatch).toHaveBeenCalledTimes(1);
  }, LONG);
});

describe("useDocBinding · 保存还在路上时换场", () => {
  function routeTwoScenes(client) {
    let ensures = 0;
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        ensures += 1;
        return Promise.resolve(ensures === 1
          ? { draft: { draft_id: "d1", revision_no: 1, content: "<p>起点正文</p>" } }
          : { draft: { draft_id: "d1", revision_no: 3, content: "<p>另一台设备的正文</p>" } });
      }
      if (/\/author-drafts\/scene\/s2\/ensure$/.test(url)) {
        return Promise.resolve({ draft: { draft_id: "d2", revision_no: 1, content: "<p>第二场</p>" } });
      }
      return Promise.resolve({});
    });
    return () => ensures;
  }
  const patchesTo = (client, draftId) => client.apiPatch.mock.calls.filter(([url]) => url.endsWith(`/author-drafts/${draftId}`));

  it("之后没再敲字：不再叠一次同样的正文；那一次 409 时另一台设备的正文不被盖掉", async () => {
    const { client, WriterRoom, WrRecovery } = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    const ensures = routeTwoScenes(client);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1
      ? first.promise
      : Promise.resolve({ draft: { draft_id: "d1", revision_no: Number(body.base_revision_no) + 1, content: body.content } })));

    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().textContent).toContain("起点正文"), T);
    await typeInto(editor(), "<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:writer-scene", { detail: "ch01s2" })); });
    await vi.waitFor(() => expect(editor().textContent).toContain("第二场"), T);
    await act(async () => { first.reject(conflict()); });

    await vi.waitFor(() => expect(ensures()).toBe(2), T); // WrDocs 按冲突重新水合了第一场
    await wait(300);
    expect(patchesTo(client, "d1")).toHaveLength(1);
    expect(WrRecovery.list().some((entry) => entry.html.includes("本机改了一句"))).toBe(true);
  }, LONG);

  it("那一次断网失败：离场之后补发同一稿", async () => {
    const { client, WriterRoom } = await loadWriter({ catalog: [TWO_SCENE_CHAP] });
    routeTwoScenes(client);
    const first = deferred();
    client.apiPatch.mockImplementation((url, body) => (client.apiPatch.mock.calls.length === 1
      ? first.promise
      : Promise.resolve({ draft: { draft_id: "d1", revision_no: Number(body.base_revision_no) + 1, content: body.content } })));

    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().textContent).toContain("起点正文"), T);
    await typeInto(editor(), "<p>起点正文，本机改了一句</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:writer-scene", { detail: "ch01s2" })); });
    await vi.waitFor(() => expect(editor().textContent).toContain("第二场"), T);
    await act(async () => { first.reject(Object.assign(new Error("offline"), { code: "NETWORK_ERROR" })); });

    await vi.waitFor(() => expect(patchesTo(client, "d1")).toHaveLength(2), T);
    expect(patchesTo(client, "d1")[1][1]).toMatchObject({ base_revision_no: 1 });
    expect(patchesTo(client, "d1")[1][1].content).toContain("本机改了一句");
  }, LONG);
});

describe("useDocBinding · 字数以服务端 words_rollup 为准（F03-08）", () => {
  it("保存回包带回的场景 / 章节字数不被本机计数盖掉", async () => {
    const { client, WriterRoom } = await loadWriter();
    client.apiPost.mockImplementation((url) => {
      if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
        return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "<p>起点</p>" } });
      }
      return Promise.resolve({});
    });
    client.apiPatch.mockImplementation((url, body) => Promise.resolve({
      draft: { draft_id: "d1", revision_no: Number(body.base_revision_no) + 1 },
      words_rollup: { scene_words: 321, chapter_words: 654 },
    }));
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().textContent).toContain("起点"), T);
    await typeInto(editor(), "<p>起点加五个字</p>");
    await vi.waitFor(() => expect(client.apiPatch).toHaveBeenCalledTimes(1), T);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="draft-save-status"]').textContent).toContain("已保存"), T);
    const hit = window.WsCatalog.sceneById("ch01s1");
    expect(hit.scene.words).toBe(321);
    expect(hit.chapter.words.cur).toBe(654);
  }, LONG);
});

describe("写作台 · 当前段标记（F03-09）", () => {
  it("光标挪到另一段：只有那一段是当前段；拆段 / 粘贴复制过来的 is-active 会被清掉", async () => {
    const { client, WriterRoom } = await loadWriter();
    client.apiPost.mockImplementation((url) => (
      /\/author-drafts\/scene\/s1\/ensure$/.test(url)
        ? Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "<p>第一段。</p><p>第二段。</p><p>第三段。</p>" } })
        : Promise.resolve({})
    ));
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().querySelectorAll("p").length).toBe(3), T);
    const put = async (index, key) => {
      const block = editor().querySelectorAll("p")[index];
      const range = document.createRange();
      range.setStart(block.firstChild, 1);
      range.collapse(true);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
      await act(async () => editor().dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key })));
    };
    const active = () => [...editor().querySelectorAll("p")].map((p) => p.classList.contains("is-active"));
    await put(0, "ArrowDown");
    expect(active()).toEqual([true, false, false]);
    await put(2, "ArrowDown");
    expect(active()).toEqual([false, false, true]);
    // 浏览器拆段时把 class 一起复制给了新段落
    editor().querySelectorAll("p")[0].classList.add("is-active");
    await put(1, "ArrowUp");
    expect(active()).toEqual([false, true, false]);
  }, LONG);
});


describe("写作台 · 目录只改了字数（F03-10）", () => {
  it("自动保存回写的字数不让整间写作台重渲染；目录里这一场的设计变了，页头照样跟着变", async () => {
    const { client, WriterRoom } = await loadWriter();
    client.apiPost.mockImplementation((url) => (/\/author-drafts\/scene\/s1\/ensure$/.test(url)
      ? Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "<p>起点</p>" } })
      : Promise.resolve({})));
    let commits = 0;
    const host = await render(
      <React.Profiler id="writer-room" onRender={() => { commits += 1; }}>
        <WriterRoom t={{}} setTweak={() => {}} />
      </React.Profiler>,
    );
    const editor = () => host.querySelector(".wr-editor");
    const card = () => host.querySelector('[data-testid="scene-design-card"]');
    await vi.waitFor(() => expect(editor().textContent).toContain("起点"), T);
    await vi.waitFor(() => expect(card().textContent).toContain("替父亲点名"), T);
    await wait(300);
    const settled = commits;
    await act(async () => { window.WsCatalog.recordSceneWords("ch01s1", 4321); });
    await wait(100);
    expect(window.WsCatalog.sceneById("ch01s1").scene.words).toBe(4321);
    expect(commits).toBe(settled);

    const base = client.apiGet.getMockImplementation();
    const scene = DEFAULT_CHAP.scenes[0];
    client.apiGet.mockImplementation((url) => (/\/api\/v2\/projects\/[^/]+\/catalog(\?|$)/.test(url)
      ? Promise.resolve({ chapters: [{ ...DEFAULT_CHAP, scenes: [{ ...scene, brief: { ...scene.brief, goal: "替父亲去码头点名" } }] }] })
      : base(url)));
    await act(async () => { await window.WsCatalog.__refresh(); });
    await vi.waitFor(() => expect(card().textContent).toContain("替父亲去码头点名"), T);
  }, LONG);
});

describe("写作台 · @ 唤档案", () => {
  const rangeRect = Object.getOwnPropertyDescriptor(Range.prototype, "getBoundingClientRect");
  afterEach(() => {
    delete window.LIB_ENTRIES;
    delete window.LIB_BY_ID;
    if (rangeRect) Object.defineProperty(Range.prototype, "getBoundingClientRect", rangeRect);
    else delete Range.prototype.getBoundingClientRect;
  });

  it("↓ 挪到第二条：松键时不跳回第一条，回车插入的是第二条", async () => {
    if (!rangeRect) {
      Object.defineProperty(Range.prototype, "getBoundingClientRect", {
        configurable: true,
        value: () => ({ left: 10, right: 10, top: 10, bottom: 20, width: 0, height: 10 }),
      });
    }
    window.LIB_ENTRIES = [
      { id: "e1", name: "林昭", cat: "people", kind: "人物", accent: "crimson", glyph: "林", summary: "" },
      { id: "e2", name: "雨城", cat: "places", kind: "地点", accent: "slate", glyph: "雨", summary: "" },
    ];
    window.LIB_BY_ID = { e1: window.LIB_ENTRIES[0], e2: window.LIB_ENTRIES[1] };
    const { client, WriterRoom } = await loadWriter();
    client.apiPost.mockImplementation((url) => (/\/author-drafts\/scene\/s1\/ensure$/.test(url)
      ? Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: "<p>起点</p>" } })
      : Promise.resolve({})));
    client.apiPatch.mockImplementation((url, body) => Promise.resolve({ draft: { draft_id: "d1", revision_no: 2, content: body.content } }));
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().textContent).toContain("起点"), T);
    await act(async () => {
      editor().innerHTML = "<p>起点@</p>";
      const text = editor().querySelector("p").firstChild;
      const range = document.createRange();
      range.setStart(text, text.nodeValue.length);
      range.collapse(true);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
      editor().dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: "@" }));
    });
    const items = () => [...host.querySelectorAll(".wr-mention-item")];
    await vi.waitFor(() => expect(items()).toHaveLength(2), T);
    const key = (type, name) => act(async () => { editor().dispatchEvent(new KeyboardEvent(type, { bubbles: true, key: name })); });
    await key("keydown", "ArrowDown");
    await key("keyup", "ArrowDown");
    expect(items().map((li) => li.classList.contains("is-sel"))).toEqual([false, true]);
    await key("keydown", "Enter");
    expect(editor().querySelector(".wr-entity").getAttribute("data-lib-id")).toBe("e2");
  }, LONG);
});
