// 写作台选区改写：一次改写有字数上限（WR_REWRITE_MAX_CHARS）。超了就在本地说「选区太长，请分段改写」，
// 不发请求、不截短——过去只把前 2000 字送去改，却把整个选区换掉，后面的字就这么没了（重评 R12 / #20a）。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

const T = { timeout: 5000, interval: 25 };
const mounted = [];
const innerTextDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");
const rangeRectDescriptor = Object.getOwnPropertyDescriptor(Range.prototype, "getBoundingClientRect");
const scrollToDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTo");

const LONG = "雨城的旧信压在案卷底下。".repeat(209).slice(0, 2500);   // 2,500 字
const SHORT = "林昭把旧信又读了一遍。".repeat(173).slice(0, 1900);    // 1,900 字

function matchMedia() {
  return { matches: false, media: "", addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() };
}

async function loadWriter() {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBeGreaterThan(0), T);
  client.apiPost.mockImplementation((url) => {
    if (/\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
      return Promise.resolve({ draft: { draft_id: "d1", revision_no: 1, content: `<p>${LONG}</p><p>${SHORT}</p>` } });
    }
    if (url === "/api/v1/passages/patch-candidates") {
      return Promise.resolve({ candidate: { patch_id: "patch-1", replacement_options: [{ option_id: "o1", replacement_text: "改好的这一段。" }] } });
    }
    return Promise.resolve({});
  });
  const writer = await import("./ws-writer.jsx");
  return { client, ...writer };
}

async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return host;
}
const click = (node) => act(async () => node.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const button = (root, text) => [...root.querySelectorAll("button")].find((node) => node.textContent.trim() === text);

/* 选中第 index 段的全部文字（与作者拖选一整段一样，停在这一段里） */
async function selectParagraph(editor, index) {
  const node = editor.querySelectorAll("p")[index].firstChild;
  const range = document.createRange();
  range.setStart(node, 0);
  range.setEnd(node, node.nodeValue.length);
  await act(async () => {
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
}

const rewriteCalls = (client) => client.apiPost.mock.calls.filter(([url]) => url === "/api/v1/passages/patch-candidates");

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
  if (!rangeRectDescriptor) {
    Object.defineProperty(Range.prototype, "getBoundingClientRect", {
      configurable: true,
      value() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; },
    });
  }
  if (!scrollToDescriptor) Object.defineProperty(Element.prototype, "scrollTo", { configurable: true, value() {} });
  vi.spyOn(window, "alert").mockImplementation(() => {});
});

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  if (!innerTextDescriptor) delete HTMLElement.prototype.innerText;
  if (!rangeRectDescriptor) delete Range.prototype.getBoundingClientRect;
  if (!scrollToDescriptor) delete Element.prototype.scrollTo;
  window.getSelection().removeAllRanges();
  vi.restoreAllMocks();
});

describe("写作台 · 选区改写的字数上限（#20a）", () => {
  it("选了 2,500 字：不发改写请求、不截短，弹层说选区太长、请分段改写，没有「重试」", async () => {
    const { client, WriterRoom } = await loadWriter();
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().querySelectorAll("p")).toHaveLength(2), T);
    await selectParagraph(editor(), 0);
    await vi.waitFor(() => expect(button(document.body, "润色")).toBeTruthy(), T);

    await click(button(document.body, "润色"));

    const pop = () => document.body.querySelector(".wr-irw-pop");
    await vi.waitFor(() => expect(pop().textContent).toContain("选区太长（2500 字），请分段改写"), T);
    expect(rewriteCalls(client)).toEqual([]);
    expect(button(pop(), "重试")).toBeUndefined();
    expect(editor().querySelectorAll("p")[0].textContent).toBe(LONG);
  });

  it("选了 1,900 字：整段原样送去改，一个字不截", async () => {
    const { client, WriterRoom } = await loadWriter();
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    const editor = () => host.querySelector(".wr-editor");
    await vi.waitFor(() => expect(editor().querySelectorAll("p")).toHaveLength(2), T);
    await selectParagraph(editor(), 1);
    await vi.waitFor(() => expect(button(document.body, "润色")).toBeTruthy(), T);

    await click(button(document.body, "润色"));

    await vi.waitFor(() => expect(rewriteCalls(client)).toHaveLength(1), T);
    expect(rewriteCalls(client)[0][1].source_excerpt).toBe(SHORT);
  });
});
