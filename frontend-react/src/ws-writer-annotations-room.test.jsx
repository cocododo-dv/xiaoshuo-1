// 写作台批注：存在本机浏览器、不进正文、换场 / 重开后还在。
// 过去批注是正文里的 <mark data-note>，一落盘就被消毒器剥掉属性：换一场回来，批注页签说「本场还没有批注」。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_CHAP, installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 5000, interval: 25 };
const mounted = [];
const innerTextDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "innerText");
const rangeRectDescriptor = Object.getOwnPropertyDescriptor(Range.prototype, "getBoundingClientRect");
const scrollToDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTo");

const SECOND_CHAP = {
  ...DEFAULT_CHAP,
  slug: "ch02", chapter_id: "c2", no: "02", title: "第二章", current: false,
  scenes: [{ ...DEFAULT_CHAP.scenes[0], slug: "ch02s1", scene_id: "s2", title: "另一场" }],
};
const DOCS = {
  ch01s1: "<p>门外的风很大，她没有回头。</p>",
  ch02s1: "<p>另一场的正文。</p>",
};

function matchMedia() {
  return { matches: false, media: "", addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() };
}

async function loadWriter() {
  const client = await import("./lib/client.js");
  installApiRouter(client, { catalog: [DEFAULT_CHAP, SECOND_CHAP] });
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog && window.WsCatalog.get().length).toBe(2), T);
  const store = await import("./wr-doc-store.jsx");
  const writer = await import("./ws-writer.jsx");
  return { ...writer, ...store };
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

async function select(editor, text) {
  const node = [...editor.querySelectorAll("p")].map((p) => p.firstChild).find((n) => n && n.nodeValue.includes(text));
  const at = node.nodeValue.indexOf(text);
  const range = document.createRange();
  range.setStart(node, at);
  range.setEnd(node, at + text.length);
  await act(async () => {
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
}

async function typeNote(textarea, value) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
  await act(async () => {
    setter.call(textarea, value);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function openScene(sid) {
  await act(async () => window.dispatchEvent(new CustomEvent("ws:writer-scene", { detail: sid })));
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

describe("写作台 · 批注存在本机浏览器", () => {
  it("加一条批注：存进本机清单、不进正文；换一场再回来，批注页签和正文里的标注都还在", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    const save = vi.spyOn(WrDocs, "save").mockResolvedValue({});
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
    const editor = host.querySelector(".wr-editor");

    await select(editor, "风很大");
    await click(button(document.querySelector(".wr-irw-bar"), "批注"));
    const pop = document.querySelector('.wr-irw-pop[aria-label="批注"]');
    expect(pop.textContent).toContain("保存在本机浏览器");
    await typeNote(pop.querySelector("textarea"), "这里再冷一点");
    await click(button(pop, "添加批注"));

    const stored = JSON.parse(window.localStorage.getItem("wr-anno:ch01s1::prj-main"));
    expect(stored.items).toEqual([expect.objectContaining({ quote: "风很大", prefix: "门外的", note: "这里再冷一点" })]);
    expect(editor.querySelector("mark.wr-anno").getAttribute("title")).toBe("这里再冷一点");
    // 批注本身不触发正文保存
    expect(save).not.toHaveBeenCalled();

    // 改一个字触发自动保存：存下去的正文里没有批注标记
    await act(async () => editor.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: "" })));
    await vi.waitFor(() => expect(save).toHaveBeenCalled(), T);
    expect(save.mock.calls[0]).toEqual(["ch01s1", "<p>门外的风很大，她没有回头。</p>"]);

    await openScene("ch02s1");
    await vi.waitFor(() => expect(host.textContent).toContain("另一场的正文"), T);
    expect(host.querySelector(".wr-editor mark.wr-anno")).toBeNull();

    await openScene("ch01s1");
    await vi.waitFor(() => expect(host.querySelector('.wr-editor mark.wr-anno')).not.toBeNull(), T);
    expect(host.querySelector(".wr-editor mark.wr-anno").textContent).toBe("风很大");
    const annoTab = [...host.querySelectorAll(".wr-drawer.right [role='tab']")].find((node) => node.textContent.includes("批注"));
    expect(annoTab.textContent).toContain("1");
    await click(annoTab);
    const list = host.querySelector(".wr-anno-list");
    expect(list.textContent).toContain("这里再冷一点");
    expect(list.textContent).toContain("本机浏览器");
  });

  it("删掉被批注的那几个字：自动保存后批注页签把那一条改成「找不到原文」，不留一个点了没反应的「定位」", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    const save = vi.spyOn(WrDocs, "save").mockResolvedValue({});
    window.localStorage.setItem("wr-anno:ch01s1::prj-main", JSON.stringify({ v: 1, items: [
      { id: "a1", quote: "风很大", prefix: "门外的", suffix: "，她没有回头。", note: "再冷一点", createdAt: 1, updatedAt: 1 },
    ] }));
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.querySelector(".wr-editor mark.wr-anno")).not.toBeNull(), T);
    await click([...host.querySelectorAll(".wr-drawer.right [role='tab']")].find((node) => node.textContent.includes("批注")));
    expect(host.querySelector(".wr-anno-list button.wr-anno-item")).not.toBeNull();

    // 作者把批注的那几个字删掉：浏览器连同标注一起拿掉，接着触发自动保存。
    // 批注页签是 memo 的，只靠 ws:anno-change 重读——保存时必须发这个事件，不能指望别的原因碰巧重渲染
    const announced = vi.fn();
    window.addEventListener("ws:anno-change", announced);
    const editor = host.querySelector(".wr-editor");
    await act(async () => {
      editor.querySelector("mark.wr-anno").remove();
      editor.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }));
    });
    await vi.waitFor(() => expect(save).toHaveBeenCalled(), T);
    window.removeEventListener("ws:anno-change", announced);
    expect(announced).toHaveBeenCalledWith(expect.objectContaining({ detail: { sid: "ch01s1" } }));
    await vi.waitFor(() => expect(host.querySelector(".wr-anno-item.is-lost")).not.toBeNull(), T);
    expect(host.querySelector(".wr-anno-list button.wr-anno-item")).toBeNull();
    expect(host.querySelector(".wr-anno-item.is-lost").textContent).toContain("找不到这段文字");
  });

  it("还没到自动保存就点那一条：标注已经不在正文里，清单当场改成「找不到原文」", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    window.localStorage.setItem("wr-anno:ch01s1::prj-main", JSON.stringify({ v: 1, items: [
      { id: "a1", quote: "风很大", prefix: "门外的", suffix: "，她没有回头。", note: "再冷一点", createdAt: 1, updatedAt: 1 },
    ] }));
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.querySelector(".wr-editor mark.wr-anno")).not.toBeNull(), T);
    await click([...host.querySelectorAll(".wr-drawer.right [role='tab']")].find((node) => node.textContent.includes("批注")));
    host.querySelector(".wr-editor mark.wr-anno").remove();
    await click(host.querySelector(".wr-anno-list button.wr-anno-item"));
    expect(host.querySelector(".wr-anno-item.is-lost")).not.toBeNull();
    expect(host.querySelector(".wr-anno-list button.wr-anno-item")).toBeNull();
  });

  it("正文里找不到原文的批注留在清单里，可以删掉", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    window.localStorage.setItem("wr-anno:ch01s1::prj-main", JSON.stringify({ v: 1, items: [
      { id: "a1", quote: "早就删掉的一句", prefix: "", suffix: "", note: "旧批注", createdAt: 1, updatedAt: 1 },
    ] }));
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
    await click([...host.querySelectorAll(".wr-drawer.right [role='tab']")].find((node) => node.textContent.includes("批注")));
    const lost = host.querySelector(".wr-anno-item.is-lost");
    expect(lost.textContent).toContain("找不到这段文字");
    await click(button(lost, "删除这条批注"));
    expect(window.localStorage.getItem("wr-anno:ch01s1::prj-main")).toBeNull();
    expect(host.querySelector(".wr-anno-list")).toBeNull();
    expect(host.textContent).toContain("本场还没有批注");
  });
});

/* 选区弹层属于它弹出时的那一场：换场后改写候选、没存的新批注都得作废。
   过去弹层留着——「替换为第 N 版」把上一场的改写插到另一场正文的最前面并自动保存，
   「添加批注」把上一场的引文记进另一场的批注清单 */
async function mockRewrite(client, text = "改写出来的一句") {
  client.apiPost.mockImplementation((url) => {
    if (url === "/api/v1/passages/patch-candidates") {
      return Promise.resolve({ candidate: { patch_id: "p1", rationale: "ok", replacement_options: [{ option_id: "o1", replacement_text: text }] } });
    }
    return Promise.resolve({});
  });
}
const startsWith = (root, text) => [...root.querySelectorAll("button")].find((node) => node.textContent.trim().startsWith(text));

describe("写作台 · 选区弹层只作用于它弹出的那一场", () => {
  it("改写候选出来后换了一场：弹层收起、候选作废，另一场的正文不会被插进上一场的改写", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    const client = await import("./lib/client.js");
    await mockRewrite(client);
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    const save = vi.spyOn(WrDocs, "save").mockResolvedValue({});
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
    const editor = host.querySelector(".wr-editor");

    await select(editor, "风很大");
    await click(startsWith(document.querySelector(".wr-irw-bar"), "润色"));
    await vi.waitFor(() => expect(startsWith(document.body, "替换为第 1 版")).toBeTruthy(), T);

    await openScene("ch02s1");
    await vi.waitFor(() => expect(editor.textContent).toContain("另一场的正文"), T);
    expect(startsWith(document.body, "替换为第 1 版")).toBeUndefined();
    expect(document.querySelector(".wr-irw-pop")).toBeNull();
    expect(editor.textContent).not.toContain("改写出来的一句");
    // 没采纳的候选照样回传弃用
    expect(client.apiPost).toHaveBeenCalledWith("/api/v1/passage-patch-candidates/p1/reject", {});
    expect(save.mock.calls.some(([, html]) => String(html).includes("改写出来的一句"))).toBe(false);
  });

  it("候选出来前那段字被改掉了：不再原样替换，说清楚原因并留着候选", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    const client = await import("./lib/client.js");
    await mockRewrite(client);
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    vi.spyOn(WrDocs, "save").mockResolvedValue({});
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
    const editor = host.querySelector(".wr-editor");

    await select(editor, "风很大");
    await click(startsWith(document.querySelector(".wr-irw-bar"), "润色"));
    await vi.waitFor(() => expect(startsWith(document.body, "替换为第 1 版")).toBeTruthy(), T);
    // 作者在等候选时把选中的字删了（整段重写）
    await act(async () => { editor.querySelector("p").textContent = "门外安静下来，她没有回头。"; });

    await click(startsWith(document.body, "替换为第 1 版"));
    const pop = document.querySelector(".wr-irw-pop");
    expect(pop).not.toBeNull();
    expect(pop.textContent).toContain("选中的那段字已经不在原处");
    expect(startsWith(pop, "替换为第 1 版").disabled).toBe(true);
    expect(editor.textContent).toBe("门外安静下来，她没有回头。");
    expect(editor.querySelector(".wr-rev")).toBeNull();
  });

  it("新批注写到一半换了一场：弹层收起，哪一场的批注清单都没有多出这一条", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    vi.spyOn(WrDocs, "save").mockResolvedValue({});
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
    const editor = host.querySelector(".wr-editor");

    await select(editor, "风很大");
    await click(button(document.querySelector(".wr-irw-bar"), "批注"));
    await typeNote(document.querySelector('.wr-irw-pop[aria-label="批注"] textarea'), "这里再冷一点");

    await openScene("ch02s1");
    await vi.waitFor(() => expect(editor.textContent).toContain("另一场的正文"), T);
    expect(document.querySelector('.wr-irw-pop[aria-label="批注"]')).toBeNull();
    expect(editor.querySelector("mark.wr-anno")).toBeNull();
    expect(window.localStorage.getItem("wr-anno:ch02s1::prj-main")).toBeNull();
    expect(window.localStorage.getItem("wr-anno:ch01s1::prj-main")).toBeNull();
  });
});

describe("写作台 · 批注的上限说在明处", () => {
  it("一场已有 200 条批注时再加：不存、说明到了上限，清单一条不少", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    const full = Array.from({ length: 200 }, (_, i) => ({ id: `n${i}`, quote: `早就删掉的第${i}句`, prefix: "", suffix: "", note: `旧批注${i}`, createdAt: 1, updatedAt: 1 }));
    window.localStorage.setItem("wr-anno:ch01s1::prj-main", JSON.stringify({ v: 1, items: full }));
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
    const editor = host.querySelector(".wr-editor");

    await select(editor, "风很大");
    await click(button(document.querySelector(".wr-irw-bar"), "批注"));
    const pop = document.querySelector('.wr-irw-pop[aria-label="批注"]');
    const textarea = pop.querySelector("textarea");
    expect(textarea.maxLength).toBe(2000);
    await typeNote(textarea, "第二百零一条");
    await click(button(pop, "添加批注"));

    const stillOpen = document.querySelector('.wr-irw-pop[aria-label="批注"]');
    expect(stillOpen).not.toBeNull();
    expect(stillOpen.querySelector('[role="alert"]').textContent).toContain("200 条批注，到上限了");
    const stored = JSON.parse(window.localStorage.getItem("wr-anno:ch01s1::prj-main")).items;
    expect(stored).toHaveLength(200);
    expect(stored.some((item) => item.note === "第二百零一条")).toBe(false);
  });

  it("批注快写满 2000 字时显示字数，到上限时直说", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
    await select(host.querySelector(".wr-editor"), "风很大");
    await click(button(document.querySelector(".wr-irw-bar"), "批注"));
    const pop = document.querySelector('.wr-irw-pop[aria-label="批注"]');
    expect(pop.querySelector(".wr-anno-count")).toBeNull();
    await typeNote(pop.querySelector("textarea"), "字".repeat(1700));
    expect(pop.querySelector(".wr-anno-count").textContent).toBe("1700 / 2000 字");
    await typeNote(pop.querySelector("textarea"), "字".repeat(2000));
    expect(pop.querySelector(".wr-anno-count").textContent).toBe("已到 2000 字上限");
  });
});

/* ---------- 选区工具条 / 弹层的键盘与焦点（2026-09-21 a11y 复审） ---------- */
const keyOn = async (target, init) => {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  await act(async () => { target.dispatchEvent(event); });
  return event;
};

async function openWriter() {
  const { WriterRoom, WrDocs } = await loadWriter();
  vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
  vi.spyOn(WrDocs, "save").mockResolvedValue({});
  const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
  await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
  const editor = host.querySelector(".wr-editor");
  await act(async () => { editor.focus(); });
  return { host, editor };
}

describe("写作台 · 选区弹层的焦点与输入法", () => {
  it("批注写到一半、输入法组字时按 Esc：弹层不关、字还在；真正的 Esc 取消这条批注，焦点回到正文", async () => {
    const { editor } = await openWriter();
    await select(editor, "风很大");
    await click(button(document.querySelector(".wr-irw-bar"), "批注"));
    const pop = document.querySelector('.wr-irw-pop[aria-label="批注"]');
    const textarea = pop.querySelector("textarea");
    expect(document.activeElement).toBe(textarea);
    await typeNote(textarea, "这里再冷");

    await keyOn(textarea, { key: "Escape", isComposing: true });
    await keyOn(textarea, { key: "Escape", keyCode: 229 });
    expect(document.querySelector('.wr-irw-pop[aria-label="批注"]')).toBe(pop);
    expect(textarea.value).toBe("这里再冷");

    await keyOn(textarea, { key: "Escape" });
    expect(document.querySelector(".wr-irw-pop")).toBeNull();
    expect(editor.querySelector("mark.wr-anno")).toBeNull();
    expect(document.activeElement).toBe(editor);                 // 过去掉到 <body>
    expect(editor.contains(window.getSelection().anchorNode)).toBe(true);
  });

  it("「自定义…」里 Esc：焦点回到正文、原来的选区还回去，而且不再弹工具条；组字中的 Esc 不关", async () => {
    const { editor } = await openWriter();
    await select(editor, "风很大");
    await click(button(document.querySelector(".wr-irw-bar"), "自定义…"));
    const input = document.querySelector('.wr-irw-pop input[aria-label="改写要求"]');
    expect(document.activeElement).toBe(input);
    await keyOn(input, { key: "Escape", isComposing: true });
    expect(document.querySelector('.wr-irw-pop input[aria-label="改写要求"]')).toBe(input);

    await keyOn(input, { key: "Escape" });
    expect(document.querySelector(".wr-irw-pop")).toBeNull();
    expect(document.activeElement).toBe(editor);
    expect(window.getSelection().toString()).toBe("风很大");
    await act(async () => { document.dispatchEvent(new Event("selectionchange")); });
    expect(document.querySelector(".wr-irw-bar")).toBeNull();
  });

  it("「调音」打开时焦点落在第一个滑杆上（过去留在 <body>）", async () => {
    const { editor } = await openWriter();
    await select(editor, "风很大");
    await click(button(document.querySelector(".wr-irw-bar"), "调音"));
    const pop = document.querySelector(".wr-irw-pop");
    expect(document.activeElement).toBe(pop.querySelector('input[type="range"]'));
    await click(button(pop, "取消"));
    expect(document.activeElement).toBe(editor);
  });

  it("Alt+F10 从正文进工具条，←→ 在按钮间走；Esc 回到正文、选区还在", async () => {
    const { editor } = await openWriter();
    await select(editor, "风很大");
    const bar = document.querySelector(".wr-irw-bar");
    expect(bar.getAttribute("aria-keyshortcuts")).toBe("Alt+F10");
    expect(document.getElementById(editor.getAttribute("aria-describedby")).textContent).toContain("Alt+F10");

    expect((await keyOn(editor, { key: "F10", altKey: true })).defaultPrevented).toBe(true);
    const buttons = [...bar.querySelectorAll("button")];
    expect(document.activeElement).toBe(buttons[0]);
    await keyOn(buttons[0], { key: "ArrowRight" });
    expect(document.activeElement).toBe(buttons[1]);
    await keyOn(buttons[1], { key: "ArrowLeft" });
    await keyOn(buttons[0], { key: "ArrowLeft" });
    expect(document.activeElement).toBe(buttons[buttons.length - 1]);

    await keyOn(document.activeElement, { key: "Escape" });
    expect(document.querySelector(".wr-irw-bar")).toBeNull();
    expect(document.activeElement).toBe(editor);
    expect(window.getSelection().toString()).toBe("风很大");

    // 工具条收起后选区还在：再按一次 Alt+F10 把它叫回来并进去
    await keyOn(editor, { key: "F10", altKey: true });
    expect(document.querySelector(".wr-irw-bar")).not.toBeNull();
    expect(document.activeElement).toBe(document.querySelector(".wr-irw-bar button"));
  });

  it("改写结果出来焦点落在选中的那一版上；替换之后焦点回到正文、光标在改写的那一句后面", async () => {
    const { WriterRoom, WrDocs } = await loadWriter();
    const client = await import("./lib/client.js");
    await mockRewrite(client);
    vi.spyOn(WrDocs, "load").mockImplementation((sid) => DOCS[sid] || null);
    vi.spyOn(WrDocs, "save").mockResolvedValue({});
    const host = await render(<WriterRoom t={{}} setTweak={() => {}} />);
    await vi.waitFor(() => expect(host.textContent).toContain("门外的风很大"), T);
    const editor = host.querySelector(".wr-editor");
    await act(async () => { editor.focus(); });

    await select(editor, "风很大");
    await click(startsWith(document.querySelector(".wr-irw-bar"), "润色"));
    await vi.waitFor(() => expect(startsWith(document.body, "替换为第 1 版")).toBeTruthy(), T);
    const checked = document.querySelector('.wr-irw-pop [role="radio"][aria-checked="true"]');
    expect(document.activeElement).toBe(checked);

    await click(startsWith(document.body, "替换为第 1 版"));
    const rev = editor.querySelector(".wr-rev");
    expect(rev.textContent).toBe("改写出来的一句");
    expect(document.activeElement).toBe(editor);
    const sel = window.getSelection();
    expect(sel.isCollapsed).toBe(true);
    const caret = sel.getRangeAt(0);
    const after = document.createRange();
    after.setStartAfter(rev);
    expect(caret.compareBoundaryPoints(Range.START_TO_START, after)).toBe(0);
  });
});
