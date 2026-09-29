// useDocBinding 的房间级回归：409 冲突之后编辑器必须换成服务端版本（F03-01），
// 否则下一次敲字会带着新的 base_revision_no 把另一台设备的正文静默盖掉。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

const T = { timeout: 4000, interval: 25 };
const LONG = 20000;
const mounted = [];
const innerTextDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");

function matchMedia() {
  return {
    matches: false, media: "",
    addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn(),
  };
}

async function loadWriter() {
  const client = await import("./lib/client.js");
  installApiRouter(client);
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

