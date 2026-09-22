// 写作台的深改姿态与「AI 放在抽屉里」时的续写面板。
// 深改只诊断、不代笔：每一项给「选中这一句去改写」，回到起草姿态并选中那一句——
// 过去这里挂着一条「采纳候选 · 写回正文」的通路，可诊断从不产出候选，那条路永远走不到。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 5000, interval: 25 };
const mounted = [];
const innerTextDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");
const rangeRectDescriptor = Object.getOwnPropertyDescriptor(Range.prototype, "getBoundingClientRect");
const scrollToDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTo");

function matchMedia() {
  return { matches: false, media: "", addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() };
}
/* 按窗口宽度回答 (max-width: Npx)：写作台的断点一律写成 max-width */
function matchMediaAt(width) {
  return (query) => {
    const hit = /max-width:\s*(\d+)px/.exec(String(query));
    return { matches: hit ? width <= Number(hit[1]) : false, media: String(query), addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() };
  };
}
const keydown = (init) => act(async () => window.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init })));
const deepRadio = (host) => [...host.querySelectorAll('[role="radio"]')].find((node) => node.textContent.includes("深改"));

/* 在编辑器里按「整篇拼接文字」的偏移选中一段（深改时正文被诊断高亮拆成好几个文本节点） */
async function selectByOffsets(editor, start, end) {
  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let text = "";
  let node;
  while ((node = walker.nextNode())) { nodes.push({ node, from: text.length }); text += node.nodeValue; }
  const at = (offset) => {
    const hit = nodes.find(({ node: n, from }) => offset <= from + n.nodeValue.length && offset >= from);
    return [hit.node, offset - hit.from];
  };
  const range = document.createRange();
  range.setStart(...at(start));
  range.setEnd(...at(end));
  await act(async () => {
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
}

async function loadWriter() {
  const client = await import("./lib/client.js");
  installApiRouter(client);
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const store = await import("./wr-doc-store.jsx");
  const writer = await import("./ws-writer.jsx");
  return { ...writer, ...store, client };
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
  // jsdom 没有布局与滚动：定位诊断段落要用 scrollTo
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

describe("写作台 · 深改只诊断、不代笔", () => {
  it("诊断项给「选中这一句去改写」：回到起草姿态并选中那一句；没有「采纳 · 写回正文」", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const save = vi.spyOn(WrDocs, "save").mockResolvedValue({});
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);

    const deep = [...host.querySelectorAll('[role="radio"]')].find((node) => node.textContent.includes("深改"));
    await click(deep);
    const drawer = host.querySelector(".wr-dxd");
    await vi.waitFor(() => expect(drawer.textContent).toContain("贴邻重复"), T);
    expect(drawer.textContent).not.toMatch(/采纳|写回正文|没有现成候选|ECH|RDN/);
    expect(host.querySelector(".wr-editor").getAttribute("contenteditable")).toBe("false");

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await click([...drawer.querySelectorAll("button")].find((node) => node.textContent.includes("选中这一句去改写")));
      await act(async () => { vi.advanceTimersByTime(120); });
    } finally {
      vi.useRealTimers();
    }
    expect(host.querySelector(".wr-root").getAttribute("data-posture")).toBe("draft");
    expect(host.querySelector(".wr-editor").getAttribute("contenteditable")).toBe("true");
    expect(window.getSelection().toString()).toBe("安静，安静");
    // 深改进出都没有改动正文，所以一次也没存
    expect(save).not.toHaveBeenCalled();
  });
});

describe("写作台 · 深改时正文只读，也不能续写", () => {
  it("AI 续写按钮置灰并说明怎么回去写；⌘J 与命令面板都打不开托盘；进深改时收起已开的托盘", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    const tray = host.querySelector(".wr-tray");
    const aiButton = () => [...host.querySelectorAll(".wr-dock button")].find((node) => node.textContent.includes("AI 续写"));

    // 起草姿态：⌘J 打开托盘（证明下面的「打不开」不是快捷键本身失灵）
    await keydown({ key: "j", ctrlKey: true });
    expect(tray.classList.contains("show")).toBe(true);

    await click(deepRadio(host));
    expect(host.querySelector(".wr-root").getAttribute("data-posture")).toBe("deep");
    expect(tray.classList.contains("show")).toBe(false);
    expect(aiButton().disabled).toBe(true);
    expect(aiButton().getAttribute("title")).toBe("深改只诊断，回到起草再续写");

    await keydown({ key: "j", ctrlKey: true });
    expect(tray.classList.contains("show")).toBe(false);
    await act(async () => window.dispatchEvent(new CustomEvent("ws:writer-action", { detail: "ai" })));
    expect(tray.classList.contains("show")).toBe(false);
  });

  it("深改工具条「回起草改这句」：跨段的选区带回起始段里的那一截，同一句出现两次也选回作者选的那一处", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>雨下了一整夜。雨下了一整夜。</p><p>第二段也有字。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("第二段也有字"), T);
    await click(deepRadio(host));
    const editor = host.querySelector(".wr-editor");
    expect(editor.getAttribute("contenteditable")).toBe("false");

    // 从第一段第二个「雨下了一整夜。」一直选到第二段的「第二段」
    await selectByOffsets(editor, 7, 17);
    const bar = document.querySelector(".wr-irw-bar");
    expect(bar).not.toBeNull();
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await click([...bar.querySelectorAll("button")].find((node) => node.textContent.includes("回起草改这句")));
      await act(async () => { vi.advanceTimersByTime(120); });
    } finally {
      vi.useRealTimers();
    }
    expect(host.querySelector(".wr-root").getAttribute("data-posture")).toBe("draft");
    const selection = window.getSelection();
    expect(selection.toString()).toBe("雨下了一整夜。");
    const first = editor.querySelector("p");
    const before = document.createRange();
    before.selectNodeContents(first);
    before.setEnd(selection.getRangeAt(0).startContainer, selection.getRangeAt(0).startOffset);
    expect(before.toString().length).toBe(7);
  });
});

describe("写作台 · 1000–1279 宽时的深改", () => {
  it("上下文栏在这个宽度叠放；深改时诊断栏改为停靠、不画遮罩，回起草后又收回叠放", async () => {
    Object.defineProperty(window, "matchMedia", { configurable: true, value: matchMediaAt(1100) });
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockReturnValue("<p>门外很安静，安静到能听见潮水。</p>");
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("安静到能听见潮水"), T);
    const root = host.querySelector(".wr-root");
    const scrim = host.querySelector(".wr-scrim-drawer");
    expect(root.getAttribute("data-dock-left")).toBe("on");
    expect(root.getAttribute("data-dock-right")).toBe("off");
    expect(root.getAttribute("data-right")).toBe("off");

    await click(deepRadio(host));
    await vi.waitFor(() => expect(host.querySelector(".wr-dxd").textContent).toContain("贴邻重复"), T);
    expect(root.getAttribute("data-dock-right")).toBe("on");
    expect(root.getAttribute("data-right")).toBe("on");
    expect(root.getAttribute("data-left")).toBe("on");
    expect(scrim.classList.contains("show")).toBe(false);

    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      await click([...host.querySelectorAll(".wr-dxd button")].find((node) => node.textContent.includes("选中这一句去改写")));
      await act(async () => { vi.advanceTimersByTime(120); });
    } finally {
      vi.useRealTimers();
    }
    expect(root.getAttribute("data-posture")).toBe("draft");
    expect(root.getAttribute("data-dock-right")).toBe("off");
    expect(root.getAttribute("data-right")).toBe("off");
    expect(scrim.classList.contains("show")).toBe(false);
    expect(window.getSelection().toString()).toBe("安静，安静");
  });
});

describe("写作台 · AI 放在抽屉里", () => {
  it("抽屉里多一个 AI 页签：提示框 + 只追加下一段的快捷词；放托盘时没有这个页签", async () => {
    const { WriterRoom } = await loadWriter();
    const host = await render(<WriterRoom t={{ aiPlace: "drawer" }} setTweak={() => {}} />);
    const tabs = () => [...host.querySelectorAll(".wr-drawer.right [role='tab']")].map((node) => node.textContent);
    expect(tabs()).toEqual(["戏剧", "AI", "批注", "笔记"]);
    await click([...host.querySelectorAll(".wr-drawer.right [role='tab']")].find((node) => node.textContent === "AI"));
    const panel = host.querySelector(".wr-ai-panel");
    expect(panel.querySelector("textarea")).not.toBeNull();
    expect(panel.textContent).toContain("续写只在正文末尾追加下一段");
    expect(panel.textContent).not.toMatch(/删冗余|让节奏更紧/);

    const tray = await render(<WriterRoom t={{ aiPlace: "tray" }} setTweak={() => {}} />);
    expect([...tray.querySelectorAll(".wr-drawer.right [role='tab']")].map((node) => node.textContent)).toEqual(["戏剧", "批注", "笔记"]);
  });
});
